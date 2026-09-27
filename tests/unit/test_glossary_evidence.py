"""EVID-004 checks use temporary catalogs and never connect to PostgreSQL."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.contracts.evidence import EvidenceType
from src.evidence.glossary import build_glossary_evidence, load_glossary_catalog


def write_catalogs(
    directory: Path,
    *,
    metadata_status: str = "DRAFT_FOR_TEAM_REVIEW",
    glossary_status: str = "DRAFT_FOR_TEAM_REVIEW",
) -> tuple[Path, Path, Path]:
    snapshot_path = directory / "schema_snapshot.json"
    metadata_path = directory / "metadata.json"
    glossary_path = directory / "glossary.json"
    snapshot = {
        "database_id": "sales_database",
        "schema": "sales",
        "schema_hash": "schema-hash-v1",
        "tables": [{"table_name": "orders", "columns": [{"column_name": "total"}]}],
    }
    metadata = {
        "format_version": 1,
        "database_id": "sales_database",
        "schema": "sales",
        "schema_hash": "schema-hash-v1",
        "metadata_version": "metadata-v1",
        "status": metadata_status,
        "source": "team_curated_metadata",
        "access_label": "internal",
        "records": [{
            "target": "sales.orders.total",
            "scope": "sales.orders",
            "description": "Recorded order total.",
            "aliases": ["order amount"],
            "business_terms": ["order total"],
            "unit": "USD",
            "provenance_id": "PROV-META-ORDER-TOTAL",
        }],
    }
    glossary = {
        "format_version": 1,
        "database_id": "sales_database",
        "schema": "sales",
        "schema_hash": "schema-hash-v1",
        "glossary_version": "glossary-v1",
        "source": "team_curated_glossary",
        "access_label": "internal",
        "records": [{
            "term": "order total",
            "definition": "Total recorded for an order.",
            "target": "sales.orders.total",
            "aliases": ["order amount"],
            "definition_version": "v1",
            "status": glossary_status,
            "effective_from": "2025-01-01" if glossary_status == "APPROVED" else None,
            "effective_to": None,
            "provenance_id": "PROV-GLOSSARY-ORDER-TOTAL",
        }],
    }
    for path, payload in (
        (snapshot_path, snapshot),
        (metadata_path, metadata),
        (glossary_path, glossary),
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")
    return glossary_path, metadata_path, snapshot_path


def test_draft_is_valid_but_not_runtime_evidence(tmp_path: Path) -> None:
    paths = write_catalogs(tmp_path)

    assert len(load_glossary_catalog(*paths)["records"]) == 1
    assert build_glossary_evidence(*paths, as_of=date(2026, 1, 1)) == ()


def test_approved_definition_becomes_versioned_evidence(tmp_path: Path) -> None:
    paths = write_catalogs(
        tmp_path, metadata_status="APPROVED", glossary_status="APPROVED"
    )

    (item,) = build_glossary_evidence(*paths, as_of=date(2026, 1, 1))

    assert item.database_id == "sales_database"
    assert item.type is EvidenceType.GLOSSARY
    assert item.key == "order total"
    assert item.value["target"] == "sales.orders.total"
    assert item.source_version == "v1"
    assert item.authority_level > 70  # Metadata authority in the current loader.
    assert item.effective_from.date() == date(2025, 1, 1)
    assert len(item.content_hash) == 64


def test_latest_applicable_approved_version_wins(tmp_path: Path) -> None:
    paths = write_catalogs(
        tmp_path, metadata_status="APPROVED", glossary_status="APPROVED"
    )
    glossary = json.loads(paths[0].read_text(encoding="utf-8"))
    newer = dict(glossary["records"][0])
    newer.update(
        definition="Revised approved order total.",
        definition_version="v2",
        effective_from="2026-01-01",
        provenance_id="PROV-GLOSSARY-ORDER-TOTAL-V2",
    )
    glossary["records"].append(newer)
    paths[0].write_text(json.dumps(glossary), encoding="utf-8")

    (old,) = build_glossary_evidence(*paths, as_of=date(2025, 6, 1))
    (current,) = build_glossary_evidence(*paths, as_of=date(2026, 6, 1))

    assert old.source_version == "v1"
    assert current.source_version == "v2"
    assert current.value["definition"] == "Revised approved order total."


def test_same_day_approved_conflict_is_not_silently_resolved(tmp_path: Path) -> None:
    paths = write_catalogs(
        tmp_path, metadata_status="APPROVED", glossary_status="APPROVED"
    )
    glossary = json.loads(paths[0].read_text(encoding="utf-8"))
    competing = dict(glossary["records"][0])
    competing.update(
        definition="A competing definition.",
        definition_version="v2",
        provenance_id="PROV-GLOSSARY-COMPETING",
    )
    glossary["records"].append(competing)
    paths[0].write_text(json.dumps(glossary), encoding="utf-8")

    with pytest.raises(ValueError, match="Conflicting approved definitions"):
        build_glossary_evidence(*paths, as_of=date(2026, 1, 1))


def test_approval_cannot_bypass_draft_metadata(tmp_path: Path) -> None:
    paths = write_catalogs(tmp_path, glossary_status="APPROVED")

    with pytest.raises(ValueError, match="requires approved metadata"):
        load_glossary_catalog(*paths)


def test_unknown_target_is_rejected(tmp_path: Path) -> None:
    paths = write_catalogs(tmp_path)
    glossary = json.loads(paths[0].read_text(encoding="utf-8"))
    glossary["records"][0]["target"] = "sales.orders.unknown"
    paths[0].write_text(json.dumps(glossary), encoding="utf-8")

    with pytest.raises(ValueError, match="not in the metadata catalog"):
        load_glossary_catalog(*paths)


def test_invalid_effective_period_is_rejected(tmp_path: Path) -> None:
    paths = write_catalogs(
        tmp_path, metadata_status="APPROVED", glossary_status="APPROVED"
    )
    glossary = json.loads(paths[0].read_text(encoding="utf-8"))
    glossary["records"][0]["effective_to"] = "2024-12-31"
    paths[0].write_text(json.dumps(glossary), encoding="utf-8")

    with pytest.raises(ValueError, match="effective_to must be later"):
        load_glossary_catalog(*paths)
