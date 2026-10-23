"""Apply approved alias resolutions to query evidence obligations."""

from __future__ import annotations

from src.contracts.evidence import EvidenceItem, EvidenceType
from src.query_intelligence.aliases import (
    AliasResolutionStatus,
    resolve_alias,
)
from src.query_intelligence.pipeline import QueryAnalysisResult


RESOLVABLE_TYPES = {
    EvidenceType.SCHEMA,
    EvidenceType.METADATA,
    EvidenceType.GLOSSARY,
    EvidenceType.PERMISSION,
}


def expand_alias_obligations(
    analysis: QueryAnalysisResult,
    evidence_items: tuple[EvidenceItem, ...],
) -> QueryAnalysisResult:
    """Replace matching business terms with canonical evidence targets."""
    expanded = []

    for obligation in analysis.required_evidence_obligations:
        if obligation.evidence_type not in RESOLVABLE_TYPES:
            expanded.append(obligation)
            continue

        resolution = resolve_alias(obligation.target, evidence_items)

        if resolution.status == AliasResolutionStatus.AMBIGUOUS:
            raise ValueError(
                f"Ambiguous term {obligation.target!r}; clarify it before retrieval."
            )

        if resolution.status == AliasResolutionStatus.MATCHED:
            obligation = obligation.model_copy(
                update={"target": resolution.candidates[0].target}
            )

        expanded.append(obligation)

    return analysis.model_copy(
        update={"required_evidence_obligations": tuple(expanded)}
    )