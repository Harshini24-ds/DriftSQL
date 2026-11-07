"""Tests for the EEVA pre-generation decision engine."""

from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

from src.contracts.evidence import (
    EvidenceItem,
    EvidenceSnapshot,
    EvidenceType,
)
from src.eeva.decision import (
    Decision,
    EEVAResult,
    evaluate_evidence,
)
from src.eeva.evidence_state import EvidenceState
from src.eeva.reason_codes import ReasonCode
from src.eeva.validation import (
    ConflictAssessment,
    SourceReference,
    ValidationContext,
    snapshot_fingerprint,
)
from src.query_intelligence.models import AccessContext, QueryIntent
from src.query_intelligence.obligations import EvidenceObligation
from src.query_intelligence.pipeline import analyze_query_intent


NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)
ROLE = "driftsql_olist_reader"


def content_hash(value):
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def make_case(*, optional=False):
    """Use real QIM analysis with controlled authoritative fixtures."""
    analysis = analyze_query_intent(
        QueryIntent(
            request_id="decision-request",
            database_id="test_database",
            operation="aggregate",
            entities=("test.orders",),
            measures=("count",),
        ),
        access_context=AccessContext(
            principal_id="test-user",
            database_role=ROLE,
        ),
    )

    if optional:
        optional_obligation = EvidenceObligation(
            requirement_id="OPTIONAL-001",
            request_id="decision-request",
            database_id="test_database",
            evidence_type=EvidenceType.METADATA,
            target="test.notes",
            mandatory=False,
            expected_source_types=("approved_metadata_catalog",),
            constraint_type="field_description",
            recovery_actions=(),
            diagnostic_weight=1000.0,
            reason="Optional supporting description.",
        )

        analysis = analysis.model_copy(
            update={
                "required_evidence_obligations": (
                    *analysis.required_evidence_obligations,
                    optional_obligation,
                )
            }
        )

    items = []
    references = []

    for index, obligation in enumerate(
        analysis.required_evidence_obligations,
        start=1,
    ):
        if obligation.evidence_type == EvidenceType.PERMISSION:
            value = {
                "target": obligation.target,
                "database_role": ROLE,
                "decision": "ALLOW",
            }
        elif obligation.evidence_type == EvidenceType.SCHEMA:
            value = {
                "target": obligation.target,
                "kind": "table",
            }
        else:
            value = {
                "target": obligation.target,
                "description": "Approved supporting description.",
            }

        version = (
            "schema-v1"
            if obligation.evidence_type == EvidenceType.SCHEMA
            else "source-v1"
        )
        digest = content_hash(value)
        provenance_id = f"PROV-{index}"

        items.append(
            EvidenceItem(
                evidence_id=f"EV-{index}",
                database_id="test_database",
                type=obligation.evidence_type,
                key=obligation.target,
                value=value,
                source="approved_test_repository",
                source_version=version,
                retrieved_at=NOW,
                authority_level=100,
                access_label="internal",
                scope=obligation.target,
                provenance_id=provenance_id,
                content_hash=digest,
            )
        )

        references.append(
            SourceReference(
                source="approved_test_repository",
                provenance_id=provenance_id,
                evidence_type=obligation.evidence_type,
                key=obligation.target,
                source_type=obligation.expected_source_types[0],
                current_version=version,
                current_content_hash=digest,
                scope=obligation.target,
                access_label="internal",
                authority_level=100,
                schema_hash=(
                    None
                    if obligation.evidence_type
                    == EvidenceType.PERMISSION
                    else "schema-v1"
                ),
            )
        )

    snapshot = EvidenceSnapshot(
        snapshot_id="decision-snapshot",
        request_id="decision-request",
        database_id="test_database",
        evidence_items=tuple(items),
        schema_hash="schema-v1",
        retrieval_metadata={"diagnostic_note": "not generator context"},
        freshness_policy_version="freshness-v1",
        created_at=NOW,
    )

    context = ValidationContext(
        request_id="decision-request",
        database_id="test_database",
        database_role=ROLE,
        schema_hash="schema-v1",
        freshness_policy_version="freshness-v1",
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

    return analysis, snapshot, context


def make_reviews(analysis, snapshot, overrides=None):
    """Simulate conflict-detector output for the simple fixtures."""
    overrides = {} if overrides is None else overrides
    digest = snapshot_fingerprint(snapshot)

    return tuple(
        ConflictAssessment(
            snapshot_id=snapshot.snapshot_id,
            snapshot_digest=digest,
            requirement_id=obligation.requirement_id,
            evidence_item_ids=tuple(
                item.evidence_id
                for item in snapshot.evidence_items
                if item.type == obligation.evidence_type
                and item.key == obligation.target
            ),
            state=overrides.get(
                obligation.requirement_id,
                EvidenceState.PASS,
            ),
        )
        for obligation in analysis.required_evidence_obligations
    )


def evaluate_case(analysis, snapshot, context, *, overrides=None):
    return evaluate_evidence(
        analysis,
        snapshot,
        context,
        conflict_assessments=make_reviews(
            analysis,
            snapshot,
            overrides,
        ),
    )


def schema_requirement_id(analysis):
    return next(
        obligation.requirement_id
        for obligation in analysis.required_evidence_obligations
        if obligation.evidence_type == EvidenceType.SCHEMA
    )


def test_all_mandatory_evidence_passes_and_creates_bundle():
    analysis, snapshot, context = make_case()

    result = evaluate_case(analysis, snapshot, context)

    assert result.decision == Decision.GENERATE
    assert result.decision_stage == "EEVA"
    assert result.coverage.mandatory_coverage == 1.0
    assert result.coverage.mandatory_total == 2
    assert result.coverage.mandatory_passed == 2
    assert result.hard_failures == ()
    assert result.recovery_actions == ()
    assert result.reason_codes == (
        ReasonCode.ALL_MANDATORY_EVIDENCE_PASS,
    )

    bundle = result.validated_evidence_bundle

    assert bundle is not None
    assert result.validated_evidence_bundle_id == bundle.bundle_id
    assert bundle.principal_id == "test-user"
    assert bundle.database_role == ROLE
    assert bundle.request_id == snapshot.request_id
    assert bundle.evidence_snapshot().evidence_item_ids == (
        "EV-1",
        "EV-2",
    )


@pytest.mark.parametrize(
    "failure,expected_state,expected_reason",
    [
        (
            "permission",
            EvidenceState.UNAUTHORIZED,
            ReasonCode.PERMISSION_DENIED,
        ),
        (
            "missing",
            EvidenceState.MISSING_CRITICAL,
            ReasonCode.SCHEMA_MISSING_CRITICAL,
        ),
        (
            "integrity",
            EvidenceState.INVALID,
            ReasonCode.PROVENANCE_INVALID,
        ),
        (
            "conflict",
            EvidenceState.CONFLICT_CRITICAL,
            ReasonCode.EVIDENCE_CONFLICT_CRITICAL,
        ),
    ],
)
def test_hard_failures_reject_without_bundle(
    failure,
    expected_state,
    expected_reason,
):
    analysis, snapshot, context = make_case()
    requirement_id = schema_requirement_id(analysis)
    overrides = {}

    if failure == "permission":
        context = context.model_copy(
            update={"permission_decisions": {"test.orders": False}}
        )

    elif failure == "missing":
        snapshot = snapshot.model_copy(
            update={
                "evidence_items": tuple(
                    item
                    for item in snapshot.evidence_items
                    if item.type != EvidenceType.SCHEMA
                )
            }
        )
        context = context.model_copy(
            update={"available_recovery_actions": frozenset()}
        )

    elif failure == "integrity":
        snapshot = snapshot.model_copy(
            update={
                "evidence_items": tuple(
                    item.model_copy(
                        update={"content_hash": "0" * 64}
                    )
                    if item.type == EvidenceType.SCHEMA
                    else item
                    for item in snapshot.evidence_items
                )
            }
        )
        context = context.model_copy(
            update={"available_recovery_actions": frozenset()}
        )

    else:
        overrides[requirement_id] = (
            EvidenceState.CONFLICT_CRITICAL
        )

    result = evaluate_case(
        analysis,
        snapshot,
        context,
        overrides=overrides,
    )

    states = {
        record.requirement_id: record.state
        for record in result.evidence_states
    }

    assert result.decision == Decision.REJECT
    assert states[requirement_id] == expected_state
    assert requirement_id in result.hard_failures
    assert expected_reason in result.reason_codes
    assert result.validated_evidence_bundle is None
    assert result.validated_evidence_bundle_id is None
    assert result.recovery_actions == ()


def test_missing_recoverable_evidence_clarifies():
    analysis, snapshot, context = make_case()
    requirement_id = schema_requirement_id(analysis)

    snapshot = snapshot.model_copy(
        update={
            "evidence_items": tuple(
                item
                for item in snapshot.evidence_items
                if item.type != EvidenceType.SCHEMA
            )
        }
    )

    result = evaluate_case(analysis, snapshot, context)

    assert result.decision == Decision.CLARIFY
    assert result.coverage.mandatory_coverage == 0.5
    assert result.hard_failures == ()
    assert requirement_id in result.missing_items
    assert result.validated_evidence_bundle is None
    assert [
        (hint.requirement_id, hint.action)
        for hint in result.recovery_actions
    ] == [
        (requirement_id, "refresh_schema_snapshot"),
    ]


def test_stale_schema_clarifies_instead_of_generating():
    analysis, snapshot, context = make_case()
    context = context.model_copy(
        update={"schema_hash": "new-live-schema"}
    )

    result = evaluate_case(analysis, snapshot, context)

    assert result.decision == Decision.CLARIFY
    assert result.coverage.mandatory_coverage == 0.5
    assert ReasonCode.SCHEMA_STALE_RECOVERABLE in result.reason_codes
    assert result.validated_evidence_bundle is None


def test_recoverable_conflict_clarifies():
    analysis, snapshot, context = make_case()
    requirement_id = schema_requirement_id(analysis)

    result = evaluate_case(
        analysis,
        snapshot,
        context,
        overrides={
            requirement_id: EvidenceState.CONFLICT_RECOVERABLE,
        },
    )

    assert result.decision == Decision.CLARIFY
    assert result.hard_failures == ()
    assert len(result.conflicts) == 1
    assert result.conflicts[0].requirement_id == requirement_id
    assert result.validated_evidence_bundle is None
    assert result.recovery_actions


def test_unresolved_intent_clarifies_even_when_evidence_passes():
    analysis, snapshot, context = make_case()
    analysis = analysis.model_copy(
        update={
            "intent": analysis.intent.model_copy(
                update={"unresolved_terms": ("revenue",)}
            )
        }
    )

    result = evaluate_case(analysis, snapshot, context)

    assert result.decision == Decision.CLARIFY
    assert result.coverage.mandatory_coverage == 1.0
    assert result.unresolved_terms == ("revenue",)
    assert ReasonCode.EVIDENCE_AMBIGUOUS in result.reason_codes
    assert result.validated_evidence_bundle is None


def test_hard_failure_takes_precedence_over_unresolved_intent():
    analysis, snapshot, context = make_case()
    analysis = analysis.model_copy(
        update={
            "intent": analysis.intent.model_copy(
                update={"unresolved_terms": ("revenue",)}
            )
        }
    )
    context = context.model_copy(
        update={"permission_decisions": {"test.orders": False}}
    )

    result = evaluate_case(analysis, snapshot, context)

    assert result.decision == Decision.REJECT
    assert ReasonCode.PERMISSION_DENIED in result.reason_codes
    assert result.recovery_actions == ()
    assert result.validated_evidence_bundle is None


def test_optional_success_cannot_compensate_for_permission_denial():
    analysis, snapshot, context = make_case(optional=True)
    context = context.model_copy(
        update={
            "permission_decisions": {
                "test.orders": False,
                "test.notes": True,
            }
        }
    )

    result = evaluate_case(analysis, snapshot, context)

    assert result.coverage.optional_coverage == 1.0
    assert result.coverage.mandatory_coverage == 0.0
    assert result.decision == Decision.REJECT
    assert result.validated_evidence_bundle is None


def test_missing_optional_evidence_without_recovery_does_not_block():
    analysis, snapshot, context = make_case(optional=True)
    snapshot = snapshot.model_copy(
        update={
            "evidence_items": tuple(
                item
                for item in snapshot.evidence_items
                if item.type != EvidenceType.METADATA
            )
        }
    )

    result = evaluate_case(analysis, snapshot, context)

    assert result.decision == Decision.GENERATE
    assert result.coverage.mandatory_coverage == 1.0
    assert result.coverage.optional_coverage == 0.0
    assert "OPTIONAL-001" in result.missing_items
    assert result.validated_evidence_bundle is not None


def test_bundle_excludes_retrieval_diagnostics():
    result = evaluate_case(*make_case())
    approved = result.validated_evidence_bundle.evidence_snapshot()

    assert approved.retrieval_metadata == {}


def test_bundle_survives_later_input_mutation():
    analysis, snapshot, context = make_case()
    result = evaluate_case(analysis, snapshot, context)
    bundle = result.validated_evidence_bundle

    snapshot.evidence_items[0].value["target"] = "tampered.table"

    approved = bundle.evidence_snapshot()
    assert approved.evidence_items[0].value["target"] == "test.orders"


def test_mutating_returned_view_does_not_change_frozen_bundle():
    result = evaluate_case(*make_case())
    bundle = result.validated_evidence_bundle

    first_view = bundle.evidence_snapshot()
    first_view.evidence_items[0].value["target"] = "tampered.table"

    second_view = bundle.evidence_snapshot()
    assert second_view.evidence_items[0].value["target"] == "test.orders"


def test_identical_inputs_produce_identical_bundle_ids():
    analysis, snapshot, context = make_case()

    first = evaluate_case(analysis, snapshot, context)
    second = evaluate_case(analysis, snapshot, context)

    assert (
        first.validated_evidence_bundle_id
        == second.validated_evidence_bundle_id
    )


def test_bundle_identity_is_bound_to_principal():
    analysis, snapshot, context = make_case()
    first = evaluate_case(analysis, snapshot, context)

    changed_analysis = analysis.model_copy(
        update={
            "access_context": analysis.access_context.model_copy(
                update={"principal_id": "another-user"}
            )
        }
    )

    second = evaluate_case(changed_analysis, snapshot, context)

    assert (
        first.validated_evidence_bundle_id
        != second.validated_evidence_bundle_id
    )


def test_result_can_be_serialized_and_restored():
    result = evaluate_case(*make_case())

    restored = EEVAResult.model_validate_json(
        result.model_dump_json()
    )

    assert restored == result


def test_missing_conflict_scan_rejects_generation():
    analysis, snapshot, context = make_case()

    result = evaluate_evidence(
        analysis,
        snapshot,
        context,
        conflict_assessments=(),
    )

    assert result.decision == Decision.REJECT
    assert ReasonCode.PROVENANCE_INVALID in result.reason_codes
    assert result.validated_evidence_bundle is None


def test_unsupported_operation_must_stop_at_qim_precheck():
    analysis, snapshot, context = make_case()
    unsupported = analyze_query_intent(
        analysis.intent.model_copy(update={"operation": "delete"}),
        access_context=analysis.access_context,
    )

    with pytest.raises(ValueError, match="QIM_PRECHECK"):
        evaluate_evidence(
            unsupported,
            snapshot,
            context,
            conflict_assessments=(),
        )


def test_missing_access_context_is_rejected():
    analysis, snapshot, context = make_case()
    analysis = analysis.model_copy(
        update={"access_context": None}
    )

    with pytest.raises(ValueError, match="Trusted access"):
        evaluate_evidence(
            analysis,
            snapshot,
            context,
            conflict_assessments=(),
        )


def test_active_role_mismatch_is_rejected():
    analysis, snapshot, context = make_case()
    context = context.model_copy(
        update={"database_role": "driftsql_orders_limited"}
    )

    with pytest.raises(ValueError, match="same trusted active role"):
        evaluate_evidence(
            analysis,
            snapshot,
            context,
            conflict_assessments=(),
        )


def test_empty_obligation_set_cannot_approve_generation():
    analysis, snapshot, context = make_case()
    analysis = analysis.model_copy(
        update={"required_evidence_obligations": ()}
    )

    with pytest.raises(ValueError, match="non-empty"):
        evaluate_evidence(
            analysis,
            snapshot,
            context,
            conflict_assessments=(),
        )


def test_wrong_request_snapshot_is_rejected():
    analysis, snapshot, context = make_case()
    snapshot = snapshot.model_copy(
        update={"request_id": "different-request"}
    )

    with pytest.raises(ValueError, match="same request"):
        evaluate_evidence(
            analysis,
            snapshot,
            context,
            conflict_assessments=(),
        )