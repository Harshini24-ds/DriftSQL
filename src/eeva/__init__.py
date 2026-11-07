"""Public interfaces for the EEVA core."""

from src.eeva.coverage import (
    CoverageResult,
    calculate_coverage,
)
from src.eeva.decision import (
    Decision,
    EEVAResult,
    RecoveryHint,
    ValidatedEvidenceBundle,
    evaluate_evidence,
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
    reason_for_state,
)
from src.eeva.validation import (
    ConflictAssessment,
    SourceReference,
    ValidationContext,
    snapshot_fingerprint,
    validate_obligations,
)


__all__ = [
    "ConflictAssessment",
    "CoverageResult",
    "Decision",
    "EEVAResult",
    "EvidenceState",
    "HARD_FAILURE_STATES",
    "ObligationState",
    "RECOVERABLE_STATES",
    "ReasonCode",
    "RecoveryHint",
    "SourceReference",
    "ValidatedEvidenceBundle",
    "ValidationContext",
    "calculate_coverage",
    "collect_reason_codes",
    "evaluate_evidence",
    "reason_for_state",
    "snapshot_fingerprint",
    "validate_obligations",
]