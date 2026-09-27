from __future__ import annotations

import getpass
import os
from urllib.parse import quote_plus

import psycopg
import pytest


@pytest.fixture(scope="module")
def dsn() -> str:
    configured = os.getenv("DRIFTSQL_TEST_DSN")
    if configured:
        return configured

    password = quote_plus(getpass.getpass("PostgreSQL password for EVID-006 test: "))
    return f"postgresql://postgres:{password}@localhost:5432/driftsql"


def test_limited_role_can_read_allowed_order_columns(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("SET ROLE driftsql_orders_limited")
        rows = conn.execute(
            "SELECT order_id, order_status FROM olist_raw.orders LIMIT 1"
        ).fetchall()
        assert rows


def test_limited_role_cannot_read_customer_id(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("SET ROLE driftsql_orders_limited")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "SELECT customer_id FROM olist_raw.orders LIMIT 0"
            )
        conn.rollback()


def test_limited_role_cannot_update_orders(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("SET ROLE driftsql_orders_limited")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE olist_raw.orders "
                "SET order_status = order_status "
                "WHERE FALSE"
            )
        conn.rollback()


def test_reader_role_can_read_olist_table(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("SET ROLE driftsql_olist_reader")
        rows = conn.execute(
            "SELECT order_id FROM olist_raw.orders LIMIT 1"
        ).fetchall()
        assert rows