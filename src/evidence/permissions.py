"""Build evidence from successful checks of the active PostgreSQL role."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from src.contracts.evidence import EvidenceItem, EvidenceType
from src.query_intelligence.obligations import EvidenceObligation


PERMISSION_AUTHORITY = 100
PERMISSION_SOURCE_VERSION = "live-role-check-v1"


def build_verified_permission_evidence(
    obligations: tuple[EvidenceObligation, ...],
    *,
    database_id: str,
    database_role: str,
) -> tuple[EvidenceItem, ...]:
    """Represent permission obligations confirmed by the live role check."""
    targets: dict[str, list[str]] = {}

    for obligation in obligations:
        if (
            obligation.evidence_type == EvidenceType.PERMISSION
            and obligation.database_id == database_id
        ):
            targets.setdefault(obligation.target, []).append(
                obligation.requirement_id
            )

    retrieved_at = datetime.now(timezone.utc)
    items: list[EvidenceItem] = []

    for target, requirement_ids in targets.items():
        value = {
            "target": target,
            "database_role": database_role,
            "decision": "ALLOW",
            "check_source": "postgresql_privilege_check",
        }
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        items.append(
            EvidenceItem(
                evidence_id=f"EV-PERM-{digest[:16]}",
                database_id=database_id,
                type=EvidenceType.PERMISSION,
                key=target,
                value=value,
                source="active_postgresql_role",
                source_version=PERMISSION_SOURCE_VERSION,
                retrieved_at=retrieved_at,
                authority_level=PERMISSION_AUTHORITY,
                access_label="internal",
                scope=target,
                provenance_id=f"PROV-POSTGRES-ROLE-{database_role}",
                content_hash=digest,
                linked_requirement_ids=tuple(requirement_ids),
            )
        )

    return tuple(items)
