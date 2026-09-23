"""Build normalized DriftSQL schema evidence from a verified schema snapshot.

EVID-002

This module converts ``artifacts/schema_snapshot.json`` into typed
``EvidenceItem`` objects. It does not connect to PostgreSQL and it does not
modify the database.

Evidence produced:
- one SCHEMA item per table;
- one SCHEMA item per column;
- one RELATIONSHIP item per verified strict relationship;
- one RELATIONSHIP item per verified non-strict lookup.

The saved schema hash is carried as the source version so that later schema-drift
logic can detect when structural evidence no longer matches the live catalog.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.contracts.evidence import EvidenceItem, EvidenceType


DEFAULT_SNAPSHOT_PATH = Path("artifacts/schema_snapshot.json")

# Initial authority levels follow the frozen source hierarchy:
# live structural catalog > verified relationship facts > supporting lookups.
SCHEMA_AUTHORITY = 100
VERIFIED_RELATIONSHIP_AUTHORITY = 90
NON_STRICT_LOOKUP_AUTHORITY = 60


def _stable_hash(payload: Any) -> str:
    """Return a deterministic SHA-256 hash for JSON-compatible content."""
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_created_at(value: str) -> datetime:
    """Parse the ISO timestamp stored in the schema snapshot."""
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid schema snapshot created_at value: {value!r}"
        ) from exc


def load_schema_snapshot(
    path: str | Path = DEFAULT_SNAPSHOT_PATH,
) -> dict[str, Any]:
    """Load and minimally validate a saved DriftSQL schema snapshot."""
    snapshot_path = Path(path)

    if not snapshot_path.exists():
        raise FileNotFoundError(
            f"Schema snapshot not found: {snapshot_path}"
        )

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    required = {
        "database_id",
        "schema",
        "tables",
        "strict_relationships",
        "non_strict_lookups",
        "schema_hash",
        "created_at",
    }
    missing = required.difference(snapshot)
    if missing:
        raise ValueError(
            "Schema snapshot is missing required fields: "
            + ", ".join(sorted(missing))
        )

    if snapshot["database_id"] != "olist_primary":
        raise ValueError(
            "EVID-002 expected database_id 'olist_primary', got "
            f"{snapshot['database_id']!r}."
        )

    if snapshot["schema"] != "olist_raw":
        raise ValueError(
            "EVID-002 expected schema 'olist_raw', got "
            f"{snapshot['schema']!r}."
        )

    return snapshot


def build_schema_evidence(
    path: str | Path = DEFAULT_SNAPSHOT_PATH,
) -> tuple[EvidenceItem, ...]:
    """Convert the verified Olist schema snapshot into EvidenceItem objects."""
    snapshot = load_schema_snapshot(path)

    database_id = snapshot["database_id"]
    schema_name = snapshot["schema"]
    schema_hash = snapshot["schema_hash"]
    retrieved_at = _parse_created_at(snapshot["created_at"])

    items: list[EvidenceItem] = []

    # 1. Table-level structural evidence.
    for table in snapshot["tables"]:
        table_name = table["table_name"]

        value = {
            "schema": schema_name,
            "table": table_name,
            "kind": "table",
        }

        items.append(
            EvidenceItem(
                evidence_id=f"EV-SCHEMA-TABLE-{table_name}",
                database_id=database_id,
                type=EvidenceType.SCHEMA,
                key=f"{schema_name}.{table_name}",
                value=value,
                source="postgresql_information_schema",
                source_version=schema_hash,
                retrieved_at=retrieved_at,
                authority_level=SCHEMA_AUTHORITY,
                access_label="internal",
                scope=f"{schema_name}.{table_name}",
                provenance_id=(
                    f"PROV-SCHEMA-TABLE-{table_name}"
                ),
                content_hash=_stable_hash(value),
                linked_requirement_ids=(),
            )
        )

        # 2. Column-level structural evidence.
        for column in table["columns"]:
            column_name = column["column_name"]

            value = {
                "schema": schema_name,
                "table": table_name,
                "column": column_name,
                "data_type": column["data_type"],
                "udt_name": column["udt_name"],
                "is_nullable": column["is_nullable"],
                "ordinal_position": column["ordinal_position"],
                "kind": "column",
            }

            items.append(
                EvidenceItem(
                    evidence_id=(
                        "EV-SCHEMA-COLUMN-"
                        f"{table_name}-{column_name}"
                    ),
                    database_id=database_id,
                    type=EvidenceType.SCHEMA,
                    key=(
                        f"{schema_name}.{table_name}."
                        f"{column_name}"
                    ),
                    value=value,
                    source="postgresql_information_schema",
                    source_version=schema_hash,
                    retrieved_at=retrieved_at,
                    authority_level=SCHEMA_AUTHORITY,
                    access_label="internal",
                    scope=f"{schema_name}.{table_name}",
                    provenance_id=(
                        "PROV-SCHEMA-COLUMN-"
                        f"{table_name}-{column_name}"
                    ),
                    content_hash=_stable_hash(value),
                    linked_requirement_ids=(),
                )
            )

    # 3. Verified strict relationship evidence from DATA-003.
    for index, relationship in enumerate(
        snapshot["strict_relationships"],
        start=1,
    ):
        value = dict(relationship)

        child_table = relationship["child_table"]
        parent_table = relationship["parent_table"]

        items.append(
            EvidenceItem(
                evidence_id=f"EV-REL-STRICT-{index:03d}",
                database_id=database_id,
                type=EvidenceType.RELATIONSHIP,
                key=(
                    f"{child_table}:"
                    f"{','.join(relationship['child_columns'])}"
                    "->"
                    f"{parent_table}:"
                    f"{','.join(relationship['parent_columns'])}"
                ),
                value=value,
                source="data003_verified_relationships",
                source_version=schema_hash,
                retrieved_at=retrieved_at,
                authority_level=VERIFIED_RELATIONSHIP_AUTHORITY,
                access_label="internal",
                scope=f"{schema_name}",
                provenance_id=f"PROV-REL-STRICT-{index:03d}",
                content_hash=_stable_hash(value),
                linked_requirement_ids=(),
            )
        )

    # 4. Supporting lookup relationships remain explicitly non-strict.
    for index, relationship in enumerate(
        snapshot["non_strict_lookups"],
        start=1,
    ):
        value = dict(relationship)

        child_table = relationship["child_table"]
        parent_table = relationship["parent_table"]

        items.append(
            EvidenceItem(
                evidence_id=f"EV-REL-LOOKUP-{index:03d}",
                database_id=database_id,
                type=EvidenceType.RELATIONSHIP,
                key=(
                    f"{child_table}:"
                    f"{','.join(relationship['child_columns'])}"
                    "~>"
                    f"{parent_table}:"
                    f"{','.join(relationship['parent_columns'])}"
                ),
                value=value,
                source="data003_verified_lookups",
                source_version=schema_hash,
                retrieved_at=retrieved_at,
                authority_level=NON_STRICT_LOOKUP_AUTHORITY,
                access_label="internal",
                scope=f"{schema_name}",
                provenance_id=f"PROV-REL-LOOKUP-{index:03d}",
                content_hash=_stable_hash(value),
                linked_requirement_ids=(),
            )
        )

    return tuple(items)
