from src.query_intelligence.models import AccessContext, QueryIntent
from src.query_intelligence.pipeline import analyze_query_intent


def test_analysis_preserves_trusted_access_context():
    access_context = AccessContext(
        principal_id="employee-001",
        database_role="driftsql_orders_limited",
    )
    intent = QueryIntent(
        request_id="access-context-test",
        database_id="olist_primary",
        operation="aggregate",
        entities=("olist_raw.orders",),
        measures=("count",),
    )

    analysis = analyze_query_intent(
        intent,
        access_context=access_context,
    )

    assert analysis.access_context == access_context