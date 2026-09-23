"""Unit tests for DriftSQL evidence contracts.

EVID-001

These tests validate the behavior of:
- EvidenceType
- EvidenceItem
- EvidenceSnapshot

They do not connect to PostgreSQL and do not modify any database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.contracts.evidence import EvidenceItem, EvidenceSnapshot, EvidenceType


NOW = datetime.now(timezone.utc)


def make_schema_item(
    *,
    evidence_id: str = "EV-SCHEMA-001",
    database_id: str = "olist_primary",
) -> EvidenceItem:
    """Create one valid schema EvidenceItem for reuse across tests."""
    return EvidenceItem(
        evidence_id=evidence_id,
        database_id=database_id,
        type=EvidenceType.SCHEMA,
        key="olist_raw.orders.order_id",
        value={
            "table": "orders",
            "column": "order_id",
            "data_type": "text",
            "is_nullable": "NO",
        },
        source="postgresql_catalog",
        source_version="schema-hash-example",
        effective_from=None,
        effective_to=None,
        retrieved_at=NOW,
        authority_level=100,
        access_label="internal",
        scope="olist_raw.orders",
        provenance_id="PROV-SCHEMA-001",
        content_hash="sha256-example",
        linked_requirement_ids=("REQ-001",),
    )


def test_valid_evidence_item() -> None:
    """A correctly formed EvidenceItem should validate successfully."""
    item = make_schema_item()

    assert item.evidence_id == "EV-SCHEMA-001"
    assert item.database_id == "olist_primary"
    assert item.type is EvidenceType.SCHEMA
    assert item.linked_requirement_ids == ("REQ-001",)


def test_evidence_item_rejects_empty_required_identifier() -> None:
    """Required string identifiers cannot be empty."""
    with pytest.raises(ValidationError):
        make_schema_item(evidence_id="")


def test_evidence_item_rejects_unknown_extra_field() -> None:
    """Unexpected fields should fail instead of silently entering evidence."""
    valid_data = make_schema_item().model_dump()
    valid_data["unexpected_field"] = "must not be accepted"

    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(valid_data)


def test_evidence_item_rejects_invalid_effective_period() -> None:
    """effective_to cannot occur before effective_from."""
    with pytest.raises(ValidationError):
        EvidenceItem(
            evidence_id="EV-DOC-001",
            database_id="olist_primary",
            type=EvidenceType.DOCUMENT,
            key="delivery_rule",
            value="example",
            source="approved_documentation",
            source_version="v1",
            effective_from=NOW,
            effective_to=NOW - timedelta(days=1),
            retrieved_at=NOW,
            authority_level=80,
            access_label="internal",
            scope="olist_primary",
            provenance_id="PROV-DOC-001",
            content_hash="sha256-doc-example",
        )


def test_evidence_item_is_immutable() -> None:
    """EvidenceItem is frozen after validation to preserve evidence integrity."""
    item = make_schema_item()

    with pytest.raises(ValidationError):
        item.source = "changed_source"  # type: ignore[misc]


def test_valid_evidence_snapshot() -> None:
    """A snapshot may contain evidence belonging to the same database."""
    item = make_schema_item()

    snapshot = EvidenceSnapshot(
        snapshot_id="SNAP-001",
        request_id="REQUEST-001",
        database_id="olist_primary",
        evidence_items=(item,),
        schema_hash="9544788f24808fb3ccfcfb2f798954f9059db40d4f92bfed10fa71d3a9f2f560",
        retrieval_metadata={
            "retrieval_mode": "direct_catalog",
            "item_count": 1,
        },
        freshness_policy_version="freshness-v1",
        created_at=NOW,
    )

    assert snapshot.database_id == "olist_primary"
    assert snapshot.evidence_item_ids == ("EV-SCHEMA-001",)


def test_snapshot_rejects_cross_database_evidence() -> None:
    """A snapshot cannot mix evidence from another database_id."""
    wrong_db_item = make_schema_item(database_id="another_database")

    with pytest.raises(ValidationError):
        EvidenceSnapshot(
            snapshot_id="SNAP-002",
            request_id="REQUEST-002",
            database_id="olist_primary",
            evidence_items=(wrong_db_item,),
            schema_hash="schema-hash-example",
            retrieval_metadata={},
            freshness_policy_version="freshness-v1",
            created_at=NOW,
        )


def test_snapshot_json_serialization() -> None:
    """Evidence contracts must serialize cleanly for audit/storage/API use."""
    item = make_schema_item()

    snapshot = EvidenceSnapshot(
        snapshot_id="SNAP-003",
        request_id="REQUEST-003",
        database_id="olist_primary",
        evidence_items=(item,),
        schema_hash="schema-hash-example",
        retrieval_metadata={},
        freshness_policy_version="freshness-v1",
        created_at=NOW,
    )

    payload = snapshot.model_dump(mode="json")

    assert payload["database_id"] == "olist_primary"
    assert payload["evidence_items"][0]["type"] == "SCHEMA"
    assert payload["evidence_items"][0]["evidence_id"] == "EV-SCHEMA-001"
