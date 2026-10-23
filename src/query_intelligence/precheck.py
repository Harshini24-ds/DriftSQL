"""Reject unsupported query operations before evidence retrieval."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from src.query_intelligence.models import QueryIntent


SUPPORTED_OPERATIONS = frozenset(
    {
        "select",
        "lookup",
        "filter",
        "aggregate",
        "rank",
        "compare",
        "trend",
    }
)


class PrecheckDecision(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


class QueryPrecheckResult(BaseModel):
    """Result of checking whether an intent uses a supported operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    database_id: str
    operation: str
    decision: PrecheckDecision
    reason_code: str | None = None
    reason: str | None = None


def precheck_query_intent(intent: QueryIntent) -> QueryPrecheckResult:
    """Allow known read-only analytical operations; reject others."""
    operation = intent.operation.strip().casefold()

    if operation not in SUPPORTED_OPERATIONS:
        return QueryPrecheckResult(
            request_id=intent.request_id,
            database_id=intent.database_id,
            operation=operation,
            decision=PrecheckDecision.REJECT,
            reason_code="UNSUPPORTED_OPERATION",
            reason=f"Operation {operation!r} is not supported.",
        )

    return QueryPrecheckResult(
        request_id=intent.request_id,
        database_id=intent.database_id,
        operation=operation,
        decision=PrecheckDecision.ACCEPT,
    )