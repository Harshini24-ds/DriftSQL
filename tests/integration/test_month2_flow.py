import getpass
import os
from urllib.parse import quote_plus

import pytest

from src.data.adapters.olist import OlistAdapter
from src.query_intelligence.models import AccessContext, QueryRequest
from src.query_intelligence.pipeline import analyze_query_intent
from src.query_intelligence.precheck import PrecheckDecision
from src.query_intelligence.qim import build_query_intent
from src.retrieval.engine import retrieve_for_analysis
from src.retrieval.snapshot import build_evidence_snapshot


@pytest.fixture(scope="module")
def adapter():
    dsn = os.getenv("DRIFTSQL_TEST_DSN")
    if not dsn:
        password = quote_plus(
            getpass.getpass("PostgreSQL password for Month 2 flow test: ")
        )
        dsn = f"postgresql://postgres:{password}@localhost:5432/driftsql"

    return OlistAdapter(
        dsn=dsn,
        database_role="driftsql_olist_reader",
    )


def test_month2_question_to_evidence_snapshot(adapter):
    # This is the natural-language input. Change it to try another question.
    question = "Show orders where order status is delivered"

    request = QueryRequest(
        request_id="month2-live-flow",
        database_id="olist_primary",
        access_context=AccessContext(
            principal_id="month2-demo-user",
            database_role="driftsql_olist_reader",
        ),
        question=question,
    )

    intent = build_query_intent(request)
    analysis = analyze_query_intent(
        intent,
        access_context=request.access_context,
    )

    assert analysis.precheck.decision == PrecheckDecision.ACCEPT
    assert intent.entities == ("olist_raw.orders",)
    assert len(intent.filters) == 1
    assert intent.filters[0].field == "olist_raw.orders.order_status"
    assert intent.filters[0].value == "delivered"

    retrieval = retrieve_for_analysis(
        analysis,
        adapter=adapter,
    )

    print("\nINPUT:", question)
    print("INTENT:", intent.model_dump(mode="json"))
    print("PRECHECK:", analysis.precheck.decision.value)
    print("RETRIEVAL COMPLETE:", retrieval.complete)
    print("MISSING REQUIREMENTS:", retrieval.missing_requirement_ids)

    assert retrieval.complete
    assert retrieval.missing_requirement_ids == ()

    evidence_types = {item.type.value for item in retrieval.evidence_items}
    print("EVIDENCE TYPES:", sorted(evidence_types))

    assert {"SCHEMA", "PERMISSION", "VALUE"} <= evidence_types

    value_item = next(
        item
        for item in retrieval.evidence_items
        if item.type.value == "VALUE"
    )
    print("VALUE EVIDENCE:", value_item.value)

    snapshot = build_evidence_snapshot(
        analysis,
        retrieval,
        freshness_policy_version="month2-demo-v1",
    )

    print("SNAPSHOT REQUEST:", snapshot.request_id)
    print("SNAPSHOT ID:", snapshot.snapshot_id)
    print("SNAPSHOT SCHEMA HASH:", snapshot.schema_hash)
    assert snapshot.request_id == request.request_id

    # Unsupported operation should stop at precheck, before retrieval.
    unsupported_request = QueryRequest(
        request_id="month2-unsupported-flow",
        database_id="olist_primary",
        access_context=request.access_context,
        question="Delete orders",
    )
    unsupported_intent = build_query_intent(unsupported_request)
    unsupported_analysis = analyze_query_intent(
        unsupported_intent,
        access_context=unsupported_request.access_context,
    )

    print("UNSUPPORTED PRECHECK:", unsupported_analysis.precheck.decision.value)
    print("UNSUPPORTED OBLIGATIONS:",
          len(unsupported_analysis.required_evidence_obligations))

    assert unsupported_analysis.precheck.decision == PrecheckDecision.REJECT
    assert unsupported_analysis.required_evidence_obligations == ()