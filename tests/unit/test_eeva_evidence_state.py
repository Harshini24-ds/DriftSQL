"""Tests for EEVA evidence states and obligation results."""

import pytest
from pydantic import ValidationError

from src.eeva.evidence_state import (
    HARD_FAILURE_STATES,
    RECOVERABLE_STATES,
    EvidenceState,
    ObligationState,
)


def test_evidence_state_values():
    assert {state.value for state in EvidenceState} == {
        "PASS",
        "MISSING_RECOVERABLE",
        "STALE_RECOVERABLE",
        "AMBIGUOUS_RECOVERABLE",
        "CONFLICT_RECOVERABLE",
        "MISSING_CRITICAL",
        "CONFLICT_CRITICAL",
        "UNAUTHORIZED",
        "INVALID",
        "OPTIONAL_MISSING",
    }


def test_hard_failure_states():
    assert HARD_FAILURE_STATES == {
        EvidenceState.MISSING_CRITICAL,
        EvidenceState.CONFLICT_CRITICAL,
        EvidenceState.UNAUTHORIZED,
        EvidenceState.INVALID,
    }


def test_recoverable_states():
    assert RECOVERABLE_STATES == {
        EvidenceState.MISSING_RECOVERABLE,
        EvidenceState.STALE_RECOVERABLE,
        EvidenceState.AMBIGUOUS_RECOVERABLE,
        EvidenceState.CONFLICT_RECOVERABLE,
    }


def test_state_categories_do_not_overlap():
    assert HARD_FAILURE_STATES.isdisjoint(RECOVERABLE_STATES)

    for state in (
        EvidenceState.PASS,
        EvidenceState.OPTIONAL_MISSING,
    ):
        assert state not in HARD_FAILURE_STATES
        assert state not in RECOVERABLE_STATES


@pytest.mark.parametrize("state", list(EvidenceState))
def test_state_json_round_trip(state):
    original = ObligationState(
        requirement_id="REQ-001",
        state=state,
        evidence_item_ids=("EV-001",),
    )

    restored = ObligationState.model_validate_json(
        original.model_dump_json()
    )

    assert restored == original
    assert original.model_dump(mode="json")["state"] == state.value


def test_default_collections_are_empty():
    result = ObligationState(
        requirement_id="REQ-001",
        state=EvidenceState.MISSING_CRITICAL,
    )

    assert result.evidence_item_ids == ()
    assert result.recovery_actions == ()


@pytest.mark.parametrize(
    "invalid_state",
    ["UNKNOWN", "GLOSSARY_AMBIGUOUS", ""],
)
def test_invalid_states_are_rejected(invalid_state):
    with pytest.raises(ValidationError):
        ObligationState(
            requirement_id="REQ-001",
            state=invalid_state,
        )


def test_empty_requirement_id_is_rejected():
    with pytest.raises(ValidationError):
        ObligationState(
            requirement_id="",
            state=EvidenceState.PASS,
        )


def test_unexpected_fields_are_rejected():
    with pytest.raises(ValidationError):
        ObligationState(
            requirement_id="REQ-001",
            state=EvidenceState.PASS,
            unexpected_field=True,
        )


def test_result_cannot_be_modified():
    result = ObligationState(
        requirement_id="REQ-001",
        state=EvidenceState.PASS,
    )

    with pytest.raises(ValidationError):
        result.state = EvidenceState.INVALID

    assert result.state == EvidenceState.PASS