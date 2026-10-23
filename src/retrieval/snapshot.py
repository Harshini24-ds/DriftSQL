"""Build an EvidenceSnapshot from complete direct-retrieval results."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from src.contracts.evidence import EvidenceSnapshot, EvidenceType
from src.evidence.schema import DEFAULT_SNAPSHOT_PATH, load_schema_snapshot
from src.query_intelligence.pipeline import QueryAnalysisResult
from src.query_intelligence.precheck import PrecheckDecision
from src.retrieval.direct import DirectRetrievalResult


def build_evidence_snapshot(
    analysis: QueryAnalysisResult,
    retrieval: DirectRetrievalResult,
    *,
    freshness_policy_version: str,
    schema_snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
) -> EvidenceSnapshot:
    """Create a query-specific snapshot, failing closed if evidence is incomplete."""
    if analysis.precheck.decision != PrecheckDecision.ACCEPT:
        raise ValueError("Cannot build a snapshot for a rejected query.")

    if analysis.intent.unresolved_terms:
        raise ValueError(
            "Cannot build a snapshot while query terms are unresolved: "
            + ", ".join(analysis.intent.unresolved_terms)
        )

    if not retrieval.complete:
        raise ValueError(
            "Cannot build a snapshot while required evidence is missing."
        )

    if retrieval.database_id != analysis.intent.database_id:
        raise ValueError("Query and retrieved evidence use different databases.")

    schema = load_schema_snapshot(schema_snapshot_path)
    if schema["database_id"] != analysis.intent.database_id:
        raise ValueError("Schema snapshot belongs to a different database.")

    schema_hash = schema["schema_hash"]

    for item in retrieval.evidence_items:
        if (
            item.type in {EvidenceType.SCHEMA, EvidenceType.RELATIONSHIP}
            and item.source_version != schema_hash
        ):
            raise ValueError(
                f"Evidence item {item.evidence_id!r} uses a stale schema hash."
            )

    return EvidenceSnapshot(
        snapshot_id=str(uuid4()),
        request_id=analysis.intent.request_id,
        database_id=analysis.intent.database_id,
        evidence_items=retrieval.evidence_items,
        schema_hash=schema_hash,
        retrieval_metadata={
            "method": "direct_exact_match",
            "obligations_total": len(analysis.required_evidence_obligations),
            "evidence_items_returned": len(retrieval.evidence_items),
        },
        freshness_policy_version=freshness_policy_version,
        created_at=datetime.now(timezone.utc),
    )