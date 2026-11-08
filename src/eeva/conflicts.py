"""Deterministic conflict detection for current approved evidence.

Compare records asserting the same named fact. Do not infer semantic
contradictions merely because unrelated descriptions differ.

Approved evidence may include explicit cross-source facts:

    "facts": [
        {
            "target": "olist_raw.order_items.price",
            "predicate": "unit",
            "value": "BRL",
        }
    ]

The facts must be part of the authenticated evidence payload.
"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.contracts.evidence import (
    EvidenceItem,
    EvidenceSnapshot,
    EvidenceType,
)
from src.eeva.evidence_state import EvidenceState
from src.eeva.validation import (
    ConflictAssessment,
    SourceReference,
    ValidationContext,
    _candidate_state,
    _matches,
    snapshot_fingerprint,
)
from src.query_intelligence.obligations import (
    EXPECTED_SOURCE_TYPES,
    EvidenceObligation,
)


class ConflictFinding(BaseModel):
    """One disagreement between approved sources."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str
    target: str
    predicate: str

    state: Literal[
        EvidenceState.CONFLICT_RECOVERABLE,
        EvidenceState.CONFLICT_CRITICAL,
    ]

    evidence_item_ids: tuple[str, ...]
    preferred_evidence_ids: tuple[str, ...] = ()


class ConflictReport(BaseModel):
    """Assessments for EEVA and details for the recovery planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assessments: tuple[ConflictAssessment, ...]
    findings: tuple[ConflictFinding, ...]


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _source_identity(item):
    return (
        item.source,
        item.provenance_id,
        item.type,
        item.key,
    )


def _extract_claims(item: EvidenceItem):
    """Return target, predicate and canonical value for each claim."""
    value = item.value

    if not isinstance(value, dict):
        return (
            (
                item.key.casefold(),
                f"{item.type.value}:value",
                _canonical_json(value),
            ),
        )

    targets = {item.key.casefold()}

    if isinstance(value.get("target"), str):
        targets.add(value["target"].casefold())

    claims = []

    explicit_facts = value.get("facts", [])

    if not isinstance(explicit_facts, list):
        raise ValueError("Evidence facts must be a list.")

    for fact in explicit_facts:
        if (
            not isinstance(fact, dict)
            or set(fact) != {"target", "predicate", "value"}
            or not isinstance(fact["target"], str)
            or not fact["target"].strip()
            or not isinstance(fact["predicate"], str)
            or not fact["predicate"].strip()
        ):
            raise ValueError(
                "Each fact requires target, predicate and value."
            )

        claims.append(
            (
                fact["target"].strip().casefold(),
                fact["predicate"].strip(),
                _canonical_json(fact["value"]),
            )
        )

    ignored_fields = {
        "facts",
        "target",
        "schema",
        "table",
        "column",
        "term",
        "aliases",
        "business_terms",
        "document_id",
        "title",
        "source_id",
    }

    shared_properties = {
        "unit",
        "data_type",
        "udt_name",
        "is_nullable",
        "kind",
    }

    for field, field_value in value.items():
        if field in ignored_fields:
            continue

        if item.type == EvidenceType.VALUE:
            # Different samples do not imply conflicting value domains.
            if (
                field != "values"
                or value.get("coverage") != "COMPLETE"
            ):
                continue

            if not isinstance(field_value, list):
                raise ValueError(
                    "Complete value evidence requires a values list."
                )

            encoded = _canonical_json(
                sorted({
                    _canonical_json(element)
                    for element in field_value
                })
            )
            predicate = "allowed_values"

        else:
            encoded = _canonical_json(field_value)

            if field in shared_properties:
                predicate = field

            elif field == "definition":
                term = str(
                    value.get("term", item.key)
                ).casefold()

                predicate = f"definition:{term}"

            else:
                predicate = (
                    f"{item.type.value}:"
                    f"{item.key.casefold()}:"
                    f"{field}"
                )

        claims.extend(
            (target, predicate, encoded)
            for target in sorted(targets)
        )

    return tuple(dict.fromkeys(claims))


def _source_priority(
    reference: SourceReference,
    predicate: str,
):
    """Use trusted source categories before attested authority levels."""
    if predicate in {
        "kind",
        "data_type",
        "udt_name",
        "is_nullable",
    }:
        order = {
            "active_postgresql_catalog": 3,
            "current_schema_snapshot": 2,
        }

    elif predicate == "allowed_values":
        order = {
            "bounded_read_only_value_probe": 2,
            "approved_value_profile": 1,
        }

    elif reference.evidence_type == EvidenceType.PERMISSION:
        order = {
            "active_postgresql_role": 3,
        }

    elif reference.evidence_type == EvidenceType.RELATIONSHIP:
        order = {
            "live_pk_fk_catalog": 3,
            "approved_relationship_evidence": 2,
        }

    else:
        order = {
            "approved_business_glossary": 3,
            "approved_documentation": 2,
            "approved_metadata_catalog": 1,
        }

    return (
        order.get(reference.source_type, 0),
        reference.authority_level,
    )


def _has_ambiguous_binding(candidates, current_records):
    """Check whether one approved glossary term has several targets."""
    bindings = {}

    for item in candidates:
        if (
            item.evidence_id not in current_records
            or not isinstance(item.value, dict)
        ):
            continue

        target = item.value.get("target")

        if isinstance(target, str):
            bindings.setdefault(
                item.key.casefold(),
                set(),
            ).add(target.casefold())

    return any(
        len(targets) > 1
        for targets in bindings.values()
    )


def detect_conflicts(
    obligations: tuple[EvidenceObligation, ...],
    snapshot: EvidenceSnapshot,
    context: ValidationContext,
) -> ConflictReport:
    """Produce conflict assessments compatible with Member 1 EEVA.

    Missing, stale, invalid and unauthorized evidence remains subject
    to the validation rules. A PASS assessment only means this scan
    found no unresolved conflict or ambiguous binding.

    Preferred evidence IDs are diagnostic. This function does not
    edit evidence or silently discard a conflicting source.
    """
    if (
        snapshot.request_id != context.request_id
        or snapshot.database_id != context.database_id
        or snapshot.freshness_policy_version
        != context.freshness_policy_version
    ):
        raise ValueError(
            "Snapshot and context request, database and policy must match."
        )

    requirement_ids = {
        obligation.requirement_id
        for obligation in obligations
    }

    if len(requirement_ids) != len(obligations):
        raise ValueError("Duplicate requirement IDs.")

    if any(
        obligation.request_id != snapshot.request_id
        or obligation.database_id != snapshot.database_id
        for obligation in obligations
    ):
        raise ValueError(
            "Obligations must belong to the snapshot request and database."
        )

    if (
        len({
            item.evidence_id
            for item in snapshot.evidence_items
        })
        != len(snapshot.evidence_items)
    ):
        raise ValueError("Duplicate evidence IDs.")

    if not obligations:
        return ConflictReport(
            assessments=(),
            findings=(),
        )

    references = {
        (
            reference.source,
            reference.provenance_id,
            reference.evidence_type,
            reference.key,
        ): reference
        for reference in context.sources
    }

    if len(references) != len(context.sources):
        raise ValueError("Duplicate current source references.")

    current_records = {}
    claim_groups = {}

    for item in snapshot.evidence_items:
        reference = references.get(_source_identity(item))

        if (
            reference is None
            or item.access_label not in context.allowed_access_labels
        ):
            continue

        target = (
            item.value.get("target", item.key)
            if isinstance(item.value, dict)
            else item.key
        )

        if (
            isinstance(target, str)
            and context.permission_decisions.get(target) is False
        ):
            continue

        expected_sources = tuple(
            dict.fromkeys(
                (
                    *EXPECTED_SOURCE_TYPES[item.type],
                    *(
                        source_type
                        for obligation in obligations
                        if obligation.evidence_type == item.type
                        for source_type
                        in obligation.expected_source_types
                    ),
                )
            )
        )

        probe = obligations[0].model_copy(
            update={
                "expected_source_types": expected_sources,
            }
        )

        if (
            _candidate_state(
                item,
                reference,
                probe,
                snapshot,
                context,
            )
            != EvidenceState.PASS
        ):
            continue

        claims = _extract_claims(item)

        current_records[item.evidence_id] = (
            item,
            reference,
            claims,
        )

        for claim_target, predicate, encoded_value in claims:
            claim_groups.setdefault(
                (claim_target, predicate),
                [],
            ).append(
                (
                    item.evidence_id,
                    encoded_value,
                    reference,
                )
            )

    findings = []
    assessments = []
    digest = snapshot_fingerprint(snapshot)

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

        available_actions = (
            set(obligation.recovery_actions)
            & context.available_recovery_actions
        )

        if (
            obligation.evidence_type == EvidenceType.GLOSSARY
            and _has_ambiguous_binding(
                candidates,
                current_records,
            )
        ):
            assessments.append(
                ConflictAssessment(
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_digest=digest,
                    requirement_id=obligation.requirement_id,
                    evidence_item_ids=candidate_ids,
                    state=EvidenceState.AMBIGUOUS_RECOVERABLE,
                )
            )
            continue

        relevant_claims = {
            (claim_target, predicate)
            for item in candidates
            if item.evidence_id in current_records
            for claim_target, predicate, _
            in current_records[item.evidence_id][2]
        }

        state = EvidenceState.PASS

        for claim_target, predicate in sorted(relevant_claims):
            claims = claim_groups[(claim_target, predicate)]

            if len({
                encoded_value
                for _, encoded_value, _ in claims
            }) < 2:
                continue

            highest_priority = max(
                _source_priority(reference, predicate)
                for _, _, reference in claims
            )

            preferred = [
                claim
                for claim in claims
                if _source_priority(claim[2], predicate)
                == highest_priority
            ]

            preferred_values = {
                encoded_value
                for _, encoded_value, _ in preferred
            }

            preferred_ids = (
                tuple(sorted({
                    evidence_id
                    for evidence_id, _, _ in preferred
                }))
                if len(preferred_values) == 1
                else ()
            )

            state = (
                EvidenceState.CONFLICT_RECOVERABLE
                if available_actions
                else EvidenceState.CONFLICT_CRITICAL
            )

            findings.append(
                ConflictFinding(
                    requirement_id=obligation.requirement_id,
                    target=claim_target,
                    predicate=predicate,
                    state=state,
                    evidence_item_ids=tuple(sorted({
                        evidence_id
                        for evidence_id, _, _ in claims
                    })),
                    preferred_evidence_ids=preferred_ids,
                )
            )

        assessments.append(
            ConflictAssessment(
                snapshot_id=snapshot.snapshot_id,
                snapshot_digest=digest,
                requirement_id=obligation.requirement_id,
                evidence_item_ids=candidate_ids,
                state=state,
            )
        )

    return ConflictReport(
        assessments=tuple(assessments),
        findings=tuple(findings),
    )