from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.contracts.evidence import EvidenceItem, EvidenceType
from src.query_intelligence.models import AccessContext, QueryIntent
from src.query_intelligence.pipeline import analyze_query_intent
from src.retrieval import snapshot


def make_analysis(request_id: str):
    return analyze_query_intent(
        QueryIntent(
            request_id=request_id,
            database_id="olist_primary",
            operation="aggregate",
            entities=("olist_raw.orders",),
            measures=("count",),
        ),
        access_context=AccessContext(
            principal_id="snapshot-unit-test",
            database_role="driftsql_olist_reader",
        ),
    )


def test_build_evidence_snapshot_preserves_request_and_evidence(monkeypatch):
    schema_hash = "test-schema-hash"
    evidence = EvidenceItem(
        evidence_id="EV-DOC-orders-guide",
        database_id="olist_primary",
        type=EvidenceType.DOCUMENT,
        key="olist_raw.orders",
        value={"description": "Orders table guide"},
        source="test",
        source_version="v1",
        retrieved_at=datetime.now(timezone.utc),
        authority_level=70,
        access_label="internal",
        scope="olist_raw.orders",
        provenance_id="PROV-orders-guide",
        content_hash="test-content-hash",
    )

    monkeypatch.setattr(
        snapshot,
        "load_schema_snapshot",
        lambda *_: {
            "database_id": "olist_primary",
            "schema_hash": schema_hash,
        },
    )

    analysis = make_analysis("snapshot-test")
    retrieval = SimpleNamespace(
        database_id="olist_primary",
        complete=True,
        evidence_items=(evidence,),
    )

    result = snapshot.build_evidence_snapshot(
        analysis,
        retrieval,
        freshness_policy_version="v1",
    )

    assert result.request_id == "snapshot-test"
    assert result.database_id == "olist_primary"
    assert result.schema_hash == schema_hash
    assert result.evidence_items == (evidence,)
    assert result.freshness_policy_version == "v1"
    assert result.retrieval_metadata == {
        "method": "direct_exact_match",
        "obligations_total": len(analysis.required_evidence_obligations),
        "evidence_items_returned": 1,
    }


def test_build_evidence_snapshot_rejects_incomplete_retrieval():
    analysis = make_analysis("incomplete-snapshot-test")
    retrieval = SimpleNamespace(
        database_id="olist_primary",
        complete=False,
        evidence_items=(),
    )

    with pytest.raises(ValueError, match="required evidence is missing"):
        snapshot.build_evidence_snapshot(
            analysis,
            retrieval,
            freshness_policy_version="v1",
        )

def test_snapshot_rejects_unsupported_query():
    analysis = analyze_query_intent(
        QueryIntent(
            request_id="rejected-snapshot-test",
            database_id="olist_primary",
            operation="delete",
            entities=("olist_raw.orders",),
        ),
        access_context=AccessContext(
            principal_id="snapshot-unit-test",
            database_role="driftsql_olist_reader",
        ),
    )
    retrieval = SimpleNamespace(
        database_id="olist_primary",
        complete=True,
        evidence_items=(),
    )

    with pytest.raises(ValueError, match="rejected query"):
        snapshot.build_evidence_snapshot(
            analysis,
            retrieval,
            freshness_policy_version="v1",
        )


def test_snapshot_rejects_unresolved_query_terms():
    analysis = analyze_query_intent(
        QueryIntent(
            request_id="unresolved-snapshot-test",
            database_id="olist_primary",
            operation="aggregate",
            entities=("olist_raw.orders",),
            unresolved_terms=("measure",),
        ),
        access_context=AccessContext(
            principal_id="snapshot-unit-test",
            database_role="driftsql_olist_reader",
        ),
    )
    retrieval = SimpleNamespace(
        database_id="olist_primary",
        complete=True,
        evidence_items=(),
    )

    with pytest.raises(ValueError, match="query terms are unresolved"):
        snapshot.build_evidence_snapshot(
            analysis,
            retrieval,
            freshness_policy_version="v1",
        )


def test_snapshot_rejects_retrieval_from_different_database():
    analysis = make_analysis("different-retrieval-database-test")
    retrieval = SimpleNamespace(
        database_id="another_database",
        complete=True,
        evidence_items=(),
    )

    with pytest.raises(ValueError, match="different databases"):
        snapshot.build_evidence_snapshot(
            analysis,
            retrieval,
            freshness_policy_version="v1",
        )


def test_snapshot_rejects_schema_snapshot_from_different_database(monkeypatch):
    analysis = make_analysis("different-schema-database-test")
    retrieval = SimpleNamespace(
        database_id="olist_primary",
        complete=True,
        evidence_items=(),
    )

    monkeypatch.setattr(
        snapshot,
        "load_schema_snapshot",
        lambda *_: {
            "database_id": "another_database",
            "schema_hash": "test-schema-hash",
        },
    )

    with pytest.raises(ValueError, match="Schema snapshot belongs to a different database"):
        snapshot.build_evidence_snapshot(
            analysis,
            retrieval,
            freshness_policy_version="v1",
        )


def test_snapshot_rejects_stale_schema_evidence(monkeypatch):
    current_schema_hash = "current-schema-hash"
    analysis = make_analysis("stale-schema-evidence-test")
    stale_schema_item = EvidenceItem(
        evidence_id="EV-SCHEMA-stale",
        database_id="olist_primary",
        type=EvidenceType.SCHEMA,
        key="olist_raw.orders",
        value={"target": "olist_raw.orders"},
        source="test",
        source_version="old-schema-hash",
        retrieved_at=datetime.now(timezone.utc),
        authority_level=70,
        access_label="internal",
        scope="olist_raw.orders",
        provenance_id="PROV-stale-schema",
        content_hash="stale-schema-content-hash",
    )
    retrieval = SimpleNamespace(
        database_id="olist_primary",
        complete=True,
        evidence_items=(stale_schema_item,),
    )

    monkeypatch.setattr(
        snapshot,
        "load_schema_snapshot",
        lambda *_: {
            "database_id": "olist_primary",
            "schema_hash": current_schema_hash,
        },
    )

    with pytest.raises(ValueError, match="stale schema hash"):
        snapshot.build_evidence_snapshot(
            analysis,
            retrieval,
            freshness_policy_version="v1",
        )