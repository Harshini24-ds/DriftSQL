import json
from pathlib import Path

import pytest

from src.contracts.evidence import EvidenceType
from src.query_intelligence.models import AccessContext, QueryRequest
from src.query_intelligence.pipeline import analyze_query_intent
from src.query_intelligence.precheck import PrecheckDecision
from src.query_intelligence.qim import build_query_intent


DEMO_COUNT_CASES = (
    ("How many customers are in Olist?", "customers"),
    ("Count the customer records.", "customers"),
    ("What is the number of customer rows?", "customers"),
    ("How many geolocation records are stored?", "geolocation"),
    ("Count the geolocation rows.", "geolocation"),
    ("How many order items are listed?", "order_items"),
    ("Count the order item records.", "order_items"),
    ("How many order payments are recorded?", "order_payments"),
    ("Count the order payment rows.", "order_payments"),
    ("How many order reviews are stored?", "order_reviews"),
    ("Count the order review records.", "order_reviews"),
    ("How many orders are in Olist?", "orders"),
    ("Count the order rows.", "orders"),
    ("How many product category name translation entries exist?",
     "product_category_name_translation"),
    ("Count the product-category-name-translation records.",
     "product_category_name_translation"),
    ("How many products are listed?", "products"),
    ("Count the product rows.", "products"),
    ("How many sellers are recorded?", "sellers"),
    ("Count the seller records.", "sellers"),
    ("Give me the count of order items.", "order_items"),
)


@pytest.mark.parametrize(
    ("question", "table_name"),
    DEMO_COUNT_CASES,
)
def test_qim_builds_intent_and_table_obligations(question, table_name):
    request = QueryRequest(
        request_id=f"qim-{table_name}",
        database_id="olist_primary",
        access_context=AccessContext(
            principal_id="qim-unit-test",
            database_role="driftsql_olist_reader",
        ),
        question=question,
    )

    intent = build_query_intent(request)
    analysis = analyze_query_intent(
        intent,
        access_context=request.access_context,
    )

    table_target = f"olist_raw.{table_name}"

    assert intent.operation == "aggregate"
    assert intent.entities == (table_target,)
    assert intent.measures == ("count",)
    assert intent.aggregation == "count"
    assert analysis.precheck.decision == PrecheckDecision.ACCEPT

    obligation_pairs = {
        (item.evidence_type, item.target)
        for item in analysis.required_evidence_obligations
    }
    assert (EvidenceType.SCHEMA, table_target) in obligation_pairs
    assert (EvidenceType.PERMISSION, table_target) in obligation_pairs


def test_qim_recognizes_gold_question_entities():
    gold_path = Path("datasets/gold_obligations/month2_gold_rq.json")
    dataset = json.loads(gold_path.read_text(encoding="utf-8"))

    for case in dataset["cases"]:
        request = QueryRequest(
            request_id=f"gold-qim-{case['question_id']}",
            database_id=dataset["database_id"],
            access_context=AccessContext(
                principal_id="gold-qim-unit-test",
                database_role="driftsql_olist_reader",
            ),
            question=case["question"],
        )
        intent = build_query_intent(request)

        expected_entities = {
            obligation["canonical_target_id"]
            for obligation in case["gold_obligations"]
            if obligation["evidence_type"] == EvidenceType.SCHEMA.value
            and obligation["constraint_type"] == "entity_exists"
        }

        assert intent.operation == "aggregate", case["question_id"]
        assert set(intent.entities) == expected_entities, (
            f"{case['question_id']} entity mismatch: question="
            f"{case['question']!r}, expected={sorted(expected_entities)!r}, "
            f"actual={list(intent.entities)!r}"
        )


def test_qim_extracts_gold_single_table_fields_and_status_filter():
    gold_path = Path("datasets/gold_obligations/month2_gold_rq.json")
    dataset = json.loads(gold_path.read_text(encoding="utf-8"))
    expected = {
        "M2-Q10": ("count", (), "olist_raw.orders.order_status", "delivered"),
        "M2-Q11": ("count", ("olist_raw.customers.customer_state",), None, None),
        "M2-Q12": ("count", ("olist_raw.sellers.seller_state",), None, None),
        "M2-Q13": ("count", ("olist_raw.order_payments.payment_type",), None, None),
        "M2-Q14": ("count", ("olist_raw.order_reviews.review_score",), None, None),
        "M2-Q15": ("olist_raw.order_items.price", (), None, None),
        "M2-Q16": ("olist_raw.order_items.freight_value", (), None, None),
    }
    expected_aggregations = {
        "M2-Q10": "count",
        "M2-Q11": "count",
        "M2-Q12": "count",
        "M2-Q13": "count",
        "M2-Q14": "count",
        "M2-Q15": "avg",
        "M2-Q16": "sum",
    }

    cases = {case["question_id"]: case for case in dataset["cases"]}
    for question_id, (measure, dimensions, filter_field, filter_value) in expected.items():
        case = cases[question_id]
        request = QueryRequest(
            request_id=f"gold-shape-{question_id}",
            database_id=dataset["database_id"],
            access_context=AccessContext(
                principal_id="gold-shape-unit-test",
                database_role="driftsql_olist_reader",
            ),
            question=case["question"],
        )

        intent = build_query_intent(request)

        assert intent.measures == (measure,), question_id
        assert intent.aggregation == expected_aggregations[question_id]
        assert intent.dimensions == dimensions, question_id
        if filter_field is None:
            assert intent.filters == (), question_id
        else:
            assert len(intent.filters) == 1, question_id
            assert intent.filters[0].field == filter_field
            assert intent.filters[0].operator == "="
            assert intent.filters[0].value == filter_value
        assert intent.unresolved_terms == (), question_id


def test_qim_extracts_gold_dimensions_from_verified_join_keys():
    gold_path = Path("datasets/gold_obligations/month2_gold_rq.json")
    dataset = json.loads(gold_path.read_text(encoding="utf-8"))
    expected_dimensions = {
        "M2-Q17": ("olist_raw.customers.customer_id",),
        "M2-Q18": ("olist_raw.sellers.seller_id",),
        "M2-Q19": (),
        "M2-Q20": ("olist_raw.orders.order_id",),
    }

    cases = {case["question_id"]: case for case in dataset["cases"]}
    for question_id, expected in expected_dimensions.items():
        case = cases[question_id]
        request = QueryRequest(
            request_id=f"gold-join-shape-{question_id}",
            database_id=dataset["database_id"],
            access_context=AccessContext(
                principal_id="gold-join-shape-unit-test",
                database_role="driftsql_olist_reader",
            ),
            question=case["question"],
        )

        intent = build_query_intent(request)

        assert intent.measures == ("count",), question_id
        assert intent.aggregation == "count", question_id
        assert intent.dimensions == expected, question_id
        assert intent.unresolved_terms == (), question_id


def test_rq_contains_gold_field_and_join_key_permission_obligations():
    gold_path = Path("datasets/gold_obligations/month2_gold_rq.json")
    dataset = json.loads(gold_path.read_text(encoding="utf-8"))

    for case in dataset["cases"]:
        request = QueryRequest(
            request_id=f"gold-permission-{case['question_id']}",
            database_id=dataset["database_id"],
            access_context=AccessContext(
                principal_id="gold-permission-unit-test",
                database_role="driftsql_olist_reader",
            ),
            question=case["question"],
        )
        analysis = analyze_query_intent(
            build_query_intent(request),
            access_context=request.access_context,
        )

        expected_permissions = {
            obligation["canonical_target_id"]
            for obligation in case["gold_obligations"]
            if obligation["evidence_type"] == EvidenceType.PERMISSION.value
        }
        actual_permissions = {
            obligation.target
            for obligation in analysis.required_evidence_obligations
            if obligation.evidence_type == EvidenceType.PERMISSION
        }

        assert expected_permissions <= actual_permissions, (
            f"{case['question_id']} is missing Gold permission obligations: "
            f"{sorted(expected_permissions - actual_permissions)!r}"
        )


def test_rq_evidence_types_and_targets_match_gold_dataset():
    gold_path = Path("datasets/gold_obligations/month2_gold_rq.json")
    dataset = json.loads(gold_path.read_text(encoding="utf-8"))

    for case in dataset["cases"]:
        access_context = AccessContext(
            principal_id="gold-rq-parity-unit-test",
            database_role="driftsql_olist_reader",
        )
        request = QueryRequest(
            request_id=f"gold-rq-parity-{case['question_id']}",
            database_id=dataset["database_id"],
            access_context=access_context,
            question=case["question"],
        )
        analysis = analyze_query_intent(
            build_query_intent(request),
            access_context=access_context,
        )

        expected = {
            (item["evidence_type"], item["canonical_target_id"])
            for item in case["gold_obligations"]
        }
        actual = {
            (item.evidence_type.value, item.target)
            for item in analysis.required_evidence_obligations
        }

        assert actual == expected, (
            f"{case['question_id']} Gold R(q) mismatch; "
            f"missing={sorted(expected - actual)!r}; "
            f"unexpected={sorted(actual - expected)!r}"
        )

        gold_by_target = {
            (item["evidence_type"], item["canonical_target_id"]): item
            for item in case["gold_obligations"]
        }
        actual_by_target = {
            (item.evidence_type.value, item.target): item
            for item in analysis.required_evidence_obligations
        }
        for key, gold_obligation in gold_by_target.items():
            obligation = actual_by_target[key]
            assert obligation.mandatory == gold_obligation["mandatory"], key
            assert obligation.expected_source_types == tuple(
                gold_obligation["expected_source_types"]
            ), key
            assert obligation.constraint_type == gold_obligation["constraint_type"], (
                f"{case['question_id']} {key}: Gold constraint="
                f"{gold_obligation['constraint_type']!r}, generated="
                f"{obligation.constraint_type!r}"
            )
            assert obligation.recovery_actions, key
            assert obligation.diagnostic_weight > 0, key
