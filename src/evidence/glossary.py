"""Validate versioned business definitions and build approved glossary evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from src.contracts.evidence import EvidenceItem, EvidenceType
from src.evidence.metadata import (
    APPROVED_STATUS,
    DEFAULT_METADATA_PATH,
    DEFAULT_SCHEMA_SNAPSHOT_PATH,
    load_metadata_catalog,
)


DEFAULT_GLOSSARY_PATH = Path("evidence/glossary.json")
GLOSSARY_AUTHORITY = 90
DRAFT_STATUS = "DRAFT_FOR_TEAM_REVIEW"


def _required_text(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must have a non-empty {key!r}.")
    return value.strip()


def _optional_date(value: Any, label: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD) or null.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD).") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD).")
    return parsed


def _stable_hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_glossary_catalog(
    glossary_path: str | Path = DEFAULT_GLOSSARY_PATH,
    metadata_path: str | Path = DEFAULT_METADATA_PATH,
    schema_snapshot_path: str | Path = DEFAULT_SCHEMA_SNAPSHOT_PATH,
) -> dict[str, Any]:
    """Validate glossary identity, targets, approval and effective periods."""
    metadata = load_metadata_catalog(metadata_path, schema_snapshot_path)
    path = Path(glossary_path)
    if not path.exists():
        raise FileNotFoundError(f"Glossary catalog not found: {path}")
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Glossary catalog contains invalid JSON: {path}") from exc
    if not isinstance(catalog, dict):
        raise ValueError("Glossary catalog must be a JSON object.")
    if catalog.get("format_version") != 1:
        raise ValueError("Unsupported glossary format_version.")

    for key in ("database_id", "schema", "schema_hash"):
        if _required_text(catalog, key, "Glossary catalog") != metadata[key]:
            raise ValueError(f"Glossary {key} does not match the metadata catalog.")
    for key in ("glossary_version", "source", "access_label"):
        _required_text(catalog, key, "Glossary catalog")

    records = catalog.get("records")
    if not isinstance(records, list):
        raise ValueError("Glossary catalog must contain a 'records' list.")
    metadata_targets = {record["target"] for record in metadata["records"]}
    seen_versions: set[tuple[str, str]] = set()
    for index, record in enumerate(records):
        label = f"Glossary record {index}"
        if not isinstance(record, dict):
            raise ValueError(f"{label} must be an object.")
        term = _required_text(record, "term", label)
        _required_text(record, "definition", label)
        target = _required_text(record, "target", label)
        version = _required_text(record, "definition_version", label)
        _required_text(record, "provenance_id", label)
        if target not in metadata_targets:
            raise ValueError(f"{label} target {target!r} is not in the metadata catalog.")

        status = _required_text(record, "status", label)
        if status not in {APPROVED_STATUS, DRAFT_STATUS}:
            raise ValueError(f"{label} has unsupported status {status!r}.")
        aliases = record.get("aliases")
        if not isinstance(aliases, list) or any(
            not isinstance(alias, str) or not alias.strip() for alias in aliases
        ):
            raise ValueError(f"{label} aliases must be a list of non-empty strings.")

        effective_from = _optional_date(record.get("effective_from"), f"{label} effective_from")
        effective_to = _optional_date(record.get("effective_to"), f"{label} effective_to")
        if effective_from is not None and effective_to is not None and effective_to <= effective_from:
            raise ValueError(f"{label} effective_to must be later than effective_from.")
        if status == APPROVED_STATUS:
            if metadata["status"] != APPROVED_STATUS:
                raise ValueError("Approved glossary requires approved metadata.")
            if effective_from is None:
                raise ValueError(f"{label} approved definition needs effective_from.")

        identity = (term.casefold(), version)
        if identity in seen_versions:
            raise ValueError(f"Duplicate glossary term/version: {term!r} {version!r}.")
        seen_versions.add(identity)

    return catalog


def build_glossary_evidence(
    glossary_path: str | Path = DEFAULT_GLOSSARY_PATH,
    metadata_path: str | Path = DEFAULT_METADATA_PATH,
    schema_snapshot_path: str | Path = DEFAULT_SCHEMA_SNAPSHOT_PATH,
    *,
    as_of: date | None = None,
) -> tuple[EvidenceItem, ...]:
    """Return the latest approved definition per term; effective_to is exclusive."""
    catalog = load_glossary_catalog(glossary_path, metadata_path, schema_snapshot_path)
    current_date = as_of if as_of is not None else datetime.now(timezone.utc).date()
    selected: dict[str, dict[str, Any]] = {}
    for record in catalog["records"]:
        if record["status"] != APPROVED_STATUS:
            continue
        start = _optional_date(record["effective_from"], "effective_from")
        end = _optional_date(record.get("effective_to"), "effective_to")
        if start is None or current_date < start or (end is not None and current_date >= end):
            continue
        term_id = record["term"].casefold()
        previous = selected.get(term_id)
        if previous is not None:
            previous_start = _optional_date(previous["effective_from"], "effective_from")
            if start == previous_start:
                raise ValueError(f"Conflicting approved definitions for {record['term']!r}.")
            if previous_start is not None and start < previous_start:
                continue
        selected[term_id] = record

    retrieved_at = datetime.now(timezone.utc)
    items: list[EvidenceItem] = []
    for term_id in sorted(selected):
        record = selected[term_id]
        value = {
            "term": record["term"],
            "definition": record["definition"],
            "target": record["target"],
            "aliases": record["aliases"],
        }
        slug = re.sub(r"[^A-Z0-9]+", "-", record["term"].upper()).strip("-")
        version_slug = re.sub(r"[^A-Z0-9]+", "-", record["definition_version"].upper()).strip("-")
        start = date.fromisoformat(record["effective_from"])
        end = _optional_date(record.get("effective_to"), "effective_to")
        items.append(
            EvidenceItem(
                evidence_id=f"EV-GLOSSARY-{slug}-{version_slug}",
                database_id=catalog["database_id"],
                type=EvidenceType.GLOSSARY,
                key=record["term"],
                value=value,
                source=catalog["source"],
                source_version=record["definition_version"],
                effective_from=datetime.combine(start, time.min, timezone.utc),
                effective_to=(datetime.combine(end, time.min, timezone.utc) if end else None),
                retrieved_at=retrieved_at,
                authority_level=GLOSSARY_AUTHORITY,
                access_label=catalog["access_label"],
                scope=record["target"],
                provenance_id=record["provenance_id"],
                content_hash=_stable_hash(value),
                linked_requirement_ids=(),
            )
        )
    return tuple(items)
