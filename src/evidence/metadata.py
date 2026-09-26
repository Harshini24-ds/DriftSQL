"""Load curated metadata as normalized DriftSQL evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.contracts.evidence import EvidenceItem, EvidenceType


DEFAULT_METADATA_PATH = Path("evidence/metadata.json")
DEFAULT_SCHEMA_SNAPSHOT_PATH = Path("artifacts/schema_snapshot.json")

# Approved glossary entries outrank curated metadata in the evidence hierarchy.
METADATA_AUTHORITY = 70
APPROVED_STATUS = "APPROVED"


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} contains invalid JSON: {path}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"{label} must contain a JSON object.")

    return data


def _required_text(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must have a non-empty {key!r}.")
    return value.strip()


def _parse_optional_datetime(value: Any, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO datetime string or null.")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid ISO datetime: {value!r}") from exc


def _stable_hash(value: Any) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _schema_targets(snapshot: dict[str, Any], schema: str) -> set[str]:
    tables = snapshot.get("tables")
    if not isinstance(tables, list):
        raise ValueError("Schema snapshot must contain a 'tables' list.")

    targets: set[str] = set()
    for table in tables:
        if not isinstance(table, dict):
            raise ValueError("Each schema snapshot table must be an object.")

        table_name = _required_text(table, "table_name", "Schema table")
        targets.add(f"{schema}.{table_name}")

        columns = table.get("columns")
        if not isinstance(columns, list):
            raise ValueError(f"Columns for table {table_name!r} must be a list.")

        for column in columns:
            if not isinstance(column, dict):
                raise ValueError(f"Each column in {table_name!r} must be an object.")
            column_name = _required_text(column, "column_name", "Schema column")
            targets.add(f"{schema}.{table_name}.{column_name}")

    return targets


def load_metadata_catalog(
    metadata_path: str | Path = DEFAULT_METADATA_PATH,
    schema_snapshot_path: str | Path = DEFAULT_SCHEMA_SNAPSHOT_PATH,
) -> dict[str, Any]:
    """Load and validate the metadata catalog against the saved schema snapshot."""
    catalog = _read_json(Path(metadata_path), "Metadata catalog")
    snapshot = _read_json(Path(schema_snapshot_path), "Schema snapshot")

    if catalog.get("format_version") != 1:
        raise ValueError("Unsupported metadata catalog format_version.")

    for key in ("database_id", "schema", "schema_hash"):
        catalog_value = _required_text(catalog, key, "Metadata catalog")
        snapshot_value = _required_text(snapshot, key, "Schema snapshot")
        if catalog_value != snapshot_value:
            raise ValueError(
                f"Metadata catalog {key} does not match the schema snapshot."
            )

    for key in ("metadata_version", "status", "source", "access_label"):
        _required_text(catalog, key, "Metadata catalog")

    if catalog["status"] not in {APPROVED_STATUS, "DRAFT_FOR_TEAM_REVIEW"}:
        raise ValueError(f"Unsupported metadata status: {catalog['status']!r}")

    records = catalog.get("records")
    if not isinstance(records, list):
        raise ValueError("Metadata catalog must contain a 'records' list.")

    valid_targets = _schema_targets(snapshot, catalog["schema"])
    seen_targets: set[str] = set()

    for index, record in enumerate(records):
        label = f"Metadata record {index}"
        if not isinstance(record, dict):
            raise ValueError(f"{label} must be an object.")

        target = _required_text(record, "target", label)
        _required_text(record, "scope", label)
        _required_text(record, "description", label)
        _required_text(record, "provenance_id", label)

        if target not in valid_targets:
            raise ValueError(
                f"{label} target {target!r} is not present in the schema snapshot."
            )
        if target in seen_targets:
            raise ValueError(f"Duplicate metadata target: {target!r}")
        seen_targets.add(target)

        for list_key in ("aliases", "business_terms"):
            values = record.get(list_key)
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value.strip()
                for value in values
            ):
                raise ValueError(f"{label} {list_key!r} must be a list of strings.")

        unit = record.get("unit")
        if unit is not None and not isinstance(unit, str):
            raise ValueError(f"{label} 'unit' must be a string or null.")

        effective_from = _parse_optional_datetime(
            record.get("effective_from"), f"{label} effective_from"
        )
        effective_to = _parse_optional_datetime(
            record.get("effective_to"), f"{label} effective_to"
        )
        if (
            effective_from is not None
            and effective_to is not None
            and effective_to < effective_from
        ):
            raise ValueError(f"{label} effective_to is earlier than effective_from.")

    return catalog


def build_metadata_evidence(
    metadata_path: str | Path = DEFAULT_METADATA_PATH,
    schema_snapshot_path: str | Path = DEFAULT_SCHEMA_SNAPSHOT_PATH,
) -> tuple[EvidenceItem, ...]:
    """Build evidence items from an approved, schema-matched metadata catalog."""
    catalog = load_metadata_catalog(metadata_path, schema_snapshot_path)

    # Draft metadata is validated, but it must not be used as runtime evidence.
    if catalog["status"] != APPROVED_STATUS:
        return ()

    retrieved_at = datetime.now(timezone.utc)
    items: list[EvidenceItem] = []

    for record in catalog["records"]:
        value = {
            "target": record["target"],
            "description": record["description"],
            "aliases": record["aliases"],
            "business_terms": record["business_terms"],
            "unit": record.get("unit"),
        }
        slug = re.sub(r"[^A-Z0-9]+", "-", record["target"].upper()).strip("-")

        items.append(
            EvidenceItem(
                evidence_id=f"EV-META-{slug}",
                database_id=catalog["database_id"],
                type=EvidenceType.METADATA,
                key=record["target"],
                value=value,
                source=catalog["source"],
                source_version=catalog["metadata_version"],
                effective_from=_parse_optional_datetime(
                    record.get("effective_from"), "effective_from"
                ),
                effective_to=_parse_optional_datetime(
                    record.get("effective_to"), "effective_to"
                ),
                retrieved_at=retrieved_at,
                authority_level=METADATA_AUTHORITY,
                access_label=catalog["access_label"],
                scope=record["scope"],
                provenance_id=record["provenance_id"],
                content_hash=_stable_hash(value),
                linked_requirement_ids=(),
            )
        )

    return tuple(items)
