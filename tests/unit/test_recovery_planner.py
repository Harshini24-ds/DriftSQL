"""Tests for approved EEVA recovery planning."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.clarification.planner import (
    RecoveryAction,
    RecoveryActionType,
    RecoveryPlan,
    build_recovery_plan,
)
from src.contracts.evidence import EvidenceType
from src.eeva.coverage import calculate_coverage
from src.eeva.decision import Decision, EEVAResult, RecoveryHint
from src.eeva.evidence_state import (
    HARD_FAILURE_STATES,
    RECOVERABLE_STATES,
    EvidenceState,
    ObligationState,
)
from src.eeva.reason_codes import collect_reason_codes
from src.eeva.validation import ValidationContext
from src.query_intelligence.obligations import (
    EXPECTED_SOURCE_TYPES,
    RECOVERY_ACTIONS,
    EvidenceObligation,
)


NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)

TARGETS = {
    EvidenceType.SCHEMA: "olist_raw.orders",
    EvidenceType.METADATA: "olist_raw.order_items.price",
    EvidenceType.GLOSSARY: "revenue",
    EvidenceType.DOCUMENT: "olist_raw",
    EvidenceType.RELATIONSHIP: (
        "olist_raw.order_items -> olist_raw.orders"
    ),
    EvidenceType.VALUE: "olist_raw.orders.order_status",
    EvidenceType.PERMISSION: "olist_raw.orders",
}


def make_case(
    kind=EvidenceType.METADATA,
    *,
    state=EvidenceState.MISSING_RECOVERABLE,
    action_names=None,
    available_actions=None,
    expected_sources=None,
    decision=Decision.CLARIFY,
    unresolved_terms=(),
):
    """Build controlled planner inputs without executing recovery."""
    if action_names is None:
        action_names = RECOVERY_ACTIONS[kind]

    if available_actions is None:
        available_actions = frozenset(action_names)

    if expected_sources is None:
        expected_sources = EXPECTED_SOURCE_TYPES[kind]

    obligation = EvidenceObligation(
        requirement_id="REQ-001",
        request_id="planner-request",
        database_id="olist_primary",
        evidence_type=kind,
        target=TARGETS[kind],
        mandatory=True,
        expected_source_types=expected_sources,
        constraint_type="approved_fact",
        recovery_actions=action_names,
        reason="Recover the required approved evidence.",
    )

    state_actions = (
        tuple(action_names)
        if state in RECOVERABLE_STATES
        else ()
    )

    evidence_state = ObligationState(
        requirement_id=obligation.requirement_id,
        state=state,
        recovery_actions=state_actions,
    )

    states = (evidence_state,)
    obligations = (obligation,)

    result = EEVAResult(
        decision=decision,
        snapshot_id="planner-snapshot",
        request_id="planner-request",
        database_id="olist_primary",
        evidence_states=states,
        coverage=calculate_coverage(obligations, states),
        hard_failures=(
            (obligation.requirement_id,)
            if state in HARD_FAILURE_STATES
            else ()
        ),
        conflicts=(),
        missing_items=(
            (obligation.requirement_id,)
            if state in {
                EvidenceState.MISSING_RECOVERABLE,
                EvidenceState.MISSING_CRITICAL,
            }
            else ()
        ),
        reason_codes=collect_reason_codes(obligations, states),
        recovery_actions=tuple(
            RecoveryHint(
                requirement_id=obligation.requirement_id,
                action=action,
            )
            for action in state_actions
        ),
        unresolved_terms=unresolved_terms,
    )

    context = ValidationContext(
        request_id="planner-request",
        database_id="olist_primary",
        database_role="driftsql_olist_reader",
        schema_hash="schema-v1",
        freshness_policy_version="planner-policy-v1",
        evaluated_at=NOW,
        sources=(),
        permission_decisions={
            obligation.target: True,
        },
        allowed_access_labels=frozenset({"internal"}),
        available_recovery_actions=frozenset(available_actions),
    )

    return result, obligations, context


def plan_case(
    result,
    obligations,
    context,
    *,
    attempt=1,
    timeout_seconds=3.0,
    attempted_actions=frozenset(),
):
    return build_recovery_plan(
        result,
        obligations,
        context,
        attempt=attempt,
        timeout_seconds=timeout_seconds,
        attempted_actions=attempted_actions,
    )


@pytest.mark.parametrize(
    "kind,expected_action,expected_sources",
    [
        (
            EvidenceType.SCHEMA,
            RecoveryActionType.REFRESH_SCHEMA,
            (
                "active_postgresql_catalog",
                "current_schema_snapshot",
            ),
        ),
        (
            EvidenceType.METADATA,
            RecoveryActionType.RETRIEVE_METADATA,
            ("approved_metadata_catalog",),
        ),
        (
            EvidenceType.GLOSSARY,
            RecoveryActionType.RETRIEVE_GLOSSARY,
            ("approved_business_glossary",),
        ),
        (
            EvidenceType.DOCUMENT,
            RecoveryActionType.RETRIEVE_DOCUMENTATION,
            ("approved_documentation",),
        ),
        (
            EvidenceType.RELATIONSHIP,
            RecoveryActionType.REFRESH_RELATIONSHIPS,
            (
                "live_pk_fk_catalog",
                "approved_relationship_evidence",
            ),
        ),
        (
            EvidenceType.VALUE,
            RecoveryActionType.PROBE_VALUES,
            ("bounded_read_only_value_probe",),
        ),
        (
            EvidenceType.PERMISSION,
            RecoveryActionType.REFRESH_PERMISSIONS,
            ("active_postgresql_role",),
        ),
    ],
)
def test_approved_automatic_action_mapping(
    kind,
    expected_action,
    expected_sources,
):
    result, obligations, context = make_case(kind)

    plan = plan_case(result, obligations, context)

    assert len(plan.actions) == 1
    assert plan.unresolved_requirement_ids == ()

    action = plan.actions[0]

    assert action.requirement_id == "REQ-001"
    assert action.type == expected_action
    assert action.target == TARGETS[kind]
    assert action.target_source_types == expected_sources
    assert action.timeout_seconds == 3.0
    assert action.attempt == 1
    assert action.status == "PENDING"
    assert action.intent_only is False
    assert plan.needs_user_input is False


def test_only_one_action_is_selected_per_obligation():
    result, obligations, context = make_case(EvidenceType.VALUE)

    assert obligations[0].recovery_actions == (
        "run_bounded_read_only_probe",
        "retrieve_approved_value_profile",
    )

    plan = plan_case(result, obligations, context)

    assert len(plan.actions) == 1
    assert plan.actions[0].type == RecoveryActionType.PROBE_VALUES


def test_previously_attempted_probe_uses_approved_profile_next():
    result, obligations, context = make_case(EvidenceType.VALUE)

    plan = plan_case(
        result,
        obligations,
        context,
        attempt=2,
        attempted_actions=frozenset({
            ("REQ-001", "run_bounded_read_only_probe"),
        }),
    )

    assert len(plan.actions) == 1
    assert plan.actions[0].type == (
        RecoveryActionType.RETRIEVE_VALUE_PROFILE
    )
    assert plan.actions[0].target_source_types == (
        "approved_value_profile",
    )
    assert plan.actions[0].attempt == 2


def test_exhausted_action_choices_are_reported_as_unresolved():
    result, obligations, context = make_case()

    plan = plan_case(
        result,
        obligations,
        context,
        attempt=2,
        attempted_actions=frozenset({
            ("REQ-001", "retrieve_approved_metadata"),
        }),
    )

    assert plan.actions == ()
    assert plan.unresolved_requirement_ids == ("REQ-001",)


@pytest.mark.parametrize(
    "decision",
    [Decision.GENERATE, Decision.REJECT],
)
def test_nonclarify_decisions_do_not_authorize_recovery(decision):
    result, obligations, context = make_case(decision=decision)

    plan = plan_case(result, obligations, context)

    assert plan.actions == ()
    assert plan.unresolved_requirement_ids == ()
    assert plan.unresolved_terms == ()


def test_action_requires_current_availability():
    result, obligations, context = make_case(
        available_actions=frozenset(),
    )

    plan = plan_case(result, obligations, context)

    assert plan.actions == ()
    assert plan.unresolved_requirement_ids == ("REQ-001",)


def test_action_requires_obligation_authorization():
    result, obligations, context = make_case()

    changed_obligation = obligations[0].model_copy(
        update={"recovery_actions": ()}
    )

    plan = plan_case(
        result,
        (changed_obligation,),
        context,
    )

    assert plan.actions == ()
    assert plan.unresolved_requirement_ids == ("REQ-001",)


def test_action_requires_state_authorization():
    result, obligations, context = make_case()

    changed_state = result.evidence_states[0].model_copy(
        update={"recovery_actions": ()}
    )
    result = result.model_copy(
        update={"evidence_states": (changed_state,)}
    )

    plan = plan_case(result, obligations, context)

    assert plan.actions == ()
    assert plan.unresolved_requirement_ids == ("REQ-001",)


def test_action_requires_an_eeva_recovery_hint():
    result, obligations, context = make_case()

    result = result.model_copy(
        update={"recovery_actions": ()}
    )

    plan = plan_case(result, obligations, context)

    assert plan.actions == ()
    assert plan.unresolved_requirement_ids == ("REQ-001",)


def test_unknown_action_name_cannot_be_planned():
    result, obligations, context = make_case(
        action_names=("run_arbitrary_command",),
    )

    plan = plan_case(result, obligations, context)

    assert plan.actions == ()
    assert plan.unresolved_requirement_ids == ("REQ-001",)


def test_action_must_match_evidence_type():
    result, obligations, context = make_case(
        EvidenceType.METADATA,
        action_names=("refresh_active_role_policy",),
    )

    plan = plan_case(result, obligations, context)

    assert plan.actions == ()
    assert plan.unresolved_requirement_ids == ("REQ-001",)


def test_unapproved_source_type_cannot_be_selected():
    result, obligations, context = make_case(
        expected_sources=("unapproved_web_source",),
    )

    plan = plan_case(result, obligations, context)

    assert plan.actions == ()
    assert plan.unresolved_requirement_ids == ("REQ-001",)


def test_business_clarification_is_intent_only():
    result, obligations, context = make_case(
        EvidenceType.GLOSSARY,
        state=EvidenceState.AMBIGUOUS_RECOVERABLE,
        action_names=("clarify_business_term",),
    )

    plan = plan_case(result, obligations, context)

    assert len(plan.actions) == 1

    action = plan.actions[0]

    assert action.type == (
        RecoveryActionType.CLARIFY_BUSINESS_TERM
    )
    assert action.intent_only is True
    assert action.target_source_types == ()
    assert action.timeout_seconds is None
    assert plan.needs_user_input is True


@pytest.mark.parametrize(
    "state",
    [
        EvidenceState.MISSING_RECOVERABLE,
        EvidenceState.STALE_RECOVERABLE,
        EvidenceState.CONFLICT_RECOVERABLE,
    ],
)
def test_user_cannot_replace_missing_or_conflicting_facts(state):
    result, obligations, context = make_case(
        EvidenceType.GLOSSARY,
        state=state,
        action_names=("clarify_business_term",),
    )

    plan = plan_case(result, obligations, context)

    assert plan.actions == ()
    assert plan.unresolved_requirement_ids == ("REQ-001",)


def test_glossary_conflict_uses_authoritative_retrieval():
    result, obligations, context = make_case(
        EvidenceType.GLOSSARY,
        state=EvidenceState.CONFLICT_RECOVERABLE,
    )

    plan = plan_case(result, obligations, context)

    assert len(plan.actions) == 1
    assert plan.actions[0].type == (
        RecoveryActionType.RETRIEVE_GLOSSARY
    )
    assert plan.actions[0].intent_only is False
    assert plan.actions[0].target_source_types == (
        "approved_business_glossary",
    )


def test_unresolved_intent_is_preserved_for_the_controller():
    result, obligations, context = make_case(
        state=EvidenceState.PASS,
        unresolved_terms=("revenue",),
    )

    plan = plan_case(result, obligations, context)

    assert plan.actions == ()
    assert plan.unresolved_requirement_ids == ()
    assert plan.unresolved_terms == ("revenue",)
    assert plan.needs_user_input is True


def test_changed_mandatory_permission_requires_eeva_again():
    result, obligations, context = make_case()

    context = context.model_copy(
        update={
            "permission_decisions": {
                obligations[0].target: False,
            }
        }
    )

    with pytest.raises(ValueError, match="Authorization changed"):
        plan_case(result, obligations, context)


def test_hard_failure_cannot_be_recovered_as_clarification():
    result, obligations, context = make_case(
        state=EvidenceState.UNAUTHORIZED,
        decision=Decision.CLARIFY,
    )

    with pytest.raises(ValueError, match="hard failures"):
        plan_case(result, obligations, context)


def test_cross_request_context_is_rejected():
    result, obligations, context = make_case()

    context = context.model_copy(
        update={"request_id": "another-request"}
    )

    with pytest.raises(ValueError, match="same request"):
        plan_case(result, obligations, context)


def test_cross_request_obligation_is_rejected():
    result, obligations, context = make_case()

    obligation = obligations[0].model_copy(
        update={"request_id": "another-request"}
    )

    with pytest.raises(ValueError, match="must belong"):
        plan_case(result, (obligation,), context)


def test_duplicate_obligations_are_rejected():
    result, obligations, context = make_case()

    with pytest.raises(ValueError, match="Duplicate obligation"):
        plan_case(
            result,
            (obligations[0], obligations[0]),
            context,
        )


def test_duplicate_states_are_rejected():
    result, obligations, context = make_case()

    result = result.model_copy(
        update={
            "evidence_states": (
                result.evidence_states[0],
                result.evidence_states[0],
            )
        }
    )

    with pytest.raises(ValueError, match="Duplicate state"):
        plan_case(result, obligations, context)


def test_missing_state_is_rejected():
    result, obligations, context = make_case()

    result = result.model_copy(
        update={"evidence_states": ()}
    )

    with pytest.raises(ValueError, match="Exactly one state"):
        plan_case(result, obligations, context)


def test_unknown_recovery_hint_requirement_is_rejected():
    result, obligations, context = make_case()

    result = result.model_copy(
        update={
            "recovery_actions": (
                RecoveryHint(
                    requirement_id="UNKNOWN-REQ",
                    action="retrieve_approved_metadata",
                ),
            )
        }
    )

    with pytest.raises(ValueError, match="unknown requirement"):
        plan_case(result, obligations, context)


@pytest.mark.parametrize(
    "attempt",
    [0, 3, -1, True, False, 1.5, "1"],
)
def test_invalid_attempt_is_rejected(attempt):
    result, obligations, context = make_case()

    with pytest.raises(ValueError, match="attempt must be"):
        plan_case(
            result,
            obligations,
            context,
            attempt=attempt,
        )


@pytest.mark.parametrize(
    "timeout",
    [
        0,
        -1,
        float("inf"),
        float("nan"),
        True,
        "three",
    ],
)
def test_invalid_timeout_is_rejected(timeout):
    result, obligations, context = make_case()

    with pytest.raises(ValueError, match="timeout must be"):
        plan_case(
            result,
            obligations,
            context,
            timeout_seconds=timeout,
        )


def test_action_identity_is_stable_for_identical_inputs():
    result, obligations, context = make_case()

    first = plan_case(result, obligations, context)
    second = plan_case(result, obligations, context)

    assert first.actions[0].action_id == second.actions[0].action_id


def test_different_attempt_gets_different_action_identity():
    result, obligations, context = make_case()

    first = plan_case(
        result, obligations, context, attempt=1
    )
    second = plan_case(
        result, obligations, context, attempt=2
    )

    assert first.actions[0].action_id != second.actions[0].action_id


def test_plan_preserves_request_and_role_identity():
    result, obligations, context = make_case()

    plan = plan_case(result, obligations, context)

    assert plan.request_id == result.request_id
    assert plan.database_id == result.database_id
    assert plan.snapshot_id == result.snapshot_id
    assert plan.database_role == context.database_role


def test_plan_json_round_trip():
    result, obligations, context = make_case()

    original = plan_case(result, obligations, context)

    restored = RecoveryPlan.model_validate_json(
        original.model_dump_json()
    )

    assert restored == original


def test_automatic_action_requires_timeout():
    result, obligations, context = make_case()
    action = plan_case(result, obligations, context).actions[0]

    data = action.model_dump()
    data["timeout_seconds"] = None

    with pytest.raises(ValidationError, match="requires"):
        RecoveryAction.model_validate(data)


def test_automatic_action_requires_approved_sources():
    result, obligations, context = make_case()
    action = plan_case(result, obligations, context).actions[0]

    data = action.model_dump()
    data["target_source_types"] = ()

    with pytest.raises(ValidationError, match="requires"):
        RecoveryAction.model_validate(data)


def test_user_clarification_cannot_be_authoritative_source():
    result, obligations, context = make_case(
        EvidenceType.GLOSSARY,
        state=EvidenceState.AMBIGUOUS_RECOVERABLE,
        action_names=("clarify_business_term",),
    )

    action = plan_case(result, obligations, context).actions[0]

    data = action.model_dump()
    data["target_source_types"] = (
        "approved_business_glossary",
    )

    with pytest.raises(ValidationError, match="authoritative source"):
        RecoveryAction.model_validate(data)