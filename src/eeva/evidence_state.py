"""Evidence states and per-obligation results for EEVA."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class EvidenceState(str, Enum):
    """Classification of evidence supporting one requirement."""

    PASS = "PASS"

    MISSING_RECOVERABLE = "MISSING_RECOVERABLE"
    STALE_RECOVERABLE = "STALE_RECOVERABLE"
    AMBIGUOUS_RECOVERABLE = "AMBIGUOUS_RECOVERABLE"
    CONFLICT_RECOVERABLE = "CONFLICT_RECOVERABLE"

    MISSING_CRITICAL = "MISSING_CRITICAL"
    CONFLICT_CRITICAL = "CONFLICT_CRITICAL"
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID = "INVALID"

    # The blueprint's pseudocode treats absent optional evidence separately.
    OPTIONAL_MISSING = "OPTIONAL_MISSING"


HARD_FAILURE_STATES = frozenset(
    {
        EvidenceState.MISSING_CRITICAL,
        EvidenceState.CONFLICT_CRITICAL,
        EvidenceState.UNAUTHORIZED,
        EvidenceState.INVALID,
    }
)


RECOVERABLE_STATES = frozenset(
    {
        EvidenceState.MISSING_RECOVERABLE,
        EvidenceState.STALE_RECOVERABLE,
        EvidenceState.AMBIGUOUS_RECOVERABLE,
        EvidenceState.CONFLICT_RECOVERABLE,
    }
)


class ObligationState(BaseModel):
    """Validation result for one required-evidence obligation.

    evidence_item_ids identifies evidence approved for this obligation.
    recovery_actions contains action names for the recovery planner.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    requirement_id: str = Field(min_length=1)
    state: EvidenceState

    evidence_item_ids: tuple[str, ...] = ()
    recovery_actions: tuple[str, ...] = ()