"""Offline semantic-retrieval benchmark using Month 2 Gold R(q) questions."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from src.contracts.evidence import EvidenceItem, EvidenceType
from src.retrieval.semantic import retrieve_semantic_evidence


GOLD_RQ_PATH = Path("datasets/gold_obligations/month2_gold_rq.json")

# Short evidence-style phrases for the isolated synthetic benchmark fixtures.
# These are not production evidence and are not loaded by the runtime system.
SEARCHABLE_TEXT = {
    "M2-Q01": "customer records in dataset",
    "M2-Q02": "geolocation records stored",
    "M2-Q03": "order line items recorded",
    "M2-Q04": "payment records in dataset",
    "M2-Q05": "review records exist",
    "M2-Q06": "orders recorded",
    "M2-Q07": "product category translation entries",
    "M2-Q08": "products in catalog",
    "M2-Q09": "sellers recorded",
    "M2-Q10": "orders delivered status",
    "M2-Q11": "customers grouped by state",
    "M2-Q12": "sellers grouped by state",
    "M2-Q13": "average review score",
    "M2-Q14": "customer ZIP prefix",
    "M2-Q15": "average order item price",
    "M2-Q16": "total order item freight value",
    "M2-Q17": "orders for each customer join",
    "M2-Q18": "order items supplied by each seller",
    "M2-Q19": "products with category translation",
    "M2-Q20": "reviews associated with each order",
}

VARIANT_LABELS = (
    "metadata field description",
    "metadata alias description",
    "business glossary note",
    "dataset documentation",
    "source evidence note",
)

VARIANT_TYPES = (
    EvidenceType.METADATA,
    EvidenceType.METADATA,
    EvidenceType.GLOSSARY,
    EvidenceType.DOCUMENT,
    EvidenceType.DOCUMENT,
)

_STOPWORDS = {
    "a", "an", "and", "are", "by", "for", "from", "had", "how",
    "in", "is", "it", "many", "of", "on", "or", "the", "to",
    "was", "were", "what", "which", "with",
}


def load_evaluation_cases() -> tuple[tuple[str, str, str], ...]:
    """Load Gold questions and their primary table-level schema targets."""
    dataset = json.loads(GOLD_RQ_PATH.read_text(encoding="utf-8"))
    cases = []

    for case in dataset["cases"]:
        table_obligation = next(
            obligation
            for obligation in case["gold_obligations"]
            if obligation["evidence_type"] == EvidenceType.SCHEMA.value
            and obligation["constraint_type"] == "entity_exists"
            and len(obligation["canonical_target_id"].split(".")) == 2
        )
        question_id = case["question_id"]
        cases.append(
            (
                case["question"],
                SEARCHABLE_TEXT[question_id],
                table_obligation["canonical_target_id"],
            )
        )

    return tuple(cases)


EVALUATION_CASES = load_evaluation_cases()


def build_evaluation_evidence() -> tuple[EvidenceItem, ...]:
    """Build isolated synthetic evidence variants; never used at runtime."""
    retrieved_at = datetime.now(timezone.utc)
    items = []

    for case_number, (_, searchable_text, target) in enumerate(
        EVALUATION_CASES,
        start=1,
    ):
        for variant_number, (label, evidence_type) in enumerate(
            zip(VARIANT_LABELS, VARIANT_TYPES),
            start=1,
        ):
            items.append(
                EvidenceItem(
                    evidence_id=f"EV-EVAL-Q{case_number:02d}-V{variant_number}",
                    database_id="olist_primary",
                    type=evidence_type,
                    key=target,
                    value={
                        "target": target,
                        "description": f"{label}: {searchable_text}",
                    },
                    source="month2_evaluation_fixture",
                    source_version="fixture-v1",
                    retrieved_at=retrieved_at,
                    authority_level=70,
                    access_label="internal",
                    scope=target,
                    provenance_id=f"PROV-EVAL-Q{case_number:02d}-V{variant_number}",
                    content_hash=f"test-hash-q{case_number:02d}-v{variant_number}",
                )
            )

    return tuple(items)


def _query_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.casefold())
        if token not in _STOPWORDS
    }


def test_month2_gold_entity_target_hit_rate_at_5():
    assert len(EVALUATION_CASES) == 20
    assert len({question for question, _, _ in EVALUATION_CASES}) == 20
    assert len(SEARCHABLE_TEXT) == 20

    evidence_items = build_evaluation_evidence()
    assert len(evidence_items) == 100

    hits = []

    for question, _, expected_target in EVALUATION_CASES:
        matches = retrieve_semantic_evidence(
            question,
            evidence_items,
            database_id="olist_primary",
            top_k=5,
        )
        retrieved_targets = {
            match.evidence_item.key
            for match in matches
        }
        hits.append(expected_target in retrieved_targets)

    hit_rate_at_5 = sum(hits) / len(hits)

    # Benchmark acceptance: top-5 must surface the expected table for at least
    # 18 of the 20 Gold questions.
    assert hit_rate_at_5 >= 0.90