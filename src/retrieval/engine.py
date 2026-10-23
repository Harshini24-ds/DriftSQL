"""Load available evidence and run direct retrieval for an accepted query."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Protocol


from src.contracts.evidence import EvidenceItem, EvidenceType
from src.evidence.documents import (
    DEFAULT_DOCUMENTS_PATH,
    build_document_evidence,
)
from src.evidence.glossary import (
    DEFAULT_GLOSSARY_PATH,
    build_glossary_evidence,
)
from src.evidence.metadata import (
    DEFAULT_METADATA_PATH,
    build_metadata_evidence,
)
from src.evidence.permissions import build_verified_permission_evidence
from src.evidence.schema import (
    DEFAULT_SNAPSHOT_PATH,
    build_schema_evidence,
)
from src.query_intelligence.alias_expansion import expand_alias_obligations
from src.query_intelligence.pipeline import QueryAnalysisResult
from src.query_intelligence.precheck import PrecheckDecision
from src.retrieval.direct import (
    DirectRetrievalResult,
    retrieve_direct_evidence,
)
from src.retrieval.drift import ensure_schema_snapshot_current

from src.retrieval.semantic import retrieve_semantic_evidence

from src.evidence.value_facts import (
    DEFAULT_VALUE_PROFILES_PATH,
    build_value_evidence,
)

class RetrievalAdapter(Protocol):
    """Adapter interface required for role-aware retrieval."""

    database_id: str
    database_role: str
    schema: str

    def can_select(
        self,
        table: str,
        column: str | None = None,
    ) -> bool: ...

    def get_schema_snapshot(self) -> dict[str, Any]: ...

def _check_required_select_permissions(
    analysis: QueryAnalysisResult,
    adapter: RetrievalAdapter,
) -> None:
    """Reject required fields the configured database role cannot read."""
    required_column_tables: set[str] = set()
    checked_columns: set[tuple[str, str]] = set()

    for obligation in analysis.required_evidence_obligations:
        if obligation.evidence_type != EvidenceType.SCHEMA:
            continue

        parts = obligation.target.split(".")
        if len(parts) != 3:
            continue

        schema_name, table_name, column_name = parts
        if schema_name != adapter.schema:
            raise PermissionError(
                f"Required field {obligation.target!r} is outside the "
                "configured schema."
            )

        table = f"{schema_name}.{table_name}"
        required_column_tables.add(table)

        if not adapter.can_select(table, column_name):
            raise PermissionError(
                f"The configured role cannot read required field "
                f"{obligation.target!r}."
            )

        checked_columns.add((table, column_name))

    for obligation in analysis.required_evidence_obligations:
        if obligation.evidence_type != EvidenceType.PERMISSION:
            continue

        parts = obligation.target.split(".")

        if len(parts) == 3:
            schema_name, table_name, column_name = parts
            if schema_name != adapter.schema:
                raise PermissionError(
                    f"Required field {obligation.target!r} is outside the "
                    "configured schema."
                )

            table = f"{schema_name}.{table_name}"
            if (table, column_name) in checked_columns:
                continue

            if not adapter.can_select(table, column_name):
                raise PermissionError(
                    f"The configured role cannot read required field "
                    f"{obligation.target!r}."
                )
            continue

        if len(parts) != 2 or parts[0] != adapter.schema:
            raise PermissionError(
                f"Permission target {obligation.target!r} must be a "
                "schema-qualified table or column."
            )

        if obligation.target in required_column_tables:
            continue

        if not adapter.can_select(obligation.target):
            raise PermissionError(
                f"The configured role cannot read required table "
                f"{obligation.target!r}."
            )

def _semantic_query_text(analysis: QueryAnalysisResult) -> str:
    """Build a search query from the structured intent and its obligations."""
    terms = [
        *analysis.intent.entities,
        *analysis.intent.measures,
        *analysis.intent.dimensions,
    ]

    for query_filter in analysis.intent.filters:
        terms.extend(
            (
                query_filter.field,
                query_filter.operator,
                str(query_filter.value),
            )
        )

    terms.extend(
        obligation.target
        for obligation in analysis.required_evidence_obligations
        if obligation.evidence_type
        in {
            EvidenceType.METADATA,
            EvidenceType.GLOSSARY,
            EvidenceType.DOCUMENT,
        }
    )
    return " ".join(terms)


def retrieve_for_analysis(
    analysis: QueryAnalysisResult,
    *,
    adapter: RetrievalAdapter,
    schema_snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
    metadata_path: str | Path = DEFAULT_METADATA_PATH,
    glossary_path: str | Path = DEFAULT_GLOSSARY_PATH,
    documents_path: str | Path = DEFAULT_DOCUMENTS_PATH,
    value_profiles_path: str | Path = DEFAULT_VALUE_PROFILES_PATH,
    as_of: date | None = None,
    additional_evidence: tuple[EvidenceItem, ...] = (),

) -> DirectRetrievalResult:
    """Load evidence and match it to an accepted query's obligations."""
    if analysis.precheck.decision != PrecheckDecision.ACCEPT:
        raise ValueError("Rejected queries must stop before evidence retrieval.")

    if analysis.access_context is None:
        raise PermissionError(
            "Trusted access context is required before evidence retrieval."
        )

    if adapter.database_id != analysis.intent.database_id:
        raise PermissionError(
            "The adapter database does not match the query database."
        )

    if adapter.database_role != analysis.access_context.database_role:
        raise PermissionError(
            "The analysis role does not match the configured adapter role."
        )

    ensure_schema_snapshot_current(
        adapter,
        schema_snapshot_path=schema_snapshot_path,
    )

    evidence_items = (
        *build_schema_evidence(schema_snapshot_path),
        *build_metadata_evidence(metadata_path, schema_snapshot_path),
        *build_glossary_evidence(
            glossary_path,
            metadata_path,
            schema_snapshot_path,
            as_of=as_of,
        ),
        *build_document_evidence(documents_path, as_of=as_of),
        *additional_evidence,
    )

    expanded_analysis = expand_alias_obligations(
        analysis,
        tuple(evidence_items),
    )

    _check_required_select_permissions(expanded_analysis, adapter)
    evidence_items = (
        *evidence_items,
        *build_verified_permission_evidence(
            expanded_analysis.required_evidence_obligations,
            database_id=analysis.intent.database_id,
            database_role=adapter.database_role,
        ),
    )
    if any(
        obligation.evidence_type == EvidenceType.VALUE
        for obligation in expanded_analysis.required_evidence_obligations
    ):
        evidence_items = (
            *evidence_items,
            *build_value_evidence(
                value_profiles_path,
                schema_snapshot_path=schema_snapshot_path,
            ),
        )

    direct_result = retrieve_direct_evidence(
        expanded_analysis.required_evidence_obligations,
        tuple(evidence_items),
    )

    # Semantic evidence supplements an already-complete direct result.
    # It cannot satisfy missing obligations or bypass permission checks.
    if not direct_result.complete:
        return direct_result

    semantic_matches = retrieve_semantic_evidence(
        _semantic_query_text(expanded_analysis),
        tuple(evidence_items),
        database_id=analysis.intent.database_id,
        top_k=5,
    )

    retrieved_by_id = {
        item.evidence_id: item
        for item in direct_result.evidence_items
    }
    for match in semantic_matches:
        retrieved_by_id.setdefault(
            match.evidence_item.evidence_id,
            match.evidence_item,
        )

    return direct_result.model_copy(
        update={"evidence_items": tuple(retrieved_by_id.values())}
    )
