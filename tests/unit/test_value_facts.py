import json

import pytest

from src.contracts.evidence import EvidenceType
from src.evidence import value_facts


SCHEMA_SNAPSHOT = {
    "database_id": "olist_primary",
    "schema": "olist_raw",
    "schema_hash": "test-schema-hash",
    "tables": [
        {
            "table_name": "orders",
            "columns": [{"column_name": "order_status"}],
        }
    ],
}


def make_catalog(status="APPROVED", record_status="APPROVED"):
    return {
        "format_version": 1,
        "database_id": "olist_primary",
        "schema": "olist_raw",
        "schema_hash": "test-schema-hash",
        "profile_version": "test-v1",
        "source": "team_approved_value_profile",
        "access_label": "internal",
        "status": status,
        "records": [
            {
                "target": "olist_raw.orders.order_status",
                "values": ["delivered", "shipped"],
                "coverage": "COMPLETE",
                "status": record_status,
                "provenance_id": "PROV-TEST-ORDER-STATUS",
            }
        ],
    }


def write_catalog(tmp_path, catalog):
    path = tmp_path / "value_profiles.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    return path


def test_build_value_evidence_emits_approved_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(
        value_facts,
        "load_schema_snapshot",
        lambda *_: SCHEMA_SNAPSHOT,
    )
    path = write_catalog(tmp_path, make_catalog())

    items = value_facts.build_value_evidence(path)

    assert len(items) == 1
    assert items[0].type == EvidenceType.VALUE
    assert items[0].key == "olist_raw.orders.order_status"
    assert items[0].value["values"] == ["delivered", "shipped"]
    assert items[0].value["coverage"] == "COMPLETE"


def test_build_value_evidence_ignores_draft_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(
        value_facts,
        "load_schema_snapshot",
        lambda *_: SCHEMA_SNAPSHOT,
    )
    path = write_catalog(
        tmp_path,
        make_catalog(status="DRAFT_FOR_TEAM_REVIEW",
                     record_status="DRAFT_FOR_TEAM_REVIEW"),
    )

    assert value_facts.build_value_evidence(path) == ()


def test_value_profile_rejects_target_missing_from_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(
        value_facts,
        "load_schema_snapshot",
        lambda *_: SCHEMA_SNAPSHOT,
    )
    catalog = make_catalog()
    catalog["records"][0]["target"] = "olist_raw.orders.customer_id"
    path = write_catalog(tmp_path, catalog)

    with pytest.raises(ValueError, match="not a schema column"):
        value_facts.build_value_evidence(path)