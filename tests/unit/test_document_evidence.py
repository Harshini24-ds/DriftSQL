import json
from datetime import date

import pytest

from src.contracts.evidence import EvidenceType
from src.evidence.documents import build_document_evidence
from src.evidence.glossary import GLOSSARY_AUTHORITY


def make_record(
    document_id="DOC-TEST-RULE",
    *,
    rule="Use the verified rule.",
    status="APPROVED",
    effective_from="2026-01-01T00:00:00Z",
    effective_to=None,
    authority_level=60,
):
    return {
        "document_id": document_id,
        "title": "Test business rule",
        "rule": rule,
        "scope": "olist_raw",
        "source_id": "test-source",
        "document_version": "v1",
        "authority_level": authority_level,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "status": status,
        "provenance_id": f"PROV-{document_id}",
    }


def write_catalog(tmp_path, records, *, status="APPROVED"):
    path = tmp_path / "documents.json"
    catalog = {
        "format_version": 1,
        "database_id": "olist_primary",
        "documents_version": "test-v1",
        "source": "test-documents",
        "access_label": "internal",
        "status": status,
        "records": records,
    }
    path.write_text(json.dumps(catalog), encoding="utf-8")
    return path


def test_draft_document_catalog_produces_no_runtime_evidence(tmp_path):
    path = write_catalog(
        tmp_path,
        [make_record(status="DRAFT_FOR_TEAM_REVIEW", effective_from=None)],
        status="DRAFT_FOR_TEAM_REVIEW",
    )

    assert build_document_evidence(path) == ()


def test_active_approved_document_builds_document_evidence(tmp_path):
    path = write_catalog(tmp_path, [make_record()])

    items = build_document_evidence(path, as_of=date(2026, 10, 18))

    assert len(items) == 1
    assert items[0].type == EvidenceType.DOCUMENT
    assert items[0].database_id == "olist_primary"
    assert items[0].value["rule"] == "Use the verified rule."
    assert items[0].source_version == "v1"


def test_effective_to_is_exclusive(tmp_path):
    path = write_catalog(
        tmp_path,
        [
            make_record(
                effective_to="2026-10-18T00:00:00Z",
            )
        ],
    )

    assert build_document_evidence(path, as_of=date(2026, 10, 18)) == ()


def test_competing_active_rules_are_both_preserved(tmp_path):
    path = write_catalog(
        tmp_path,
        [
            make_record(
                document_id="DOC-RULE-A",
                rule="Rule A.",
            ),
            make_record(
                document_id="DOC-RULE-B",
                rule="Conflicting rule B.",
            ),
        ],
    )

    items = build_document_evidence(path, as_of=date(2026, 10, 18))

    assert len(items) == 2
    assert {item.value["rule"] for item in items} == {
        "Rule A.",
        "Conflicting rule B.",
    }


def test_document_authority_must_be_below_glossary(tmp_path):
    path = write_catalog(
        tmp_path,
        [make_record(authority_level=GLOSSARY_AUTHORITY)],
    )

    with pytest.raises(ValueError, match="below glossary authority"):
        build_document_evidence(path)