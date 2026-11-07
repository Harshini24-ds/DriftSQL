"""Tests for mandatory and optional EEVA evidence coverage."""

import pytest

from src.eeva.coverage import calculate_coverage
from src.eeva.evidence_state import EvidenceState, ObligationState
from src.query_intelligence.obligations import EvidenceObligation


def make_obligation(
    requirement_id: str,
    *,
    mandatory: bool = True,
    weight: float = 1.0,
) -> EvidenceObligation:
    return EvidenceObligation(
        requirement_id=requirement_id,
        request_id="coverage-test",
        database_id="olist_primary",
        evidence_type="SCHEMA",
        target=f"test_schema.{requirement_id}",
        mandatory=mandatory,
        expected_source_types=("current_schema_snapshot",),
        constraint_type="entity_exists",
        diagnostic_weight=weight,
        reason="Confirm the required schema object exists.",
    )


def make_state(
    requirement_id: str,
    state: EvidenceState = EvidenceState.PASS,
) -> ObligationState:
    return ObligationState(
        requirement_id=requirement_id,
        state=state,
    )


def test_all_mandatory_obligations_pass():
    obligations = (
        make_obligation("m1"),
        make_obligation("m2"),
    )
    states = (
        make_state("m1"),
        make_state("m2"),
    )

    result = calculate_coverage(obligations, states)

    assert result.mandatory_coverage == 1.0
    assert result.mandatory_total == 2
    assert result.mandatory_passed == 2
    assert result.optional_total == 0
    assert result.optional_coverage == 1.0


def test_mandatory_coverage_uses_counts_not_weights():
    obligations = (
        make_obligation("m1", weight=99.0),
        make_obligation("m2", weight=1.0),
    )
    states = (
        make_state("m1"),
        make_state("m2", EvidenceState.MISSING_CRITICAL),
    )

    result = calculate_coverage(obligations, states)

    assert result.mandatory_coverage == 0.5
    assert result.mandatory_passed == 1


def test_optional_coverage_uses_weights():
    obligations = (
        make_obligation("o1", mandatory=False, weight=3.0),
        make_obligation("o2", mandatory=False, weight=1.0),
    )
    states = (
        make_state("o1"),
        make_state("o2", EvidenceState.OPTIONAL_MISSING),
    )

    result = calculate_coverage(obligations, states)

    assert result.optional_coverage == 0.75
    assert result.optional_total == 2
    assert result.mandatory_total == 0
    assert result.mandatory_coverage == 1.0


def test_optional_success_does_not_increase_mandatory_coverage():
    obligations = (
        make_obligation("m1"),
        make_obligation("o1", mandatory=False, weight=1000.0),
    )
    states = (
        make_state("m1", EvidenceState.UNAUTHORIZED),
        make_state("o1"),
    )

    result = calculate_coverage(obligations, states)

    assert result.mandatory_coverage == 0.0
    assert result.optional_coverage == 1.0


@pytest.mark.parametrize("state", list(EvidenceState))
def test_only_pass_counts_as_satisfied(state):
    obligations = (
        make_obligation("m1"),
        make_obligation("o1", mandatory=False),
    )
    states = (
        make_state("m1", state),
        make_state("o1", state),
    )

    result = calculate_coverage(obligations, states)
    expected = 1.0 if state == EvidenceState.PASS else 0.0

    assert result.mandatory_coverage == expected
    assert result.optional_coverage == expected


def test_empty_obligations_have_no_outstanding_requirements():
    result = calculate_coverage((), ())

    assert result.mandatory_coverage == 1.0
    assert result.optional_coverage == 1.0
    assert result.mandatory_total == 0
    assert result.mandatory_passed == 0
    assert result.optional_total == 0


def test_state_order_does_not_change_coverage():
    obligations = (
        make_obligation("m1"),
        make_obligation("m2"),
    )
    states = (
        make_state("m2", EvidenceState.MISSING_RECOVERABLE),
        make_state("m1"),
    )

    result = calculate_coverage(obligations, states)

    assert result.mandatory_coverage == 0.5
    assert result.mandatory_passed == 1


def test_missing_state_is_rejected():
    with pytest.raises(ValueError, match="Exactly one state"):
        calculate_coverage(
            (make_obligation("m1"),),
            (),
        )


def test_extra_state_is_rejected():
    with pytest.raises(ValueError, match="Exactly one state"):
        calculate_coverage(
            (make_obligation("m1"),),
            (make_state("m1"), make_state("unknown")),
        )


def test_duplicate_states_are_rejected():
    with pytest.raises(ValueError, match="Duplicate state"):
        calculate_coverage(
            (make_obligation("m1"),),
            (make_state("m1"), make_state("m1")),
        )


def test_duplicate_obligations_are_rejected():
    with pytest.raises(ValueError, match="Duplicate obligation"):
        calculate_coverage(
            (make_obligation("m1"), make_obligation("m1")),
            (make_state("m1"),),
        )


def test_infinite_weight_is_rejected():
    with pytest.raises(ValueError, match="finite and positive"):
        calculate_coverage(
            (
                make_obligation(
                    "o1",
                    mandatory=False,
                    weight=float("inf"),
                ),
            ),
            (make_state("o1"),),
        )


def test_large_finite_optional_weights_do_not_overflow():
    obligations = (
        make_obligation("o1", mandatory=False, weight=1e308),
        make_obligation("o2", mandatory=False, weight=1e308),
    )
    states = (
        make_state("o1"),
        make_state("o2", EvidenceState.OPTIONAL_MISSING),
    )

    result = calculate_coverage(obligations, states)

    assert result.optional_coverage == 0.5