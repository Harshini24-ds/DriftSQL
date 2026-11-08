"""Offline integration of retrieval, conflict detection and recovery.

PostgreSQL is replaced by a saved-catalog adapter.
No live database, LLM or SQL executor is exercised.
"""

from datetime import datetime, timezone

import pytest

from src.clarification.controller import (
    RecoveryUpdate,
    advance_clarification,
    start_clarification,
)
from src.clarification.planner import RecoveryActionType
from src.contracts.evidence import EvidenceType
from src.eeva.decision import Decision
from src.eeva.reason_codes import ReasonCode
from src.eeva.validation import SourceReference, ValidationContext
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
from src.retrieval import engine
from src.retrieval.direct import DirectRetrievalResult, RetrievalMatch
from src.retrieval.snapshot import (
    build_evidence_snapshot,
    build_validation_snapshot,
)


AS_OF = datetime(2026, 11, 7, tzinfo=timezone.utc)
ROLE = "driftsql_olist_reader"
POLICY = "month3-flow-v1"

SOURCE_TYPES = {
    EvidenceType.SCHEMA: "current_schema_snapshot",
    EvidenceType.METADATA: "approved_metadata_catalog",
    EvidenceType.GLOSSARY: "approved_business_glossary",
    EvidenceType.DOCUMENT: "approved_documentation",
    EvidenceType.RELATIONSHIP: "approved_relationship_evidence",
    EvidenceType.VALUE: "approved_value_profile",
    EvidenceType.PERMISSION: "active_postgresql_role",
}

# Only source operations supported by this offline fixture are available.
AVAILABLE_ACTIONS = frozenset({
    "refresh_schema_snapshot",
    "retrieve_approved_metadata",
    "retrieve_approved_glossary",
    "retrieve_approved_documentation",
    "refresh_relationship_evidence",
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
    """Controlled substitute for active database catalog and role checks."""

    database_id = "olist_primary"
    database_role = ROLE
    schema = "olist_raw"

    def __init__(self, denied_targets=()):
        self.catalog = load_schema_snapshot()
        self.denied_targets = frozenset(denied_targets)
        self.retrieval_calls = 0
        self.schema_calls = 0
        self.permission_calls = 0

    def get_schema_snapshot(self):
        self.schema_calls += 1
        return self.catalog

    def can_select(self, table, column=None):
        self.permission_calls += 1

        if table in self.denied_targets:
            return False

        if (
            column is not None
            and f"{table}.{column}" in self.denied_targets
        ):
            return False

        prefix = f"{self.schema}."

        if not table.startswith(prefix):
            return False

        for entry in self.catalog["tables"]:
            if entry["table_name"] == table.removeprefix(prefix):
                return column is None or any(
                    candidate["column_name"] == column
                    for candidate in entry["columns"]
                )

        return False


@pytest.fixture(scope="module")
def current_repository():
    """Independent current approved records for the fixture."""
    return (
        *build_schema_evidence(),
        *build_metadata_evidence(),
        *build_glossary_evidence(as_of=AS_OF.date()),
        *build_document_evidence(as_of=AS_OF.date()),
        *build_value_evidence(),
    )


def make_analysis(question, repository):
    request = QueryRequest(
        request_id="month3-flow-request",
        database_id="olist_primary",
        access_context=AccessContext(
            principal_id="flow-user",
            database_role=ROLE,
        ),
        question=question,
    )

    analysis = analyze_query_intent(
        build_query_intent(request),
        access_context=request.access_context,
    )

    assert analysis.precheck.decision == PrecheckDecision.ACCEPT

    return expand_alias_obligations(analysis, repository)


def build_context(analysis, adapter, repository):
    permissions = {}
    permission_requirements = []

    for requirement in analysis.required_evidence_obligations:
        if requirement.evidence_type != EvidenceType.PERMISSION:
            continue

        parts = requirement.target.split(".")

        allowed = adapter.can_select(
            ".".join(parts[:2]),
            parts[2] if len(parts) == 3 else None,
        )

        permissions[requirement.target] = allowed

        if allowed:
            permission_requirements.append(requirement)

    permission_items = build_verified_permission_evidence(
        tuple(permission_requirements),
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
        for item in (*repository, *permission_items)
    )

    return ValidationContext(
        request_id=analysis.intent.request_id,
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


def retrieve_snapshot(analysis, adapter, repository):
    context = build_context(analysis, adapter, repository)

    if any(
        allowed is False
        for allowed in context.permission_decisions.values()
    ):
        # Supply denial facts to EEVA without retrieving protected evidence.
        retrieval = DirectRetrievalResult(
            database_id=adapter.database_id,
            matches=tuple(
                RetrievalMatch(
                    requirement_id=requirement.requirement_id,
                    evidence_item_ids=(),
                )
                for requirement
                in analysis.required_evidence_obligations
            ),
            missing_requirement_ids=tuple(
                requirement.requirement_id
                for requirement
                in analysis.required_evidence_obligations
            ),
            complete=False,
            evidence_items=(),
            partial_evidence_items=(),
        )

    else:
        adapter.retrieval_calls += 1

        retrieval = engine.retrieve_for_analysis(
            analysis,
            adapter=adapter,
            as_of=AS_OF.date(),
        )

    snapshot = build_validation_snapshot(
        analysis,
        retrieval,
        freshness_policy_version=POLICY,
    )

    return retrieval, snapshot, context


def start_flow(question, repository, adapter=None):
    if adapter is None:
        adapter = CatalogAdapter()

    analysis = make_analysis(question, repository)

    retrieval, snapshot, context = retrieve_snapshot(
        analysis,
        adapter,
        repository,
    )

    session = start_clarification(
        analysis,
        snapshot,
        context,
        timeout_seconds=3.0,
    )

    return session, retrieval, adapter


def refresh_pending_source(session, adapter, repository):
    """Test-side executor followed by real retrieval and normalization."""
    assert session.pending_plan is not None

    action = session.pending_plan.actions[0]

    assert not action.intent_only
    assert action.type not in {
        RecoveryActionType.PROBE_VALUES,
        RecoveryActionType.CLARIFY_BUSINESS_TERM,
    }

    _, snapshot, context = retrieve_snapshot(
        session.analysis,
        adapter,
        repository,
    )

    return advance_clarification(
        session,
        RecoveryUpdate(
            snapshot=snapshot,
            context=context,
            attempted_action_ids=(action.action_id,),
        ),
    )


@pytest.mark.parametrize("question", DEMO_QUESTIONS)
def test_current_catalog_questions_use_real_conflict_detector(
    question,
    current_repository,
):
    session, retrieval, _ = start_flow(
        question,
        current_repository,
    )

    assert retrieval.complete
    assert session.decision == Decision.GENERATE
    assert session.completed_cycles == 0
    assert session.conflict_report.findings == ()
    assert session.result.coverage.mandatory_coverage == 1.0
    assert session.result.validated_evidence_bundle is not None


@pytest.mark.parametrize(
    "builder,question,action_type",
    [
        (
            "build_schema_evidence",
            "How many orders are recorded?",
            RecoveryActionType.REFRESH_SCHEMA,
        ),
        (
            "build_metadata_evidence",
            "What is the average order-item price?",
            RecoveryActionType.RETRIEVE_METADATA,
        ),
        (
            "build_verified_permission_evidence",
            "How many orders are recorded?",
            RecoveryActionType.REFRESH_PERMISSIONS,
        ),
        (
            "build_value_evidence",
            "How many orders have delivered status?",
            RecoveryActionType.RETRIEVE_VALUE_PROFILE,
        ),
    ],
)
def test_partial_retrieval_recovers_through_controller(
    builder,
    question,
    action_type,
    current_repository,
    monkeypatch,
):
    with monkeypatch.context() as patch:
        patch.setattr(
            engine,
            builder,
            lambda *args, **kwargs: (),
        )

        session, retrieval, adapter = start_flow(
            question,
            current_repository,
        )

    assert not retrieval.complete
    assert retrieval.evidence_items == ()
    assert retrieval.partial_evidence_items

    assert (
        session.snapshot.evidence_items
        == retrieval.partial_evidence_items
    )

    assert session.decision == Decision.CLARIFY
    assert session.pending_plan.actions[0].type == action_type

    # The legacy builder still rejects incomplete evidence.
    with pytest.raises(
        ValueError,
        match="required evidence is missing",
    ):
        build_evidence_snapshot(
            session.analysis,
            retrieval,
            freshness_policy_version=POLICY,
        )

    recovered = refresh_pending_source(
        session,
        adapter,
        current_repository,
    )

    assert recovered.decision == Decision.GENERATE
    assert recovered.completed_cycles == 1
    assert recovered.pending_plan is None
    assert recovered.result.validated_evidence_bundle is not None
    assert adapter.retrieval_calls == 2


def test_repeated_incomplete_retrieval_is_not_progress(
    current_repository,
    monkeypatch,
):
    with monkeypatch.context() as patch:
        patch.setattr(
            engine,
            "build_metadata_evidence",
            lambda *args, **kwargs: (),
        )

        session, _, adapter = start_flow(
            "What is the average order-item price?",
            current_repository,
        )

        stopped = refresh_pending_source(
            session,
            adapter,
            current_repository,
        )

    assert stopped.decision == Decision.REJECT
    assert ReasonCode.RECOVERY_NO_PROGRESS in stopped.result.reason_codes
    assert ReasonCode.RECOVERY_EXHAUSTED in stopped.result.reason_codes
    assert stopped.result.validated_evidence_bundle is None
    assert adapter.retrieval_calls == 2


def test_denied_role_reaches_eeva_without_evidence_retrieval(
    current_repository,
):
    adapter = CatalogAdapter(
        denied_targets={"olist_raw.orders"},
    )

    session, retrieval, _ = start_flow(
        "How many orders are recorded?",
        current_repository,
        adapter,
    )

    assert adapter.retrieval_calls == 0
    assert adapter.schema_calls == 0
    assert retrieval.partial_evidence_items == ()
    assert session.snapshot.evidence_items == ()
    assert session.decision == Decision.REJECT
    assert ReasonCode.PERMISSION_DENIED in session.result.reason_codes
    assert session.pending_plan is None
    assert session.result.validated_evidence_bundle is None


def test_policy_denial_during_recovery_stops_generation(
    current_repository,
    monkeypatch,
):
    with monkeypatch.context() as patch:
        patch.setattr(
            engine,
            "build_schema_evidence",
            lambda *args, **kwargs: (),
        )

        session, _, adapter = start_flow(
            "How many orders are recorded?",
            current_repository,
        )

    adapter.denied_targets = frozenset({"olist_raw.orders"})

    stopped = refresh_pending_source(
        session,
        adapter,
        current_repository,
    )

    assert stopped.decision == Decision.REJECT
    assert ReasonCode.PERMISSION_DENIED in stopped.result.reason_codes
    assert stopped.result.validated_evidence_bundle is None
    assert adapter.retrieval_calls == 1


@pytest.mark.parametrize(
    "change,error",
    [
        (
            {"matches": ()},
            "exactly the query obligations",
        ),
        (
            {"complete": False},
            "completeness disagrees",
        ),
        (
            {"missing_requirement_ids": ("UNKNOWN",)},
            "unknown missing requirement",
        ),
    ],
)
def test_validation_snapshot_rejects_inconsistent_retrieval(
    change,
    error,
    current_repository,
):
    session, retrieval, _ = start_flow(
        "How many orders are recorded?",
        current_repository,
    )

    with pytest.raises(ValueError, match=error):
        build_validation_snapshot(
            session.analysis,
            retrieval.model_copy(update=change),
            freshness_policy_version=POLICY,
        )


def test_unresolved_intent_reaches_eeva_then_returns_after_clarification(
    current_repository,
):
    analysis = make_analysis(
        "How many orders are recorded?",
        current_repository,
    )

    analysis = analysis.model_copy(
        update={
            "intent": analysis.intent.model_copy(
                update={
                    "unresolved_terms": ("meaning_to_clarify",),
                }
            )
        }
    )

    adapter = CatalogAdapter()

    retrieval, snapshot, context = retrieve_snapshot(
        analysis,
        adapter,
        current_repository,
    )

    with pytest.raises(
        ValueError,
        match="query terms are unresolved",
    ):
        build_evidence_snapshot(
            analysis,
            retrieval,
            freshness_policy_version=POLICY,
        )

    session = start_clarification(
        analysis,
        snapshot,
        context,
        timeout_seconds=3.0,
    )

    assert session.decision == Decision.CLARIFY
    assert session.awaiting_user_input

    clarified_intent = analysis.intent.model_copy(
        update={"unresolved_terms": ()}
    )

    recovered = advance_clarification(
        session,
        RecoveryUpdate(
            snapshot=snapshot,
            context=context,
            clarified_intent=clarified_intent,
        ),
    )

    assert recovered.decision == Decision.GENERATE
    assert recovered.completed_cycles == 1


def test_unsupported_query_is_rejected_before_retrieval():
    adapter = CatalogAdapter()

    request = QueryRequest(
        database_id=adapter.database_id,
        access_context=AccessContext(
            principal_id="test",
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
        engine.retrieve_for_analysis(
            analysis,
            adapter=adapter,
        )

    assert adapter.retrieval_calls == 0
    assert adapter.schema_calls == 0
    assert adapter.permission_calls == 0