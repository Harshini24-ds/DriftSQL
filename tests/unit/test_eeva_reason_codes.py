"""Tests for EEVA reason-code mapping and collection."""

import pytest

from src.contracts.evidence import EvidenceType
from src.eeva.evidence_state import EvidenceState, ObligationState
from src.eeva.reason_codes import (
    ReasonCode,
    collect_reason_codes,
    reason_for_state,
)
from src.query_intelligence.obligations import EvidenceObligation


def make_obligation(
    requirement_id: str,
    evidence_type: EvidenceType,
) -> EvidenceObligation:
    return EvidenceObligation(
        requirement_id=requirement_id,
        request_id="reason-test",
        database_id="olist_primary",
        evidence_type=evidence_type,
        target=f"test_schema.{requirement_id}",
        mandatory=True,
        expected_source_types=("approved_test_source",),
        constraint_type="test_constraint",
        reason="Verify the required evidence.",
    )


def make_state(
    requirement_id: str,
    state: EvidenceState,
) -> ObligationState:
    return ObligationState(
        requirement_id=requirement_id,
        state=state,
    )


@pytest.mark.parametrize(
    "evidence_type,state,expected",
    [
        ("SCHEMA", "MISSING_CRITICAL", "SCHEMA_MISSING_CRITICAL"),
        ("SCHEMA", "STALE_RECOVERABLE", "SCHEMA_STALE_RECOVERABLE"),
        (
            "METADATA",
            "MISSING_RECOVERABLE",
            "METADATA_MISSING_RECOVERABLE",
        ),
        ("METADATA", "INVALID", "METADATA_INVALID"),
        ("GLOSSARY", "AMBIGUOUS_RECOVERABLE", "GLOSSARY_AMBIGUOUS"),
        (
            "DOCUMENT",
            "CONFLICT_RECOVERABLE",
            "DOCUMENT_CONFLICT_RECOVERABLE",
        ),
        (
            "DOCUMENT",
            "CONFLICT_CRITICAL",
            "DOCUMENT_CONFLICT_CRITICAL",
        ),
        (
            "RELATIONSHIP",
            "MISSING_CRITICAL",
            "RELATIONSHIP_MISSING_CRITICAL",
        ),
        (
            "RELATIONSHIP",
            "STALE_RECOVERABLE",
            "RELATIONSHIP_STALE_RECOVERABLE",
        ),
        ("PERMISSION", "UNAUTHORIZED", "PERMISSION_DENIED"),
        ("VALUE", "INVALID", "PROVENANCE_INVALID"),
        ("VALUE", "MISSING_RECOVERABLE", "RETRIEVAL_COVERAGE_LOW"),
    ],
)
def test_blueprint_reason_mappings(evidence_type, state, expected):
    result = reason_for_state(
        EvidenceType(evidence_type),
        EvidenceState(state),
    )

    assert result == ReasonCode(expected)


@pytest.mark.parametrize(
    "state,expected",
    [
        ("MISSING_CRITICAL", "EVIDENCE_MISSING_CRITICAL"),
        ("STALE_RECOVERABLE", "EVIDENCE_STALE_RECOVERABLE"),
        ("AMBIGUOUS_RECOVERABLE", "EVIDENCE_AMBIGUOUS"),
        ("CONFLICT_RECOVERABLE", "EVIDENCE_CONFLICT_RECOVERABLE"),
        ("CONFLICT_CRITICAL", "EVIDENCE_CONFLICT_CRITICAL"),
    ],
)
def test_generic_fallback_mappings(state, expected):
    # These generic codes are implementation additions.
    result = reason_for_state(
        EvidenceType.VALUE,
        EvidenceState(state),
    )

    assert result == ReasonCode(expected)


@pytest.mark.parametrize("evidence_type", list(EvidenceType))
@pytest.mark.parametrize(
    "state",
    [EvidenceState.PASS, EvidenceState.OPTIONAL_MISSING],
)
def test_nonfailure_states_have_no_failure_reason(evidence_type, state):
    assert reason_for_state(evidence_type, state) is None


def test_reasons_follow_obligation_order_not_state_order():
    obligations = (
        make_obligation("r1", EvidenceType.SCHEMA),
        make_obligation("r2", EvidenceType.PERMISSION),
    )
    states = (
        make_state("r2", EvidenceState.UNAUTHORIZED),
        make_state("r1", EvidenceState.MISSING_CRITICAL),
    )

    assert collect_reason_codes(obligations, states) == (
        ReasonCode.SCHEMA_MISSING_CRITICAL,
        ReasonCode.PERMISSION_DENIED,
    )


def test_duplicate_reason_codes_are_returned_once():
    obligations = (
        make_obligation("r1", EvidenceType.PERMISSION),
        make_obligation("r2", EvidenceType.PERMISSION),
    )
    states = (
        make_state("r1", EvidenceState.UNAUTHORIZED),
        make_state("r2", EvidenceState.UNAUTHORIZED),
    )

    assert collect_reason_codes(obligations, states) == (
        ReasonCode.PERMISSION_DENIED,
    )


def test_pass_does_not_automatically_approve_generation():
    obligations = (
        make_obligation("r1", EvidenceType.SCHEMA),
    )
    states = (
        make_state("r1", EvidenceState.PASS),
    )

    # Only the decision engine may issue generation approval.
    assert collect_reason_codes(obligations, states) == ()


def test_empty_collection_returns_no_reasons():
    assert collect_reason_codes((), ()) == ()


def test_missing_state_is_rejected():
    with pytest.raises(ValueError):
        collect_reason_codes(
            (make_obligation("r1", EvidenceType.SCHEMA),),
            (),
        )


def test_extra_state_is_rejected():
    with pytest.raises(ValueError):
        collect_reason_codes(
            (make_obligation("r1", EvidenceType.SCHEMA),),
            (
                make_state("r1", EvidenceState.PASS),
                make_state("unknown", EvidenceState.INVALID),
            ),
        )


def test_duplicate_states_are_rejected():
    with pytest.raises(ValueError):
        collect_reason_codes(
            (make_obligation("r1", EvidenceType.SCHEMA),),
            (
                make_state("r1", EvidenceState.PASS),
                make_state("r1", EvidenceState.INVALID),
            ),
        )


def test_duplicate_obligations_are_rejected():
    with pytest.raises(ValueError):
        collect_reason_codes(
            (
                make_obligation("r1", EvidenceType.SCHEMA),
                make_obligation("r1", EvidenceType.SCHEMA),
            ),
            (make_state("r1", EvidenceState.PASS),),
        )