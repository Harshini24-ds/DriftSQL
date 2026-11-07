"""Stable reason codes explaining EEVA validation outcomes."""

from enum import Enum

from src.contracts.evidence import EvidenceType
from src.eeva.evidence_state import EvidenceState, ObligationState
from src.query_intelligence.obligations import EvidenceObligation


class ReasonCode(str, Enum):
    """Blueprint reason codes and explicit generic fallbacks."""

    SCHEMA_MISSING_CRITICAL = "SCHEMA_MISSING_CRITICAL"
    SCHEMA_STALE_RECOVERABLE = "SCHEMA_STALE_RECOVERABLE"

    METADATA_MISSING_RECOVERABLE = "METADATA_MISSING_RECOVERABLE"
    METADATA_INVALID = "METADATA_INVALID"

    GLOSSARY_AMBIGUOUS = "GLOSSARY_AMBIGUOUS"

    DOCUMENT_CONFLICT_RECOVERABLE = "DOCUMENT_CONFLICT_RECOVERABLE"
    DOCUMENT_CONFLICT_CRITICAL = "DOCUMENT_CONFLICT_CRITICAL"

    RELATIONSHIP_MISSING_CRITICAL = "RELATIONSHIP_MISSING_CRITICAL"
    RELATIONSHIP_STALE_RECOVERABLE = "RELATIONSHIP_STALE_RECOVERABLE"

    PERMISSION_DENIED = "PERMISSION_DENIED"
    PROVENANCE_INVALID = "PROVENANCE_INVALID"
    RETRIEVAL_COVERAGE_LOW = "RETRIEVAL_COVERAGE_LOW"

    RECOVERY_NO_PROGRESS = "RECOVERY_NO_PROGRESS"
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"

    ALL_MANDATORY_EVIDENCE_PASS = "ALL_MANDATORY_EVIDENCE_PASS"

    # Generic fallbacks for combinations without a dedicated blueprint code.
    EVIDENCE_MISSING_CRITICAL = "EVIDENCE_MISSING_CRITICAL"
    EVIDENCE_STALE_RECOVERABLE = "EVIDENCE_STALE_RECOVERABLE"
    EVIDENCE_AMBIGUOUS = "EVIDENCE_AMBIGUOUS"
    EVIDENCE_CONFLICT_RECOVERABLE = "EVIDENCE_CONFLICT_RECOVERABLE"
    EVIDENCE_CONFLICT_CRITICAL = "EVIDENCE_CONFLICT_CRITICAL"


_SPECIFIC_REASONS = {
    EvidenceType.SCHEMA: {
        EvidenceState.MISSING_CRITICAL:
            ReasonCode.SCHEMA_MISSING_CRITICAL,
        EvidenceState.STALE_RECOVERABLE:
            ReasonCode.SCHEMA_STALE_RECOVERABLE,
    },
    EvidenceType.METADATA: {
        EvidenceState.MISSING_RECOVERABLE:
            ReasonCode.METADATA_MISSING_RECOVERABLE,
        EvidenceState.INVALID:
            ReasonCode.METADATA_INVALID,
    },
    EvidenceType.GLOSSARY: {
        EvidenceState.AMBIGUOUS_RECOVERABLE:
            ReasonCode.GLOSSARY_AMBIGUOUS,
    },
    EvidenceType.DOCUMENT: {
        EvidenceState.CONFLICT_RECOVERABLE:
            ReasonCode.DOCUMENT_CONFLICT_RECOVERABLE,
        EvidenceState.CONFLICT_CRITICAL:
            ReasonCode.DOCUMENT_CONFLICT_CRITICAL,
    },
    EvidenceType.RELATIONSHIP: {
        EvidenceState.MISSING_CRITICAL:
            ReasonCode.RELATIONSHIP_MISSING_CRITICAL,
        EvidenceState.STALE_RECOVERABLE:
            ReasonCode.RELATIONSHIP_STALE_RECOVERABLE,
    },
}


_GENERIC_REASONS = {
    EvidenceState.MISSING_CRITICAL:
        ReasonCode.EVIDENCE_MISSING_CRITICAL,
    EvidenceState.MISSING_RECOVERABLE:
        ReasonCode.RETRIEVAL_COVERAGE_LOW,
    EvidenceState.STALE_RECOVERABLE:
        ReasonCode.EVIDENCE_STALE_RECOVERABLE,
    EvidenceState.AMBIGUOUS_RECOVERABLE:
        ReasonCode.EVIDENCE_AMBIGUOUS,
    EvidenceState.CONFLICT_RECOVERABLE:
        ReasonCode.EVIDENCE_CONFLICT_RECOVERABLE,
    EvidenceState.CONFLICT_CRITICAL:
        ReasonCode.EVIDENCE_CONFLICT_CRITICAL,
    EvidenceState.UNAUTHORIZED:
        ReasonCode.PERMISSION_DENIED,
    EvidenceState.INVALID:
        ReasonCode.PROVENANCE_INVALID,
}


def reason_for_state(
    evidence_type: EvidenceType,
    state: EvidenceState,
) -> ReasonCode | None:
    """Return the specific reason, or a generic failure reason.

    PASS and OPTIONAL_MISSING are not failures, so they return None.
    The decision engine adds ALL_MANDATORY_EVIDENCE_PASS only when
    the complete evaluation permits generation.
    """
    specific = _SPECIFIC_REASONS.get(evidence_type, {})

    if state in specific:
        return specific[state]

    return _GENERIC_REASONS.get(state)


def collect_reason_codes(
    obligations: tuple[EvidenceObligation, ...],
    states: tuple[ObligationState, ...],
) -> tuple[ReasonCode, ...]:
    """Return unique reason codes in obligation order.

    Every obligation must have exactly one state. Missing, additional,
    or duplicate records are rejected instead of silently ignored.
    """
    obligation_ids = {
        obligation.requirement_id
        for obligation in obligations
    }

    states_by_id = {
        result.requirement_id: result
        for result in states
    }

    if len(obligation_ids) != len(obligations):
        raise ValueError("Duplicate obligation requirement IDs.")

    if len(states_by_id) != len(states):
        raise ValueError("Duplicate state requirement IDs.")

    if obligation_ids != states_by_id.keys():
        raise ValueError(
            "Exactly one state is required for every obligation."
        )

    reasons: list[ReasonCode] = []
    seen: set[ReasonCode] = set()

    for obligation in obligations:
        state = states_by_id[obligation.requirement_id].state
        reason = reason_for_state(obligation.evidence_type, state)

        if reason is not None and reason not in seen:
            reasons.append(reason)
            seen.add(reason)

    return tuple(reasons)