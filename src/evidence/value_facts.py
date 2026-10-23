"""Load approved, bounded value profiles as DriftSQL evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.contracts.evidence import EvidenceItem, EvidenceType
from src.evidence.schema import DEFAULT_SNAPSHOT_PATH, load_schema_snapshot


DEFAULT_VALUE_PROFILES_PATH = Path("evidence/value_profiles.json")
APPROVED = "APPROVED"
DRAFT = "DRAFT_FOR_TEAM_REVIEW"
MAX_PROFILE_VALUES = 100
VALUE_AUTHORITY = 50


def _stable_hash(value: Any) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _required_text(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must have a non-empty {key!r}.")
    return value.strip()


def _schema_column_targets(snapshot: dict[str, Any]) -> set[str]:
    schema = snapshot["schema"]
    targets: set[str] = set()

    for table in snapshot["tables"]:
        table_name = table["table_name"]
        for column in table["columns"]:
            targets.add(f"{schema}.{table_name}.{column['column_name']}")

    return targets


def load_value_profile_catalog(
    path: str | Path = DEFAULT_VALUE_PROFILES_PATH,
    schema_snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
) -> dict[str, Any]:
    """Validate the value-profile catalog against the saved schema snapshot."""
    profile_path = Path(path)
    if not profile_path.exists():
        raise FileNotFoundError(f"Value profile catalog not found: {profile_path}")

    try:
        catalog = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Value profile catalog contains invalid JSON.") from exc

    if not isinstance(catalog, dict):
        raise ValueError("Value profile catalog must be a JSON object.")
    if catalog.get("format_version") != 1:
        raise ValueError("Unsupported value profile format_version.")

    snapshot = load_schema_snapshot(schema_snapshot_path)
    for key in ("database_id", "schema", "schema_hash"):
        if _required_text(catalog, key, "Value profile catalog") != snapshot[key]:
            raise ValueError(
                f"Value profile catalog {key} does not match the schema snapshot."
            )

    for key in ("profile_version", "source", "access_label", "status"):
        _required_text(catalog, key, "Value profile catalog")

    if catalog["status"] not in {APPROVED, DRAFT}:
        raise ValueError(f"Unsupported value profile status: {catalog['status']!r}")

    records = catalog.get("records")
    if not isinstance(records, list):
        raise ValueError("Value profile catalog must contain a 'records' list.")

    valid_targets = _schema_column_targets(snapshot)
    seen_targets: set[str] = set()

    for index, record in enumerate(records):
        label = f"Value profile record {index}"
        if not isinstance(record, dict):
            raise ValueError(f"{label} must be an object.")

        target = _required_text(record, "target", label)
        _required_text(record, "provenance_id", label)
        _required_text(record, "status", label)

        if target not in valid_targets:
            raise ValueError(
                f"{label} target {target!r} is not a schema column."
            )
        if target in seen_targets:
            raise ValueError(f"Duplicate value profile target: {target!r}")
        seen_targets.add(target)

        if record["status"] not in {APPROVED, DRAFT}:
            raise ValueError(f"{label} has an unsupported status.")

        coverage = record.get("coverage")
        if coverage not in {"COMPLETE", "SAMPLE"}:
            raise ValueError(
                f"{label} coverage must be 'COMPLETE' or 'SAMPLE'."
            )

        values = record.get("values")
        if not isinstance(values, list) or len(values) > MAX_PROFILE_VALUES:
            raise ValueError(
                f"{label} values must be a list of at most "
                f"{MAX_PROFILE_VALUES} items."
            )
        if any(
            value is None or isinstance(value, (dict, list))
            or not isinstance(value, (str, int, float, bool))
            for value in values
        ):
            raise ValueError(f"{label} values must be non-null scalar values.")

        if len({_stable_hash(value) for value in values}) != len(values):
            raise ValueError(f"{label} contains duplicate values.")

    return catalog


def build_value_evidence(
    path: str | Path = DEFAULT_VALUE_PROFILES_PATH,
    *,
    schema_snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
) -> tuple[EvidenceItem, ...]:
    """Build evidence only from approved catalog and record entries."""
    catalog = load_value_profile_catalog(path, schema_snapshot_path)

    if catalog["status"] != APPROVED:
        return ()

    retrieved_at = datetime.now(timezone.utc)
    items: list[EvidenceItem] = []

    for record in catalog["records"]:
        if record["status"] != APPROVED:
            continue

        value = {
            "target": record["target"],
            "values": record["values"],
            "coverage": record["coverage"],
        }
        slug = record["target"].upper().replace(".", "-")

        items.append(
            EvidenceItem(
                evidence_id=f"EV-VALUE-{slug}",
                database_id=catalog["database_id"],
                type=EvidenceType.VALUE,
                key=record["target"],
                value=value,
                source=catalog["source"],
                source_version=catalog["profile_version"],
                retrieved_at=retrieved_at,
                authority_level=VALUE_AUTHORITY,
                access_label=catalog["access_label"],
                scope=record["target"].rsplit(".", maxsplit=1)[0],
                provenance_id=record["provenance_id"],
                content_hash=_stable_hash(value),
                linked_requirement_ids=(),
            )
        )

    return tuple(items)