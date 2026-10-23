"""Olist PostgreSQL adapter for DriftSQL.

DATA-003 adapter for the verified Olist primary database.

This adapter:
- registers the database as ``olist_primary``;
- reads tables/columns from the live PostgreSQL catalog;
- exposes only the relationships verified during DATA-003 as strict relationships;
- keeps non-strict lookup relationships separate;
- can build a deterministic schema snapshot/hash;
- provides a bounded read-only query helper.

It does NOT contain passwords or other credentials. A PostgreSQL DSN is supplied
from outside the file at runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Sequence

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .base import DatabaseAdapter


class OlistAdapter(DatabaseAdapter):
    """Database adapter for the verified Olist PostgreSQL database."""

    DATABASE_ID = "olist_primary"
    DEFAULT_SCHEMA = "olist_raw"
    SUPPORTED_ROLES = frozenset({
        "driftsql_olist_reader",
        "driftsql_orders_limited",
    })

    # Verified in DATA-003: each relationship produced 0 orphan child rows.
    _STRICT_RELATIONSHIPS: tuple[dict[str, Any], ...] = (
        {
            "child_table": "orders",
            "child_columns": ["customer_id"],
            "parent_table": "customers",
            "parent_columns": ["customer_id"],
            "relationship_type": "STRICT_VERIFIED",
            "verification": "DATA-003 orphan_rows=0",
        },
        {
            "child_table": "order_items",
            "child_columns": ["order_id"],
            "parent_table": "orders",
            "parent_columns": ["order_id"],
            "relationship_type": "STRICT_VERIFIED",
            "verification": "DATA-003 orphan_rows=0",
        },
        {
            "child_table": "order_payments",
            "child_columns": ["order_id"],
            "parent_table": "orders",
            "parent_columns": ["order_id"],
            "relationship_type": "STRICT_VERIFIED",
            "verification": "DATA-003 orphan_rows=0",
        },
        {
            "child_table": "order_reviews",
            "child_columns": ["order_id"],
            "parent_table": "orders",
            "parent_columns": ["order_id"],
            "relationship_type": "STRICT_VERIFIED",
            "verification": "DATA-003 orphan_rows=0",
        },
        {
            "child_table": "order_items",
            "child_columns": ["product_id"],
            "parent_table": "products",
            "parent_columns": ["product_id"],
            "relationship_type": "STRICT_VERIFIED",
            "verification": "DATA-003 orphan_rows=0",
        },
        {
            "child_table": "order_items",
            "child_columns": ["seller_id"],
            "parent_table": "sellers",
            "parent_columns": ["seller_id"],
            "relationship_type": "STRICT_VERIFIED",
            "verification": "DATA-003 orphan_rows=0",
        },
    )

    # These were verified as useful lookups, but NOT as strict FKs.
    _NON_STRICT_LOOKUPS: tuple[dict[str, Any], ...] = (
        {
            "child_table": "products",
            "child_columns": ["product_category_name"],
            "parent_table": "product_category_name_translation",
            "parent_columns": ["product_category_name"],
            "relationship_type": "NON_STRICT_LOOKUP",
            "unmatched_rows": 13,
        },
        {
            "child_table": "customers",
            "child_columns": ["customer_zip_code_prefix"],
            "parent_table": "geolocation",
            "parent_columns": ["geolocation_zip_code_prefix"],
            "relationship_type": "NON_STRICT_LOOKUP",
            "unmatched_rows": 278,
        },
        {
            "child_table": "sellers",
            "child_columns": ["seller_zip_code_prefix"],
            "parent_table": "geolocation",
            "parent_columns": ["geolocation_zip_code_prefix"],
            "relationship_type": "NON_STRICT_LOOKUP",
            "unmatched_rows": 7,
        },
    )

    _DISALLOWED_SQL = re.compile(
        r"\b("
        r"insert|update|delete|merge|create|alter|drop|truncate|"
        r"grant|revoke|copy|call|do|vacuum|analyze|refresh|reindex|cluster"
        r")\b",
        re.IGNORECASE,
    )
    def __init__(
        self,
        dsn: str,
        database_role: Literal[
            "driftsql_olist_reader",
            "driftsql_orders_limited",
        ],
        *,
        schema: str = DEFAULT_SCHEMA,
        default_timeout_ms: int = 5_000,
        default_row_limit: int = 500,
    ) -> None:
        if not dsn or not dsn.strip():
            raise ValueError("A PostgreSQL DSN is required.")
        if database_role not in self.SUPPORTED_ROLES:
            raise ValueError("Unsupported PostgreSQL role.")
        if default_timeout_ms <= 0:
            raise ValueError("default_timeout_ms must be greater than 0.")
        if default_row_limit <= 0:
            raise ValueError("default_row_limit must be greater than 0.")

        self._dsn = dsn
        self.database_role = database_role
        self.schema = schema
        self.default_timeout_ms = default_timeout_ms
        self.default_row_limit = default_row_limit
    @property
    def database_id(self) -> str:
        """Stable DriftSQL identifier for the primary Olist database."""
        return self.DATABASE_ID

    def _connect(self, *, read_only: bool = False) -> psycopg.Connection:
        """Open a connection using this adapter's server-configured role."""
        conn = psycopg.connect(self._dsn, row_factory=dict_row)
        try:
            if read_only:
                conn.execute("SET TRANSACTION READ ONLY")

            conn.execute(
                sql.SQL("SET LOCAL ROLE {}").format(
                    sql.Identifier(self.database_role)
                )
            )
        except Exception:
            conn.close()
            raise
        return conn
    def can_select(
        self,
        table: str,
        column: str | None = None,
    ) -> bool:
        """Check SELECT permission using this adapter's configured role."""
        parts = table.split(".", maxsplit=1)
        if len(parts) != 2 or parts[0] != self.schema or not parts[1]:
            raise ValueError(
                f"Table must be qualified with the configured schema "
                f"{self.schema!r}."
            )

        if column is not None and not column.strip():
            raise ValueError("column cannot be empty.")

        if column is None:
            query = """
                SELECT has_table_privilege(
                    current_user, %s, 'SELECT'
                ) AS allowed
            """
            params = (table,)
        else:
            query = """
                SELECT has_column_privilege(
                    current_user, %s, %s, 'SELECT'
                ) AS allowed
            """
            params = (table, column)

        with self._connect(read_only=True) as conn:
            row = conn.execute(query, params).fetchone()

        return bool(row and row["allowed"])

    def ping(self) -> bool:
        """Check that PostgreSQL is reachable without changing any data."""
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT 1 AS ok").fetchone()
                return bool(row and row["ok"] == 1)
        except psycopg.Error:
            return False

    def get_tables(self) -> list[str]:
        """Read all ordinary and partitioned tables from PostgreSQL's catalog."""
        query = """
            SELECT relation.relname AS table_name
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = %s
              AND relation.relkind IN ('r', 'p')
            ORDER BY relation.relname
        """

        with self._connect() as conn:
            rows = conn.execute(query, (self.schema,)).fetchall()

        return [row["table_name"] for row in rows]

    def get_columns(self, table_name: str) -> list[dict[str, Any]]:
        """Read all column facts, independent of the active role's SELECT grants."""
        query = """
            SELECT
                attribute.attname AS column_name,
                pg_catalog.format_type(
                    attribute.atttypid,
                    attribute.atttypmod
                ) AS data_type,
                data_type.typname AS udt_name,
                CASE WHEN attribute.attnotnull THEN 'NO' ELSE 'YES' END
                    AS is_nullable,
                attribute.attnum AS ordinal_position
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            JOIN pg_catalog.pg_type AS data_type
              ON data_type.oid = attribute.atttypid
            WHERE namespace.nspname = %s
              AND relation.relname = %s
              AND relation.relkind IN ('r', 'p')
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            ORDER BY attribute.attnum
        """

        with self._connect() as conn:
            rows = conn.execute(query, (self.schema, table_name)).fetchall()

        if not rows:
            raise KeyError(
                f"Table {self.schema}.{table_name!s} was not found in the live catalog."
            )

        return [dict(row) for row in rows]

    def get_primary_keys(self) -> list[dict[str, Any]]:
        """Read normalized live primary-key columns from PostgreSQL catalogs."""
        query = """
            SELECT
                relation.relname AS table_name,
                array_agg(attribute.attname ORDER BY key_column.ordinality)
                    AS columns
            FROM pg_catalog.pg_constraint AS constraint_record
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = constraint_record.conrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            JOIN LATERAL unnest(constraint_record.conkey)
                WITH ORDINALITY AS key_column(attnum, ordinality)
              ON TRUE
            JOIN pg_catalog.pg_attribute AS attribute
              ON attribute.attrelid = relation.oid
             AND attribute.attnum = key_column.attnum
            WHERE namespace.nspname = %s
              AND constraint_record.contype = 'p'
            GROUP BY relation.relname, constraint_record.oid
            ORDER BY relation.relname, columns
        """

        with self._connect(read_only=True) as conn:
            rows = conn.execute(query, (self.schema,)).fetchall()

        return [dict(row) for row in rows]

    def get_foreign_keys(self) -> list[dict[str, Any]]:
        """Read normalized live foreign-key columns and validation state."""
        query = """
            SELECT
                source_relation.relname AS child_table,
                array_agg(source_attribute.attname ORDER BY key_pair.ordinality)
                    AS child_columns,
                target_namespace.nspname AS parent_schema,
                target_relation.relname AS parent_table,
                array_agg(target_attribute.attname ORDER BY key_pair.ordinality)
                    AS parent_columns,
                constraint_record.convalidated AS validated
            FROM pg_catalog.pg_constraint AS constraint_record
            JOIN pg_catalog.pg_class AS source_relation
              ON source_relation.oid = constraint_record.conrelid
            JOIN pg_catalog.pg_namespace AS source_namespace
              ON source_namespace.oid = source_relation.relnamespace
            JOIN pg_catalog.pg_class AS target_relation
              ON target_relation.oid = constraint_record.confrelid
            JOIN pg_catalog.pg_namespace AS target_namespace
              ON target_namespace.oid = target_relation.relnamespace
            JOIN LATERAL unnest(
                constraint_record.conkey,
                constraint_record.confkey
            ) WITH ORDINALITY AS key_pair(child_attnum, parent_attnum, ordinality)
              ON TRUE
            JOIN pg_catalog.pg_attribute AS source_attribute
              ON source_attribute.attrelid = source_relation.oid
             AND source_attribute.attnum = key_pair.child_attnum
            JOIN pg_catalog.pg_attribute AS target_attribute
              ON target_attribute.attrelid = target_relation.oid
             AND target_attribute.attnum = key_pair.parent_attnum
            WHERE source_namespace.nspname = %s
              AND constraint_record.contype = 'f'
            GROUP BY
                source_relation.relname,
                target_namespace.nspname,
                target_relation.relname,
                constraint_record.convalidated,
                constraint_record.oid
            ORDER BY source_relation.relname, child_columns,
                     target_namespace.nspname, target_relation.relname,
                     parent_columns
        """

        with self._connect(read_only=True) as conn:
            rows = conn.execute(query, (self.schema,)).fetchall()

        return [dict(row) for row in rows]

    def get_relationships(self) -> list[dict[str, Any]]:
        """Return only the six DATA-003 verified strict relationships."""
        return [dict(item) for item in self._STRICT_RELATIONSHIPS]

    def get_lookup_relationships(self) -> list[dict[str, Any]]:
        """Return verified supporting lookups that must NOT be treated as strict FKs."""
        return [dict(item) for item in self._NON_STRICT_LOOKUPS]

    def get_schema_snapshot(self) -> dict[str, Any]:
        """Build a deterministic structural snapshot and SHA-256 schema hash.

        The hash is calculated only from structural content, not from the timestamp.
        Therefore an unchanged live schema produces the same hash.
        """
        tables = self.get_tables()

        structural_payload = {
            "database_id": self.database_id,
            "schema": self.schema,
            "tables": [
                {
                    "table_name": table_name,
                    "columns": self.get_columns(table_name),
                }
                for table_name in tables
            ],
            "primary_keys": self.get_primary_keys(),
            "foreign_keys": self.get_foreign_keys(),
            "strict_relationships": self.get_relationships(),
            "non_strict_lookups": self.get_lookup_relationships(),
        }

        canonical = json.dumps(
            structural_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        schema_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        return {
            **structural_payload,
            "schema_hash": schema_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def _validate_readonly_sql(cls, sql_text: str) -> str:
        """Apply a conservative pre-check before the DB read-only transaction.

        This is defense in depth only. The final DriftSQL Stage-2 validator will
        later use SQLGlot/AST validation before generated SQL reaches execution.
        """
        cleaned = sql_text.strip()
        if not cleaned:
            raise ValueError("SQL cannot be empty.")

        # Allow one optional trailing semicolon, but reject multiple statements.
        without_trailing = cleaned[:-1].strip() if cleaned.endswith(";") else cleaned
        if ";" in without_trailing:
            raise ValueError("Multiple SQL statements are not allowed.")

        first_token = without_trailing.split(None, 1)[0].lower()
        if first_token not in {"select", "with"}:
            raise ValueError("Only SELECT or safe WITH queries are allowed.")

        if cls._DISALLOWED_SQL.search(without_trailing):
            raise ValueError("Potentially mutating SQL is not allowed.")

        return without_trailing

    def execute_readonly(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] | None = None,
        *,
        timeout_ms: int | None = None,
        row_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a bounded query inside an explicit read-only transaction."""
        safe_sql = self._validate_readonly_sql(sql)

        effective_timeout = (
            self.default_timeout_ms if timeout_ms is None else timeout_ms
        )
        effective_limit = self.default_row_limit if row_limit is None else row_limit

        if effective_timeout <= 0:
            raise ValueError("timeout_ms must be greater than 0.")
        if effective_limit <= 0:
            raise ValueError("row_limit must be greater than 0.")


        with self._connect(read_only=True) as conn:
            # PostgreSQL enforces the read-only transaction and configured role.

            # Apply the timeout only to this transaction.
            conn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{effective_timeout}ms",),
            )

            cursor = conn.execute(safe_sql, params)
            rows = cursor.fetchmany(effective_limit)

        return [dict(row) for row in rows]
