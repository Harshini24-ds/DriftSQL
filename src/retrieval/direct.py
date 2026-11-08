"""Retrieve evidence by exact obligation target.

Complete retrieval remains available through evidence_items.

Partial matches are retained separately for EEVA validation and recovery.
Partial evidence is not permission to generate SQL.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from src.contracts.evidence import EvidenceItem, EvidenceType
from src.query_intelligence.obligations import EvidenceObligation


class RetrievalMatch(BaseModel):
    """Evidence IDs matched to one obligation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    requirement_id: str
    evidence_item_ids: tuple[str, ...]


class DirectRetrievalResult(BaseModel):
    """Direct retrieval results, including incomplete evidence."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    database_id: str
    matches: tuple[RetrievalMatch, ...]
    missing_requirement_ids: tuple[str, ...]
    complete: bool

    # Existing Month 2 interface: populated only when complete.
    evidence_items: tuple[EvidenceItem, ...]

    # Retained matches for EEVA when other obligations are missing.
    partial_evidence_items: tuple[EvidenceItem, ...] = ()


def _table_name(value: str) -> str:
    return value.rsplit(".", maxsplit=1)[-1].casefold()


def _matches_target(
    item: EvidenceItem,
    target: str,
) -> bool:
    normalized_target = target.casefold()

    if normalized_target in {
        item.key.casefold(),
        item.scope.casefold(),
    }:
        return True

    value: Any = item.value

    if isinstance(value, dict):
        item_target = value.get("target")

        if (
            isinstance(item_target, str)
            and item_target.casefold() == normalized_target
        ):
            return True

    if (
        item.type == EvidenceType.RELATIONSHIP
        and " -> " in target
    ):
        left, right = target.split(" -> ", maxsplit=1)

        if isinstance(value, dict):
            child = value.get("child_table")
            parent = value.get("parent_table")

            if (
                isinstance(child, str)
                and isinstance(parent, str)
            ):
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
    """Match obligations to evidence from the same database.

    When incomplete:
        evidence_items is empty.
        partial_evidence_items retains the matched records.

    When complete:
        both fields contain the directly matched records.
    """
    database_ids = {
        obligation.database_id
        for obligation in obligations
    }

    if len(database_ids) > 1:
        raise ValueError(
            "All obligations must use the same database_id."
        )

    database_id = next(iter(database_ids), "")

    available = tuple(
        item
        for item in evidence_items
        if item.database_id == database_id
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
                evidence_item_ids=tuple(
                    item.evidence_id
                    for item in found
                ),
            )
        )

        for item in found:
            retrieved_by_id[item.evidence_id] = item

    complete = not missing
    matched_items = tuple(retrieved_by_id.values())

    return DirectRetrievalResult(
        database_id=database_id,
        matches=tuple(matches),
        missing_requirement_ids=tuple(missing),
        complete=complete,
        evidence_items=matched_items if complete else (),
        partial_evidence_items=matched_items,
    )