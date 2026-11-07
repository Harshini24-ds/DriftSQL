"""EEVA's pre-generation decision engine.

This module evaluates evidence. It does not generate SQL, execute SQL,
retrieve evidence, or perform clarification/recovery cycles.
"""

from enum import Enum
from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.evidence import EvidenceSnapshot
from src.eeva.coverage import (
    CoverageResult,
    calculate_coverage,
)
from src.eeva.evidence_state import (
    HARD_FAILURE_STATES,
    RECOVERABLE_STATES,
    EvidenceState,
    ObligationState,
)
from src.eeva.reason_codes import (
    ReasonCode,
    collect_reason_codes,
)
from src.eeva.validation import (
    ConflictAssessment,
    ValidationContext,
    validate_obligations,
)
from src.query_intelligence.pipeline import QueryAnalysisResult
from src.query_intelligence.precheck import PrecheckDecision


class Decision(str, Enum):
    GENERATE = "GENERATE"
    CLARIFY = "CLARIFY"
    REJECT = "REJECT"


class RecoveryHint(BaseModel):
    """An approved action hint for Member 2's recovery planner.

    This is not an executed action or a complete recovery plan.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str = Field(min_length=1)
    action: str = Field(min_length=1)


class ValidatedEvidenceBundle(BaseModel):
    """Frozen evidence approved for downstream generation.

    Month 2 EvidenceItem.value can contain mutable dictionaries.
    Canonical JSON prevents later input mutations from changing
    the evidence already approved by EEVA.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    database_id: str = Field(min_length=1)

    principal_id: str = Field(min_length=1)
    database_role: str = Field(min_length=1)

    requirement_ids: tuple[str, ...]
    snapshot_json: str

    def evidence_snapshot(self) -> EvidenceSnapshot:
        """Return a fresh typed view of the frozen evidence."""
        return EvidenceSnapshot.model_validate_json(
            self.snapshot_json
        )


class EEVAResult(BaseModel):
    """Decision and diagnostics for one evaluated evidence snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Decision
    decision_stage: Literal["EEVA"] = "EEVA"

    snapshot_id: str
    request_id: str
    database_id: str

    evidence_states: tuple[ObligationState, ...]
    coverage: CoverageResult

    hard_failures: tuple[str, ...]
    conflicts: tuple[ConflictAssessment, ...]
    missing_items: tuple[str, ...]

    reason_codes: tuple[ReasonCode, ...]
    recovery_actions: tuple[RecoveryHint, ...] = ()
    unresolved_terms: tuple[str, ...] = ()

    validated_evidence_bundle: ValidatedEvidenceBundle | None = None

    @property
    def validated_evidence_bundle_id(self) -> str | None:
        if self.validated_evidence_bundle is None:
            return None

        return self.validated_evidence_bundle.bundle_id


def _freeze_bundle(
    analysis: QueryAnalysisResult,
    snapshot: EvidenceSnapshot,
    states: tuple[ObligationState, ...],
) -> ValidatedEvidenceBundle:
    """Freeze only evidence approved by passing obligations."""
    access = analysis.access_context

    if access is None:
        raise ValueError(
            "Trusted access context is required."
        )

    approved_ids = {
        evidence_id
        for result in states
        if result.state == EvidenceState.PASS
        for evidence_id in result.evidence_item_ids
    }

    approved_snapshot = EvidenceSnapshot(
        snapshot_id=snapshot.snapshot_id,
        request_id=snapshot.request_id,
        database_id=snapshot.database_id,
        evidence_items=tuple(
            item
            for item in snapshot.evidence_items
            if item.evidence_id in approved_ids
        ),
        schema_hash=snapshot.schema_hash,
        # Retrieval diagnostics are not validated generator context.
        retrieval_metadata={},
        freshness_policy_version=snapshot.freshness_policy_version,
        created_at=snapshot.created_at,
    )

    snapshot_json = json.dumps(
        approved_snapshot.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )

    requirement_ids = tuple(
        obligation.requirement_id
        for obligation in analysis.required_evidence_obligations
    )

    # Bind bundle identity to evidence, request identity,
    # trusted access context and the evaluated obligations.
    identity_json = json.dumps(
        {
            "snapshot": json.loads(snapshot_json),
            "principal_id": access.principal_id,
            "database_role": access.database_role,
            "requirement_ids": requirement_ids,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )

    digest = sha256(
        identity_json.encode("utf-8")
    ).hexdigest()

    return ValidatedEvidenceBundle(
        bundle_id=f"EEVA-{digest}",
        snapshot_id=snapshot.snapshot_id,
        request_id=snapshot.request_id,
        database_id=snapshot.database_id,
        principal_id=access.principal_id,
        database_role=access.database_role,
        requirement_ids=requirement_ids,
        snapshot_json=snapshot_json,
    )


def evaluate_evidence(
    analysis: QueryAnalysisResult,
    snapshot: EvidenceSnapshot,
    context: ValidationContext,
    *,
    conflict_assessments: tuple[ConflictAssessment, ...],
) -> EEVAResult:
    """Evaluate one snapshot using the blueprint's decision precedence.

    Invalid pipeline inputs raise ValueError.
    Well-formed evidence failures produce REJECT or CLARIFY.

    Unsupported operations must stop earlier at QIM_PRECHECK.
    Member 2 owns recovery execution and clarification cycle limits.
    """
    if analysis.precheck.decision != PrecheckDecision.ACCEPT:
        raise ValueError(
            "Unsupported requests must stop at QIM_PRECHECK."
        )

    if (
        analysis.intent.request_id != snapshot.request_id
        or analysis.intent.database_id != snapshot.database_id
    ):
        raise ValueError(
            "Analysis and snapshot must use the same request "
            "and database."
        )

    access = analysis.access_context

    if access is None:
        raise ValueError(
            "Trusted access context is required."
        )

    if access.database_role != context.database_role:
        raise ValueError(
            "QIM and EEVA must use the same trusted active role."
        )

    obligations = analysis.required_evidence_obligations

    if not obligations:
        raise ValueError(
            "EEVA requires a non-empty evidence obligation set."
        )

    states = validate_obligations(
        obligations,
        snapshot,
        context,
        conflict_assessments,
        filters=analysis.intent.filters,
    )

    coverage = calculate_coverage(obligations, states)
    reasons = collect_reason_codes(obligations, states)

    hard_failures = tuple(
        result.requirement_id
        for result in states
        if result.state in HARD_FAILURE_STATES
    )

    has_recoverable_evidence = any(
        result.state in RECOVERABLE_STATES
        for result in states
    )

    states_by_id = {
        result.requirement_id: result.state
        for result in states
    }

    all_mandatory_pass = all(
        states_by_id[obligation.requirement_id]
        == EvidenceState.PASS
        for obligation in obligations
        if obligation.mandatory
    )

    unresolved_terms = analysis.intent.unresolved_terms
    bundle = None
    recovery_actions: tuple[RecoveryHint, ...] = ()

    if hard_failures:
        decision = Decision.REJECT

    elif has_recoverable_evidence or unresolved_terms:
        decision = Decision.CLARIFY

        recovery_actions = tuple(
            RecoveryHint(
                requirement_id=result.requirement_id,
                action=action,
            )
            for result in states
            if result.state in RECOVERABLE_STATES
            for action in result.recovery_actions
        )

        if unresolved_terms:
            reasons = tuple(
                dict.fromkeys(
                    (
                        *reasons,
                        ReasonCode.EVIDENCE_AMBIGUOUS,
                    )
                )
            )

    elif (
        coverage.mandatory_coverage == 1.0
        and all_mandatory_pass
    ):
        decision = Decision.GENERATE
        reasons = (
            ReasonCode.ALL_MANDATORY_EVIDENCE_PASS,
        )

        bundle = _freeze_bundle(
            analysis,
            snapshot,
            states,
        )

    else:
        # Defensive fallback: incomplete mandatory evidence
        # must never reach generation.
        decision = Decision.REJECT
        reasons = tuple(
            dict.fromkeys(
                (
                    *reasons,
                    ReasonCode.RETRIEVAL_COVERAGE_LOW,
                )
            )
        )

    return EEVAResult(
        decision=decision,
        snapshot_id=snapshot.snapshot_id,
        request_id=snapshot.request_id,
        database_id=snapshot.database_id,
        evidence_states=states,
        coverage=coverage,
        hard_failures=hard_failures,
        conflicts=tuple(
            assessment
            for assessment in conflict_assessments
            if assessment.state
            in {
                EvidenceState.CONFLICT_RECOVERABLE,
                EvidenceState.CONFLICT_CRITICAL,
            }
        ),
        missing_items=tuple(
            result.requirement_id
            for result in states
            if result.state
            in {
                EvidenceState.MISSING_RECOVERABLE,
                EvidenceState.MISSING_CRITICAL,
                EvidenceState.OPTIONAL_MISSING,
            }
        ),
        reason_codes=reasons,
        recovery_actions=recovery_actions,
        unresolved_terms=unresolved_terms,
        validated_evidence_bundle=bundle,
    )