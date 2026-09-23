"""Database adapter registry for DriftSQL.

The registry maps a stable ``database_id`` to the adapter implementation that
knows how to talk to that database.

Current DATA-003 registration:
    olist_primary -> OlistAdapter

No credentials are stored here. Runtime connection details such as a PostgreSQL
DSN are supplied when an adapter instance is created.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import DatabaseAdapter
from .olist import OlistAdapter


AdapterBuilder = Callable[..., DatabaseAdapter]


class AdapterRegistry:
    """Registry of DriftSQL database adapter builders."""

    def __init__(self) -> None:
        self._builders: dict[str, AdapterBuilder] = {}

    def register(
        self,
        database_id: str,
        builder: AdapterBuilder,
        *,
        replace: bool = False,
    ) -> None:
        """Register an adapter builder under a stable ``database_id``.

        Parameters
        ----------
        database_id:
            Stable identifier used by DriftSQL requests, e.g. ``olist_primary``.
        builder:
            Callable/class that creates a ``DatabaseAdapter`` instance.
        replace:
            If False, accidentally registering the same database twice raises an
            error. This helps prevent silent configuration mistakes.
        """
        normalized = database_id.strip()
        if not normalized:
            raise ValueError("database_id cannot be empty.")

        if normalized in self._builders and not replace:
            raise ValueError(f"database_id {normalized!r} is already registered.")

        self._builders[normalized] = builder

    def create(self, database_id: str, **kwargs: Any) -> DatabaseAdapter:
        """Create the adapter registered for ``database_id``.

        Connection settings are passed at runtime through ``kwargs``. For Olist,
        that means the PostgreSQL DSN is supplied here rather than being saved in
        source code.
        """
        try:
            builder = self._builders[database_id]
        except KeyError as exc:
            available = ", ".join(sorted(self._builders)) or "<none>"
            raise KeyError(
                f"Unknown database_id {database_id!r}. "
                f"Registered database_ids: {available}"
            ) from exc

        adapter = builder(**kwargs)

        if adapter.database_id != database_id:
            raise ValueError(
                "Adapter/database_id mismatch: "
                f"requested {database_id!r}, adapter reports {adapter.database_id!r}."
            )

        return adapter

    def registered_database_ids(self) -> tuple[str, ...]:
        """Return all registered database IDs in deterministic order."""
        return tuple(sorted(self._builders))


DEFAULT_REGISTRY = AdapterRegistry()
DEFAULT_REGISTRY.register(OlistAdapter.DATABASE_ID, OlistAdapter)
