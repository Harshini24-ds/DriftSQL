"""Deterministic evidence validation for EEVA.

Trusted validation context must come from current approved repositories
and active database-role checks, independently of retrieved evidence.

Conflict detection and recovery execution belong to Member 2.
"""

from datetime import datetime
from hashlib import sha256
import json
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    model_validator,
)

from src.contracts.evidence import (
    EvidenceItem,
    EvidenceSnapshot,
    EvidenceType,
)
from src.eeva.evidence_state import (
    RECOVERABLE_STATES,
    EvidenceState,
    ObligationState,
)
from src.query_intelligence.models import QueryFilter
from src.query_intelligence.obligations import EvidenceObligation


class SourceReference(BaseModel):
    """One current approved source record.

    source_type corresponds to an obligation's expected_source_types.

    References must come from trusted current sources, not be copied
    blindly from retrieved evidence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1)
    provenance_id: str = Field(min_length=1)
    evidence_type: EvidenceType
    key: str = Field(min_length=1)
    source_type: str = Field(min_length=1)

    current_version: str = Field(min_length=1)
    current_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    scope: str = Field(min_length=1)
    access_label: str = Field(min_length=1)
    authority_level: int

    effective_from: datetime | None = None
    effective_to: datetime | None = None

    schema_hash: str | None = None

    @model_validator(mode="after")
    def validate_effective_period(self) -> "SourceReference":
        for timestamp in (self.effective_from, self.effective_to):
            if timestamp is not None and timestamp.utcoffset() is None:
                raise ValueError(
                    "Source effective dates must be timezone-aware."
                )

        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError(
                "effective_to cannot precede effective_from."
            )

        return self


class ValidationContext(BaseModel):
    """Trusted current facts for one request and active database role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    database_id: str = Field(min_length=1)
    database_role: str = Field(min_length=1)

    schema_hash: str = Field(min_length=1)
    freshness_policy_version: str = Field(min_length=1)
    evaluated_at: datetime

    sources: tuple[SourceReference, ...]

    permission_decisions: dict[str, StrictBool]
    allowed_access_labels: frozenset[str]

    available_recovery_actions: frozenset[str] = frozenset()
    confirmed_absent_requirement_ids: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def validate_context(self) -> "ValidationContext":
        if self.evaluated_at.utcoffset() is None:
            raise ValueError(
                "evaluated_at must be timezone-aware."
            )

        # One source/provenance can legitimately describe several targets.
        identities = [
            (
                source.source,
                source.provenance_id,
                source.evidence_type,
                source.key,
            )
            for source in self.sources
        ]

        if len(identities) != len(set(identities)):
            raise ValueError(
                "Duplicate current source references."
            )

        return self


class ConflictAssessment(BaseModel):
    """Proposed input contract for Member 2's conflict detector.

    PASS means the detector reviewed the candidates in this exact
    snapshot. It cannot override permission or integrity failures.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str = Field(min_length=1)
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    requirement_id: str = Field(min_length=1)

    evidence_item_ids: tuple[str, ...]

    state: Literal[
        EvidenceState.PASS,
        EvidenceState.AMBIGUOUS_RECOVERABLE,
        EvidenceState.CONFLICT_RECOVERABLE,
        EvidenceState.CONFLICT_CRITICAL,
    ]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def snapshot_fingerprint(snapshot: EvidenceSnapshot) -> str:
    """Bind a conflict assessment to snapshot contents, not just its ID."""
    payload = _canonical_json(
        snapshot.model_dump(mode="json")
    )

    return sha256(payload.encode("utf-8")).hexdigest()


def _matches(
    item: EvidenceItem,
    obligation: EvidenceObligation,
) -> bool:
    """Match evidence to the actual obligation target."""
    if item.type != obligation.evidence_type:
        return False

    target = obligation.target.casefold()

    if item.key.casefold() == target:
        return True

    if isinstance(item.value, dict):
        value_target = item.value.get("target")

        if (
            isinstance(value_target, str)
            and value_target.casefold() == target
        ):
            return True

        if (
            item.type == EvidenceType.RELATIONSHIP
            and " -> " in target
        ):
            endpoints = target.split(" -> ")
            child = item.value.get("child_table")
            parent = item.value.get("parent_table")

            if (
                len(endpoints) == 2
                and isinstance(child, str)
                and isinstance(parent, str)
            ):
                def qualify(name: str) -> str:
                    if "." in name:
                        return name.casefold()

                    return f"{item.scope}.{name}".casefold()

                return {
                    qualify(child),
                    qualify(parent),
                } == set(endpoints)

    return (
        item.type == EvidenceType.DOCUMENT
        and item.scope.casefold() == target
    )


def _candidate_state(
    item: EvidenceItem,
    reference: SourceReference | None,
    obligation: EvidenceObligation,
    snapshot: EvidenceSnapshot,
    context: ValidationContext,
) -> EvidenceState:
    """Check provenance, integrity, source type and freshness."""
    if reference is None:
        return EvidenceState.INVALID

    if (
        reference.evidence_type != item.type
        or reference.key != item.key
        or reference.scope != item.scope
        or reference.access_label != item.access_label
        or reference.authority_level != item.authority_level
        or reference.source_type not in obligation.expected_source_types
    ):
        return EvidenceState.INVALID

    try:
        payload = _canonical_json(item.value)
    except (TypeError, ValueError):
        return EvidenceState.INVALID

    actual_hash = sha256(
        payload.encode("utf-8")
    ).hexdigest()

    if actual_hash != item.content_hash:
        return EvidenceState.INVALID

    for timestamp in (
        item.retrieved_at,
        item.effective_from,
        item.effective_to,
    ):
        if timestamp is not None and timestamp.utcoffset() is None:
            return EvidenceState.INVALID

    if item.source_version != reference.current_version:
        return EvidenceState.STALE_RECOVERABLE

    if (
        item.content_hash != reference.current_content_hash
        or item.effective_from != reference.effective_from
        or item.effective_to != reference.effective_to
    ):
        return EvidenceState.INVALID

    if item.type in {
        EvidenceType.SCHEMA,
        EvidenceType.RELATIONSHIP,
    }:
        if (
            item.source_version != context.schema_hash
            or snapshot.schema_hash != context.schema_hash
        ):
            return EvidenceState.STALE_RECOVERABLE

    if (
        reference.schema_hash is not None
        and reference.schema_hash != context.schema_hash
    ):
        return EvidenceState.STALE_RECOVERABLE

    if (
        item.effective_from is not None
        and context.evaluated_at < item.effective_from
    ):
        return EvidenceState.STALE_RECOVERABLE

    if (
        item.effective_to is not None
        and context.evaluated_at >= item.effective_to
    ):
        return EvidenceState.STALE_RECOVERABLE

    return EvidenceState.PASS


def _supports_filters(
    obligation: EvidenceObligation,
    candidates: tuple[EvidenceItem, ...],
    filters: tuple[QueryFilter, ...],
) -> bool:
    """Check equality-filter values against approved value evidence."""
    relevant_filters = [
        query_filter
        for query_filter in filters
        if query_filter.field == obligation.target
    ]

    if not relevant_filters:
        return False

    for query_filter in relevant_filters:
        if query_filter.operator != "=":
            return False

        supported = any(
            isinstance(item.value, dict)
            and isinstance(item.value.get("values"), list)
            and query_filter.value in item.value["values"]
            for item in candidates
        )

        if not supported:
            return False

    return True


def validate_obligations(
    obligations: tuple[EvidenceObligation, ...],
    snapshot: EvidenceSnapshot,
    context: ValidationContext,
    assessments: tuple[ConflictAssessment, ...],
    *,
    filters: tuple[QueryFilter, ...] = (),
) -> tuple[ObligationState, ...]:
    """Classify each obligation without fetching or executing anything.

    Malformed input raises ValueError.
    Evidence failures produce explicit evidence states.
    """
    if (
        snapshot.request_id != context.request_id
        or snapshot.database_id != context.database_id
    ):
        raise ValueError(
            "Snapshot and context must use the same request and database."
        )

    if (
        snapshot.freshness_policy_version
        != context.freshness_policy_version
    ):
        raise ValueError(
            "Snapshot and context freshness policies must match."
        )

    obligation_ids = {
        obligation.requirement_id
        for obligation in obligations
    }

    if len(obligation_ids) != len(obligations):
        raise ValueError("Duplicate requirement IDs.")

    for obligation in obligations:
        if (
            obligation.request_id != context.request_id
            or obligation.database_id != context.database_id
        ):
            raise ValueError(
                "Obligations must use the snapshot request and database."
            )

    evidence_ids = [
        item.evidence_id
        for item in snapshot.evidence_items
    ]

    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("Duplicate evidence IDs.")

    if not (
        context.confirmed_absent_requirement_ids <= obligation_ids
    ):
        raise ValueError("Unknown confirmed-absent requirement.")

    reviews = {
        assessment.requirement_id: assessment
        for assessment in assessments
    }

    if (
        len(reviews) != len(assessments)
        or not reviews.keys() <= obligation_ids
    ):
        raise ValueError(
            "Duplicate or unknown conflict assessment requirement."
        )

    fingerprint = snapshot_fingerprint(snapshot)

    for assessment in assessments:
        if (
            assessment.snapshot_id != snapshot.snapshot_id
            or assessment.snapshot_digest != fingerprint
        ):
            raise ValueError(
                "Conflict assessment does not match snapshot contents."
            )

    sources = {
        (
            source.source,
            source.provenance_id,
            source.evidence_type,
            source.key,
        ): source
        for source in context.sources
    }

    results: list[ObligationState] = []

    for obligation in obligations:
        candidates = tuple(
            item
            for item in snapshot.evidence_items
            if _matches(item, obligation)
        )

        candidate_ids = tuple(
            item.evidence_id
            for item in candidates
        )

        actions = tuple(
            dict.fromkeys(
                action
                for action in obligation.recovery_actions
                if action in context.available_recovery_actions
            )
        )

        assessment = reviews.get(obligation.requirement_id)

        if assessment is not None:
            reviewed_ids = assessment.evidence_item_ids

            if (
                len(reviewed_ids) != len(set(reviewed_ids))
                or set(reviewed_ids) != set(candidate_ids)
            ):
                raise ValueError(
                    "Conflict assessment must cover exact candidate IDs."
                )

        approved: tuple[EvidenceItem, ...] = ()

        permission = context.permission_decisions.get(
            obligation.target
        )

        if permission is False or any(
            item.access_label not in context.allowed_access_labels
            for item in candidates
        ):
            state = EvidenceState.UNAUTHORIZED

        elif (
            obligation.requirement_id
            in context.confirmed_absent_requirement_ids
        ):
            state = (
                EvidenceState.MISSING_CRITICAL
                if obligation.mandatory
                else EvidenceState.OPTIONAL_MISSING
            )

        elif (
            assessment is not None
            and assessment.state == EvidenceState.CONFLICT_CRITICAL
        ):
            state = EvidenceState.CONFLICT_CRITICAL

        elif not candidates:
            if actions:
                state = EvidenceState.MISSING_RECOVERABLE
            elif obligation.mandatory:
                state = EvidenceState.MISSING_CRITICAL
            else:
                state = EvidenceState.OPTIONAL_MISSING

        else:
            checked = tuple(
                (
                    item,
                    _candidate_state(
                        item,
                        sources.get(
                            (
                                item.source,
                                item.provenance_id,
                                item.type,
                                item.key,
                            )
                        ),
                        obligation,
                        snapshot,
                        context,
                    ),
                )
                for item in candidates
            )

            approved = tuple(
                item
                for item, candidate_state in checked
                if candidate_state == EvidenceState.PASS
            )

            if not approved:
                if not actions:
                    state = EvidenceState.INVALID
                elif any(
                    candidate_state
                    == EvidenceState.STALE_RECOVERABLE
                    for _, candidate_state in checked
                ):
                    state = EvidenceState.STALE_RECOVERABLE
                else:
                    state = EvidenceState.MISSING_RECOVERABLE

            elif (
                obligation.evidence_type
                in {
                    EvidenceType.SCHEMA,
                    EvidenceType.METADATA,
                    EvidenceType.VALUE,
                    EvidenceType.PERMISSION,
                }
                and permission is not True
            ):
                state = EvidenceState.INVALID

            elif (
                obligation.evidence_type == EvidenceType.PERMISSION
                and any(
                    not isinstance(item.value, dict)
                    or item.value.get("database_role")
                    != context.database_role
                    or item.value.get("decision") != "ALLOW"
                    for item in approved
                )
            ):
                state = EvidenceState.INVALID

            elif assessment is None:
                state = EvidenceState.INVALID

            elif (
                obligation.constraint_type == "filter_value_exists"
                and not _supports_filters(
                    obligation,
                    approved,
                    filters,
                )
            ):
                state = (
                    EvidenceState.MISSING_RECOVERABLE
                    if actions
                    else EvidenceState.MISSING_CRITICAL
                )

            else:
                state = EvidenceState(assessment.state)

                if (
                    state
                    in {
                        EvidenceState.AMBIGUOUS_RECOVERABLE,
                        EvidenceState.CONFLICT_RECOVERABLE,
                    }
                    and not actions
                ):
                    state = EvidenceState.CONFLICT_CRITICAL

        results.append(
            ObligationState(
                requirement_id=obligation.requirement_id,
                state=state,
                evidence_item_ids=(
                    tuple(item.evidence_id for item in approved)
                    if state == EvidenceState.PASS
                    else ()
                ),
                recovery_actions=(
                    actions
                    if state in RECOVERABLE_STATES
                    else ()
                ),
            )
        )

    return tuple(results)