"""Request and intent contracts for DriftSQL query intelligence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any,Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class QueryFilter(BaseModel):
    """One filter understood from the user's question."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    field: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    value: Any

class AccessContext(BaseModel):
    """Trusted server-supplied identity and database role for a request."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    principal_id: str = Field(min_length=1)
    database_role: Literal[
        "driftsql_olist_reader",
        "driftsql_orders_limited",
    ]

class QueryRequest(BaseModel):
    """The original question and the context needed to process it."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    database_id: str = Field(min_length=1)
    access_context: AccessContext
    question: str = Field(min_length=1)
    submitted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class QueryIntent(BaseModel):
    """Structured interpretation of a QueryRequest.

    This represents the question's meaning. It is not generated SQL.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    request_id: str = Field(min_length=1)
    database_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    entities: tuple[str, ...] = Field(default_factory=tuple)
    measures: tuple[str, ...] = Field(default_factory=tuple)
    aggregation: Literal["count", "sum", "avg"] | None = None
    dimensions: tuple[str, ...] = Field(default_factory=tuple)
    filters: tuple[QueryFilter, ...] = Field(default_factory=tuple)
    unresolved_terms: tuple[str, ...] = Field(default_factory=tuple)
