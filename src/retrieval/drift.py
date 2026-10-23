"""Detect stale evidence snapshots by comparing them with the live DB catalog."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Any, Protocol

from src.evidence.glossary import DEFAULT_GLOSSARY_PATH
from src.evidence.metadata import DEFAULT_METADATA_PATH
from src.evidence.schema import DEFAULT_SNAPSHOT_PATH, load_schema_snapshot


class SchemaFreshnessState(str, Enum):
    """Freshness states used by SDARE for a saved schema snapshot."""

    CURRENT = "CURRENT"
    STALE_RECOVERABLE = "STALE_RECOVERABLE"


@dataclass(frozen=True)
class SchemaDriftAssessment:
    """Immutable comparison result for saved and live schema snapshots."""

    state: SchemaFreshnessState
    saved_schema_hash: str
    live_schema_hash: str
    changed_components: tuple[str, ...]


@dataclass(frozen=True)
class SchemaRefreshBundle:
    """Immutable refreshed schema plus review-only dependent catalog copies."""

    assessment: SchemaDriftAssessment
    schema_snapshot_path: Path
    metadata_draft_path: Path | None
    glossary_draft_path: Path | None
    review_required: tuple[str, ...]


class SchemaSnapshotAdapter(Protocol):
    """Minimum adapter interface required for a live drift comparison."""

    database_id: str

    def get_schema_snapshot(self) -> dict[str, Any]: ...


class SchemaDriftDetectedError(RuntimeError):
    """Fail-closed retrieval error carrying a recoverable drift assessment."""

    def __init__(self, assessment: SchemaDriftAssessment) -> None:
        self.assessment = assessment
        components = ", ".join(assessment.changed_components) or "schema_hash"
        super().__init__(
            "Live database schema differs from the saved evidence snapshot "
            f"({components}); state={assessment.state.value}. Refresh the schema "
            "snapshot and revalidate schema-bound evidence before retrieval."
        )


_STRUCTURAL_COMPONENTS = (
    "tables",
    "primary_keys",
    "foreign_keys",
    "strict_relationships",
    "non_strict_lookups",
)


def assess_schema_drift(
    saved_snapshot: dict[str, Any],
    live_snapshot: dict[str, Any],
) -> SchemaDriftAssessment:
    """Compare schema identity, structural facts, and deterministic hashes."""
    for key in ("database_id", "schema"):
        if saved_snapshot.get(key) != live_snapshot.get(key):
            raise ValueError(
                f"Cannot compare schema snapshots with different {key!r} values."
            )

    saved_hash = saved_snapshot.get("schema_hash")
    live_hash = live_snapshot.get("schema_hash")
    if not isinstance(saved_hash, str) or not saved_hash:
        raise ValueError("Saved schema snapshot must contain a schema_hash.")
    if not isinstance(live_hash, str) or not live_hash:
        raise ValueError("Live schema snapshot must contain a schema_hash.")

    changed = tuple(
        component
        for component in _STRUCTURAL_COMPONENTS
        if saved_snapshot.get(component) != live_snapshot.get(component)
    )
    if saved_hash != live_hash and not changed:
        changed = ("schema_hash",)
    elif saved_hash == live_hash and changed:
        # A matching hash with different structure indicates an inconsistent
        # snapshot, so fail closed instead of trusting either representation.
        changed = (*changed, "schema_hash_consistency")

    return SchemaDriftAssessment(
        state=(
            SchemaFreshnessState.STALE_RECOVERABLE
            if changed
            else SchemaFreshnessState.CURRENT
        ),
        saved_schema_hash=saved_hash,
        live_schema_hash=live_hash,
        changed_components=changed,
    )


def ensure_schema_snapshot_current(
    adapter: SchemaSnapshotAdapter,
    *,
    schema_snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
) -> SchemaDriftAssessment:
    """Reject retrieval if the live schema no longer matches the saved snapshot."""
    saved_snapshot = load_schema_snapshot(schema_snapshot_path)
    live_snapshot = adapter.get_schema_snapshot()

    if live_snapshot.get("database_id") != adapter.database_id:
        raise ValueError("Live schema snapshot belongs to a different database.")

    assessment = assess_schema_drift(saved_snapshot, live_snapshot)
    if assessment.state == SchemaFreshnessState.STALE_RECOVERABLE:
        raise SchemaDriftDetectedError(assessment)

    return assessment


def _schema_targets(snapshot: dict[str, Any]) -> set[str]:
    schema = snapshot["schema"]
    return {
        target
        for table in snapshot["tables"]
        for target in (
            f"{schema}.{table['table_name']}",
            *(
                f"{schema}.{table['table_name']}.{column['column_name']}"
                for column in table["columns"]
            ),
        )
    }


def _write_immutable_json(path: Path, value: dict[str, Any]) -> None:
    """Create a JSON artifact without replacing an existing version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        # created_at may differ when the same structural hash is exported again.
        existing.pop("created_at", None)
        comparable = dict(value)
        comparable.pop("created_at", None)
        if existing != comparable:
            raise ValueError(
                f"Refusing to overwrite a different immutable artifact: {path}"
            )
        return

    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized)
    except FileExistsError:
        # A concurrent writer won the create race; verify it is identical.
        _write_immutable_json(path, value)


def _draft_metadata_catalog(
    catalog: dict[str, Any],
    *,
    schema_hash: str,
    valid_targets: set[str],
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    records = catalog.get("records", [])
    missing = tuple(
        sorted(
            record.get("target", "<missing target>")
            for record in records
            if record.get("target") not in valid_targets
        )
    )
    if missing:
        return None, missing

    draft = dict(catalog)
    draft["schema_hash"] = schema_hash
    draft["metadata_version"] = (
        f"{catalog['metadata_version']}-schema-{schema_hash[:12]}"
    )
    draft["status"] = "DRAFT_FOR_TEAM_REVIEW"
    return draft, ()


def _draft_glossary_catalog(
    catalog: dict[str, Any],
    *,
    schema_hash: str,
    metadata_targets: set[str],
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    records = catalog.get("records", [])
    missing = tuple(
        sorted(
            record.get("target", "<missing target>")
            for record in records
            if record.get("target") not in metadata_targets
        )
    )
    if missing:
        return None, missing

    draft = dict(catalog)
    draft["schema_hash"] = schema_hash
    draft["glossary_version"] = (
        f"{catalog['glossary_version']}-schema-{schema_hash[:12]}"
    )
    draft["records"] = [
        {
            **record,
            "definition_version": (
                f"{record['definition_version']}-schema-{schema_hash[:12]}"
            ),
            "status": "DRAFT_FOR_TEAM_REVIEW",
            "effective_from": None,
            "effective_to": None,
        }
        for record in records
    ]
    return draft, ()


def prepare_schema_refresh(
    adapter: SchemaSnapshotAdapter,
    *,
    schema_snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
    metadata_path: str | Path = DEFAULT_METADATA_PATH,
    glossary_path: str | Path = DEFAULT_GLOSSARY_PATH,
    refresh_root: str | Path = "artifacts/schema_refresh",
) -> SchemaRefreshBundle:
    """Prepare an explicit, immutable SDARE refresh without changing active files.

    A drifted live schema is persisted under its hash. Metadata and glossary
    copies are tied to that hash but downgraded to team-review drafts. The
    caller must review those copies and explicitly pass their paths when
    adopting the refreshed snapshot; the existing request and active catalogs
    remain untouched.
    """
    saved_snapshot = load_schema_snapshot(schema_snapshot_path)
    live_snapshot = adapter.get_schema_snapshot()
    if live_snapshot.get("database_id") != adapter.database_id:
        raise ValueError("Live schema snapshot belongs to a different database.")

    assessment = assess_schema_drift(saved_snapshot, live_snapshot)
    if assessment.state == SchemaFreshnessState.CURRENT:
        return SchemaRefreshBundle(
            assessment=assessment,
            schema_snapshot_path=Path(schema_snapshot_path),
            metadata_draft_path=None,
            glossary_draft_path=None,
            review_required=(),
        )

    version_dir = Path(refresh_root) / live_snapshot["schema_hash"]
    snapshot_output = version_dir / "schema_snapshot.json"
    _write_immutable_json(snapshot_output, live_snapshot)

    review_required = ["Review refreshed schema and affected relationship evidence."]
    if any(
        component in assessment.changed_components
        for component in ("tables", "strict_relationships", "non_strict_lookups")
    ):
        review_required.append(
            "Review applicable dataset documentation and join-path rules."
        )

    metadata_draft_path: Path | None = None
    glossary_draft_path: Path | None = None
    try:
        metadata_catalog = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
        metadata_draft, missing_metadata = _draft_metadata_catalog(
            metadata_catalog,
            schema_hash=live_snapshot["schema_hash"],
            valid_targets=_schema_targets(live_snapshot),
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        missing_metadata = (f"metadata catalog could not be refreshed: {exc}",)
        metadata_draft = None

    if metadata_draft is None:
        review_required.extend(
            f"Metadata needs manual reconciliation: {target}"
            for target in missing_metadata
        )
    else:
        metadata_draft_path = version_dir / "metadata.draft.json"
        _write_immutable_json(metadata_draft_path, metadata_draft)

        try:
            glossary_catalog = json.loads(
                Path(glossary_path).read_text(encoding="utf-8")
            )
            glossary_draft, missing_glossary = _draft_glossary_catalog(
                glossary_catalog,
                schema_hash=live_snapshot["schema_hash"],
                metadata_targets={record["target"] for record in metadata_draft["records"]},
            )
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            missing_glossary = (f"glossary catalog could not be refreshed: {exc}",)
            glossary_draft = None

        if glossary_draft is None:
            review_required.extend(
                f"Glossary needs manual reconciliation: {target}"
                for target in missing_glossary
            )
        else:
            glossary_draft_path = version_dir / "glossary.draft.json"
            _write_immutable_json(glossary_draft_path, glossary_draft)

    review_required.append(
        "Semantic retrieval has no persisted index; it will be rebuilt from "
        "the refreshed evidence when explicitly adopted."
    )
    return SchemaRefreshBundle(
        assessment=assessment,
        schema_snapshot_path=snapshot_output,
        metadata_draft_path=metadata_draft_path,
        glossary_draft_path=glossary_draft_path,
        review_required=tuple(review_required),
    )
