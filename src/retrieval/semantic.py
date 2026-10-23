"""Rank supporting evidence using the deterministic RET-002 keyword fallback."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.evidence import EvidenceItem, EvidenceType


SEARCHABLE_TYPES = {
    EvidenceType.METADATA,
    EvidenceType.GLOSSARY,
    EvidenceType.DOCUMENT,
}

_STOPWORDS = {
    "a", "an", "and", "are", "by", "for", "from", "had", "how",
    "in", "is", "it", "many", "of", "on", "or", "the", "to",
    "was", "were", "what", "which", "with",
}


class SemanticMatch(BaseModel):
    """One ranked supporting evidence item."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_item: EvidenceItem
    score: float = Field(gt=0, le=1)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(
        token
        for token in re.findall(r"[a-z0-9]+", text.casefold())
        if token not in _STOPWORDS
    )


def _text_fragments(value: Any) -> Iterator[str]:
    """Yield searchable strings from nested evidence values."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str):
                yield key
            yield from _text_fragments(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _text_fragments(child)


def _evidence_tokens(item: EvidenceItem) -> frozenset[str]:
    fragments = [item.key, item.scope, *_text_fragments(item.value)]
    return frozenset(
        token
        for fragment in fragments
        for token in _tokens(fragment)
    )


def retrieve_semantic_evidence(
    query: str,
    evidence_items: tuple[EvidenceItem, ...],
    *,
    database_id: str,
    top_k: int = 5,
) -> tuple[SemanticMatch, ...]:
    """Rank approved supporting evidence for one database using keyword overlap.

    This fallback retrieves candidates only. It does not replace exact
    obligation matching or authorization checks.
    """
    if not database_id.strip():
        raise ValueError("database_id must not be empty.")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise ValueError("top_k must be a positive integer.")

    query_tokens = _tokens(query)
    if not query_tokens:
        return ()

    ranked: list[SemanticMatch] = []

    for item in evidence_items:
        if item.database_id != database_id or item.type not in SEARCHABLE_TYPES:
            continue

        overlap = query_tokens & _evidence_tokens(item)
        if not overlap:
            continue

        score = len(overlap) / len(query_tokens)
        ranked.append(
            SemanticMatch(
                evidence_item=item,
                score=score,
            )
        )

    ranked.sort(
        key=lambda match: (
            -match.score,
            match.evidence_item.evidence_id,
        )
    )
    return tuple(ranked[:top_k])