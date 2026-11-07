"""Offline Month 2 -> EEVA integration checks.

Uses real QIM, retrieval, and catalog loaders.

PostgreSQL access and conflict-detector outputs are controlled fixtures.
Failure variants modify an already-built snapshot or fixture context.
"""

from datetime import datetime, timezone

import pytest

from src.contracts.evidence import EvidenceType
from src.eeva.decision import Decision, evaluate_evidence
from src.eeva.evidence_state import EvidenceState
from src.eeva.reason_codes import ReasonCode
from src.eeva.validation import (
    ConflictAssessment,
    SourceReference,
    ValidationContext,
    snapshot_fingerprint,
)
from src.evidence.documents import build_document_evidence
from src.evidence.glossary import build_glossary_evidence
from src.evidence.metadata import build_metadata_evidence
from src.evidence.permissions import build_verified_permission_evidence
from src.evidence.schema import (
    build_schema_evidence,
    load_schema_snapshot,
)
from src.evidence.value_facts import build_value_evidence
from src.query_intelligence.alias_expansion import expand_alias_obligations
from src.query_intelligence.models import AccessContext, QueryRequest
from src.query_intelligence.pipeline import analyze_query_intent
from src.query_intelligence.precheck import PrecheckDecision
from src.query_intelligence.qim import build_query_intent
from src.retrieval.engine import retrieve_for_analysis
from src.retrieval.snapshot import build_evidence_snapshot


AS_OF = datetime(2026, 11, 3, tzinfo=timezone.utc)
POLICY = "eeva-integration-v1"
ROLE = "driftsql_olist_reader"

SOURCE_TYPES = {
    EvidenceType.SCHEMA: "current_schema_snapshot",
    EvidenceType.METADATA: "approved_metadata_catalog",
    EvidenceType.GLOSSARY: "approved_business_glossary",
    EvidenceType.DOCUMENT: "approved_documentation",
    EvidenceType.RELATIONSHIP: "approved_relationship_evidence",
    EvidenceType.VALUE: "approved_value_profile",
    EvidenceType.PERMISSION: "active_postgresql_role",
}

AVAILABLE_ACTIONS = frozenset({
    "refresh_schema_snapshot",
    "retrieve_approved_metadata",
    "retrieve_approved_glossary",
    "clarify_business_term",
    "retrieve_approved_documentation",
    "refresh_relationship_evidence",
    "run_bounded_read_only_probe",
    "retrieve_approved_value_profile",
    "refresh_active_role_policy",
})

DEMO_QUESTIONS = (
    "How many customer records are in the dataset?",
    "How many geolocation records are stored?",
    "How many order line items are recorded?",
    "How many payment records are in the dataset?",
    "How many review records exist?",
    "How many orders are recorded?",
    "How many product-category translation entries exist?",
    "How many products are in the catalog?",
    "How many sellers are recorded?",
    "How many orders have delivered status?",
    "How many customers are in each state?",
    "How many sellers are in each state?",
    "How many payments use each payment type?",
    "How many reviews have each review score?",
    "What is the average order-item price?",
    "What is the total freight value on order items?",
    "How many orders are there for each customer?",
    "How many order items are supplied by each seller?",
    "How many products have a category translation?",
    "How many reviews are associated with each order?",
)


class CatalogAdapter:
    """Test adapter granting the fixture role access to known objects."""

    database_id = "olist_primary"
    database_role = ROLE
    schema = "olist_raw"

    def __init__(self):
        self.catalog = load_schema_snapshot()
        self.catalog_calls = 0

    def get_schema_snapshot(self):
        self.catalog_calls += 1
        return self.catalog

    def can_select(self, table, column=None):
        prefix = f"{self.schema}."

        if not table.startswith(prefix):
            return False

        table_name = table.removeprefix(prefix)

        for entry in self.catalog["tables"]:
            if entry["table_name"] == table_name:
                return column is None or any(
                    candidate["column_name"] == column
                    for candidate in entry["columns"]
                )

        return False


@pytest.fixture(scope="module")
def current_repository():
    """Load current approved records independently of retrieval."""
    return (
        *build_schema_evidence(),
        *build_metadata_evidence(),
        *build_glossary_evidence(as_of=AS_OF.date()),
        *build_document_evidence(as_of=AS_OF.date()),
        *build_value_evidence(),
    )


def build_flow(question, current_repository):
    adapter = CatalogAdapter()

    request = QueryRequest(
        request_id="integration-request",
        database_id=adapter.database_id,
        access_context=AccessContext(
            principal_id="integration-user",
            database_role=ROLE,
        ),
        question=question,
    )

    analysis = analyze_query_intent(
        build_query_intent(request),
        access_context=request.access_context,
    )

    assert analysis.precheck.decision == PrecheckDecision.ACCEPT

    analysis = expand_alias_obligations(
        analysis,
        current_repository,
    )

    retrieval = retrieve_for_analysis(
        analysis,
        adapter=adapter,
        as_of=AS_OF.date(),
    )

    assert retrieval.complete, retrieval.missing_requirement_ids

    snapshot = build_evidence_snapshot(
        analysis,
        retrieval,
        freshness_policy_version=POLICY,
    )

    permissions = {}

    for obligation in analysis.required_evidence_obligations:
        if obligation.evidence_type == EvidenceType.PERMISSION:
            parts = obligation.target.split(".")

            permissions[obligation.target] = adapter.can_select(
                ".".join(parts[:2]),
                parts[2] if len(parts) == 3 else None,
            )

    assert permissions and all(permissions.values())

    permission_items = build_verified_permission_evidence(
        analysis.required_evidence_obligations,
        database_id=adapter.database_id,
        database_role=ROLE,
    )

    schema_hash = adapter.catalog["schema_hash"]

    references = tuple(
        SourceReference(
            source=item.source,
            provenance_id=item.provenance_id,
            evidence_type=item.type,
            key=item.key,
            source_type=SOURCE_TYPES[item.type],
            current_version=item.source_version,
            current_content_hash=item.content_hash,
            scope=item.scope,
            access_label=item.access_label,
            authority_level=item.authority_level,
            effective_from=item.effective_from,
            effective_to=item.effective_to,
            schema_hash=(
                None
                if item.type in {
                    EvidenceType.DOCUMENT,
                    EvidenceType.PERMISSION,
                }
                else schema_hash
            ),
        )
        for item in (*current_repository, *permission_items)
    )

    context = ValidationContext(
        request_id=request.request_id,
        database_id=adapter.database_id,
        database_role=ROLE,
        schema_hash=schema_hash,
        freshness_policy_version=POLICY,
        evaluated_at=AS_OF,
        sources=references,
        permission_decisions=permissions,
        allowed_access_labels=frozenset({"internal"}),
        available_recovery_actions=AVAILABLE_ACTIONS,
    )

    return analysis, snapshot, context


def fixture_assessments(analysis, snapshot, overrides=None):
    """Supply controlled detector results using catalog target identities.

    This indexes fixture candidates; it does not detect semantic conflicts.
    A table obligation excludes columns merely sharing that table's scope.
    """
    overrides = {} if overrides is None else overrides
    index = {}

    for item in snapshot.evidence_items:
        targets = {item.key.casefold()}

        if isinstance(item.value, dict):
            target = item.value.get("target")

            if isinstance(target, str):
                targets.add(target.casefold())

            if item.type == EvidenceType.RELATIONSHIP:
                child = item.value["child_table"]
                parent = item.value["parent_table"]

                child = (
                    child
                    if "." in child
                    else f"{item.scope}.{child}"
                )
                parent = (
                    parent
                    if "." in parent
                    else f"{item.scope}.{parent}"
                )

                targets.update({
                    f"{child} -> {parent}".casefold(),
                    f"{parent} -> {child}".casefold(),
                })

        if item.type == EvidenceType.DOCUMENT:
            targets.add(item.scope.casefold())

        for target in targets:
            index.setdefault(
                (item.type, target),
                [],
            ).append(item.evidence_id)

    digest = snapshot_fingerprint(snapshot)

    return tuple(
        ConflictAssessment(
            snapshot_id=snapshot.snapshot_id,
            snapshot_digest=digest,
            requirement_id=obligation.requirement_id,
            evidence_item_ids=tuple(
                index.get(
                    (
                        obligation.evidence_type,
                        obligation.target.casefold(),
                    ),
                    (),
                )
            ),
            state=overrides.get(
                obligation.requirement_id,
                EvidenceState.PASS,
            ),
        )
        for obligation in analysis.required_evidence_obligations
    )


def evaluate_flow(analysis, snapshot, context, overrides=None):
    return evaluate_evidence(
        analysis,
        snapshot,
        context,
        conflict_assessments=fixture_assessments(
            analysis,
            snapshot,
            overrides,
        ),
    )


@pytest.mark.parametrize("question", DEMO_QUESTIONS)
def test_twenty_current_catalog_questions_generate(
    question,
    current_repository,
):
    analysis, snapshot, context = build_flow(
        question,
        current_repository,
    )

    result = evaluate_flow(analysis, snapshot, context)

    assert result.decision == Decision.GENERATE
    assert result.coverage.mandatory_coverage == 1.0
    assert all(
        state.state == EvidenceState.PASS
        for state in result.evidence_states
    )
    assert result.reason_codes == (
        ReasonCode.ALL_MANDATORY_EVIDENCE_PASS,
    )

    bundle = result.validated_evidence_bundle

    assert bundle is not None
    assert bundle.snapshot_id == snapshot.snapshot_id

    approved = bundle.evidence_snapshot()

    assert approved.evidence_items
    assert set(approved.evidence_item_ids) <= set(
        snapshot.evidence_item_ids
    )


def test_real_filter_and_multi_target_permissions_reach_eeva(
    current_repository,
):
    analysis, snapshot, context = build_flow(
        DEMO_QUESTIONS[9],
        current_repository,
    )

    assert analysis.intent.filters[0].value == "delivered"

    assert {
        EvidenceType.SCHEMA,
        EvidenceType.PERMISSION,
        EvidenceType.VALUE,
    } <= {
        item.type
        for item in snapshot.evidence_items
    }

    permission_items = [
        item
        for item in snapshot.evidence_items
        if item.type == EvidenceType.PERMISSION
    ]

    assert len({item.key for item in permission_items}) >= 2
    assert len({
        item.provenance_id
        for item in permission_items
    }) == 1

    assert evaluate_flow(
        analysis,
        snapshot,
        context,
    ).decision == Decision.GENERATE


def test_missing_schema_variant_clarifies(current_repository):
    analysis, snapshot, context = build_flow(
        DEMO_QUESTIONS[5],
        current_repository,
    )

    snapshot = snapshot.model_copy(
        update={
            "evidence_items": tuple(
                item
                for item in snapshot.evidence_items
                if item.type != EvidenceType.SCHEMA
            )
        }
    )

    result = evaluate_flow(analysis, snapshot, context)

    assert result.decision == Decision.CLARIFY
    assert result.coverage.mandatory_coverage == 0.5
    assert result.validated_evidence_bundle is None
    assert any(
        hint.action == "refresh_schema_snapshot"
        for hint in result.recovery_actions
    )


def test_live_schema_hash_change_clarifies(current_repository):
    analysis, snapshot, context = build_flow(
        DEMO_QUESTIONS[5],
        current_repository,
    )

    context = context.model_copy(
        update={"schema_hash": "fixture-new-live-hash"}
    )

    result = evaluate_flow(analysis, snapshot, context)

    assert result.decision == Decision.CLARIFY
    assert (
        ReasonCode.SCHEMA_STALE_RECOVERABLE
        in result.reason_codes
    )
    assert result.validated_evidence_bundle is None


def test_active_permission_denial_rejects(current_repository):
    analysis, snapshot, context = build_flow(
        DEMO_QUESTIONS[5],
        current_repository,
    )

    context = context.model_copy(
        update={
            "permission_decisions": {
                "olist_raw.orders": False,
            }
        }
    )

    result = evaluate_flow(analysis, snapshot, context)

    assert result.decision == Decision.REJECT
    assert ReasonCode.PERMISSION_DENIED in result.reason_codes
    assert result.validated_evidence_bundle is None
    assert result.recovery_actions == ()


def test_invalid_provenance_without_replacement_rejects(
    current_repository,
):
    analysis, snapshot, context = build_flow(
        DEMO_QUESTIONS[5],
        current_repository,
    )

    snapshot = snapshot.model_copy(
        update={
            "evidence_items": tuple(
                item.model_copy(
                    update={"provenance_id": "UNTRUSTED"}
                )
                if item.type == EvidenceType.SCHEMA
                else item
                for item in snapshot.evidence_items
            )
        }
    )

    context = context.model_copy(
        update={
            "available_recovery_actions": frozenset(),
        }
    )

    result = evaluate_flow(analysis, snapshot, context)

    assert result.decision == Decision.REJECT
    assert ReasonCode.PROVENANCE_INVALID in result.reason_codes
    assert result.validated_evidence_bundle is None


def test_member2_critical_conflict_output_rejects(
    current_repository,
):
    analysis, snapshot, context = build_flow(
        DEMO_QUESTIONS[5],
        current_repository,
    )

    requirement_id = next(
        obligation.requirement_id
        for obligation in analysis.required_evidence_obligations
        if obligation.evidence_type == EvidenceType.SCHEMA
    )

    result = evaluate_flow(
        analysis,
        snapshot,
        context,
        {
            requirement_id: EvidenceState.CONFLICT_CRITICAL,
        },
    )

    assert result.decision == Decision.REJECT
    assert requirement_id in result.hard_failures
    assert result.validated_evidence_bundle is None


def test_unsupported_request_stops_before_adapter_access():
    adapter = CatalogAdapter()

    request = QueryRequest(
        database_id=adapter.database_id,
        access_context=AccessContext(
            principal_id="test-user",
            database_role=ROLE,
        ),
        question="Delete orders",
    )

    analysis = analyze_query_intent(
        build_query_intent(request),
        access_context=request.access_context,
    )

    assert analysis.precheck.decision == PrecheckDecision.REJECT

    with pytest.raises(ValueError, match="Rejected queries"):
        retrieve_for_analysis(analysis, adapter=adapter)

    assert adapter.catalog_calls == 0