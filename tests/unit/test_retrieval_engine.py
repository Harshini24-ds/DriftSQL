from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.contracts.evidence import EvidenceItem, EvidenceType
from src.query_intelligence.models import (
    AccessContext,
    QueryFilter,
    QueryIntent,
    QueryRequest,
)
from src.query_intelligence.pipeline import analyze_query_intent
from src.retrieval import engine
from src.query_intelligence.qim import build_query_intent
from src.evidence.schema import load_schema_snapshot
from src.retrieval.drift import SchemaDriftDetectedError


def make_evidence(
    evidence_type: EvidenceType,
    target: str,
    *,
    aliases: list[str] | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"EV-{evidence_type.value}-{target}",
        database_id="olist_primary",
        type=evidence_type,
        key=target,
        value={"target": target, "aliases": aliases or []},
        source="test",
        source_version="v1",
        retrieved_at=datetime.now(timezone.utc),
        authority_level=70,
        access_label="internal",
        scope=target,
        provenance_id=f"PROV-{target}",
        content_hash="test-hash",
    )


def make_access_context(
    role: str = "driftsql_olist_reader",
) -> AccessContext:
    return AccessContext(
        principal_id="unit-test-user",
        database_role=role,
    )


def make_adapter(
    role: str = "driftsql_olist_reader",
    denied_columns: set[tuple[str, str]] | None = None,
) -> SimpleNamespace:
    denied = denied_columns or set()

    def can_select(table: str, column: str | None = None) -> bool:
        if column is None:
            return True
        return (table, column) not in denied

    return SimpleNamespace(
        database_id="olist_primary",
        database_role=role,
        schema="olist_raw",
        can_select=can_select,
        get_schema_snapshot=load_schema_snapshot,
    )


def test_retrieve_for_analysis_expands_alias_and_finds_evidence(monkeypatch):
    table = "olist_raw.orders"
    revenue_field = "olist_raw.order_items.price"

    schema_items = (
        make_evidence(EvidenceType.SCHEMA, table),
        make_evidence(EvidenceType.SCHEMA, revenue_field),
    )
    metadata_items = (
        make_evidence(
            EvidenceType.METADATA,
            revenue_field,
            aliases=["revenue"],
        ),
    )
    glossary_items = (
        make_evidence(
            EvidenceType.GLOSSARY,
            revenue_field,
            aliases=["revenue"],
        ),
    )

    monkeypatch.setattr(
        engine,
        "build_schema_evidence",
        lambda *_: schema_items,
    )
    monkeypatch.setattr(
        engine,
        "build_metadata_evidence",
        lambda *_: metadata_items,
    )
    monkeypatch.setattr(
        engine,
        "build_glossary_evidence",
        lambda *_, **__: glossary_items,
    )

    analysis = analyze_query_intent(
        QueryIntent(
            request_id="test-request",
            database_id="olist_primary",
            operation="aggregate",
            entities=(table,),
            measures=("revenue",),
        ),
        access_context=make_access_context(),
    )
    permission = make_evidence(EvidenceType.PERMISSION, table)
    field_permission = make_evidence(
        EvidenceType.PERMISSION,
        revenue_field,
    )

    result = engine.retrieve_for_analysis(
        analysis,
        adapter=make_adapter(),
        additional_evidence=(permission, field_permission),
    )

    assert result.complete
    assert result.missing_requirement_ids == ()
    assert {item.type for item in result.evidence_items} >= {
        EvidenceType.SCHEMA,
        EvidenceType.METADATA,
        EvidenceType.GLOSSARY,
        EvidenceType.PERMISSION,
    }


def test_retrieve_for_analysis_includes_document_for_multi_table_query(
    monkeypatch,
):
    orders = "olist_raw.orders"
    order_items = "olist_raw.order_items"
    join_target = f"{orders} -> {order_items}"

    schema_items = (
        make_evidence(EvidenceType.SCHEMA, orders),
        make_evidence(EvidenceType.SCHEMA, order_items),
    )
    permission_items = (
        make_evidence(EvidenceType.PERMISSION, orders),
        make_evidence(EvidenceType.PERMISSION, order_items),
        make_evidence(EvidenceType.PERMISSION, f"{orders}.order_id"),
        make_evidence(
            EvidenceType.PERMISSION,
            f"{order_items}.order_id",
        ),
    )
    relationship_item = make_evidence(
        EvidenceType.RELATIONSHIP,
        join_target,
    )

    monkeypatch.setattr(
        engine,
        "build_schema_evidence",
        lambda *_: schema_items,
    )
    monkeypatch.setattr(
        engine,
        "build_metadata_evidence",
        lambda *_: (),
    )
    monkeypatch.setattr(
        engine,
        "build_glossary_evidence",
        lambda *_, **__: (),
    )

    analysis = analyze_query_intent(
        QueryIntent(
            request_id="document-retrieval-test",
            database_id="olist_primary",
            operation="aggregate",
            entities=(orders, order_items),
            measures=("count",),
        ),
        access_context=make_access_context(),
    )

    result = engine.retrieve_for_analysis(
        analysis,
        adapter=make_adapter(),
        additional_evidence=(*permission_items, relationship_item),
    )

    assert result.complete
    assert "EV-DOC-DOC-OLIST-JOIN-POLICY-V1" in {
        item.evidence_id for item in result.evidence_items
    }


def test_retrieval_rejects_missing_access_context():
    analysis = analyze_query_intent(
        QueryIntent(
            request_id="missing-context-test",
            database_id="olist_primary",
            operation="aggregate",
            entities=("olist_raw.orders",),
            measures=("count",),
        )
    )

    with pytest.raises(PermissionError, match="Trusted access context"):
        engine.retrieve_for_analysis(
            analysis,
            adapter=make_adapter(),
        )


def test_retrieval_rejects_adapter_role_mismatch():
    analysis = analyze_query_intent(
        QueryIntent(
            request_id="role-mismatch-test",
            database_id="olist_primary",
            operation="aggregate",
            entities=("olist_raw.orders",),
            measures=("count",),
        ),
        access_context=make_access_context("driftsql_orders_limited"),
    )

    with pytest.raises(PermissionError, match="does not match"):
        engine.retrieve_for_analysis(
            analysis,
            adapter=make_adapter("driftsql_olist_reader"),
        )


def test_retrieval_rejects_stale_live_schema_before_loading_evidence():
    analysis = analyze_query_intent(
        QueryIntent(
            request_id="stale-schema-test",
            database_id="olist_primary",
            operation="aggregate",
            entities=("olist_raw.orders",),
            measures=("count",),
        ),
        access_context=make_access_context(),
    )
    adapter = make_adapter()
    live_snapshot = load_schema_snapshot()
    live_snapshot["schema_hash"] = "changed-live-schema-hash"
    adapter.get_schema_snapshot = lambda: live_snapshot

    with pytest.raises(SchemaDriftDetectedError, match="STALE_RECOVERABLE"):
        engine.retrieve_for_analysis(analysis, adapter=adapter)


def test_retrieval_rejects_a_required_column_the_role_cannot_read(
    monkeypatch,
):
    table = "olist_raw.orders"
    restricted_column = "olist_raw.orders.customer_id"

    schema_items = (
        make_evidence(EvidenceType.SCHEMA, table),
        make_evidence(EvidenceType.SCHEMA, restricted_column),
    )
    monkeypatch.setattr(
        engine,
        "build_schema_evidence",
        lambda *_: schema_items,
    )
    monkeypatch.setattr(
        engine,
        "build_metadata_evidence",
        lambda *_: (),
    )
    monkeypatch.setattr(
        engine,
        "build_glossary_evidence",
        lambda *_, **__: (),
    )

    analysis = analyze_query_intent(
        QueryIntent(
            request_id="restricted-column-test",
            database_id="olist_primary",
            operation="select",
            entities=(table,),
            measures=(restricted_column,),
        ),
        access_context=make_access_context("driftsql_orders_limited"),
    )

    with pytest.raises(PermissionError, match="cannot read required field"):
        engine.retrieve_for_analysis(
            analysis,
            adapter=make_adapter(
                "driftsql_orders_limited",
                denied_columns={("olist_raw.orders", "customer_id")},
            ),
            additional_evidence=(
                make_evidence(EvidenceType.PERMISSION, table),
            ),
        )


def test_qim_output_flows_into_direct_retrieval(monkeypatch):
    table = "olist_raw.orders"
    schema_item = make_evidence(EvidenceType.SCHEMA, table)
    permission_item = make_evidence(EvidenceType.PERMISSION, table)

    monkeypatch.setattr(
        engine,
        "build_schema_evidence",
        lambda *_: (schema_item,),
    )
    monkeypatch.setattr(
        engine,
        "build_metadata_evidence",
        lambda *_: (),
    )
    monkeypatch.setattr(
        engine,
        "build_glossary_evidence",
        lambda *_, **__: (),
    )
    monkeypatch.setattr(
        engine,
        "build_document_evidence",
        lambda *_, **__: (),
    )

    request = QueryRequest(
        request_id="qim-retrieval-integration",
        database_id="olist_primary",
        access_context=make_access_context(),
        question="How many orders are in Olist?",
    )
    intent = build_query_intent(request)
    analysis = analyze_query_intent(
        intent,
        access_context=request.access_context,
    )

    result = engine.retrieve_for_analysis(
        analysis,
        adapter=make_adapter(),
        additional_evidence=(permission_item,),
    )

    assert result.complete
    assert result.missing_requirement_ids == ()
    assert {item.type for item in result.evidence_items} >= {
        EvidenceType.SCHEMA,
        EvidenceType.PERMISSION,
    }


def test_semantic_supporting_evidence_is_added_to_complete_retrieval(
    monkeypatch,
):
    table = "olist_raw.orders"
    schema_item = make_evidence(EvidenceType.SCHEMA, table)
    permission_item = make_evidence(EvidenceType.PERMISSION, table)
    semantic_item = make_evidence(
        EvidenceType.DOCUMENT,
        "olist_raw order guidance",
    )

    monkeypatch.setattr(
        engine,
        "build_schema_evidence",
        lambda *_: (schema_item,),
    )
    monkeypatch.setattr(
        engine,
        "build_metadata_evidence",
        lambda *_: (),
    )
    monkeypatch.setattr(
        engine,
        "build_glossary_evidence",
        lambda *_, **__: (),
    )
    monkeypatch.setattr(
        engine,
        "build_document_evidence",
        lambda *_, **__: (),
    )
    monkeypatch.setattr(
        engine,
        "retrieve_semantic_evidence",
        lambda *_, **__: (
            SimpleNamespace(evidence_item=semantic_item, score=1.0),
        ),
    )

    analysis = analyze_query_intent(
        QueryIntent(
            request_id="semantic-retrieval-integration",
            database_id="olist_primary",
            operation="aggregate",
            entities=(table,),
            measures=("count",),
        ),
        access_context=make_access_context(),
    )

    result = engine.retrieve_for_analysis(
        analysis,
        adapter=make_adapter(),
        additional_evidence=(permission_item,),
    )

    result_ids = {item.evidence_id for item in result.evidence_items}
    assert result.complete
    assert schema_item.evidence_id in result_ids
    assert permission_item.evidence_id in result_ids
    assert semantic_item.evidence_id in result_ids


def test_filter_retrieval_includes_value_evidence(monkeypatch):
    table = "olist_raw.orders"
    field = "olist_raw.orders.order_status"

    schema_items = (
        make_evidence(EvidenceType.SCHEMA, table),
        make_evidence(EvidenceType.SCHEMA, field),
    )
    permission_items = (
        make_evidence(EvidenceType.PERMISSION, table),
        make_evidence(EvidenceType.PERMISSION, field),
    )
    value_item = make_evidence(EvidenceType.VALUE, field)

    monkeypatch.setattr(
        engine,
        "build_schema_evidence",
        lambda *_: schema_items,
    )
    monkeypatch.setattr(
        engine,
        "build_metadata_evidence",
        lambda *_: (),
    )
    monkeypatch.setattr(
        engine,
        "build_glossary_evidence",
        lambda *_, **__: (),
    )
    monkeypatch.setattr(
        engine,
        "build_document_evidence",
        lambda *_, **__: (),
    )
    monkeypatch.setattr(
        engine,
        "build_value_evidence",
        lambda *_, **__: (value_item,),
    )

    analysis = analyze_query_intent(
        QueryIntent(
            request_id="value-filter-test",
            database_id="olist_primary",
            operation="filter",
            entities=(table,),
            filters=(
                QueryFilter(
                    field=field,
                    operator="=",
                    value="delivered",
                ),
            ),
        ),
        access_context=make_access_context(),
    )

    result = engine.retrieve_for_analysis(
        analysis,
        adapter=make_adapter(),
        additional_evidence=permission_items,
    )

    assert result.complete
    assert value_item in result.evidence_items

def test_filter_retrieval_loads_approved_value_profile(monkeypatch):
    table = "olist_raw.orders"
    field = "olist_raw.orders.order_status"

    monkeypatch.setattr(
        engine,
        "build_schema_evidence",
        lambda *_: (
            make_evidence(EvidenceType.SCHEMA, table),
            make_evidence(EvidenceType.SCHEMA, field),
        ),
    )
    monkeypatch.setattr(engine, "build_metadata_evidence", lambda *_: ())
    monkeypatch.setattr(
        engine,
        "build_glossary_evidence",
        lambda *_, **__: (),
    )
    monkeypatch.setattr(
        engine,
        "build_document_evidence",
        lambda *_, **__: (),
    )

    analysis = analyze_query_intent(
        QueryIntent(
            request_id="real-value-profile-test",
            database_id="olist_primary",
            operation="filter",
            entities=(table,),
            filters=(
                QueryFilter(
                    field=field,
                    operator="=",
                    value="delivered",
                ),
            ),
        ),
        access_context=make_access_context(),
    )

    result = engine.retrieve_for_analysis(
        analysis,
        adapter=make_adapter(),
        value_profiles_path="evidence/value_profiles.json",
        additional_evidence=(
            make_evidence(EvidenceType.PERMISSION, table),
            make_evidence(EvidenceType.PERMISSION, field),
        ),
    )

    value_items = [
        item for item in result.evidence_items
        if item.type == EvidenceType.VALUE
    ]

    assert result.complete
    assert len(value_items) == 1
    assert value_items[0].value["values"] == [
        "approved",
        "canceled",
        "created",
        "delivered",
        "invoiced",
        "processing",
        "shipped",
        "unavailable",
    ]


def test_retrieval_builds_permission_evidence_after_role_check(monkeypatch):
    table = "olist_raw.orders"
    schema_item = make_evidence(EvidenceType.SCHEMA, table)

    monkeypatch.setattr(
        engine,
        "build_schema_evidence",
        lambda *_: (schema_item,),
    )
    monkeypatch.setattr(engine, "build_metadata_evidence", lambda *_: ())
    monkeypatch.setattr(
        engine,
        "build_glossary_evidence",
        lambda *_, **__: (),
    )
    monkeypatch.setattr(
        engine,
        "build_document_evidence",
        lambda *_, **__: (),
    )

    analysis = analyze_query_intent(
        QueryIntent(
            request_id="live-permission-evidence-test",
            database_id="olist_primary",
            operation="aggregate",
            entities=(table,),
            measures=("count",),
        ),
        access_context=make_access_context(),
    )

    result = engine.retrieve_for_analysis(
        analysis,
        adapter=make_adapter(),
    )

    permission_items = [
        item for item in result.evidence_items
        if item.type == EvidenceType.PERMISSION
    ]

    assert result.complete
    assert len(permission_items) == 1
    assert permission_items[0].key == table
    assert permission_items[0].value["database_role"] == "driftsql_olist_reader"
    assert permission_items[0].value["decision"] == "ALLOW"
    assert permission_items[0].source == "active_postgresql_role"
