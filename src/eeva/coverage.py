"""Calculate diagnostic evidence coverage for EEVA."""

from math import fsum, isfinite

from pydantic import BaseModel, ConfigDict, Field

from src.eeva.evidence_state import EvidenceState, ObligationState
from src.query_intelligence.obligations import EvidenceObligation


class CoverageResult(BaseModel):
    """Mandatory count coverage and weighted optional coverage."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    mandatory_coverage: float = Field(ge=0.0, le=1.0)
    optional_coverage: float = Field(ge=0.0, le=1.0)

    mandatory_total: int = Field(ge=0)
    mandatory_passed: int = Field(ge=0)
    optional_total: int = Field(ge=0)


def calculate_coverage(
    obligations: tuple[EvidenceObligation, ...],
    states: tuple[ObligationState, ...],
) -> CoverageResult:
    """Calculate coverage using only obligations classified as PASS.

    Mandatory coverage:
        passing mandatory obligations / total mandatory obligations

    Optional coverage:
        passing optional weight / total optional weight

    An empty category has coverage 1.0 because it has no outstanding
    requirements. The decision engine must separately guard empty R(q).

    Coverage never overrides hard failures.
    """
    obligations_by_id = {
        obligation.requirement_id: obligation
        for obligation in obligations
    }

    states_by_id = {
        result.requirement_id: result
        for result in states
    }

    if len(obligations_by_id) != len(obligations):
        raise ValueError("Duplicate obligation requirement IDs.")

    if len(states_by_id) != len(states):
        raise ValueError("Duplicate state requirement IDs.")

    if obligations_by_id.keys() != states_by_id.keys():
        raise ValueError(
            "Exactly one state is required for every obligation."
        )

    for obligation in obligations:
        weight = obligation.diagnostic_weight
        if not isfinite(weight) or weight <= 0:
            raise ValueError(
                "Diagnostic weights must be finite and positive."
            )

    mandatory = [
        obligation
        for obligation in obligations
        if obligation.mandatory
    ]

    optional = [
        obligation
        for obligation in obligations
        if not obligation.mandatory
    ]

    mandatory_passed = sum(
        states_by_id[obligation.requirement_id].state
        == EvidenceState.PASS
        for obligation in mandatory
    )

    mandatory_coverage = (
        mandatory_passed / len(mandatory)
        if mandatory
        else 1.0
    )

    optional_coverage = 1.0

    if optional:
        # Scaling preserves the ratio and avoids overflowing the sum
        # when individual weights are large but finite.
        scale = max(
            obligation.diagnostic_weight
            for obligation in optional
        )

        total_weight = fsum(
            obligation.diagnostic_weight / scale
            for obligation in optional
        )

        passed_weight = fsum(
            obligation.diagnostic_weight / scale
            for obligation in optional
            if states_by_id[obligation.requirement_id].state
            == EvidenceState.PASS
        )

        optional_coverage = passed_weight / total_weight

    return CoverageResult(
        mandatory_coverage=mandatory_coverage,
        optional_coverage=optional_coverage,
        mandatory_total=len(mandatory),
        mandatory_passed=mandatory_passed,
        optional_total=len(optional),
    )