"""Plan approved recovery actions for EEVA clarification results.

The planner does not execute actions, change permissions, modify
authoritative facts, or bypass EEVA.
"""

from enum import Enum
from hashlib import sha256
import json
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.contracts.evidence import EvidenceType
from src.eeva.decision import Decision, EEVAResult
from src.eeva.evidence_state import (
    HARD_FAILURE_STATES,
    RECOVERABLE_STATES,
    EvidenceState,
)
from src.eeva.validation import ValidationContext
from src.query_intelligence.obligations import EvidenceObligation


class RecoveryActionType(str, Enum):
    REFRESH_SCHEMA = "refresh_schema_snapshot"
    RETRIEVE_METADATA = "retrieve_approved_metadata"
    RETRIEVE_GLOSSARY = "retrieve_approved_glossary"
    RETRIEVE_DOCUMENTATION = "retrieve_approved_documentation"
    REFRESH_RELATIONSHIPS = "refresh_relationship_evidence"
    PROBE_VALUES = "run_bounded_read_only_probe"
    RETRIEVE_VALUE_PROFILE = "retrieve_approved_value_profile"
    REFRESH_PERMISSIONS = "refresh_active_role_policy"
    CLARIFY_BUSINESS_TERM = "clarify_business_term"


class RecoveryAction(BaseModel):
    """One pending approved action.

    Automated actions have a timeout.
    User clarification changes intent only and has no execution timeout.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)

    type: RecoveryActionType
    target: str = Field(min_length=1)
    target_source_types: tuple[str, ...]

    expected_change: str = Field(min_length=1)
    attempt: int = Field(ge=1, le=2)

    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
    )

    status: Literal["PENDING"] = "PENDING"

    @property
    def intent_only(self) -> bool:
        return self.type == RecoveryActionType.CLARIFY_BUSINESS_TERM

    @model_validator(mode="after")
    def validate_action_boundary(self) -> "RecoveryAction":
        if self.intent_only:
            if self.target_source_types:
                raise ValueError(
                    "User clarification cannot be an authoritative source."
                )

            if self.timeout_seconds is not None:
                raise ValueError(
                    "User clarification has no automated execution timeout."
                )

        elif (
            not self.target_source_types
            or self.timeout_seconds is None
        ):
            raise ValueError(
                "Automated recovery requires approved sources and a timeout."
            )

        return self


class RecoveryPlan(BaseModel):
    """Recovery choices for one EEVA result and clarification attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    database_id: str
    database_role: str
    snapshot_id: str

    attempt: int = Field(ge=1, le=2)

    actions: tuple[RecoveryAction, ...] = ()
    unresolved_requirement_ids: tuple[str, ...] = ()
    unresolved_terms: tuple[str, ...] = ()

    @property
    def needs_user_input(self) -> bool:
        return bool(self.unresolved_terms) or any(
            action.intent_only
            for action in self.actions
        )


# Action policy: required evidence type, approved source categories,
# and the expected change. No arbitrary action names are executable.
_ACTION_POLICY = {
    RecoveryActionType.REFRESH_SCHEMA: (
        EvidenceType.SCHEMA,
        (
            "active_postgresql_catalog",
            "current_schema_snapshot",
        ),
        "Retrieve current authoritative schema evidence.",
    ),
    RecoveryActionType.RETRIEVE_METADATA: (
        EvidenceType.METADATA,
        ("approved_metadata_catalog",),
        "Retrieve current approved field descriptions and units.",
    ),
    RecoveryActionType.RETRIEVE_GLOSSARY: (
        EvidenceType.GLOSSARY,
        ("approved_business_glossary",),
        "Retrieve the current applicable approved business definition.",
    ),
    RecoveryActionType.RETRIEVE_DOCUMENTATION: (
        EvidenceType.DOCUMENT,
        ("approved_documentation",),
        "Retrieve current applicable approved documentation.",
    ),
    RecoveryActionType.REFRESH_RELATIONSHIPS: (
        EvidenceType.RELATIONSHIP,
        (
            "live_pk_fk_catalog",
            "approved_relationship_evidence",
        ),
        "Retrieve current approved relationship evidence.",
    ),
    RecoveryActionType.PROBE_VALUES: (
        EvidenceType.VALUE,
        ("bounded_read_only_value_probe",),
        "Obtain value evidence through a bounded read-only probe.",
    ),
    RecoveryActionType.RETRIEVE_VALUE_PROFILE: (
        EvidenceType.VALUE,
        ("approved_value_profile",),
        "Retrieve a current approved value profile.",
    ),
    RecoveryActionType.REFRESH_PERMISSIONS: (
        EvidenceType.PERMISSION,
        ("active_postgresql_role",),
        "Recheck the active role without changing its permissions.",
    ),
    RecoveryActionType.CLARIFY_BUSINESS_TERM: (
        EvidenceType.GLOSSARY,
        (),
        "Clarify intent among approved business interpretations.",
    ),
}


def _action_id(
    result: EEVAResult,
    obligation: EvidenceObligation,
    action_type: RecoveryActionType,
    attempt: int,
) -> str:
    payload = json.dumps(
        [
            result.request_id,
            result.database_id,
            result.snapshot_id,
            obligation.requirement_id,
            obligation.target,
            action_type.value,
            attempt,
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )

    digest = sha256(payload.encode("utf-8")).hexdigest()
    return f"REC-{digest}"


def build_recovery_plan(
    result: EEVAResult,
    obligations: tuple[EvidenceObligation, ...],
    context: ValidationContext,
    *,
    attempt: int,
    timeout_seconds: float,
    attempted_actions: frozenset[tuple[str, str]] = frozenset(),
) -> RecoveryPlan:
    """Choose at most one approved action per recoverable obligation.

    The caller supplies the automated-action timeout.

    attempted_actions contains pairs of:
        (requirement_id, action_name)

    The controller owns the two-cycle limit and decides when another
    attempt is permitted.
    """
    if (
        isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt not in (1, 2)
    ):
        raise ValueError("Recovery attempt must be 1 or 2.")

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError(
            "Automated-action timeout must be finite and positive."
        )

    if (
        result.request_id != context.request_id
        or result.database_id != context.database_id
    ):
        raise ValueError(
            "EEVA result and context must use the same request and database."
        )

    base = {
        "request_id": result.request_id,
        "database_id": result.database_id,
        "database_role": context.database_role,
        "snapshot_id": result.snapshot_id,
        "attempt": attempt,
    }

    # GENERATE and REJECT do not authorize recovery.
    if result.decision != Decision.CLARIFY:
        return RecoveryPlan(**base)

    obligations_by_id = {
        obligation.requirement_id: obligation
        for obligation in obligations
    }

    states_by_id = {
        state.requirement_id: state
        for state in result.evidence_states
    }

    if len(obligations_by_id) != len(obligations):
        raise ValueError("Duplicate obligation requirement IDs.")

    if len(states_by_id) != len(result.evidence_states):
        raise ValueError("Duplicate state requirement IDs.")

    if obligations_by_id.keys() != states_by_id.keys():
        raise ValueError(
            "Exactly one state is required for every obligation."
        )

    for obligation in obligations:
        if (
            obligation.request_id != result.request_id
            or obligation.database_id != result.database_id
        ):
            raise ValueError(
                "Obligations must belong to the EEVA request and database."
            )

        if (
            obligation.mandatory
            and context.permission_decisions.get(
                obligation.target
            ) is False
        ):
            raise ValueError(
                "Authorization changed; run EEVA again before recovery."
            )

    if result.hard_failures or any(
        state.state in HARD_FAILURE_STATES
        for state in result.evidence_states
    ):
        raise ValueError(
            "A clarification result cannot contain hard failures."
        )

    hinted_actions = {}

    for hint in result.recovery_actions:
        if hint.requirement_id not in obligations_by_id:
            raise ValueError(
                "Recovery hint references an unknown requirement."
            )

        hinted_actions.setdefault(
            hint.requirement_id,
            set(),
        ).add(hint.action)

    planned = []
    unresolved = []

    for obligation in obligations:
        state = states_by_id[obligation.requirement_id]

        if state.state not in RECOVERABLE_STATES:
            continue

        permitted_names = (
            set(obligation.recovery_actions)
            & set(state.recovery_actions)
            & hinted_actions.get(obligation.requirement_id, set())
            & context.available_recovery_actions
        )

        selected = None

        # Preserve the approved ordering declared by the obligation.
        for action_name in obligation.recovery_actions:
            if action_name not in permitted_names:
                continue

            if (
                obligation.requirement_id,
                action_name,
            ) in attempted_actions:
                continue

            try:
                action_type = RecoveryActionType(action_name)
            except ValueError:
                continue

            required_type, source_types, expected_change = (
                _ACTION_POLICY[action_type]
            )

            if obligation.evidence_type != required_type:
                continue

            if (
                action_type
                == RecoveryActionType.CLARIFY_BUSINESS_TERM
            ):
                # A user may disambiguate approved meanings.
                # A user cannot replace missing authoritative facts
                # or settle contradictory authoritative sources.
                if (
                    state.state
                    != EvidenceState.AMBIGUOUS_RECOVERABLE
                ):
                    continue

                approved_sources = ()
                action_timeout = None

            else:
                approved_sources = tuple(
                    source_type
                    for source_type in source_types
                    if source_type in obligation.expected_source_types
                )

                if not approved_sources:
                    continue

                action_timeout = float(timeout_seconds)

            selected = RecoveryAction(
                action_id=_action_id(
                    result,
                    obligation,
                    action_type,
                    attempt,
                ),
                requirement_id=obligation.requirement_id,
                type=action_type,
                target=obligation.target,
                target_source_types=approved_sources,
                expected_change=expected_change,
                attempt=attempt,
                timeout_seconds=action_timeout,
            )
            break

        if selected is None:
            unresolved.append(obligation.requirement_id)
        else:
            planned.append(selected)

    return RecoveryPlan(
        **base,
        actions=tuple(planned),
        unresolved_requirement_ids=tuple(unresolved),
        unresolved_terms=result.unresolved_terms,
    )