"""Validate versioned documentation rules and build document evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from src.contracts.evidence import EvidenceItem, EvidenceType
from src.evidence.glossary import GLOSSARY_AUTHORITY

DEFAULT_DOCUMENTS_PATH = Path("evidence/documents")
APPROVED_STATUS = "APPROVED"
DRAFT_STATUS = "DRAFT_FOR_TEAM_REVIEW"


def _required_text(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must have a non-empty {key!r}.")
    return value.strip()


def _parse_optional_datetime(value: Any, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO date/datetime string or null.")

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid ISO date/datetime.") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _stable_hash(value: Any) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _catalog_paths(path: str | Path) -> tuple[Path, ...]:
    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(f"Documentation store not found: {root}")
    if root.is_file():
        return (root,)

    paths = tuple(sorted(root.glob("*.json")))
    if not paths:
        raise ValueError(f"Documentation store has no JSON catalogs: {root}")
    return paths


def _read_catalog(path: Path) -> dict[str, Any]:
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Documentation catalog contains invalid JSON: {path}") from exc

    if not isinstance(catalog, dict):
        raise ValueError(f"Documentation catalog must be a JSON object: {path}")
    if catalog.get("format_version") != 1:
        raise ValueError(f"Unsupported documentation format_version in {path}.")

    for key in (
        "database_id",
        "documents_version",
        "source",
        "access_label",
        "status",
    ):
        _required_text(catalog, key, "Documentation catalog")

    if catalog["status"] not in {APPROVED_STATUS, DRAFT_STATUS}:
        raise ValueError(f"Unsupported documentation status: {catalog['status']!r}")

    records = catalog.get("records")
    if not isinstance(records, list):
        raise ValueError("Documentation catalog must contain a 'records' list.")

    seen_versions: set[tuple[str, str]] = set()
    for index, record in enumerate(records):
        label = f"Document record {index}"
        if not isinstance(record, dict):
            raise ValueError(f"{label} must be an object.")

        for key in (
            "document_id",
            "title",
            "rule",
            "scope",
            "source_id",
            "document_version",
            "status",
            "provenance_id",
        ):
            _required_text(record, key, label)

        if record["status"] not in {APPROVED_STATUS, DRAFT_STATUS}:
            raise ValueError(f"{label} has unsupported status {record['status']!r}.")

        authority = record.get("authority_level")
        if isinstance(authority, bool) or not isinstance(authority, int):
            raise ValueError(f"{label} authority_level must be an integer.")

        if authority < 0 or authority >= GLOSSARY_AUTHORITY:
            raise ValueError(
                f"{label} authority_level must be non-negative and below "
                f"glossary authority ({GLOSSARY_AUTHORITY})."
            )

    for index, record in enumerate(catalog["records"]):
        label = f"Document record {index}"
        if not isinstance(record, dict):
            raise ValueError(f"{label} must be an object.")

        effective_from = _parse_optional_datetime(
            record.get("effective_from"), f"{label} effective_from"
        )
        effective_to = _parse_optional_datetime(
            record.get("effective_to"), f"{label} effective_to"
        )
        if (
            effective_from is not None
            and effective_to is not None
            and effective_to <= effective_from
        ):
            raise ValueError(f"{label} effective_to must be later than effective_from.")

        if record["status"] == APPROVED_STATUS and effective_from is None:
            raise ValueError(f"{label} approved record needs effective_from.")
        if (
            catalog["status"] == DRAFT_STATUS
            and record["status"] == APPROVED_STATUS
        ):
            raise ValueError(f"{label} cannot be approved inside a draft catalog.")

        identity = (record["document_id"], record["document_version"])
        if identity in seen_versions:
            raise ValueError(
                f"Duplicate document ID/version: {identity[0]!r} {identity[1]!r}."
            )
        seen_versions.add(identity)

    return catalog


def load_document_catalogs(
    path: str | Path = DEFAULT_DOCUMENTS_PATH,
) -> tuple[dict[str, Any], ...]:
    """Load and validate all JSON catalogs in the documentation store."""
    catalogs = tuple(_read_catalog(item) for item in _catalog_paths(path))

    seen_records: set[tuple[str, str, str]] = set()
    for catalog in catalogs:
        for record in catalog["records"]:
            identity = (
                catalog["database_id"],
                record["document_id"],
                record["document_version"],
            )
            if identity in seen_records:
                raise ValueError(
                    "Duplicate document ID/version across catalogs: "
                    f"{identity[1]!r} {identity[2]!r}."
                )
            seen_records.add(identity)

    return catalogs


def build_document_evidence(
    path: str | Path = DEFAULT_DOCUMENTS_PATH,
    *,
    as_of: date | datetime | None = None,
) -> tuple[EvidenceItem, ...]:
    """Build evidence from approved documentation effective at the requested time."""
    catalogs = load_document_catalogs(path)

    if as_of is None:
        current = datetime.now(timezone.utc)
    elif isinstance(as_of, datetime):
        current = (
            as_of.replace(tzinfo=timezone.utc)
            if as_of.tzinfo is None
            else as_of
        )
    else:
        current = datetime.combine(as_of, time.min, timezone.utc)

    retrieved_at = datetime.now(timezone.utc)
    items: list[EvidenceItem] = []

    # Keep all applicable approved records; downstream conflict handling
    # must see competing rules rather than silently selecting one.
    for catalog in catalogs:
        if catalog["status"] != APPROVED_STATUS:
            continue

        for record in catalog["records"]:
            if record["status"] != APPROVED_STATUS:
                continue

            effective_from = _parse_optional_datetime(
                record.get("effective_from"), "effective_from"
            )
            effective_to = _parse_optional_datetime(
                record.get("effective_to"), "effective_to"
            )
            if effective_from is None or current < effective_from:
                continue
            if effective_to is not None and current >= effective_to:
                continue

            value = {
                "document_id": record["document_id"],
                "title": record["title"],
                "rule": record["rule"],
                "source_id": record["source_id"],
                "target": record.get("target", record["scope"]),
            }
            slug = re.sub(
                r"[^A-Z0-9]+", "-", record["document_id"].upper()
            ).strip("-")
            version_slug = re.sub(
                r"[^A-Z0-9]+", "-", record["document_version"].upper()
            ).strip("-")

            items.append(
                EvidenceItem(
                    evidence_id=f"EV-DOC-{slug}-{version_slug}",
                    database_id=catalog["database_id"],
                    type=EvidenceType.DOCUMENT,
                    key=record["document_id"],
                    value=value,
                    source=catalog["source"],
                    source_version=record["document_version"],
                    effective_from=effective_from,
                    effective_to=effective_to,
                    retrieved_at=retrieved_at,
                    authority_level=record["authority_level"],
                    access_label=catalog["access_label"],
                    scope=record["scope"],
                    provenance_id=record["provenance_id"],
                    content_hash=_stable_hash(value),
                    linked_requirement_ids=(),
                )
            )

    return tuple(items)
