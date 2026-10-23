"""Build required evidence obligations, R(q), from a query intent."""

from __future__ import annotations

from hashlib import sha256
from itertools import combinations

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.evidence import EvidenceType
from src.evidence.schema import load_schema_snapshot
from src.query_intelligence.models import QueryIntent


class EvidenceObligation(BaseModel):
    """One evidence requirement needed to answer a query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    database_id: str = Field(min_length=1)
    evidence_type: EvidenceType
    target: str = Field(min_length=1)
    mandatory: bool = True
    expected_source_types: tuple[str, ...] = Field(min_length=1)
    constraint_type: str = Field(min_length=1)
    recovery_actions: tuple[str, ...] = ()
    diagnostic_weight: float = Field(default=1.0, gt=0)
    reason: str = Field(min_length=1)


EXPECTED_SOURCE_TYPES = {
    EvidenceType.SCHEMA: ("active_postgresql_catalog", "current_schema_snapshot"),
    EvidenceType.METADATA: ("approved_metadata_catalog",),
    EvidenceType.GLOSSARY: ("approved_business_glossary",),
    EvidenceType.DOCUMENT: ("approved_documentation",),
    EvidenceType.RELATIONSHIP: (
        "live_pk_fk_catalog",
        "approved_relationship_evidence",
    ),
    EvidenceType.VALUE: (
        "bounded_read_only_value_probe",
        "approved_value_profile",
    ),
    EvidenceType.PERMISSION: ("active_postgresql_role",),
}

RECOVERY_ACTIONS = {
    EvidenceType.SCHEMA: ("refresh_schema_snapshot",),
    EvidenceType.METADATA: ("retrieve_approved_metadata",),
    EvidenceType.GLOSSARY: (
        "retrieve_approved_glossary",
        "clarify_business_term",
    ),
    EvidenceType.DOCUMENT: ("retrieve_approved_documentation",),
    EvidenceType.RELATIONSHIP: ("refresh_relationship_evidence",),
    EvidenceType.VALUE: (
        "run_bounded_read_only_probe",
        "retrieve_approved_value_profile",
    ),
    EvidenceType.PERMISSION: ("refresh_active_role_policy",),
}


def build_required_evidence_obligations(
    intent: QueryIntent,
) -> tuple[EvidenceObligation, ...]:
    """Return the evidence required to resolve this intent."""
    obligations: dict[tuple[EvidenceType, str], EvidenceObligation] = {}

    def add(
        evidence_type: EvidenceType,
        target: str,
        reason: str,
        *,
        constraint_type: str,
        mandatory: bool = True,
    ) -> None:
        key = (evidence_type, target)
        if key in obligations:
            existing = obligations[key]
            if (
                evidence_type == EvidenceType.PERMISSION
                and constraint_type == "role_allows_join_key_read"
                and existing.constraint_type == "role_allows_column_read"
            ):
                obligations[key] = existing.model_copy(
                    update={
                        "constraint_type": constraint_type,
                        "reason": reason,
                    }
                )
            return

        digest = sha256(
            f"{evidence_type.value}:{target}".encode("utf-8")
        ).hexdigest()[:12]

        obligations[key] = EvidenceObligation(
            requirement_id=(
                f"REQ-{intent.request_id}-{evidence_type.value}-{digest}"
            ),
            request_id=intent.request_id,
            database_id=intent.database_id,
            evidence_type=evidence_type,
            target=target,
            mandatory=mandatory,
            expected_source_types=EXPECTED_SOURCE_TYPES[evidence_type],
            constraint_type=constraint_type,
            recovery_actions=RECOVERY_ACTIONS[evidence_type],
            diagnostic_weight=1.0 if mandatory else 0.5,
            reason=reason,
        )

    # Each referenced table needs schema evidence and an access check.
    for entity in intent.entities:
        add(
            EvidenceType.SCHEMA,
            entity,
            "Confirm the entity exists in the schema.",
            constraint_type="entity_exists",
        )
        add(
            EvidenceType.PERMISSION,
            entity,
            "Confirm access is allowed.",
            constraint_type="role_allows_access",
        )

    # Numeric measures need authoritative field semantics; ordinary grouping
    # columns need structural and access checks, not invented business terms.
    built_in_measures = {"count", "count(*)"}
    for term in intent.measures:
        normalized = term.strip()
        if not normalized or normalized.casefold() in built_in_measures:
            continue

        add(
            EvidenceType.SCHEMA,
            normalized,
            "Resolve the measure to a schema field.",
            constraint_type="field_exists",
        )
        add(
            EvidenceType.METADATA,
            normalized,
            "Confirm the measure meaning and units.",
            constraint_type="field_description",
        )
        add(
            EvidenceType.PERMISSION,
            normalized,
            "Confirm the active role may read the required field.",
            constraint_type="role_allows_column_read",
        )

    for term in intent.dimensions:
        normalized = term.strip()
        if not normalized:
            continue
        add(
            EvidenceType.SCHEMA,
            normalized,
            "Confirm the grouping field exists.",
            constraint_type="field_exists",
        )
        add(
            EvidenceType.PERMISSION,
            normalized,
            "Confirm the active role may read the grouping field.",
            constraint_type="role_allows_column_read",
        )

    for query_filter in intent.filters:
        field = query_filter.field.strip()
        if not field:
            continue
        add(
            EvidenceType.SCHEMA,
            field,
            "Confirm the filter field exists.",
            constraint_type="field_exists",
        )
        add(
            EvidenceType.PERMISSION,
            field,
            "Confirm the active role may read the filter field.",
            constraint_type="role_allows_column_read",
        )
        add(
            EvidenceType.VALUE,
            field,
            f"Confirm filter value {query_filter.value!r} is valid for this field.",
            constraint_type="filter_value_exists",
        )

    # Multiple entities require verified join-path evidence.
    snapshot = load_schema_snapshot() if len(intent.entities) > 1 else None
    uses_non_strict_lookup = False
    for left, right in combinations(intent.entities, 2):
        target = f"{left} -> {right}"
        matched_relationships = []
        schema_prefix = ""
        if snapshot is not None:
            schema_prefix = f"{snapshot['schema']}."
            left_table = left.removeprefix(schema_prefix)
            right_table = right.removeprefix(schema_prefix)
            for relation_list in ("strict_relationships", "non_strict_lookups"):
                for relationship in snapshot[relation_list]:
                    if {left_table, right_table} == {
                        relationship["child_table"],
                        relationship["parent_table"],
                    }:
                        matched_relationships.append(
                            (relation_list, relationship)
                        )

        is_lookup = any(
            relation_list == "non_strict_lookups"
            for relation_list, _ in matched_relationships
        )
        uses_non_strict_lookup = uses_non_strict_lookup or is_lookup
        add(
            EvidenceType.RELATIONSHIP,
            target,
            "Verify a relationship between the referenced entities.",
            constraint_type=(
                "non_strict_lookup_relationship" if is_lookup
                else "join_relationship"
            ),
        )

        for _, relationship in matched_relationships:
            child = relationship["child_table"]
            parent = relationship["parent_table"]
            for column in relationship["child_columns"]:
                add(
                    EvidenceType.PERMISSION,
                    f"{schema_prefix}{child}.{column}",
                    "Confirm the active role may read this join key.",
                    constraint_type="role_allows_join_key_read",
                )
            for column in relationship["parent_columns"]:
                add(
                    EvidenceType.PERMISSION,
                    f"{schema_prefix}{parent}.{column}",
                    "Confirm the active role may read this join key.",
                    constraint_type="role_allows_join_key_read",
                )

    # Multi-table queries also need applicable documented dataset rules.
    if len(intent.entities) > 1:
        document_scopes = {
            entity.rsplit(".", maxsplit=1)[0]
            if "." in entity
            else entity
            for entity in intent.entities
        }
        for scope in sorted(document_scopes):
            add(
                EvidenceType.DOCUMENT,
                scope,
                "Check documented rules for this dataset.",
                constraint_type=(
                    "lookup_coverage_rule"
                    if uses_non_strict_lookup
                    else "applicable_dataset_rules"
                ),
            )

    return tuple(obligations.values())
