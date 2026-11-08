"""Tests for bounded clarification and evidence revalidation."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

import pytest
from pydantic import ValidationError

from src.clarification.controller import (
    ClarificationSession,
    RecoveryUpdate,
    advance_clarification,
    start_clarification,
)
from src.clarification.planner import RecoveryActionType
from src.contracts.evidence import (
    EvidenceItem,
    EvidenceSnapshot,
    EvidenceType,
)
from src.eeva.decision import Decision
from src.eeva.reason_codes import ReasonCode
from src.eeva.validation import SourceReference, ValidationContext
from src.query_intelligence.models import (
    AccessContext,
    QueryFilter,
    QueryIntent,
)
from src.query_intelligence.obligations import EXPECTED_SOURCE_TYPES
from src.query_intelligence.pipeline import analyze_query_intent


NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)
ROLE = "driftsql_olist_reader"
TABLE = "test.orders"
AMOUNT = "test.orders.amount"
STATUS = "test.orders.status"


def content_hash(value):
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def make_inputs(
    *,
    with_measure=False,
    with_filter=False,
    unresolved_terms=(),
):
    """Build real QIM obligations and controlled approved evidence."""
    filters = (
        (
            QueryFilter(
                field=STATUS,
                operator="=",
                value="delivered",
            ),
        )
        if with_filter
        else ()
    )

    intent = QueryIntent(
        request_id="controller-request",
        database_id="olist_primary",
        operation="aggregate",
        entities=(TABLE,),
        measures=(AMOUNT,) if with_measure else ("count",),
        aggregation="sum" if with_measure else "count",
        filters=filters,
        unresolved_terms=unresolved_terms,
    )

    analysis = analyze_query_intent(
        intent,
        access_context=AccessContext(
            principal_id="controller-user",
            database_role=ROLE,
        ),
    )

    items = []
    references = []

    for index, obligation in enumerate(
        analysis.required_evidence_obligations,
        start=1,
    ):
        kind = obligation.evidence_type

        if kind == EvidenceType.SCHEMA:
            value = {
                "target": obligation.target,
                "kind": (
                    "column"
                    if len(obligation.target.split(".")) == 3
                    else "table"
                ),
            }
            version = "schema-v1"
            source_type = EXPECTED_SOURCE_TYPES[kind][0]

        elif kind == EvidenceType.PERMISSION:
            value = {
                "target": obligation.target,
                "database_role": ROLE,
                "decision": "ALLOW",
                "check_source": "postgresql_privilege_check",
            }
            version = "permissions-v1"
            source_type = "active_postgresql_role"

        elif kind == EvidenceType.METADATA:
            value = {
                "target": obligation.target,
                "description": "Approved amount description.",
                "unit": "BRL",
                "aliases": [],
                "business_terms": [],
            }
            version = "metadata-v1"
            source_type = "approved_metadata_catalog"

        elif kind == EvidenceType.VALUE:
            value = {
                "target": obligation.target,
                "coverage": "COMPLETE",
                "values": ["delivered", "shipped"],
            }
            version = "values-v1"
            source_type = "approved_value_profile"

        else:
            raise AssertionError(
                f"Unexpected fixture evidence type: {kind}"
            )

        source = f"fixture-source-{index}"
        provenance_id = f"PROV-{index}"
        digest = content_hash(value)

        item = EvidenceItem(
            evidence_id=f"EV-{index}",
            database_id="olist_primary",
            type=kind,
            key=obligation.target,
            value=value,
            source=source,
            source_version=version,
            retrieved_at=NOW,
            authority_level=100,
            access_label="internal",
            scope=obligation.target,
            provenance_id=provenance_id,
            content_hash=digest,
        )

        reference = SourceReference(
            source=source,
            provenance_id=provenance_id,
            evidence_type=kind,
            key=obligation.target,
            source_type=source_type,
            current_version=version,
            current_content_hash=digest,
            scope=obligation.target,
            access_label="internal",
            authority_level=100,
            schema_hash=(
                None
                if kind == EvidenceType.PERMISSION
                else "schema-v1"
            ),
        )

        items.append(item)
        references.append(reference)

    snapshot = EvidenceSnapshot(
        snapshot_id="initial-snapshot",
        request_id="controller-request",
        database_id="olist_primary",
        evidence_items=(),
        schema_hash="schema-v1",
        freshness_policy_version="controller-policy-v1",
        created_at=NOW,
    )

    context = ValidationContext(
        request_id="controller-request",
        database_id="olist_primary",
        database_role=ROLE,
        schema_hash="schema-v1",
        freshness_policy_version="controller-policy-v1",
        evaluated_at=NOW,
        sources=tuple(references),
        permission_decisions={
            obligation.target: True
            for obligation in analysis.required_evidence_obligations
        },
        allowed_access_labels=frozenset({"internal"}),
        available_recovery_actions=frozenset(
            action
            for obligation in analysis.required_evidence_obligations
            for action in obligation.recovery_actions
        ),
    )

    return analysis, snapshot, context, tuple(items)


def snapshot_with(snapshot, items, snapshot_id="updated-snapshot"):
    return snapshot.model_copy(
        update={
            "snapshot_id": snapshot_id,
            "evidence_items": tuple(items),
        }
    )


def start(analysis, snapshot, context):
    return start_clarification(
        analysis,
        snapshot,
        context,
        timeout_seconds=3.0,
    )


def item_for(items, kind, target=None):
    return next(
        item
        for item in items
        if item.type == kind
        and (target is None or item.key == target)
    )


def action_for(session, action_type, target=None):
    assert session.pending_plan is not None

    return next(
        action
        for action in session.pending_plan.actions
        if action.type == action_type
        and (target is None or action.target == target)
    )


def test_complete_initial_evidence_generates_without_recovery():
    analysis, snapshot, context, items = make_inputs()

    session = start(
        analysis,
        snapshot_with(snapshot, items),
        context,
    )

    assert session.decision == Decision.GENERATE
    assert session.completed_cycles == 0
    assert session.pending_plan is None
    assert session.result.validated_evidence_bundle is not None


def test_missing_evidence_creates_pending_recovery():
    analysis, snapshot, context, _ = make_inputs()

    session = start(analysis, snapshot, context)

    assert session.decision == Decision.CLARIFY
    assert session.completed_cycles == 0
    assert session.pending_plan is not None
    assert session.pending_plan.attempt == 1
    assert {
        action.type
        for action in session.pending_plan.actions
    } == {
        RecoveryActionType.REFRESH_SCHEMA,
        RecoveryActionType.REFRESH_PERMISSIONS,
    }


def test_approved_recovery_reenters_eeva_and_generates():
    analysis, snapshot, context, items = make_inputs()

    permission = item_for(items, EvidenceType.PERMISSION)
    initial = snapshot_with(snapshot, (permission,))

    session = start(analysis, initial, context)

    action = action_for(
        session,
        RecoveryActionType.REFRESH_SCHEMA,
    )

    recovered = advance_clarification(
        session,
        RecoveryUpdate(
            snapshot=snapshot_with(snapshot, items),
            context=context,
            attempted_action_ids=(action.action_id,),
        ),
    )

    assert recovered.decision == Decision.GENERATE
    assert recovered.completed_cycles == 1
    assert recovered.pending_plan is None
    assert recovered.result.coverage.mandatory_coverage == 1.0
    assert recovered.result.validated_evidence_bundle is not None

    # The original state remains unchanged.
    assert session.decision == Decision.CLARIFY
    assert session.completed_cycles == 0


def test_two_progress_cycles_can_generate():
    analysis, snapshot, context, items = make_inputs()

    session = start(analysis, snapshot, context)

    schema = item_for(items, EvidenceType.SCHEMA)
    schema_action = action_for(
        session,
        RecoveryActionType.REFRESH_SCHEMA,
    )

    first = advance_clarification(
        session,
        RecoveryUpdate(
            snapshot=snapshot_with(snapshot, (schema,), "first"),
            context=context,
            attempted_action_ids=(schema_action.action_id,),
        ),
    )

    assert first.decision == Decision.CLARIFY
    assert first.completed_cycles == 1
    assert first.pending_plan.attempt == 2

    permission_action = action_for(
        first,
        RecoveryActionType.REFRESH_PERMISSIONS,
    )

    second = advance_clarification(
        first,
        RecoveryUpdate(
            snapshot=snapshot_with(snapshot, items, "second"),
            context=context,
            attempted_action_ids=(permission_action.action_id,),
        ),
    )

    assert second.decision == Decision.GENERATE
    assert second.completed_cycles == 2
    assert second.pending_plan is None


def test_unresolved_evidence_after_two_progress_cycles_rejects():
    analysis, snapshot, context, items = make_inputs(
        with_measure=True,
    )

    permissions = tuple(
        item
        for item in items
        if item.type == EvidenceType.PERMISSION
    )

    session = start(
        analysis,
        snapshot_with(snapshot, permissions),
        context,
    )

    table_schema = item_for(
        items,
        EvidenceType.SCHEMA,
        TABLE,
    )

    first_action = action_for(
        session,
        RecoveryActionType.REFRESH_SCHEMA,
        TABLE,
    )

    first_items = (*permissions, table_schema)

    first = advance_clarification(
        session,
        RecoveryUpdate(
            snapshot=snapshot_with(snapshot, first_items, "first"),
            context=context,
            attempted_action_ids=(first_action.action_id,),
        ),
    )

    assert first.decision == Decision.CLARIFY
    assert first.completed_cycles == 1

    column_schema = item_for(
        items,
        EvidenceType.SCHEMA,
        AMOUNT,
    )

    second_action = action_for(
        first,
        RecoveryActionType.REFRESH_SCHEMA,
        AMOUNT,
    )

    second = advance_clarification(
        first,
        RecoveryUpdate(
            snapshot=snapshot_with(
                snapshot,
                (*first_items, column_schema),
                "second",
            ),
            context=context,
            attempted_action_ids=(second_action.action_id,),
        ),
    )

    assert second.completed_cycles == 2
    assert second.decision == Decision.REJECT
    assert second.pending_plan is None
    assert ReasonCode.RECOVERY_EXHAUSTED in second.result.reason_codes
    assert (
        ReasonCode.RECOVERY_NO_PROGRESS
        not in second.result.reason_codes
    )
    assert second.result.validated_evidence_bundle is None


def test_unchanged_evidence_without_an_alternative_rejects():
    analysis, snapshot, context, items = make_inputs()

    permission = item_for(items, EvidenceType.PERMISSION)
    initial = snapshot_with(snapshot, (permission,))

    session = start(analysis, initial, context)

    action = action_for(
        session,
        RecoveryActionType.REFRESH_SCHEMA,
    )

    result = advance_clarification(
        session,
        RecoveryUpdate(
            snapshot=initial,
            context=context,
            attempted_action_ids=(action.action_id,),
        ),
    )

    assert result.decision == Decision.REJECT
    assert result.completed_cycles == 1
    assert ReasonCode.RECOVERY_NO_PROGRESS in result.result.reason_codes
    assert ReasonCode.RECOVERY_EXHAUSTED in result.result.reason_codes
    assert result.result.validated_evidence_bundle is None


def test_new_ids_timestamps_and_diagnostics_are_not_progress():
    analysis, snapshot, context, items = make_inputs()

    permission = item_for(items, EvidenceType.PERMISSION)
    initial = snapshot_with(snapshot, (permission,))

    session = start(analysis, initial, context)

    action = action_for(
        session,
        RecoveryActionType.REFRESH_SCHEMA,
    )

    changed_permission = permission.model_copy(
        update={
            "evidence_id": "FRESH-RANDOM-ID",
            "retrieved_at": NOW + timedelta(minutes=1),
        }
    )

    changed_snapshot = initial.model_copy(
        update={
            "snapshot_id": "FRESH-SNAPSHOT-ID",
            "created_at": NOW + timedelta(minutes=1),
            "retrieval_metadata": {"attempt": "new diagnostic"},
            "evidence_items": (changed_permission,),
        }
    )

    changed_context = context.model_copy(
        update={
            "evaluated_at": NOW + timedelta(minutes=1),
        }
    )

    result = advance_clarification(
        session,
        RecoveryUpdate(
            snapshot=changed_snapshot,
            context=changed_context,
            attempted_action_ids=(action.action_id,),
        ),
    )

    assert (
        result.material_fingerprint
        == session.material_fingerprint
    )
    assert result.decision == Decision.REJECT
    assert ReasonCode.RECOVERY_NO_PROGRESS in result.result.reason_codes


def test_approved_alternative_can_be_tried_within_same_cycle():
    analysis, snapshot, context, items = make_inputs(
        with_filter=True,
    )

    initial_items = tuple(
        item
        for item in items
        if item.type != EvidenceType.VALUE
    )

    initial = snapshot_with(snapshot, initial_items)

    session = start(analysis, initial, context)

    probe = action_for(
        session,
        RecoveryActionType.PROBE_VALUES,
    )

    # A failed probe adds no evidence. An approved profile remains.
    after_probe = advance_clarification(
        session,
        RecoveryUpdate(
            snapshot=initial,
            context=context,
            attempted_action_ids=(probe.action_id,),
        ),
    )

    assert after_probe.decision == Decision.CLARIFY
    assert after_probe.completed_cycles == 0

    profile = action_for(
        after_probe,
        RecoveryActionType.RETRIEVE_VALUE_PROFILE,
    )

    assert profile.attempt == 1

    recovered = advance_clarification(
        after_probe,
        RecoveryUpdate(
            snapshot=snapshot_with(snapshot, items),
            context=context,
            attempted_action_ids=(profile.action_id,),
        ),
    )

    assert recovered.decision == Decision.GENERATE
    assert recovered.completed_cycles == 1


def test_same_action_cannot_repeat_within_unchanged_cycle():
    analysis, snapshot, context, _ = make_inputs()

    session = start(analysis, snapshot, context)

    schema_action = action_for(
        session,
        RecoveryActionType.REFRESH_SCHEMA,
    )

    first = advance_clarification(
        session,
        RecoveryUpdate(
            snapshot=snapshot,
            context=context,
            attempted_action_ids=(schema_action.action_id,),
        ),
    )

    assert first.decision == Decision.CLARIFY
    assert first.completed_cycles == 0

    with pytest.raises(ValueError, match="unplanned action"):
        advance_clarification(
            first,
            RecoveryUpdate(
                snapshot=snapshot,
                context=context,
                attempted_action_ids=(schema_action.action_id,),
            ),
        )


def test_returning_to_a_previous_material_state_rejects():
    analysis, snapshot, context, items = make_inputs()

    session = start(analysis, snapshot, context)

    schema_action = action_for(
        session,
        RecoveryActionType.REFRESH_SCHEMA,
    )

    first = advance_clarification(
        session,
        RecoveryUpdate(
            snapshot=snapshot_with(
                snapshot,
                (item_for(items, EvidenceType.SCHEMA),),
                "first",
            ),
            context=context,
            attempted_action_ids=(schema_action.action_id,),
        ),
    )

    permission_action = action_for(
        first,
        RecoveryActionType.REFRESH_PERMISSIONS,
    )

    # The executor loses the previously recovered schema proof.
    repeated = advance_clarification(
        first,
        RecoveryUpdate(
            snapshot=snapshot_with(snapshot, (), "repeated"),
            context=context,
            attempted_action_ids=(permission_action.action_id,),
        ),
    )

    assert repeated.decision == Decision.REJECT
    assert repeated.completed_cycles == 2
    assert ReasonCode.RECOVERY_NO_PROGRESS in repeated.result.reason_codes
    assert ReasonCode.RECOVERY_EXHAUSTED in repeated.result.reason_codes


def test_user_clarification_can_resolve_intent_then_reenter_eeva():
    analysis, snapshot, context, items = make_inputs(
        unresolved_terms=("meaning_to_clarify",),
    )

    complete = snapshot_with(snapshot, items)
    session = start(analysis, complete, context)

    assert session.decision == Decision.CLARIFY
    assert session.awaiting_user_input is True

    clarified = analysis.intent.model_copy(
        update={"unresolved_terms": ()}
    )

    result = advance_clarification(
        session,
        RecoveryUpdate(
            snapshot=complete,
            context=context,
            clarified_intent=clarified,
        ),
    )

    assert result.decision == Decision.GENERATE
    assert result.completed_cycles == 1
    assert result.awaiting_user_input is False
    assert result.result.validated_evidence_bundle is not None


def test_identical_unhelpful_clarification_is_not_progress():
    analysis, snapshot, context, items = make_inputs(
        unresolved_terms=("meaning_to_clarify",),
    )

    complete = snapshot_with(snapshot, items)
    session = start(analysis, complete, context)

    result = advance_clarification(
        session,
        RecoveryUpdate(
            snapshot=complete,
            context=context,
            clarified_intent=analysis.intent,
        ),
    )

    assert result.decision == Decision.REJECT
    assert ReasonCode.RECOVERY_NO_PROGRESS in result.result.reason_codes
    assert ReasonCode.RECOVERY_EXHAUSTED in result.result.reason_codes


def test_user_clarification_cannot_override_mandatory_denial():
    analysis, snapshot, context, items = make_inputs(
        unresolved_terms=("meaning_to_clarify",),
    )

    complete = snapshot_with(snapshot, items)
    session = start(analysis, complete, context)

    denied_context = context.model_copy(
        update={"permission_decisions": {TABLE: False}}
    )

    clarified = analysis.intent.model_copy(
        update={"unresolved_terms": ()}
    )

    result = advance_clarification(
        session,
        RecoveryUpdate(
            snapshot=complete,
            context=denied_context,
            clarified_intent=clarified,
        ),
    )

    assert result.decision == Decision.REJECT
    assert ReasonCode.PERMISSION_DENIED in result.result.reason_codes
    assert result.result.validated_evidence_bundle is None


def test_clarified_intent_cannot_change_request_identity():
    analysis, snapshot, context, items = make_inputs(
        unresolved_terms=("meaning_to_clarify",),
    )

    complete = snapshot_with(snapshot, items)
    session = start(analysis, complete, context)

    changed_intent = analysis.intent.model_copy(
        update={"request_id": "another-request"}
    )

    with pytest.raises(ValueError, match="request or database"):
        advance_clarification(
            session,
            RecoveryUpdate(
                snapshot=complete,
                context=context,
                clarified_intent=changed_intent,
            ),
        )


def test_recovery_cannot_change_role():
    analysis, snapshot, context, _ = make_inputs()
    session = start(analysis, snapshot, context)

    action = session.pending_plan.actions[0]

    changed_context = context.model_copy(
        update={"database_role": "driftsql_orders_limited"}
    )

    with pytest.raises(ValueError, match="active role"):
        advance_clarification(
            session,
            RecoveryUpdate(
                snapshot=snapshot,
                context=changed_context,
                attempted_action_ids=(action.action_id,),
            ),
        )


def test_recovery_cannot_change_frozen_freshness_policy():
    analysis, snapshot, context, _ = make_inputs()
    session = start(analysis, snapshot, context)

    action = session.pending_plan.actions[0]

    changed_context = context.model_copy(
        update={"freshness_policy_version": "another-policy"}
    )

    changed_snapshot = snapshot.model_copy(
        update={"freshness_policy_version": "another-policy"}
    )

    with pytest.raises(ValueError, match="freshness policy"):
        advance_clarification(
            session,
            RecoveryUpdate(
                snapshot=changed_snapshot,
                context=changed_context,
                attempted_action_ids=(action.action_id,),
            ),
        )


def test_unknown_action_id_is_rejected():
    analysis, snapshot, context, _ = make_inputs()
    session = start(analysis, snapshot, context)

    with pytest.raises(ValueError, match="unplanned action"):
        advance_clarification(
            session,
            RecoveryUpdate(
                snapshot=snapshot,
                context=context,
                attempted_action_ids=("UNKNOWN-ACTION",),
            ),
        )


def test_duplicate_action_ids_are_rejected():
    analysis, snapshot, context, _ = make_inputs()
    session = start(analysis, snapshot, context)

    action_id = session.pending_plan.actions[0].action_id

    with pytest.raises(ValueError, match="Duplicate attempted"):
        advance_clarification(
            session,
            RecoveryUpdate(
                snapshot=snapshot,
                context=context,
                attempted_action_ids=(action_id, action_id),
            ),
        )


def test_empty_recovery_update_is_rejected():
    analysis, snapshot, context, _ = make_inputs()
    session = start(analysis, snapshot, context)

    with pytest.raises(ValueError, match="attempted action"):
        advance_clarification(
            session,
            RecoveryUpdate(
                snapshot=snapshot,
                context=context,
            ),
        )


def test_user_intent_change_requires_an_authorized_question():
    analysis, snapshot, context, _ = make_inputs()
    session = start(analysis, snapshot, context)

    assert session.awaiting_user_input is False

    with pytest.raises(ValueError, match="does not authorize"):
        advance_clarification(
            session,
            RecoveryUpdate(
                snapshot=snapshot,
                context=context,
                clarified_intent=analysis.intent,
            ),
        )


def test_terminal_session_cannot_accept_another_recovery():
    analysis, snapshot, context, items = make_inputs()

    complete = snapshot_with(snapshot, items)
    session = start(analysis, complete, context)

    with pytest.raises(ValueError, match="no pending"):
        advance_clarification(
            session,
            RecoveryUpdate(
                snapshot=complete,
                context=context,
                attempted_action_ids=("ANY-ACTION",),
            ),
        )


def test_unsupported_clarification_fails_qim_precheck():
    analysis, snapshot, context, items = make_inputs(
        unresolved_terms=("meaning_to_clarify",),
    )

    complete = snapshot_with(snapshot, items)
    session = start(analysis, complete, context)

    unsupported = analysis.intent.model_copy(
        update={"operation": "delete"}
    )

    with pytest.raises(ValueError, match="QIM_PRECHECK"):
        advance_clarification(
            session,
            RecoveryUpdate(
                snapshot=complete,
                context=context,
                clarified_intent=unsupported,
            ),
        )

    assert session.decision == Decision.CLARIFY
    assert session.completed_cycles == 0


def test_cross_request_recovery_context_is_rejected():
    analysis, snapshot, context, _ = make_inputs()
    session = start(analysis, snapshot, context)

    action_id = session.pending_plan.actions[0].action_id

    wrong_context = context.model_copy(
        update={"request_id": "another-request"}
    )

    with pytest.raises(ValueError, match="share request"):
        advance_clarification(
            session,
            RecoveryUpdate(
                snapshot=snapshot,
                context=wrong_context,
                attempted_action_ids=(action_id,),
            ),
        )


def test_raw_clarified_intent_cannot_contain_permission_overrides():
    analysis, _, _, _ = make_inputs()

    data = analysis.intent.model_dump()
    data["permission_decisions"] = {TABLE: True}

    with pytest.raises(ValidationError):
        QueryIntent.model_validate(data)


def test_start_copies_mutable_input_data():
    analysis, snapshot, context, items = make_inputs()

    complete = snapshot_with(snapshot, items)
    session = start(analysis, complete, context)

    schema = item_for(
        complete.evidence_items,
        EvidenceType.SCHEMA,
    )

    schema.value["kind"] = "tampered"
    context.permission_decisions[TABLE] = False

    stored_schema = item_for(
        session.snapshot.evidence_items,
        EvidenceType.SCHEMA,
    )

    assert stored_schema.value["kind"] == "table"
    assert session.context.permission_decisions[TABLE] is True
    assert session.decision == Decision.GENERATE


def test_session_can_be_serialized_and_restored():
    analysis, snapshot, context, _ = make_inputs()

    original = start(analysis, snapshot, context)

    restored = ClarificationSession.model_validate_json(
        original.model_dump_json()
    )

    assert restored == original
    assert restored.pending_plan is not None