"""Tests for EEVA evidence-validation rules."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

import pytest

from src.contracts.evidence import (
    EvidenceItem,
    EvidenceSnapshot,
    EvidenceType,
)
from src.eeva.evidence_state import EvidenceState
from src.eeva.validation import (
    ConflictAssessment,
    SourceReference,
    ValidationContext,
    snapshot_fingerprint,
    validate_obligations,
)
from src.query_intelligence.models import QueryFilter
from src.query_intelligence.obligations import EvidenceObligation


NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


def content_hash(value):
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def make_case(
    evidence_type=EvidenceType.SCHEMA,
    *,
    recovery=True,
    mandatory=True,
    value=None,
):
    """Build evidence and independently declared trusted source facts."""
    if value is None:
        value = {"target": "test.orders", "kind": "table"}

    digest = content_hash(value)

    obligation = EvidenceObligation(
        requirement_id="REQ-001",
        request_id="request-001",
        database_id="test_database",
        evidence_type=evidence_type,
        target="test.orders",
        mandatory=mandatory,
        expected_source_types=("approved_test_source",),
        constraint_type="entity_exists",
        recovery_actions=("refresh_approved_source",),
        reason="Confirm the required evidence.",
    )

    item = EvidenceItem(
        evidence_id="EV-001",
        database_id="test_database",
        type=evidence_type,
        key="test.orders",
        value=value,
        source="test_repository",
        source_version="v1",
        retrieved_at=NOW,
        authority_level=100,
        access_label="internal",
        scope="test.orders",
        provenance_id="PROV-001",
        content_hash=digest,
    )

    snapshot = EvidenceSnapshot(
        snapshot_id="snapshot-001",
        request_id="request-001",
        database_id="test_database",
        evidence_items=(item,),
        schema_hash="v1",
        freshness_policy_version="freshness-v1",
        created_at=NOW,
    )

    reference = SourceReference(
        source="test_repository",
        provenance_id="PROV-001",
        evidence_type=evidence_type,
        key="test.orders",
        source_type="approved_test_source",
        current_version="v1",
        current_content_hash=digest,
        scope="test.orders",
        access_label="internal",
        authority_level=100,
    )

    context = ValidationContext(
        request_id="request-001",
        database_id="test_database",
        database_role="test_reader",
        schema_hash="v1",
        freshness_policy_version="freshness-v1",
        evaluated_at=NOW,
        sources=(reference,),
        permission_decisions={"test.orders": True},
        allowed_access_labels=frozenset({"internal"}),
        available_recovery_actions=(
            frozenset({"refresh_approved_source"})
            if recovery
            else frozenset()
        ),
    )

    return obligation, snapshot, context


def make_review(
    snapshot,
    *,
    state=EvidenceState.PASS,
    evidence_ids=None,
):
    """Simulate conflict-detector output for controlled fixtures."""
    if evidence_ids is None:
        evidence_ids = tuple(
            item.evidence_id
            for item in snapshot.evidence_items
        )

    return ConflictAssessment(
        snapshot_id=snapshot.snapshot_id,
        snapshot_digest=snapshot_fingerprint(snapshot),
        requirement_id="REQ-001",
        evidence_item_ids=evidence_ids,
        state=state,
    )


def evaluate_case(
    obligation,
    snapshot,
    context,
    *,
    review_state=EvidenceState.PASS,
    filters=(),
):
    results = validate_obligations(
        (obligation,),
        snapshot,
        context,
        (make_review(snapshot, state=review_state),),
        filters=filters,
    )

    assert len(results) == 1
    return results[0]


def test_current_authorized_evidence_passes():
    result = evaluate_case(*make_case())

    assert result.requirement_id == "REQ-001"
    assert result.state == EvidenceState.PASS
    assert result.evidence_item_ids == ("EV-001",)
    assert result.recovery_actions == ()


@pytest.mark.parametrize(
    "recovery,mandatory,expected",
    [
        (True, True, EvidenceState.MISSING_RECOVERABLE),
        (False, True, EvidenceState.MISSING_CRITICAL),
        (True, False, EvidenceState.MISSING_RECOVERABLE),
        (False, False, EvidenceState.OPTIONAL_MISSING),
    ],
)
def test_missing_evidence_classification(recovery, mandatory, expected):
    obligation, snapshot, context = make_case(
        recovery=recovery,
        mandatory=mandatory,
    )
    snapshot = snapshot.model_copy(
        update={"evidence_items": ()}
    )

    result = validate_obligations(
        (obligation,), snapshot, context, ()
    )[0]

    assert result.state == expected
    assert result.evidence_item_ids == ()
    assert result.recovery_actions == (
        ("refresh_approved_source",) if recovery else ()
    )


def test_suggested_action_is_not_proof_recovery_is_available():
    obligation, snapshot, context = make_case(recovery=False)
    snapshot = snapshot.model_copy(
        update={"evidence_items": ()}
    )

    assert obligation.recovery_actions == (
        "refresh_approved_source",
    )

    result = validate_obligations(
        (obligation,), snapshot, context, ()
    )[0]

    assert result.state == EvidenceState.MISSING_CRITICAL


def test_confirmed_authoritative_absence_is_critical():
    obligation, snapshot, context = make_case()
    context = context.model_copy(
        update={
            "confirmed_absent_requirement_ids":
                frozenset({"REQ-001"})
        }
    )

    result = evaluate_case(obligation, snapshot, context)

    assert result.state == EvidenceState.MISSING_CRITICAL
    assert result.recovery_actions == ()


def test_permission_denial_overrides_stale_evidence_and_conflict():
    obligation, snapshot, context = make_case()
    context = context.model_copy(
        update={
            "permission_decisions": {"test.orders": False},
            "schema_hash": "new-live-schema",
        }
    )

    result = evaluate_case(
        obligation,
        snapshot,
        context,
        review_state=EvidenceState.CONFLICT_CRITICAL,
    )

    assert result.state == EvidenceState.UNAUTHORIZED
    assert result.evidence_item_ids == ()
    assert result.recovery_actions == ()


def test_denied_access_label_blocks_evidence():
    obligation, snapshot, context = make_case()
    context = context.model_copy(
        update={"allowed_access_labels": frozenset()}
    )

    result = evaluate_case(obligation, snapshot, context)

    assert result.state == EvidenceState.UNAUTHORIZED


@pytest.mark.parametrize(
    "evidence_type",
    [EvidenceType.SCHEMA, EvidenceType.RELATIONSHIP],
)
def test_schema_bound_evidence_uses_current_schema_hash(evidence_type):
    obligation, snapshot, context = make_case(evidence_type)
    context = context.model_copy(
        update={"schema_hash": "new-live-schema"}
    )

    result = evaluate_case(obligation, snapshot, context)

    assert result.state == EvidenceState.STALE_RECOVERABLE
    assert result.recovery_actions == (
        "refresh_approved_source",
    )


@pytest.mark.parametrize("evidence_type", list(EvidenceType))
def test_current_source_version_mismatch_is_detected(evidence_type):
    obligation, snapshot, context = make_case(evidence_type)

    reference = context.sources[0].model_copy(
        update={"current_version": "v2"}
    )
    context = context.model_copy(
        update={"sources": (reference,)}
    )

    result = evaluate_case(obligation, snapshot, context)

    assert result.state == EvidenceState.STALE_RECOVERABLE


def test_old_timestamp_alone_does_not_make_evidence_stale():
    obligation, snapshot, context = make_case()

    item = snapshot.evidence_items[0].model_copy(
        update={"retrieved_at": NOW - timedelta(days=500)}
    )
    snapshot = snapshot.model_copy(
        update={"evidence_items": (item,)}
    )

    result = evaluate_case(obligation, snapshot, context)

    assert result.state == EvidenceState.PASS


@pytest.mark.parametrize(
    "change",
    [
        {"content_hash": "0" * 64},
        {"provenance_id": "UNKNOWN-PROVENANCE"},
        {"authority_level": 999},
        {"scope": "other_schema"},
        {"effective_from": NOW - timedelta(days=1)},
    ],
)
def test_tampered_evidence_cannot_pass(change):
    obligation, snapshot, context = make_case(recovery=False)

    item = snapshot.evidence_items[0].model_copy(update=change)
    snapshot = snapshot.model_copy(
        update={"evidence_items": (item,)}
    )

    result = evaluate_case(obligation, snapshot, context)

    assert result.state == EvidenceState.INVALID
    assert result.evidence_item_ids == ()


def test_recomputed_hash_does_not_approve_a_modified_fact():
    obligation, snapshot, context = make_case(recovery=False)

    modified_value = {
        "target": "test.orders",
        "kind": "invented",
    }

    item = snapshot.evidence_items[0].model_copy(
        update={
            "value": modified_value,
            "content_hash": content_hash(modified_value),
        }
    )
    snapshot = snapshot.model_copy(
        update={"evidence_items": (item,)}
    )

    result = evaluate_case(obligation, snapshot, context)

    assert result.state == EvidenceState.INVALID


def test_invalid_provenance_can_request_approved_replacement():
    obligation, snapshot, context = make_case()

    item = snapshot.evidence_items[0].model_copy(
        update={"provenance_id": "UNKNOWN-PROVENANCE"}
    )
    snapshot = snapshot.model_copy(
        update={"evidence_items": (item,)}
    )

    result = evaluate_case(obligation, snapshot, context)

    assert result.state == EvidenceState.MISSING_RECOVERABLE
    assert result.evidence_item_ids == ()
    assert result.recovery_actions == (
        "refresh_approved_source",
    )


def test_source_must_match_expected_source_type():
    obligation, snapshot, context = make_case(recovery=False)

    obligation = obligation.model_copy(
        update={"expected_source_types": ("different_source",)}
    )

    result = evaluate_case(obligation, snapshot, context)

    assert result.state == EvidenceState.INVALID


def test_missing_conflict_review_does_not_mean_no_conflict():
    obligation, snapshot, context = make_case()

    result = validate_obligations(
        (obligation,), snapshot, context, ()
    )[0]

    assert result.state == EvidenceState.INVALID
    assert result.evidence_item_ids == ()


@pytest.mark.parametrize(
    "state",
    [
        EvidenceState.AMBIGUOUS_RECOVERABLE,
        EvidenceState.CONFLICT_RECOVERABLE,
    ],
)
def test_recoverable_assessments_produce_approved_action_hints(state):
    result = evaluate_case(
        *make_case(),
        review_state=state,
    )

    assert result.state == state
    assert result.evidence_item_ids == ()
    assert result.recovery_actions == (
        "refresh_approved_source",
    )


def test_critical_conflict_cannot_be_downgraded_to_schema_refresh():
    obligation, snapshot, context = make_case()

    context = context.model_copy(
        update={"schema_hash": "new-live-schema"}
    )

    result = evaluate_case(
        obligation,
        snapshot,
        context,
        review_state=EvidenceState.CONFLICT_CRITICAL,
    )

    assert result.state == EvidenceState.CONFLICT_CRITICAL
    assert result.recovery_actions == ()


def test_review_is_bound_to_snapshot_contents():
    obligation, snapshot, context = make_case()
    original_review = make_review(snapshot)

    changed_snapshot = snapshot.model_copy(
        update={"retrieval_metadata": {"changed": True}}
    )

    with pytest.raises(ValueError, match="snapshot contents"):
        validate_obligations(
            (obligation,),
            changed_snapshot,
            context,
            (original_review,),
        )


def test_review_must_cover_exact_candidate_ids():
    obligation, snapshot, context = make_case()

    incomplete_review = make_review(
        snapshot,
        evidence_ids=(),
    )

    with pytest.raises(ValueError, match="exact candidate IDs"):
        validate_obligations(
            (obligation,),
            snapshot,
            context,
            (incomplete_review,),
        )


def test_effective_to_is_exclusive():
    obligation, snapshot, context = make_case(
        EvidenceType.GLOSSARY
    )

    item = snapshot.evidence_items[0].model_copy(
        update={"effective_to": NOW}
    )
    reference = context.sources[0].model_copy(
        update={"effective_to": NOW}
    )

    snapshot = snapshot.model_copy(
        update={"evidence_items": (item,)}
    )
    context = context.model_copy(
        update={"sources": (reference,)}
    )

    result = evaluate_case(obligation, snapshot, context)

    assert result.state == EvidenceState.STALE_RECOVERABLE


@pytest.mark.parametrize(
    "permission,role,expected",
    [
        (True, "test_reader", EvidenceState.PASS),
        (False, "test_reader", EvidenceState.UNAUTHORIZED),
        (True, "other_reader", EvidenceState.INVALID),
        (None, "test_reader", EvidenceState.INVALID),
    ],
)
def test_permission_evidence_is_bound_to_active_role(
    permission,
    role,
    expected,
):
    obligation, snapshot, context = make_case(
        EvidenceType.PERMISSION,
        recovery=False,
        value={
            "target": "test.orders",
            "database_role": role,
            "decision": "ALLOW",
        },
    )

    context = context.model_copy(
        update={
            "permission_decisions": (
                {}
                if permission is None
                else {"test.orders": permission}
            )
        }
    )

    result = evaluate_case(obligation, snapshot, context)

    assert result.state == expected


@pytest.mark.parametrize(
    "requested,expected",
    [
        ("delivered", EvidenceState.PASS),
        ("invented_status", EvidenceState.MISSING_CRITICAL),
    ],
)
def test_value_evidence_supports_the_actual_filter(requested, expected):
    obligation, snapshot, context = make_case(
        EvidenceType.VALUE,
        recovery=False,
        value={
            "target": "test.orders",
            "values": ["delivered"],
            "coverage": "COMPLETE",
        },
    )

    obligation = obligation.model_copy(
        update={"constraint_type": "filter_value_exists"}
    )

    filters = (
        QueryFilter(
            field="test.orders",
            operator="=",
            value=requested,
        ),
    )

    result = evaluate_case(
        obligation,
        snapshot,
        context,
        filters=filters,
    )

    assert result.state == expected


def test_table_scope_does_not_prove_a_column_exists():
    obligation, snapshot, context = make_case()

    obligation = obligation.model_copy(
        update={"target": "test.orders.missing_column"}
    )

    result = validate_obligations(
        (obligation,), snapshot, context, ()
    )[0]

    assert result.state == EvidenceState.MISSING_RECOVERABLE
    assert result.evidence_item_ids == ()


def test_cross_request_input_is_rejected():
    obligation, snapshot, context = make_case()

    snapshot = snapshot.model_copy(
        update={"request_id": "different-request"}
    )

    with pytest.raises(ValueError, match="same request"):
        validate_obligations(
            (obligation,), snapshot, context, ()
        )


def test_duplicate_obligations_are_rejected():
    obligation, snapshot, context = make_case()

    with pytest.raises(ValueError, match="Duplicate requirement"):
        validate_obligations(
            (obligation, obligation),
            snapshot,
            context,
            (),
        )


def test_duplicate_evidence_ids_are_rejected():
    obligation, snapshot, context = make_case()

    snapshot = snapshot.model_copy(
        update={
            "evidence_items":
                snapshot.evidence_items + snapshot.evidence_items
        }
    )

    with pytest.raises(ValueError, match="Duplicate evidence"):
        validate_obligations(
            (obligation,), snapshot, context, ()
        )


def test_role_provenance_can_cover_multiple_permission_targets():
    from src.evidence.permissions import (
        build_verified_permission_evidence,
    )

    obligation, snapshot, context = make_case(
        EvidenceType.PERMISSION
    )

    obligation = obligation.model_copy(
        update={
            "expected_source_types": ("active_postgresql_role",),
            "constraint_type": "role_allows_access",
        }
    )

    second_obligation = obligation.model_copy(
        update={
            "requirement_id": "REQ-002",
            "target": "test.orders.order_id",
            "constraint_type": "role_allows_column_read",
        }
    )

    obligations = (obligation, second_obligation)

    # Real Month 2 builder; no database connection is made here.
    items = build_verified_permission_evidence(
        obligations,
        database_id=snapshot.database_id,
        database_role=context.database_role,
    )

    assert len(items) == 2
    assert items[0].provenance_id == items[1].provenance_id
    assert items[0].key != items[1].key

    references = tuple(
        SourceReference(
            source=item.source,
            provenance_id=item.provenance_id,
            evidence_type=item.type,
            key=item.key,
            source_type="active_postgresql_role",
            current_version=item.source_version,
            current_content_hash=item.content_hash,
            scope=item.scope,
            access_label=item.access_label,
            authority_level=item.authority_level,
        )
        for item in items
    )

    snapshot = snapshot.model_copy(
        update={"evidence_items": items}
    )

    context_data = context.model_dump()
    context_data["sources"] = references
    context_data["permission_decisions"] = {
        requirement.target: True
        for requirement in obligations
    }

    context = ValidationContext.model_validate(context_data)

    reviews = tuple(
        ConflictAssessment(
            snapshot_id=snapshot.snapshot_id,
            snapshot_digest=snapshot_fingerprint(snapshot),
            requirement_id=requirement.requirement_id,
            evidence_item_ids=tuple(
                item.evidence_id
                for item in items
                if item.key == requirement.target
            ),
            state=EvidenceState.PASS,
        )
        for requirement in obligations
    )

    results = validate_obligations(
        obligations,
        snapshot,
        context,
        reviews,
    )

    assert [result.state for result in results] == [
        EvidenceState.PASS,
        EvidenceState.PASS,
    ]

    assert [result.evidence_item_ids for result in results] == [
        (items[0].evidence_id,),
        (items[1].evidence_id,),
    ]


def test_identical_source_reference_is_still_rejected():
    _, _, context = make_case()

    context_data = context.model_dump()
    context_data["sources"] = (
        context.sources[0],
        context.sources[0],
    )

    with pytest.raises(
        ValueError,
        match="Duplicate current source references",
    ):
        ValidationContext.model_validate(context_data)