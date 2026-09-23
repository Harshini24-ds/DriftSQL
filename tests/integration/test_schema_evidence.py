"""Integration-style tests for DriftSQL schema evidence.

EVID-002

These tests validate that the saved verified Olist schema snapshot can be
converted into typed EvidenceItem objects without touching PostgreSQL.

Expected evidence counts from the verified Olist DATA-001/DATA-003 facts:
- 9 table evidence items
- 52 column evidence items
- 6 strict relationship evidence items
- 3 non-strict lookup relationship evidence items
Total: 70 evidence items
"""

from __future__ import annotations

import json
from pathlib import Path

from src.contracts.evidence import EvidenceType
from src.evidence.schema import build_schema_evidence, load_schema_snapshot


SNAPSHOT_PATH = Path("artifacts/schema_snapshot.json")


def test_saved_schema_snapshot_loads() -> None:
    """The exported Olist snapshot should be present and structurally valid."""
    snapshot = load_schema_snapshot(SNAPSHOT_PATH)

    assert snapshot["database_id"] == "olist_primary"
    assert snapshot["schema"] == "olist_raw"
    assert len(snapshot["tables"]) == 9
    assert len(snapshot["strict_relationships"]) == 6
    assert len(snapshot["non_strict_lookups"]) == 3
    assert len(snapshot["schema_hash"]) == 64


def test_schema_evidence_builds_expected_total() -> None:
    """The verified snapshot should produce exactly 70 evidence items."""
    items = build_schema_evidence(SNAPSHOT_PATH)

    assert len(items) == 70


def test_schema_evidence_type_counts() -> None:
    """Schema and relationship evidence counts should match verified facts."""
    items = build_schema_evidence(SNAPSHOT_PATH)

    schema_items = [item for item in items if item.type is EvidenceType.SCHEMA]
    relationship_items = [
        item for item in items if item.type is EvidenceType.RELATIONSHIP
    ]

    # 9 tables + 52 columns
    assert len(schema_items) == 61

    # 6 strict relationships + 3 non-strict lookups
    assert len(relationship_items) == 9


def test_all_schema_evidence_uses_olist_primary() -> None:
    """No schema evidence may silently belong to another database."""
    items = build_schema_evidence(SNAPSHOT_PATH)

    assert all(item.database_id == "olist_primary" for item in items)


def test_all_schema_evidence_uses_saved_schema_hash() -> None:
    """Every structural item should be versioned by the saved schema hash."""
    snapshot = load_schema_snapshot(SNAPSHOT_PATH)
    items = build_schema_evidence(SNAPSHOT_PATH)

    expected_hash = snapshot["schema_hash"]

    assert all(item.source_version == expected_hash for item in items)


def test_strict_and_lookup_relationships_remain_separate() -> None:
    """The builder must preserve strict-vs-lookup relationship classification."""
    items = build_schema_evidence(SNAPSHOT_PATH)

    strict_items = [
        item
        for item in items
        if item.type is EvidenceType.RELATIONSHIP
        and item.value.get("relationship_type") == "STRICT_VERIFIED"
    ]
    lookup_items = [
        item
        for item in items
        if item.type is EvidenceType.RELATIONSHIP
        and item.value.get("relationship_type") == "NON_STRICT_LOOKUP"
    ]

    assert len(strict_items) == 6
    assert len(lookup_items) == 3

    assert {
        item.value["unmatched_rows"]
        for item in lookup_items
    } == {13, 278, 7}


def test_schema_evidence_ids_are_unique() -> None:
    """Every EvidenceItem generated from the schema must have a unique ID."""
    items = build_schema_evidence(SNAPSHOT_PATH)

    ids = [item.evidence_id for item in items]

    assert len(ids) == len(set(ids))


def test_schema_evidence_content_hashes_are_stable() -> None:
    """Rebuilding unchanged schema evidence should produce identical hashes."""
    first = build_schema_evidence(SNAPSHOT_PATH)
    second = build_schema_evidence(SNAPSHOT_PATH)

    first_hashes = {
        item.evidence_id: item.content_hash
        for item in first
    }
    second_hashes = {
        item.evidence_id: item.content_hash
        for item in second
    }

    assert first_hashes == second_hashes


def test_snapshot_file_is_valid_json() -> None:
    """The exported snapshot must remain machine-readable JSON."""
    payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    assert payload["database_id"] == "olist_primary"
