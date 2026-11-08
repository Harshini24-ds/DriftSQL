"""Build evidence snapshots for Month 2 and EEVA validation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from src.contracts.evidence import EvidenceSnapshot, EvidenceType
from src.evidence.schema import (
    DEFAULT_SNAPSHOT_PATH,
    load_schema_snapshot,
)
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
    """Build a snapshot using the existing complete-only Month 2 path."""
    if analysis.precheck.decision != PrecheckDecision.ACCEPT:
        raise ValueError(
            "Cannot build a snapshot for a rejected query."
        )

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
        raise ValueError(
            "Query and retrieved evidence use different databases."
        )

    schema = load_schema_snapshot(schema_snapshot_path)

    if schema["database_id"] != analysis.intent.database_id:
        raise ValueError(
            "Schema snapshot belongs to a different database."
        )

    schema_hash = schema["schema_hash"]

    for item in retrieval.evidence_items:
        if (
            item.type
            in {
                EvidenceType.SCHEMA,
                EvidenceType.RELATIONSHIP,
            }
            and item.source_version != schema_hash
        ):
            raise ValueError(
                f"Evidence item {item.evidence_id!r} "
                "uses a stale schema hash."
            )

    return EvidenceSnapshot(
        snapshot_id=str(uuid4()),
        request_id=analysis.intent.request_id,
        database_id=analysis.intent.database_id,
        evidence_items=retrieval.evidence_items,
        schema_hash=schema_hash,
        retrieval_metadata={
            "method": "direct_exact_match",
            "obligations_total": len(
                analysis.required_evidence_obligations
            ),
            "evidence_items_returned": len(
                retrieval.evidence_items
            ),
        },
        freshness_policy_version=freshness_policy_version,
        created_at=datetime.now(timezone.utc),
    )


def build_validation_snapshot(
    analysis: QueryAnalysisResult,
    retrieval: DirectRetrievalResult,
    *,
    freshness_policy_version: str,
    schema_snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
) -> EvidenceSnapshot:
    """Build an initial or recovery snapshot for EEVA.

    Missing evidence, unresolved terms and potentially stale evidence
    must reach EEVA so it can classify them.

    This function checks structural consistency. It does not certify
    evidence authority, integrity, currentness, permissions or coverage.

    The retrieval result must come from the trusted retrieval pipeline,
    including its environment access checks.
    """
    if analysis.precheck.decision != PrecheckDecision.ACCEPT:
        raise ValueError(
            "Cannot build a validation snapshot for a rejected query."
        )

    obligations = analysis.required_evidence_obligations

    if not obligations:
        raise ValueError(
            "A validation snapshot requires evidence obligations."
        )

    if retrieval.database_id != analysis.intent.database_id:
        raise ValueError(
            "Query and retrieved evidence use different databases."
        )

    requirement_ids = {
        obligation.requirement_id
        for obligation in obligations
    }

    if len(requirement_ids) != len(obligations):
        raise ValueError(
            "Duplicate obligation requirement IDs."
        )

    if any(
        obligation.request_id != analysis.intent.request_id
        or obligation.database_id != analysis.intent.database_id
        for obligation in obligations
    ):
        raise ValueError(
            "Obligations must belong to the query request and database."
        )

    matched_requirement_ids = [
        match.requirement_id
        for match in retrieval.matches
    ]

    if (
        len(matched_requirement_ids)
        != len(set(matched_requirement_ids))
    ):
        raise ValueError(
            "Duplicate retrieval match requirement IDs."
        )

    if set(matched_requirement_ids) != requirement_ids:
        raise ValueError(
            "Retrieval matches must cover exactly the query obligations."
        )

    missing_ids = retrieval.missing_requirement_ids

    if len(missing_ids) != len(set(missing_ids)):
        raise ValueError(
            "Duplicate missing requirement IDs."
        )

    if not set(missing_ids) <= requirement_ids:
        raise ValueError(
            "Retrieval references an unknown missing requirement."
        )

    missing_from_matches = {
        match.requirement_id
        for match in retrieval.matches
        if not match.evidence_item_ids
    }

    if set(missing_ids) != missing_from_matches:
        raise ValueError(
            "Missing requirements disagree with retrieval matches."
        )

    if retrieval.complete != (not missing_ids):
        raise ValueError(
            "Retrieval completeness disagrees with missing requirements."
        )

    items = (
        retrieval.evidence_items
        if retrieval.complete
        else retrieval.partial_evidence_items
    )

    evidence_ids = [
        item.evidence_id
        for item in items
    ]

    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError(
            "Duplicate evidence IDs in retrieval items."
        )

    referenced_evidence_ids = {
        evidence_id
        for match in retrieval.matches
        for evidence_id in match.evidence_item_ids
    }

    if not referenced_evidence_ids <= set(evidence_ids):
        raise ValueError(
            "Matched evidence IDs are not present in retrieval items."
        )

    schema = load_schema_snapshot(schema_snapshot_path)

    if schema["database_id"] != analysis.intent.database_id:
        raise ValueError(
            "Schema snapshot belongs to a different database."
        )

    # EvidenceSnapshot itself enforces item database consistency.
    # EEVA will compare cached schema versions with trusted current facts.
    return EvidenceSnapshot(
        snapshot_id=str(uuid4()),
        request_id=analysis.intent.request_id,
        database_id=analysis.intent.database_id,
        evidence_items=items,
        schema_hash=schema["schema_hash"],
        retrieval_metadata={
            "method": "direct_eeva_validation",
            "retrieval_complete": retrieval.complete,
            "obligations_total": len(obligations),
            "evidence_items_returned": len(items),
            "missing_requirement_ids": list(missing_ids),
            "unresolved_terms": list(
                analysis.intent.unresolved_terms
            ),
        },
        freshness_policy_version=freshness_policy_version,
        created_at=datetime.now(timezone.utc),
    )