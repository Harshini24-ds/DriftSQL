"""Generic database adapter interface for DriftSQL.

This module defines the contract that every database-specific adapter must follow.

The Production Blueprint requires one DriftSQL codebase to support databases through
a ``database_id`` plus a generic database adapter. Olist is registered first as
``olist_primary``. Later databases should implement the same interface without
changing the rest of the DriftSQL pipeline.

Important:
- This file does NOT contain any database password or DSN.
- This file does NOT modify PostgreSQL.
- Runtime SQL execution exposed by adapters must remain read-only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping, Sequence


class DatabaseAdapter(ABC):
    """Abstract interface implemented by every DriftSQL database adapter."""

    @property
    @abstractmethod
    def database_id(self) -> str:
        """Return the stable DriftSQL identifier for this database.

        Example:
            "olist_primary"
        """
        raise NotImplementedError

    @abstractmethod
    def ping(self) -> bool:
        """Return True when the underlying database is reachable."""
        raise NotImplementedError

    @abstractmethod
    def get_tables(self) -> list[str]:
        """Return the database tables visible to DriftSQL.

        Implementations should derive this from the live database catalog rather
        than from hard-coded assumptions.
        """
        raise NotImplementedError

    @abstractmethod
    def get_columns(self, table_name: str) -> list[dict[str, Any]]:
        """Return catalog information for columns in ``table_name``.

        Each returned dictionary should contain enough structural information for
        schema evidence and validation, such as:
        - column_name
        - data_type
        - is_nullable
        - ordinal_position
        """
        raise NotImplementedError

    @abstractmethod
    def get_relationships(self) -> list[dict[str, Any]]:
        """Return verified structural relationships exposed by this database.

        For Olist, only relationships verified during DATA-003 should be treated as
        authoritative structural relationships. Non-strict lookup relationships
        must not be silently promoted to strict foreign keys.
        """
        raise NotImplementedError

    @abstractmethod
    def get_schema_snapshot(self) -> dict[str, Any]:
        """Return an immutable representation of the current structural catalog.

        The snapshot should be suitable for deterministic hashing and later schema
        drift checks.
        """
        raise NotImplementedError

    @abstractmethod
    def execute_readonly(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] | None = None,
        *,
        timeout_ms: int | None = None,
        row_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a bounded read-only SQL query and return rows as dictionaries.

        Generated SQL must never be allowed to mutate the primary database.
        """
        raise NotImplementedError
