from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from src.retrieval import drift
from src.retrieval.drift import (
    SchemaFreshnessState,
    assess_schema_drift,
    prepare_schema_refresh,
)


@pytest.fixture
def schema_snapshot():
    return {
        "database_id": "olist_primary",
        "schema": "olist_raw",
        "tables": [
            {
                "table_name": "orders",
                "columns": [
                    {"column_name": "order_id", "data_type": "text"},
                ],
            }
        ],
        "primary_keys": [],
        "foreign_keys": [],
        "strict_relationships": [],
        "non_strict_lookups": [],
        "schema_hash": "schema-v1",
        "created_at": "2026-10-21T00:00:00+00:00",
    }


def test_identical_schema_snapshots_are_current(schema_snapshot):
    assessment = assess_schema_drift(schema_snapshot, deepcopy(schema_snapshot))

    assert assessment.state == SchemaFreshnessState.CURRENT
    assert assessment.changed_components == ()


@pytest.mark.parametrize(
    ("component", "changed_snapshot_component"),
    [
        (
            "tables",
            [
                {
                    "table_name": "orders",
                    "columns": [
                        {"column_name": "renamed_order_id", "data_type": "text"},
                    ],
                }
            ],
        ),
        (
            "tables",
            [
                {
                    "table_name": "orders",
                    "columns": [
                        {"column_name": "order_id", "data_type": "uuid"},
                    ],
                }
            ],
        ),
        (
            "foreign_keys",
            [
                {
                    "child_table": "orders",
                    "child_columns": ["customer_id"],
                    "parent_schema": "olist_raw",
                    "parent_table": "customers",
                    "parent_columns": ["customer_id"],
                    "validated": True,
                }
            ],
        ),
    ],
)
def test_structural_changes_are_stale_recoverable(
    schema_snapshot,
    component,
    changed_snapshot_component,
):
    live_snapshot = deepcopy(schema_snapshot)
    live_snapshot[component] = changed_snapshot_component
    live_snapshot["schema_hash"] = "schema-v2"

    assessment = assess_schema_drift(schema_snapshot, live_snapshot)

    assert assessment.state == SchemaFreshnessState.STALE_RECOVERABLE
    assert component in assessment.changed_components


def _refresh_catalogs(tmp_path, *, metadata_target="olist_raw.orders.order_id"):
    metadata_path = tmp_path / "metadata.json"
    glossary_path = tmp_path / "glossary.json"
    metadata_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "database_id": "olist_primary",
                "schema": "olist_raw",
                "schema_hash": "schema-v1",
                "metadata_version": "metadata-v1",
                "status": "APPROVED",
                "source": "team_curated_metadata",
                "access_label": "internal",
                "records": [
                    {
                        "target": metadata_target,
                        "scope": "olist_raw.orders",
                        "description": "Order identifier metadata.",
                        "aliases": ["order id"],
                        "business_terms": ["order identifier"],
                        "unit": None,
                        "provenance_id": "PROV-TEST-METADATA",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    glossary_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "database_id": "olist_primary",
                "schema": "olist_raw",
                "schema_hash": "schema-v1",
                "glossary_version": "glossary-v1",
                "source": "team_curated_glossary",
                "access_label": "internal",
                "records": [
                    {
                        "term": "order ID",
                        "definition": "Identifier for an order.",
                        "target": metadata_target,
                        "aliases": ["order identifier"],
                        "definition_version": "definition-v1",
                        "status": "APPROVED",
                        "effective_from": "2026-01-01",
                        "effective_to": None,
                        "provenance_id": "PROV-TEST-GLOSSARY",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return metadata_path, glossary_path


def test_prepare_schema_refresh_writes_immutable_snapshot_and_review_drafts(
    tmp_path,
    schema_snapshot,
    monkeypatch,
):
    old_snapshot = deepcopy(schema_snapshot)
    live_snapshot = deepcopy(schema_snapshot)
    live_snapshot["tables"][0]["columns"].append(
        {"column_name": "order_status", "data_type": "text"}
    )
    live_snapshot["schema_hash"] = "schema-v2"
    live_snapshot["created_at"] = "2026-10-22T00:00:00+00:00"
    active_path = tmp_path / "active-schema.json"
    active_path.write_text("unchanged active snapshot", encoding="utf-8")
    metadata_path, glossary_path = _refresh_catalogs(tmp_path)
    adapter = SimpleNamespace(
        database_id="olist_primary",
        get_schema_snapshot=lambda: deepcopy(live_snapshot),
    )
    monkeypatch.setattr(drift, "load_schema_snapshot", lambda *_: old_snapshot)

    bundle = prepare_schema_refresh(
        adapter,
        schema_snapshot_path=active_path,
        metadata_path=metadata_path,
        glossary_path=glossary_path,
        refresh_root=tmp_path / "history",
    )

    assert bundle.assessment.state == SchemaFreshnessState.STALE_RECOVERABLE
    assert bundle.schema_snapshot_path.exists()
    assert active_path.read_text(encoding="utf-8") == "unchanged active snapshot"
    assert bundle.metadata_draft_path is not None
    assert bundle.glossary_draft_path is not None
    metadata_draft = json.loads(bundle.metadata_draft_path.read_text(encoding="utf-8"))
    glossary_draft = json.loads(bundle.glossary_draft_path.read_text(encoding="utf-8"))
    assert metadata_draft["schema_hash"] == "schema-v2"
    assert metadata_draft["status"] == "DRAFT_FOR_TEAM_REVIEW"
    assert glossary_draft["schema_hash"] == "schema-v2"
    assert glossary_draft["records"][0]["status"] == "DRAFT_FOR_TEAM_REVIEW"
    assert glossary_draft["records"][0]["effective_from"] is None
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["status"] == "APPROVED"
    assert json.loads(glossary_path.read_text(encoding="utf-8"))["records"][0]["status"] == "APPROVED"

    from src.evidence.glossary import load_glossary_catalog
    from src.evidence.metadata import load_metadata_catalog

    loaded_metadata = load_metadata_catalog(
        bundle.metadata_draft_path,
        bundle.schema_snapshot_path,
    )
    loaded_glossary = load_glossary_catalog(
        bundle.glossary_draft_path,
        bundle.metadata_draft_path,
        bundle.schema_snapshot_path,
    )
    assert loaded_metadata["status"] == "DRAFT_FOR_TEAM_REVIEW"
    assert loaded_glossary["records"][0]["status"] == "DRAFT_FOR_TEAM_REVIEW"


def test_prepare_schema_refresh_reports_removed_metadata_targets(
    tmp_path,
    schema_snapshot,
    monkeypatch,
):
    live_snapshot = deepcopy(schema_snapshot)
    live_snapshot["tables"][0]["columns"] = []
    live_snapshot["schema_hash"] = "schema-v2"
    live_snapshot["created_at"] = "2026-10-22T00:00:00+00:00"
    metadata_path, glossary_path = _refresh_catalogs(tmp_path)
    adapter = SimpleNamespace(
        database_id="olist_primary",
        get_schema_snapshot=lambda: deepcopy(live_snapshot),
    )
    monkeypatch.setattr(drift, "load_schema_snapshot", lambda *_: schema_snapshot)

    bundle = prepare_schema_refresh(
        adapter,
        metadata_path=metadata_path,
        glossary_path=glossary_path,
        refresh_root=tmp_path / "history",
    )

    assert bundle.schema_snapshot_path.exists()
    assert bundle.metadata_draft_path is None
    assert bundle.glossary_draft_path is None
    assert any("olist_raw.orders.order_id" in item for item in bundle.review_required)


def test_prepare_schema_refresh_refuses_to_overwrite_existing_hash(
    tmp_path,
    schema_snapshot,
    monkeypatch,
):
    live_snapshot = deepcopy(schema_snapshot)
    live_snapshot["tables"][0]["columns"].append(
        {"column_name": "order_status", "data_type": "text"}
    )
    live_snapshot["schema_hash"] = "schema-v2"
    live_snapshot["created_at"] = "2026-10-22T00:00:00+00:00"
    metadata_path, glossary_path = _refresh_catalogs(tmp_path)
    adapter = SimpleNamespace(
        database_id="olist_primary",
        get_schema_snapshot=lambda: deepcopy(live_snapshot),
    )
    monkeypatch.setattr(drift, "load_schema_snapshot", lambda *_: schema_snapshot)
    options = {
        "schema_snapshot_path": tmp_path / "active.json",
        "metadata_path": metadata_path,
        "glossary_path": glossary_path,
        "refresh_root": tmp_path / "history",
    }
    prepare_schema_refresh(adapter, **options)

    conflicting_snapshot = deepcopy(live_snapshot)
    conflicting_snapshot["tables"][0]["columns"][1]["data_type"] = "integer"
    adapter.get_schema_snapshot = lambda: conflicting_snapshot

    with pytest.raises(ValueError, match="Refusing to overwrite"):
        prepare_schema_refresh(adapter, **options)
