"""Bounded clarification and recovery state transitions.

RecoveryUpdate must be constructed by trusted server code after approved
recovery actions. Never accept its snapshot or context directly from a user.

The controller:
- evaluates real conflict assessments and EEVA;
- permits at most two recovery cycles;
- avoids repeating the same action within a cycle;
- detects unchanged or repeated material evidence states;
- rejects exhausted recovery;
- requires every update to pass EEVA again.
"""

from hashlib import sha256
import json

from pydantic import BaseModel, ConfigDict, Field

from src.clarification.planner import (
    RecoveryPlan,
    build_recovery_plan,
)
from src.contracts.evidence import EvidenceSnapshot
from src.eeva.conflicts import ConflictReport, detect_conflicts
from src.eeva.decision import Decision, EEVAResult, evaluate_evidence
from src.eeva.evidence_state import EvidenceState
from src.eeva.reason_codes import ReasonCode
from src.eeva.validation import (
    ValidationContext,
    _candidate_state,
)
from src.query_intelligence.alias_expansion import RESOLVABLE_TYPES
from src.query_intelligence.aliases import (
    AliasResolutionStatus,
    resolve_alias,
)
from src.query_intelligence.models import QueryIntent
from src.query_intelligence.obligations import EXPECTED_SOURCE_TYPES
from src.query_intelligence.pipeline import (
    QueryAnalysisResult,
    analyze_query_intent,
)
from src.query_intelligence.precheck import PrecheckDecision


MAX_CLARIFICATION_CYCLES = 2


class RecoveryUpdate(BaseModel):
    """Server-normalized outcome of approved recovery work.

    attempted_action_ids may include failed actions. Progress is established
    from the resulting evidence, not from an action's claimed success.

    clarified_intent must come from trusted QIM processing of a user answer
    or an approved interpretation selection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: EvidenceSnapshot
    context: ValidationContext

    attempted_action_ids: tuple[str, ...] = ()
    clarified_intent: QueryIntent | None = None


class ClarificationSession(BaseModel):
    """Server-owned state for one request.

    Keep this state on the server. A client must not reset its counters,
    replace its trusted context, or submit a fabricated session.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    analysis: QueryAnalysisResult
    snapshot: EvidenceSnapshot
    context: ValidationContext

    result: EEVAResult
    conflict_report: ConflictReport

    timeout_seconds: float = Field(
        gt=0,
        allow_inf_nan=False,
        strict=True,
    )

    completed_cycles: int = Field(
        default=0,
        ge=0,
        le=MAX_CLARIFICATION_CYCLES,
    )

    material_fingerprint: str
    seen_fingerprints: tuple[str, ...]

    # Actions attempted within the current recovery cycle.
    attempted_actions: frozenset[tuple[str, str]] = frozenset()
    intent_question_attempted: bool = False

    pending_plan: RecoveryPlan | None = None

    @property
    def decision(self) -> Decision:
        return self.result.decision

    @property
    def awaiting_user_input(self) -> bool:
        return (
            self.pending_plan is not None
            and self.pending_plan.needs_user_input
        )


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _check_frame(analysis, snapshot, context):
    if (
        analysis.intent.request_id != snapshot.request_id
        or snapshot.request_id != context.request_id
        or analysis.intent.database_id != snapshot.database_id
        or snapshot.database_id != context.database_id
    ):
        raise ValueError(
            "Analysis, snapshot and context must share request and database."
        )

    if (
        analysis.access_context is None
        or analysis.access_context.database_role != context.database_role
    ):
        raise ValueError(
            "Clarification requires the same trusted active role."
        )

    if (
        snapshot.freshness_policy_version
        != context.freshness_policy_version
    ):
        raise ValueError(
            "Snapshot and context freshness policies must match."
        )


def _approved_records(analysis, snapshot, context):
    """Select current authenticated records for alias normalization."""
    if not analysis.required_evidence_obligations:
        return ()

    references = {
        (
            reference.source,
            reference.provenance_id,
            reference.evidence_type,
            reference.key,
        ): reference
        for reference in context.sources
    }

    approved = []

    for item in snapshot.evidence_items:
        reference = references.get(
            (
                item.source,
                item.provenance_id,
                item.type,
                item.key,
            )
        )

        if (
            reference is None
            or item.access_label not in context.allowed_access_labels
        ):
            continue

        target = (
            item.value.get("target", item.key)
            if isinstance(item.value, dict)
            else item.key
        )

        if (
            isinstance(target, str)
            and context.permission_decisions.get(target) is False
        ):
            continue

        expected_sources = tuple(
            dict.fromkeys(
                (
                    *EXPECTED_SOURCE_TYPES[item.type],
                    *(
                        source_type
                        for obligation
                        in analysis.required_evidence_obligations
                        if obligation.evidence_type == item.type
                        for source_type
                        in obligation.expected_source_types
                    ),
                )
            )
        )

        probe = analysis.required_evidence_obligations[0].model_copy(
            update={"expected_source_types": expected_sources}
        )

        if (
            _candidate_state(
                item,
                reference,
                probe,
                snapshot,
                context,
            )
            == EvidenceState.PASS
        ):
            approved.append(item)

    return tuple(approved)


def _normalize_analysis(analysis, snapshot, context):
    """Resolve aliases only from current approved evidence.

    Ambiguous aliases remain unresolved and return through clarification.
    They are never resolved by selecting an arbitrary candidate.
    """
    records = _approved_records(analysis, snapshot, context)

    obligations = []
    resolved_terms = set()
    ambiguous_terms = []

    for obligation in analysis.required_evidence_obligations:
        if obligation.evidence_type not in RESOLVABLE_TYPES:
            obligations.append(obligation)
            continue

        resolution = resolve_alias(obligation.target, records)

        if resolution.status == AliasResolutionStatus.MATCHED:
            resolved_terms.add(obligation.target.casefold())

            obligation = obligation.model_copy(
                update={
                    "target": resolution.candidates[0].target,
                }
            )

        elif resolution.status == AliasResolutionStatus.AMBIGUOUS:
            ambiguous_terms.append(obligation.target)

        obligations.append(obligation)

    unresolved = [
        term
        for term in analysis.intent.unresolved_terms
        if term.casefold() not in resolved_terms
    ]
    unresolved.extend(ambiguous_terms)

    intent = analysis.intent.model_copy(
        update={
            "unresolved_terms": tuple(dict.fromkeys(unresolved)),
        }
    )

    return analysis.model_copy(
        update={
            "intent": intent,
            "required_evidence_obligations": tuple(obligations),
        }
    )


def _evaluate(analysis, snapshot, context):
    report = detect_conflicts(
        analysis.required_evidence_obligations,
        snapshot,
        context,
    )

    result = evaluate_evidence(
        analysis,
        snapshot,
        context,
        conflict_assessments=report.assessments,
    )

    return result, report


def _material_fingerprint(
    analysis,
    snapshot,
    context,
    result,
    report,
):
    """Ignore fresh IDs, retrieval timestamps and diagnostic metadata."""
    approved = _approved_records(analysis, snapshot, context)

    relevant_ids = {
        evidence_id
        for assessment in report.assessments
        for evidence_id in assessment.evidence_item_ids
    }

    relevant_ids.update(
        evidence_id
        for finding in report.findings
        for evidence_id in finding.evidence_item_ids
    )

    evidence_rows = set()

    for item in approved:
        if item.evidence_id not in relevant_ids:
            continue

        row = item.model_dump(mode="json")
        row.pop("evidence_id", None)
        row.pop("retrieved_at", None)
        row.pop("linked_requirement_ids", None)

        evidence_rows.add(_canonical_json(row))

    obligations_by_id = {
        obligation.requirement_id: obligation
        for obligation in analysis.required_evidence_obligations
    }

    obligation_rows = set()
    state_rows = set()

    for obligation in analysis.required_evidence_obligations:
        obligation_rows.add(
            _canonical_json(
                [
                    obligation.evidence_type.value,
                    obligation.target,
                    obligation.constraint_type,
                    obligation.mandatory,
                    sorted(obligation.expected_source_types),
                ]
            )
        )

    for state in result.evidence_states:
        obligation = obligations_by_id[state.requirement_id]

        state_rows.add(
            _canonical_json(
                [
                    obligation.evidence_type.value,
                    obligation.target,
                    obligation.constraint_type,
                    obligation.mandatory,
                    state.state.value,
                ]
            )
        )

    relevant_source_keys = {
        (
            item.source,
            item.provenance_id,
            item.type,
            item.key,
        )
        for item in snapshot.evidence_items
        if item.evidence_id in relevant_ids
    }

    source_rows = {
        _canonical_json(reference.model_dump(mode="json"))
        for reference in context.sources
        if (
            reference.source,
            reference.provenance_id,
            reference.evidence_type,
            reference.key,
        ) in relevant_source_keys
    }

    required_targets = {
        obligation.target
        for obligation in analysis.required_evidence_obligations
    }

    intent = analysis.intent.model_dump(mode="json")
    intent.pop("request_id", None)
    intent.pop("database_id", None)

    payload = {
        "intent": intent,
        "obligations": sorted(obligation_rows),
        "evidence": sorted(evidence_rows),
        "sources": sorted(source_rows),
        "states": sorted(state_rows),
        "schema_hash": context.schema_hash,
        "database_role": context.database_role,
        "freshness_policy_version": context.freshness_policy_version,
        "allowed_access_labels": sorted(context.allowed_access_labels),
        "permission_decisions": {
            target: context.permission_decisions[target]
            for target in sorted(required_targets)
            if target in context.permission_decisions
        },
    }

    return sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()


def _reject_exhausted(
    session,
    *,
    no_progress=False,
    consume_cycle=False,
):
    reasons = list(session.result.reason_codes)

    if no_progress:
        reasons.append(ReasonCode.RECOVERY_NO_PROGRESS)

    reasons.append(ReasonCode.RECOVERY_EXHAUSTED)

    result = session.result.model_copy(
        update={
            "decision": Decision.REJECT,
            "reason_codes": tuple(dict.fromkeys(reasons)),
            "recovery_actions": (),
            "validated_evidence_bundle": None,
        }
    )

    return session.model_copy(
        update={
            "result": result,
            "pending_plan": None,
            "completed_cycles": min(
                MAX_CLARIFICATION_CYCLES,
                session.completed_cycles + int(consume_cycle),
            ),
        }
    )


def _schedule(session, *, no_progress=False):
    if session.result.decision != Decision.CLARIFY:
        return session.model_copy(
            update={"pending_plan": None}
        )

    if session.completed_cycles >= MAX_CLARIFICATION_CYCLES:
        return _reject_exhausted(session)

    plan = build_recovery_plan(
        session.result,
        session.analysis.required_evidence_obligations,
        session.context,
        attempt=session.completed_cycles + 1,
        timeout_seconds=session.timeout_seconds,
        attempted_actions=session.attempted_actions,
    )

    if session.intent_question_attempted:
        plan = plan.model_copy(
            update={"unresolved_terms": ()}
        )

    if not plan.actions and not plan.unresolved_terms:
        return _reject_exhausted(
            session,
            no_progress=no_progress,
            consume_cycle=no_progress,
        )

    return session.model_copy(
        update={"pending_plan": plan}
    )


def start_clarification(
    analysis: QueryAnalysisResult,
    snapshot: EvidenceSnapshot,
    context: ValidationContext,
    *,
    timeout_seconds: float,
) -> ClarificationSession:
    """Evaluate the initial snapshot and prepare approved recovery."""
    analysis = analysis.model_copy(deep=True)
    snapshot = snapshot.model_copy(deep=True)
    context = context.model_copy(deep=True)

    _check_frame(analysis, snapshot, context)

    analysis = _normalize_analysis(
        analysis,
        snapshot,
        context,
    )

    result, report = _evaluate(
        analysis,
        snapshot,
        context,
    )

    fingerprint = _material_fingerprint(
        analysis,
        snapshot,
        context,
        result,
        report,
    )

    session = ClarificationSession(
        analysis=analysis,
        snapshot=snapshot,
        context=context,
        result=result,
        conflict_report=report,
        timeout_seconds=timeout_seconds,
        material_fingerprint=fingerprint,
        seen_fingerprints=(fingerprint,),
    )

    return _schedule(session)


def advance_clarification(
    session: ClarificationSession,
    update: RecoveryUpdate,
) -> ClarificationSession:
    """Accept a server-built recovery update and run EEVA again.

    Approved alternatives can be tried within the same cycle when an
    action produces no change. An action cannot repeat within that cycle.

    Material progress completes a cycle. Unchanged evidence with no
    remaining approved alternative causes recovery exhaustion.
    """
    plan = session.pending_plan

    if (
        session.result.decision != Decision.CLARIFY
        or plan is None
    ):
        raise ValueError(
            "This session has no pending clarification recovery."
        )

    if session.completed_cycles >= MAX_CLARIFICATION_CYCLES:
        raise ValueError("Clarification cycle limit reached.")

    actions_by_id = {
        action.action_id: action
        for action in plan.actions
    }

    attempted_ids = update.attempted_action_ids

    if len(attempted_ids) != len(set(attempted_ids)):
        raise ValueError("Duplicate attempted action IDs.")

    if not set(attempted_ids) <= actions_by_id.keys():
        raise ValueError(
            "Recovery update references an unplanned action."
        )

    has_clarification = update.clarified_intent is not None

    if has_clarification and not plan.needs_user_input:
        raise ValueError(
            "This plan does not authorize user clarification."
        )

    if not attempted_ids and not has_clarification:
        raise ValueError(
            "An update must identify an attempted action or clarification."
        )

    snapshot = update.snapshot.model_copy(deep=True)
    context = update.context.model_copy(deep=True)

    if (
        context.database_role != session.context.database_role
        or context.freshness_policy_version
        != session.context.freshness_policy_version
    ):
        raise ValueError(
            "Recovery cannot change the active role or freshness policy."
        )

    analysis = session.analysis.model_copy(deep=True)

    if has_clarification:
        intent = update.clarified_intent.model_copy(deep=True)

        if (
            intent.request_id != analysis.intent.request_id
            or intent.database_id != analysis.intent.database_id
        ):
            raise ValueError(
                "Clarification cannot change request or database identity."
            )

        analysis = analyze_query_intent(
            intent,
            access_context=session.analysis.access_context,
        )

        if analysis.precheck.decision != PrecheckDecision.ACCEPT:
            raise ValueError(
                "Clarification must pass QIM_PRECHECK before recovery."
            )

    _check_frame(analysis, snapshot, context)

    analysis = _normalize_analysis(
        analysis,
        snapshot,
        context,
    )

    result, report = _evaluate(
        analysis,
        snapshot,
        context,
    )

    fingerprint = _material_fingerprint(
        analysis,
        snapshot,
        context,
        result,
        report,
    )

    attempted = set(session.attempted_actions)

    for action_id in attempted_ids:
        action = actions_by_id[action_id]

        attempted.add(
            (
                action.requirement_id,
                action.type.value,
            )
        )

    if has_clarification:
        for action in plan.actions:
            if action.intent_only:
                attempted.add(
                    (
                        action.requirement_id,
                        action.type.value,
                    )
                )

    next_session = session.model_copy(
        update={
            "analysis": analysis,
            "snapshot": snapshot,
            "context": context,
            "result": result,
            "conflict_report": report,
            "material_fingerprint": fingerprint,
            "attempted_actions": frozenset(attempted),
            "intent_question_attempted": (
                session.intent_question_attempted
                or has_clarification
            ),
            "pending_plan": None,
        }
    )

    if result.decision != Decision.CLARIFY:
        return next_session.model_copy(
            update={
                "completed_cycles": session.completed_cycles + 1,
                "seen_fingerprints": (
                    *session.seen_fingerprints,
                    fingerprint,
                ),
            }
        )

    if fingerprint != session.material_fingerprint:
        if fingerprint in session.seen_fingerprints:
            return _reject_exhausted(
                next_session,
                no_progress=True,
                consume_cycle=True,
            )

        next_session = next_session.model_copy(
            update={
                "completed_cycles": session.completed_cycles + 1,
                "seen_fingerprints": (
                    *session.seen_fingerprints,
                    fingerprint,
                ),
                "attempted_actions": frozenset(),
                "intent_question_attempted": False,
            }
        )

        return _schedule(next_session)

    return _schedule(next_session, no_progress=True)