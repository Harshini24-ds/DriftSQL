"""Typed enterprise-evidence contracts for DriftSQL.

EVID-001

These Pydantic models implement the shared evidence contracts defined in the
latest DriftSQL Production Blueprint.

They are intentionally database-agnostic so the same contracts can be used by:
- Olist (database_id = "olist_primary")
- retrieval and normalization
- EEVA
- clarification/recovery
- audit
- experiments

No database credentials or SQL execution logic belongs in this module.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceType(str, Enum):
    """Enterprise evidence categories used by DriftSQL."""

    SCHEMA = "SCHEMA"
    METADATA = "METADATA"
    GLOSSARY = "GLOSSARY"
    DOCUMENT = "DOCUMENT"
    RELATIONSHIP = "RELATIONSHIP"
    VALUE = "VALUE"
    PERMISSION = "PERMISSION"


class EvidenceItem(BaseModel):
    """One normalized, version-aware, provenance-aware evidence record.

    This follows the blueprint's EvidenceItem contract:
    id, type, key/value, source/version/effective dates, authority, access,
    provenance, content hash, and linked required-evidence obligations.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    evidence_id: str = Field(min_length=1)
    database_id: str = Field(min_length=1)

    type: EvidenceType

    key: str = Field(min_length=1)
    value: Any

    source: str = Field(min_length=1)
    source_version: str = Field(min_length=1)

    effective_from: datetime | None = None
    effective_to: datetime | None = None
    retrieved_at: datetime

    authority_level: int
    access_label: str = Field(min_length=1)
    scope: str = Field(min_length=1)

    provenance_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)

    linked_requirement_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_effective_period(self) -> "EvidenceItem":
        """Reject an impossible effective-date interval."""
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError(
                "effective_to cannot be earlier than effective_from."
            )
        return self


class EvidenceSnapshot(BaseModel):
    """Query-specific collection of normalized evidence used by EEVA.

    A snapshot binds evidence to one request and one database_id, and records
    the schema hash and retrieval/freshness metadata needed for reproducibility,
    auditing, and later schema-drift checks.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    snapshot_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    database_id: str = Field(min_length=1)

    evidence_items: tuple[EvidenceItem, ...]

    schema_hash: str = Field(min_length=1)
    retrieval_metadata: dict[str, Any] = Field(default_factory=dict)
    freshness_policy_version: str = Field(min_length=1)

    created_at: datetime

    @model_validator(mode="after")
    def validate_database_consistency(self) -> "EvidenceSnapshot":
        """All evidence in one snapshot must belong to the same database."""
        mismatched = [
            item.evidence_id
            for item in self.evidence_items
            if item.database_id != self.database_id
        ]

        if mismatched:
            raise ValueError(
                "EvidenceSnapshot contains EvidenceItem objects with a different "
                f"database_id: {', '.join(mismatched)}"
            )

        return self

    @property
    def evidence_item_ids(self) -> tuple[str, ...]:
        """Return the item IDs in the snapshot in stored order."""
        return tuple(item.evidence_id for item in self.evidence_items)
