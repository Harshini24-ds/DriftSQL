"""Resolve exact business terms and aliases against approved evidence."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.evidence import EvidenceItem, EvidenceType


class AliasResolutionStatus(str, Enum):
    MATCHED = "MATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


class AliasCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(min_length=1)
    matched_terms: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    authority_level: int


class AliasResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_term: str = Field(min_length=1)
    status: AliasResolutionStatus
    candidates: tuple[AliasCandidate, ...]


def _normalize(term: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", term.casefold()).strip()


def _text_values(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()

    raw = value.get(key)
    if not isinstance(raw, list):
        return ()

    return tuple(item for item in raw if isinstance(item, str) and item.strip())


def resolve_alias(
    query_term: str,
    evidence_items: tuple[EvidenceItem, ...],
) -> AliasResolution:
    """Resolve a term using metadata and glossary evidence supplied by retrieval."""
    normalized_query = _normalize(query_term)
    found: dict[str, dict[str, Any]] = {}

    for item in evidence_items:
        if item.type not in {EvidenceType.METADATA, EvidenceType.GLOSSARY}:
            continue

        value = item.value
        if not isinstance(value, dict):
            continue

        target = value.get("target")
        if not isinstance(target, str) or not target.strip():
            target = item.key if item.type == EvidenceType.METADATA else item.scope

        terms = {
            item.key,
            target,
            *_text_values(value, "aliases"),
            *_text_values(value, "business_terms"),
        }

        if item.type == EvidenceType.GLOSSARY:
            glossary_term = value.get("term")
            if isinstance(glossary_term, str):
                terms.add(glossary_term)

        matched = {
            term.strip()
            for term in terms
            if isinstance(term, str) and _normalize(term) == normalized_query
        }
        if not matched:
            continue

        candidate = found.setdefault(
            target,
            {
                "matched_terms": set(),
                "evidence_ids": set(),
                "authority_level": item.authority_level,
            },
        )
        candidate["matched_terms"].update(matched)
        candidate["evidence_ids"].add(item.evidence_id)
        candidate["authority_level"] = max(
            candidate["authority_level"],
            item.authority_level,
        )

    candidates = tuple(
        AliasCandidate(
            target=target,
            matched_terms=tuple(sorted(details["matched_terms"])),
            evidence_ids=tuple(sorted(details["evidence_ids"])),
            authority_level=details["authority_level"],
        )
        for target, details in sorted(found.items())
    )

    if not candidates:
        status = AliasResolutionStatus.NOT_FOUND
    elif len(candidates) == 1:
        status = AliasResolutionStatus.MATCHED
    else:
        status = AliasResolutionStatus.AMBIGUOUS

    return AliasResolution(
        query_term=query_term,
        status=status,
        candidates=candidates,
    )
