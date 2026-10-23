from datetime import datetime, timezone

import pytest

from src.contracts.evidence import EvidenceItem, EvidenceType
from src.query_intelligence.alias_expansion import expand_alias_obligations
from src.query_intelligence.models import QueryIntent
from src.query_intelligence.pipeline import analyze_query_intent


def make_metadata(target: str) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"EV-{target}",
        database_id="olist_primary",
        type=EvidenceType.METADATA,
        key=target,
        value={"target": target, "aliases": ["revenue"]},
        source="test",
        source_version="v1",
        retrieved_at=datetime.now(timezone.utc),
        authority_level=70,
        access_label="internal",
        scope=target,
        provenance_id=f"PROV-{target}",
        content_hash="test-hash",
    )


def make_analysis():
    intent = QueryIntent(
        request_id="test-request",
        database_id="olist_primary",
        operation="aggregate",
        entities=("olist_raw.orders",),
        measures=("revenue",),
    )
    return analyze_query_intent(intent)


def test_unique_alias_expands_to_canonical_target():
    analysis = make_analysis()
    evidence = (make_metadata("olist_raw.order_items.price"),)

    result = expand_alias_obligations(analysis, evidence)

    revenue_obligations = [
        item
        for item in result.required_evidence_obligations
        if item.target == "olist_raw.order_items.price"
    ]
    assert revenue_obligations


def test_ambiguous_alias_is_rejected():
    analysis = make_analysis()
    evidence = (
        make_metadata("olist_raw.order_items.price"),
        make_metadata("olist_raw.payments.payment_value"),
    )

    with pytest.raises(ValueError, match="Ambiguous term"):
        expand_alias_obligations(analysis, evidence)