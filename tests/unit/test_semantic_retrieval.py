from datetime import datetime, timezone

import pytest

from src.contracts.evidence import EvidenceItem, EvidenceType
from src.retrieval.semantic import retrieve_semantic_evidence


def make_evidence(
    evidence_type: EvidenceType,
    evidence_id: str,
    *,
    database_id: str = "olist_primary",
    key: str = "evidence",
    value: dict | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        database_id=database_id,
        type=evidence_type,
        key=key,
        value=value or {},
        source="test",
        source_version="v1",
        retrieved_at=datetime.now(timezone.utc),
        authority_level=70,
        access_label="internal",
        scope=key,
        provenance_id=f"PROV-{evidence_id}",
        content_hash=f"HASH-{evidence_id}",
    )


def test_ranks_more_relevant_supporting_evidence_first():
    relevant = make_evidence(
        EvidenceType.METADATA,
        "EV-META-PRICE",
        key="olist_raw.order_items.price",
        value={"description": "Revenue amount for each order item"},
    )
    partial = make_evidence(
        EvidenceType.DOCUMENT,
        "EV-DOC-ORDER",
        key="order rule",
        value={"rule": "Order processing guidance"},
    )
    unrelated = make_evidence(
        EvidenceType.GLOSSARY,
        "EV-GLOSSARY-CUSTOMER",
        key="customer",
        value={"definition": "A person who placed a purchase"},
    )

    results = retrieve_semantic_evidence(
        "revenue for an order item",
        (partial, unrelated, relevant),
        database_id="olist_primary",
    )

    assert results[0].evidence_item.evidence_id == "EV-META-PRICE"
    assert results[0].score == 1.0
    assert all(
        match.evidence_item.evidence_id != "EV-GLOSSARY-CUSTOMER"
        for match in results
    )


def test_retrieval_excludes_other_databases_and_non_searchable_types():
    correct_database = make_evidence(
        EvidenceType.DOCUMENT,
        "EV-DOC-LOCAL",
        value={"rule": "Order delivery policy"},
    )
    other_database = make_evidence(
        EvidenceType.DOCUMENT,
        "EV-DOC-OTHER",
        database_id="another_database",
        value={"rule": "Order delivery policy"},
    )
    schema_item = make_evidence(
        EvidenceType.SCHEMA,
        "EV-SCHEMA-ORDERS",
        value={"table": "orders"},
    )

    results = retrieve_semantic_evidence(
        "order delivery policy",
        (correct_database, other_database, schema_item),
        database_id="olist_primary",
    )

    assert [match.evidence_item.evidence_id for match in results] == [
        "EV-DOC-LOCAL"
    ]


def test_retrieval_respects_top_k_and_has_stable_tie_order():
    items = (
        make_evidence(
            EvidenceType.METADATA,
            "EV-META-B",
            value={"description": "order delivery"},
        ),
        make_evidence(
            EvidenceType.DOCUMENT,
            "EV-DOC-A",
            value={"rule": "order delivery"},
        ),
    )

    results = retrieve_semantic_evidence(
        "order delivery",
        items,
        database_id="olist_primary",
        top_k=1,
    )

    assert len(results) == 1
    assert results[0].evidence_item.evidence_id == "EV-DOC-A"


def test_empty_or_unmatched_query_returns_no_results():
    item = make_evidence(
        EvidenceType.METADATA,
        "EV-META-ORDERS",
        value={"description": "order status"},
    )

    assert retrieve_semantic_evidence(
        "the and of",
        (item,),
        database_id="olist_primary",
    ) == ()

    assert retrieve_semantic_evidence(
        "revenue",
        (item,),
        database_id="olist_primary",
    ) == ()


@pytest.mark.parametrize("top_k", [0, -1, True])
def test_top_k_must_be_a_positive_integer(top_k):
    with pytest.raises(ValueError, match="top_k"):
        retrieve_semantic_evidence(
            "orders",
            (),
            database_id="olist_primary",
            top_k=top_k,
        )