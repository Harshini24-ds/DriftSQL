"""Connect operation precheck and required evidence planning."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator
from src.query_intelligence.models import AccessContext, QueryIntent

from src.query_intelligence.models import QueryIntent
from src.query_intelligence.obligations import (
    EvidenceObligation,
    build_required_evidence_obligations,
)
from src.query_intelligence.precheck import (
    PrecheckDecision,
    QueryPrecheckResult,
    precheck_query_intent,
)


class QueryAnalysisResult(BaseModel):
    """Precheck and evidence plan for one interpreted query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: QueryIntent
    access_context: AccessContext | None = None
    precheck: QueryPrecheckResult
    required_evidence_obligations: tuple[EvidenceObligation, ...]

    @model_validator(mode="after")
    def rejected_queries_have_no_obligations(self) -> "QueryAnalysisResult":
        if (
            self.precheck.decision == PrecheckDecision.REJECT
            and self.required_evidence_obligations
        ):
            raise ValueError("Rejected queries cannot have evidence obligations.")
        return self


def analyze_query_intent(
    intent: QueryIntent,
    *,
    access_context: AccessContext | None = None,
) -> QueryAnalysisResult:
    """Precheck an intent and carry trusted access context into the analysis."""
    precheck = precheck_query_intent(intent)

    obligations = (
        build_required_evidence_obligations(intent)
        if precheck.decision == PrecheckDecision.ACCEPT
        else ()
    )

    return QueryAnalysisResult(
        intent=intent,
        access_context=access_context,
        precheck=precheck,
        required_evidence_obligations=obligations,
    )