from __future__ import annotations

import getpass
import os
from typing import Iterator
from urllib.parse import quote_plus
from datetime import datetime, timezone

from src.contracts.evidence import EvidenceItem, EvidenceType
from src.query_intelligence.models import AccessContext, QueryIntent
from src.query_intelligence.pipeline import analyze_query_intent
from src.retrieval.engine import retrieve_for_analysis

import psycopg
import pytest

from src.data.adapters.olist import OlistAdapter
def make_retrieval_evidence(
    evidence_type: EvidenceType,
    target: str,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"EV-TEST-{evidence_type.value}-{target}",
        database_id="olist_primary",
        type=evidence_type,
        key=target,
        value={"target": target, "aliases": []},
        source="permission-integration-test",
        source_version="v1",
        retrieved_at=datetime.now(timezone.utc),
        authority_level=70,
        access_label="internal",
        scope=target,
        provenance_id=f"PROV-TEST-{target}",
        content_hash="permission-integration-test",
    )

@pytest.fixture(scope="module")
def dsn() -> str:
    configured = os.getenv("DRIFTSQL_TEST_DSN")
    if configured:
        return configured

    password = quote_plus(
        getpass.getpass("PostgreSQL password for EVID-006 test: ")
    )
    return f"postgresql://postgres:{password}@localhost:5432/driftsql"


@pytest.fixture
def conn(dsn: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(dsn) as connection:
        yield connection

def test_limited_role_can_read_allowed_order_columns(
    conn: psycopg.Connection,
) -> None:
    conn.execute("SET ROLE driftsql_orders_limited")
    rows = conn.execute(
        "SELECT order_id, order_status FROM olist_raw.orders LIMIT 1"
    ).fetchall()
    assert rows


def test_limited_role_cannot_read_customer_id(
    conn: psycopg.Connection,
) -> None:
    conn.execute("SET ROLE driftsql_orders_limited")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT customer_id FROM olist_raw.orders LIMIT 0")
    conn.rollback()


def test_limited_role_cannot_update_orders(
    conn: psycopg.Connection,
) -> None:
    conn.execute("SET ROLE driftsql_orders_limited")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            "UPDATE olist_raw.orders "
            "SET order_status = order_status "
            "WHERE FALSE"
        )
    conn.rollback()


def test_reader_role_can_read_olist_table(
    conn: psycopg.Connection,
) -> None:
    conn.execute("SET ROLE driftsql_olist_reader")
    rows = conn.execute(
        "SELECT order_id FROM olist_raw.orders LIMIT 1"
    ).fetchall()
    assert rows

def test_adapter_uses_limited_role_and_reads_allowed_columns(
    dsn: str,
) -> None:
    adapter = OlistAdapter(
        dsn=dsn,
        database_role="driftsql_orders_limited",
    )

    role = adapter.execute_readonly("SELECT current_user AS role")
    assert role == [{"role": "driftsql_orders_limited"}]

    # LIMIT 0 checks the column permission without depending on table contents.
    adapter.execute_readonly(
        "SELECT order_id, order_status "
        "FROM olist_raw.orders LIMIT 0"
    )


def test_adapter_limited_role_cannot_read_customer_id(dsn: str) -> None:
    adapter = OlistAdapter(
        dsn=dsn,
        database_role="driftsql_orders_limited",
    )

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        adapter.execute_readonly(
            "SELECT customer_id FROM olist_raw.orders LIMIT 0"
        )
def test_adapter_permission_check_respects_limited_role(dsn: str) -> None:
    adapter = OlistAdapter(
        dsn=dsn,
        database_role="driftsql_orders_limited",
    )

    assert adapter.can_select("olist_raw.orders", "order_id") is True
    assert adapter.can_select("olist_raw.orders", "order_status") is True
    assert adapter.can_select("olist_raw.orders", "customer_id") is False
    assert adapter.can_select("olist_raw.orders") is False

def test_retrieval_with_limited_role_allows_order_status(dsn: str) -> None:
    table = "olist_raw.orders"
    field = "olist_raw.orders.order_status"
    role = "driftsql_orders_limited"

    adapter = OlistAdapter(dsn=dsn, database_role=role)
    analysis = analyze_query_intent(
        QueryIntent(
            request_id="limited-allowed-retrieval",
            database_id="olist_primary",
            operation="select",
            entities=(table,),
            dimensions=(field,),
        ),
        access_context=AccessContext(
            principal_id="integration-test-user",
            database_role=role,
        ),
    )

    result = retrieve_for_analysis(
        analysis,
        adapter=adapter,
        additional_evidence=(
            make_retrieval_evidence(EvidenceType.METADATA, field),
            make_retrieval_evidence(EvidenceType.GLOSSARY, field),
            make_retrieval_evidence(EvidenceType.PERMISSION, table),
            make_retrieval_evidence(EvidenceType.PERMISSION, field),
        ),
    )

    assert result.complete
    assert any(item.key == field for item in result.evidence_items)


def test_retrieval_with_limited_role_rejects_customer_id(dsn: str) -> None:
    table = "olist_raw.orders"
    field = "olist_raw.orders.customer_id"
    role = "driftsql_orders_limited"

    adapter = OlistAdapter(dsn=dsn, database_role=role)
    analysis = analyze_query_intent(
        QueryIntent(
            request_id="limited-denied-retrieval",
            database_id="olist_primary",
            operation="select",
            entities=(table,),
            dimensions=(field,),
        ),
        access_context=AccessContext(
            principal_id="integration-test-user",
            database_role=role,
        ),
    )

    with pytest.raises(PermissionError, match="cannot read required field"):
        retrieve_for_analysis(
            analysis,
            adapter=adapter,
            additional_evidence=(
                make_retrieval_evidence(EvidenceType.PERMISSION, table),
            ),
        )