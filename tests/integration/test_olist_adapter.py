"""Integration tests for the DriftSQL Olist database adapter.

These tests verify DATA-003 against the real local PostgreSQL database.

Password safety:
- No password is stored in this file.
- If DRIFTSQL_TEST_DSN is not set, the test asks for the local PostgreSQL
  password interactively at runtime using getpass, so the password is not echoed.
"""

from __future__ import annotations

import getpass
import os
from urllib.parse import quote_plus

import pytest

from src.data.adapters.olist import OlistAdapter
from src.data.adapters.registry import DEFAULT_REGISTRY


EXPECTED_TABLES = {
    "customers",
    "geolocation",
    "order_items",
    "order_payments",
    "order_reviews",
    "orders",
    "products",
    "sellers",
    "product_category_name_translation",
}


@pytest.fixture(scope="module")
def dsn() -> str:
    """Return a test DSN without storing credentials in source code."""
    configured = os.getenv("DRIFTSQL_TEST_DSN")
    if configured:
        return configured

    password = getpass.getpass("PostgreSQL password for local DATA-003 test: ")
    encoded_password = quote_plus(password)

    return (
        "postgresql://postgres:"
        f"{encoded_password}@localhost:5432/driftsql"
    )


@pytest.fixture(scope="module")
def adapter(dsn: str) -> OlistAdapter:
    """Create the Olist adapter for the local verified PostgreSQL database."""
    return OlistAdapter(dsn=dsn)


def test_registry_contains_olist_primary() -> None:
    """The default registry must expose the frozen Olist database_id."""
    assert "olist_primary" in DEFAULT_REGISTRY.registered_database_ids()


def test_registry_creates_olist_adapter(dsn: str) -> None:
    """The registry should resolve olist_primary to an OlistAdapter."""
    resolved = DEFAULT_REGISTRY.create("olist_primary", dsn=dsn)

    assert isinstance(resolved, OlistAdapter)
    assert resolved.database_id == "olist_primary"


def test_database_connection(adapter: OlistAdapter) -> None:
    """The adapter must be able to reach the local PostgreSQL database."""
    assert adapter.ping() is True


def test_live_olist_tables(adapter: OlistAdapter) -> None:
    """The live catalog must contain exactly the nine verified Olist tables."""
    assert set(adapter.get_tables()) == EXPECTED_TABLES


def test_verified_strict_relationships(adapter: OlistAdapter) -> None:
    """The adapter must expose the six DATA-003 strict relationships only."""
    relationships = adapter.get_relationships()

    assert len(relationships) == 6
    assert all(
        relationship["relationship_type"] == "STRICT_VERIFIED"
        for relationship in relationships
    )


def test_non_strict_lookups_are_separate(adapter: OlistAdapter) -> None:
    """Lookup relationships must never be silently promoted to strict FKs."""
    lookups = adapter.get_lookup_relationships()

    assert len(lookups) == 3
    assert {
        (
            item["child_table"],
            item["parent_table"],
            item["unmatched_rows"],
        )
        for item in lookups
    } == {
        ("products", "product_category_name_translation", 13),
        ("customers", "geolocation", 278),
        ("sellers", "geolocation", 7),
    }


def test_schema_snapshot_is_stable(adapter: OlistAdapter) -> None:
    """An unchanged live schema should produce the same deterministic hash."""
    first = adapter.get_schema_snapshot()
    second = adapter.get_schema_snapshot()

    assert first["database_id"] == "olist_primary"
    assert first["schema"] == "olist_raw"
    assert first["schema_hash"] == second["schema_hash"]
    assert len(first["schema_hash"]) == 64
    assert {
        table["table_name"]
        for table in first["tables"]
    } == EXPECTED_TABLES
