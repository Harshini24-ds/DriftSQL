"""Tests for EEVA conflict detection."""

from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

from src.contracts.evidence import (
    EvidenceItem,
    EvidenceSnapshot,
    EvidenceType,
)
from src.eeva.conflicts import detect_conflicts
from src.eeva.evidence_state import EvidenceState
from src.eeva.validation import (
    SourceReference,
    ValidationContext,
    validate_obligations,
)
from src.query_intelligence.obligations import (
    EXPECTED_SOURCE_TYPES,
    RECOVERY_ACTIONS,
    EvidenceObligation,
)


NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)
TARGET = "olist_raw.order_items.price"


def content_hash(value):
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def make_record(
    evidence_id,
    value,
    *,
    kind=EvidenceType.METADATA,
    key=TARGET,
    source_type=None,
    authority=70,
):
    """Construct an evidence record and its trusted current reference."""
    if source_type is None:
        source_type = EXPECTED_SOURCE_TYPES[kind][0]

    scope = (
        value.get("target", key)
        if isinstance(value, dict)
        else key
    )

    source = f"approved-source-{evidence_id}"
    provenance_id = f"PROV-{evidence_id}"
    digest = content_hash(value)

    item = EvidenceItem(
        evidence_id=evidence_id,
        database_id="olist_primary",
        type=kind,
        key=key,
        value=value,
        source=source,
        source_version="v1",
        retrieved_at=NOW,
        authority_level=authority,
        access_label="internal",
        scope=scope,
        provenance_id=provenance_id,
        content_hash=digest,
    )

    reference = SourceReference(
        source=source,
        provenance_id=provenance_id,
        evidence_type=kind,
        key=key,
        source_type=source_type,
        current_version="v1",
        current_content_hash=digest,
        scope=scope,
        access_label="internal",
        authority_level=authority,
    )

    return item, reference


def make_case(
    records=None,
    *,
    kind=EvidenceType.METADATA,
    target=TARGET,
    recovery=True,
):
    if records is None:
        records = (
            make_record(
                "EV-1",
                {"target": TARGET, "unit": "BRL"},
                kind=kind,
            ),
            make_record(
                "EV-2",
                {"target": TARGET, "unit": "USD"},
                kind=kind,
            ),
        )

    obligation = EvidenceObligation(
        requirement_id="REQ-001",
        request_id="conflict-request",
        database_id="olist_primary",
        evidence_type=kind,
        target=target,
        mandatory=True,
        expected_source_types=EXPECTED_SOURCE_TYPES[kind],
        constraint_type="approved_fact",
        recovery_actions=RECOVERY_ACTIONS[kind],
        reason="Confirm consistent approved evidence.",
    )

    items = tuple(item for item, _ in records)
    references = tuple(reference for _, reference in records)

    snapshot = EvidenceSnapshot(
        snapshot_id="conflict-snapshot",
        request_id="conflict-request",
        database_id="olist_primary",
        evidence_items=items,
        schema_hash="v1",
        freshness_policy_version="conflict-policy-v1",
        created_at=NOW,
    )

    permissions = {target: True}

    for item in items:
        if isinstance(item.value, dict):
            item_target = item.value.get("target")
            if isinstance(item_target, str):
                permissions[item_target] = True

    context = ValidationContext(
        request_id="conflict-request",
        database_id="olist_primary",
        database_role="driftsql_olist_reader",
        schema_hash="v1",
        freshness_policy_version="conflict-policy-v1",
        evaluated_at=NOW,
        sources=references,
        permission_decisions=permissions,
        allowed_access_labels=frozenset({"internal"}),
        available_recovery_actions=(
            frozenset(RECOVERY_ACTIONS[kind])
            if recovery
            else frozenset()
        ),
    )

    return obligation, snapshot, context


def scan(obligation, snapshot, context):
    return detect_conflicts(
        (obligation,),
        snapshot,
        context,
    )


def test_matching_approved_facts_pass():
    records = (
        make_record("EV-1", {"target": TARGET, "unit": "BRL"}),
        make_record("EV-2", {"target": TARGET, "unit": "BRL"}),
    )

    report = scan(*make_case(records))

    assert report.findings == ()
    assert len(report.assessments) == 1
    assert report.assessments[0].state == EvidenceState.PASS
    assert report.assessments[0].evidence_item_ids == (
        "EV-1",
        "EV-2",
    )


def test_single_current_source_has_no_conflict():
    records = (
        make_record("EV-1", {"target": TARGET, "unit": "BRL"}),
    )

    report = scan(*make_case(records))

    assert report.findings == ()
    assert report.assessments[0].state == EvidenceState.PASS


def test_disagreement_with_available_recovery_is_recoverable():
    report = scan(*make_case())

    assert report.assessments[0].state == (
        EvidenceState.CONFLICT_RECOVERABLE
    )

    assert len(report.findings) == 1

    finding = report.findings[0]

    assert finding.requirement_id == "REQ-001"
    assert finding.target == TARGET
    assert finding.predicate == "unit"
    assert finding.state == EvidenceState.CONFLICT_RECOVERABLE
    assert finding.evidence_item_ids == ("EV-1", "EV-2")

    # Equal-authority sources disagree; neither is preferred.
    assert finding.preferred_evidence_ids == ()


def test_disagreement_without_recovery_is_critical():
    report = scan(*make_case(recovery=False))

    assert report.assessments[0].state == (
        EvidenceState.CONFLICT_CRITICAL
    )
    assert report.findings[0].state == (
        EvidenceState.CONFLICT_CRITICAL
    )


def test_unrelated_available_action_does_not_allow_recovery():
    obligation, snapshot, context = make_case()

    context = context.model_copy(
        update={
            "available_recovery_actions": frozenset({
                "unrelated_action",
            })
        }
    )

    report = scan(obligation, snapshot, context)

    assert report.assessments[0].state == (
        EvidenceState.CONFLICT_CRITICAL
    )


def test_higher_attested_authority_identifies_preferred_source():
    records = (
        make_record(
            "EV-1",
            {"target": TARGET, "unit": "BRL"},
            authority=90,
        ),
        make_record(
            "EV-2",
            {"target": TARGET, "unit": "USD"},
            authority=60,
        ),
    )

    report = scan(*make_case(records))

    assert report.assessments[0].state == (
        EvidenceState.CONFLICT_RECOVERABLE
    )
    assert report.findings[0].preferred_evidence_ids == ("EV-1",)


def test_agreeing_highest_authority_sources_are_both_preferred():
    records = (
        make_record(
            "EV-1",
            {"target": TARGET, "unit": "BRL"},
            authority=90,
        ),
        make_record(
            "EV-2",
            {"target": TARGET, "unit": "BRL"},
            authority=90,
        ),
        make_record(
            "EV-3",
            {"target": TARGET, "unit": "USD"},
            authority=50,
        ),
    )

    report = scan(*make_case(records))

    assert report.findings[0].preferred_evidence_ids == (
        "EV-1",
        "EV-2",
    )


def test_cross_source_fact_uses_source_category_precedence():
    metadata = make_record(
        "EV-1",
        {"target": TARGET, "unit": "USD"},
        authority=900,
    )

    documentation = make_record(
        "EV-2",
        {
            "target": TARGET,
            "facts": [
                {
                    "target": TARGET,
                    "predicate": "unit",
                    "value": "BRL",
                }
            ],
        },
        kind=EvidenceType.DOCUMENT,
        key="PRICE-UNIT-RULE",
        authority=10,
    )

    report = scan(*make_case((metadata, documentation)))

    assert report.assessments[0].state == (
        EvidenceState.CONFLICT_RECOVERABLE
    )

    # Only metadata is a direct candidate for this obligation.
    assert report.assessments[0].evidence_item_ids == ("EV-1",)

    # The conflict scan also considered the documentation claim.
    finding = report.findings[0]

    assert finding.evidence_item_ids == ("EV-1", "EV-2")

    # Approved documentation outranks curated metadata for this
    # business fact, even when the metadata numeric level is higher.
    assert finding.preferred_evidence_ids == ("EV-2",)


def test_unrelated_descriptions_are_not_automatically_conflicts():
    records = (
        make_record(
            "EV-1",
            {
                "target": TARGET,
                "description": "Price description.",
            },
            key="PRICE-DESCRIPTION",
        ),
        make_record(
            "EV-2",
            {
                "target": TARGET,
                "description": "Additional reporting guidance.",
            },
            key="REPORTING-GUIDANCE",
        ),
    )

    obligation, snapshot, context = make_case(records)

    assert (
        snapshot.evidence_items[0].content_hash
        != snapshot.evidence_items[1].content_hash
    )

    report = scan(obligation, snapshot, context)

    assert report.assessments[0].state == EvidenceState.PASS
    assert report.findings == ()


def test_stale_record_is_not_a_current_conflicting_authority():
    obligation, snapshot, context = make_case()

    stale_item = snapshot.evidence_items[1].model_copy(
        update={"source_version": "v9"}
    )

    current_reference = context.sources[1].model_copy(
        update={
            "current_version": "v10",
            "current_content_hash": content_hash({
                "target": TARGET,
                "unit": "BRL",
            }),
        }
    )

    snapshot = snapshot.model_copy(
        update={
            "evidence_items": (
                snapshot.evidence_items[0],
                stale_item,
            )
        }
    )

    context = context.model_copy(
        update={
            "sources": (
                context.sources[0],
                current_reference,
            )
        }
    )

    report = scan(obligation, snapshot, context)

    assert report.assessments[0].state == EvidenceState.PASS
    assert report.findings == ()

    # The assessment still covers every direct candidate.
    assert report.assessments[0].evidence_item_ids == (
        "EV-1",
        "EV-2",
    )

    result = validate_obligations(
        (obligation,),
        snapshot,
        context,
        report.assessments,
    )[0]

    assert result.state == EvidenceState.PASS
    assert result.evidence_item_ids == ("EV-1",)


def test_tampered_authority_is_not_used_for_precedence():
    obligation, snapshot, context = make_case()

    modified_item = snapshot.evidence_items[1].model_copy(
        update={"authority_level": 9999}
    )

    snapshot = snapshot.model_copy(
        update={
            "evidence_items": (
                snapshot.evidence_items[0],
                modified_item,
            )
        }
    )

    report = scan(obligation, snapshot, context)

    assert report.assessments[0].state == EvidenceState.PASS
    assert report.findings == ()

    result = validate_obligations(
        (obligation,),
        snapshot,
        context,
        report.assessments,
    )[0]

    assert result.state == EvidenceState.PASS
    assert result.evidence_item_ids == ("EV-1",)


def test_clean_conflict_scan_does_not_approve_invalid_provenance():
    records = (
        make_record("EV-1", {"target": TARGET, "unit": "BRL"}),
    )

    obligation, snapshot, context = make_case(
        records,
        recovery=False,
    )

    context = context.model_copy(
        update={"sources": ()}
    )

    report = scan(obligation, snapshot, context)

    assert report.assessments[0].state == EvidenceState.PASS

    result = validate_obligations(
        (obligation,),
        snapshot,
        context,
        report.assessments,
    )[0]

    assert result.state == EvidenceState.INVALID
    assert result.evidence_item_ids == ()


def test_clean_conflict_scan_cannot_override_permission_denial():
    obligation, snapshot, context = make_case()

    context = context.model_copy(
        update={"permission_decisions": {TARGET: False}}
    )

    report = scan(obligation, snapshot, context)

    result = validate_obligations(
        (obligation,),
        snapshot,
        context,
        report.assessments,
    )[0]

    assert result.state == EvidenceState.UNAUTHORIZED
    assert result.evidence_item_ids == ()


def test_one_glossary_term_with_two_targets_is_ambiguous():
    records = (
        make_record(
            "EV-1",
            {
                "term": "revenue",
                "target": "olist_raw.order_items.price",
                "definition": "Item-price interpretation.",
            },
            kind=EvidenceType.GLOSSARY,
            key="revenue",
        ),
        make_record(
            "EV-2",
            {
                "term": "revenue",
                "target": "olist_raw.order_payments.payment_value",
                "definition": "Payment-value interpretation.",
            },
            kind=EvidenceType.GLOSSARY,
            key="revenue",
        ),
    )

    obligation, snapshot, context = make_case(
        records,
        kind=EvidenceType.GLOSSARY,
        target="revenue",
    )

    report = scan(obligation, snapshot, context)

    assert report.assessments[0].state == (
        EvidenceState.AMBIGUOUS_RECOVERABLE
    )
    assert report.findings == ()

    result = validate_obligations(
        (obligation,),
        snapshot,
        context,
        report.assessments,
    )[0]

    assert result.state == EvidenceState.AMBIGUOUS_RECOVERABLE


def test_different_value_samples_are_not_conflicting_domains():
    records = (
        make_record(
            "EV-1",
            {
                "target": TARGET,
                "coverage": "SAMPLE",
                "values": ["A", "B"],
            },
            kind=EvidenceType.VALUE,
        ),
        make_record(
            "EV-2",
            {
                "target": TARGET,
                "coverage": "SAMPLE",
                "values": ["C"],
            },
            kind=EvidenceType.VALUE,
        ),
    )

    report = scan(*make_case(records, kind=EvidenceType.VALUE))

    assert report.assessments[0].state == EvidenceState.PASS
    assert report.findings == ()


def test_complete_value_domains_ignore_list_order():
    records = (
        make_record(
            "EV-1",
            {
                "target": TARGET,
                "coverage": "COMPLETE",
                "values": ["A", "B"],
            },
            kind=EvidenceType.VALUE,
        ),
        make_record(
            "EV-2",
            {
                "target": TARGET,
                "coverage": "COMPLETE",
                "values": ["B", "A"],
            },
            kind=EvidenceType.VALUE,
        ),
    )

    report = scan(*make_case(records, kind=EvidenceType.VALUE))

    assert report.assessments[0].state == EvidenceState.PASS
    assert report.findings == ()


def test_different_complete_value_domains_conflict():
    records = (
        make_record(
            "EV-1",
            {
                "target": TARGET,
                "coverage": "COMPLETE",
                "values": ["A", "B"],
            },
            kind=EvidenceType.VALUE,
        ),
        make_record(
            "EV-2",
            {
                "target": TARGET,
                "coverage": "COMPLETE",
                "values": ["A", "C"],
            },
            kind=EvidenceType.VALUE,
        ),
    )

    report = scan(*make_case(records, kind=EvidenceType.VALUE))

    assert report.assessments[0].state == (
        EvidenceState.CONFLICT_RECOVERABLE
    )
    assert report.findings[0].predicate == "allowed_values"


@pytest.mark.parametrize(
    "facts",
    [
        "invalid",
        [{"target": TARGET, "value": "BRL"}],
        [{"target": TARGET, "predicate": "", "value": "BRL"}],
    ],
)
def test_malformed_approved_fact_structure_is_rejected(facts):
    records = (
        make_record(
            "EV-1",
            {"target": TARGET, "facts": facts},
        ),
    )

    with pytest.raises(ValueError, match="facts|fact"):
        scan(*make_case(records))


def test_assessment_cannot_be_reused_after_snapshot_changes():
    obligation, snapshot, context = make_case()

    report = scan(obligation, snapshot, context)

    modified_snapshot = snapshot.model_copy(
        update={"retrieval_metadata": {"changed": True}}
    )

    with pytest.raises(ValueError, match="snapshot contents"):
        validate_obligations(
            (obligation,),
            modified_snapshot,
            context,
            report.assessments,
        )


def test_cross_request_context_is_rejected():
    obligation, snapshot, context = make_case()

    context = context.model_copy(
        update={"request_id": "another-request"}
    )

    with pytest.raises(ValueError, match="must match"):
        scan(obligation, snapshot, context)


def test_duplicate_obligations_are_rejected():
    obligation, snapshot, context = make_case()

    with pytest.raises(ValueError, match="Duplicate requirement"):
        detect_conflicts(
            (obligation, obligation),
            snapshot,
            context,
        )


def test_duplicate_evidence_ids_are_rejected():
    obligation, snapshot, context = make_case()

    snapshot = snapshot.model_copy(
        update={
            "evidence_items": (
                snapshot.evidence_items[0],
                snapshot.evidence_items[0],
            )
        }
    )

    with pytest.raises(ValueError, match="Duplicate evidence"):
        scan(obligation, snapshot, context)


def test_duplicate_current_references_are_rejected():
    obligation, snapshot, context = make_case()

    context = context.model_copy(
        update={
            "sources": (
                context.sources[0],
                context.sources[0],
            )
        }
    )

    with pytest.raises(ValueError, match="Duplicate current source"):
        scan(obligation, snapshot, context)


def test_empty_obligations_return_an_empty_report():
    _, snapshot, context = make_case()

    report = detect_conflicts((), snapshot, context)

    assert report.assessments == ()
    assert report.findings == ()