"""Retrieve evidence by exact obligation target."""

from __future__ import annotations

from itertools import combinations
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.evidence import EvidenceItem, EvidenceType
from src.query_intelligence.obligations import EvidenceObligation


class RetrievalMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str
    evidence_item_ids: tuple[str, ...]


class DirectRetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    database_id: str
    matches: tuple[RetrievalMatch, ...]
    missing_requirement_ids: tuple[str, ...]
    complete: bool
    evidence_items: tuple[EvidenceItem, ...]


def _table_name(value: str) -> str:
    return value.rsplit(".", maxsplit=1)[-1].casefold()


def _matches_target(item: EvidenceItem, target: str) -> bool:
    if target.casefold() in {
        item.key.casefold(),
        item.scope.casefold(),
    }:
        return True

    value: Any = item.value
    if isinstance(value, dict):
        item_target = value.get("target")
        if isinstance(item_target, str) and item_target.casefold() == target.casefold():
            return True

    if item.type == EvidenceType.RELATIONSHIP and " -> " in target:
        left, right = target.split(" -> ", maxsplit=1)
        if isinstance(value, dict):
            child = value.get("child_table")
            parent = value.get("parent_table")
            if isinstance(child, str) and isinstance(parent, str):
                return {
                    _table_name(child),
                    _table_name(parent),
                } == {
                    _table_name(left),
                    _table_name(right),
                }

    return False


def retrieve_direct_evidence(
    obligations: tuple[EvidenceObligation, ...],
    evidence_items: tuple[EvidenceItem, ...],
) -> DirectRetrievalResult:
    """Match each obligation to evidence from the same database."""
    database_ids = {obligation.database_id for obligation in obligations}
    if len(database_ids) > 1:
        raise ValueError("All obligations must use the same database_id.")

    database_id = next(iter(database_ids), "")
    available = tuple(
        item for item in evidence_items if item.database_id == database_id
    )

    matches: list[RetrievalMatch] = []
    missing: list[str] = []
    retrieved_by_id: dict[str, EvidenceItem] = {}

    for obligation in obligations:
        found = tuple(
            item
            for item in available
            if item.type == obligation.evidence_type
            and _matches_target(item, obligation.target)
        )

        if not found:
            missing.append(obligation.requirement_id)
            matches.append(
                RetrievalMatch(
                    requirement_id=obligation.requirement_id,
                    evidence_item_ids=(),
                )
            )
            continue

        matches.append(
            RetrievalMatch(
                requirement_id=obligation.requirement_id,
                evidence_item_ids=tuple(item.evidence_id for item in found),
            )
        )
        for item in found:
            retrieved_by_id[item.evidence_id] = item

    complete = not missing

    return DirectRetrievalResult(
        database_id=database_id,
        matches=tuple(matches),
        missing_requirement_ids=tuple(missing),
        complete=complete,
        evidence_items=(
            tuple(retrieved_by_id.values()) if complete else ()
        ),
    )