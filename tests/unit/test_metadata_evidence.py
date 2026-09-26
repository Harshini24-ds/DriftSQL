"""Unit tests for curated metadata evidence loading.

These tests use temporary JSON inputs and do not connect to PostgreSQL.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts.evidence import EvidenceType
from src.evidence.metadata import build_metadata_evidence, load_metadata_catalog


def write_catalog_files(
    directory: Path,
    *,
    status: str = "DRAFT_FOR_TEAM_REVIEW",
    catalog_schema_hash: str = "schema-hash-v1",
    snapshot_schema_hash: str = "schema-hash-v1",
    target: str = "sales.orders.total",
) -> tuple[Path, Path]:
    """Write a small generic catalog and matching structural snapshot."""
    metadata_path = directory / "metadata.json"
    snapshot_path = directory / "schema_snapshot.json"

    catalog: dict[str, Any] = {
        "format_version": 1,
        "database_id": "sales_database",
        "schema": "sales",
        "schema_hash": catalog_schema_hash,
        "metadata_version": "sales-metadata-v1",
        "status": status,
        "source": "team_curated_metadata",
        "access_label": "internal",
        "records": [
            {
                "target": target,
                "scope": "sales.orders",
                "description": "Recorded total for an order.",
                "aliases": ["order amount"],
                "business_terms": ["order total"],
                "unit": "USD",
                "provenance_id": "PROV-META-ORDER-TOTAL",
            }
        ],
    }
    snapshot: dict[str, Any] = {
        "database_id": "sales_database",
        "schema": "sales",
        "schema_hash": snapshot_schema_hash,
        "tables": [
            {
                "table_name": "orders",
                "columns": [{"column_name": "total"}],
            }
        ],
    }

    metadata_path.write_text(
        json.dumps(catalog, indent=2),
        encoding="utf-8",
    )
    snapshot_path.write_text(
        json.dumps(snapshot, indent=2),
        encoding="utf-8",
    )
    return metadata_path, snapshot_path


def test_draft_catalog_is_validated_but_not_used_as_runtime_evidence(
    tmp_path: Path,
) -> None:
    metadata_path, snapshot_path = write_catalog_files(tmp_path)

    catalog = load_metadata_catalog(metadata_path, snapshot_path)
    items = build_metadata_evidence(metadata_path, snapshot_path)

    assert catalog["status"] == "DRAFT_FOR_TEAM_REVIEW"
    assert len(catalog["records"]) == 1
    assert items == ()


def test_approved_catalog_builds_normalized_evidence(tmp_path: Path) -> None:
    metadata_path, snapshot_path = write_catalog_files(
        tmp_path,
        status="APPROVED",
    )

    items = build_metadata_evidence(metadata_path, snapshot_path)

    assert len(items) == 1
    item = items[0]
    assert item.database_id == "sales_database"
    assert item.type is EvidenceType.METADATA
    assert item.key == "sales.orders.total"
    assert item.value == {
        "target": "sales.orders.total",
        "description": "Recorded total for an order.",
        "aliases": ["order amount"],
        "business_terms": ["order total"],
        "unit": "USD",
    }
    assert item.provenance_id == "PROV-META-ORDER-TOTAL"
    assert len(item.content_hash) == 64


def test_catalog_rejects_schema_hash_mismatch(tmp_path: Path) -> None:
    metadata_path, snapshot_path = write_catalog_files(
        tmp_path,
        catalog_schema_hash="stale-schema-hash",
    )

    with pytest.raises(ValueError, match="schema_hash"):
        load_metadata_catalog(metadata_path, snapshot_path)


def test_catalog_rejects_target_missing_from_schema_snapshot(
    tmp_path: Path,
) -> None:
    metadata_path, snapshot_path = write_catalog_files(
        tmp_path,
        target="sales.orders.unknown_column",
    )

    with pytest.raises(ValueError, match="not present in the schema snapshot"):
        load_metadata_catalog(metadata_path, snapshot_path)
