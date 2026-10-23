import json
from pathlib import Path

from src.contracts.evidence import EvidenceType


GOLD_RQ_PATH = Path("datasets/gold_obligations/month2_gold_rq.json")
SCHEMA_SNAPSHOT_PATH = Path("artifacts/schema_snapshot.json")
REQUIRED_OBLIGATION_FIELDS = {
    "gold_obligation_id",
    "evidence_type",
    "canonical_target_id",
    "constraint_type",
    "mandatory",
    "expected_source_types",
    "reason",
}


def test_month2_gold_rq_dataset_is_offline_and_well_formed():
    dataset = json.loads(GOLD_RQ_PATH.read_text(encoding="utf-8"))

    assert dataset["database_id"] == "olist_primary"
    assert dataset["runtime_use"] is False
    assert dataset["annotation_status"] == "APPROVED"

    cases = dataset["cases"]
    assert len(cases) == 20
    assert len({case["question_id"] for case in cases}) == 20
    assert len({case["question"] for case in cases}) == 20

    obligation_ids = []
    for case in cases:
        assert case["question"].strip()
        assert case["gold_obligations"]

        for obligation in case["gold_obligations"]:
            assert REQUIRED_OBLIGATION_FIELDS <= obligation.keys()
            assert obligation["evidence_type"] in {
                evidence_type.value for evidence_type in EvidenceType
            }
            assert obligation["canonical_target_id"].strip()
            assert isinstance(obligation["mandatory"], bool)
            assert obligation["expected_source_types"]
            assert obligation["reason"].strip()
            obligation_ids.append(obligation["gold_obligation_id"])

    assert len(obligation_ids) == len(set(obligation_ids))


def test_month2_gold_rq_targets_exist_in_schema_snapshot():
    dataset = json.loads(GOLD_RQ_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_SNAPSHOT_PATH.read_text(encoding="utf-8"))

    assert schema["database_id"] == dataset["database_id"]
    schema_name = schema["schema"]
    tables = {
        table["table_name"]: {
            column["column_name"] for column in table["columns"]
        }
        for table in schema["tables"]
    }

    relationships = {
        (item["child_table"], item["parent_table"])
        for relationship_type in ("strict_relationships", "non_strict_lookups")
        for item in schema[relationship_type]
    }

    for case in dataset["cases"]:
        for obligation in case["gold_obligations"]:
            target = obligation["canonical_target_id"]
            evidence_type = obligation["evidence_type"]

            if evidence_type == EvidenceType.DOCUMENT.value:
                assert target == schema_name, (
                    f"{case['question_id']}: unknown documentation scope {target!r}"
                )
                continue

            if evidence_type == EvidenceType.RELATIONSHIP.value:
                assert " -> " in target, (
                    f"{case['question_id']}: malformed relationship target {target!r}"
                )
                left, right = target.split(" -> ", maxsplit=1)
                prefix = f"{schema_name}."
                assert left.startswith(prefix) and right.startswith(prefix), (
                    f"{case['question_id']}: relationship target must use schema "
                    f"{schema_name!r}: {target!r}"
                )
                child, parent = left.removeprefix(prefix), right.removeprefix(prefix)
                assert (child, parent) in relationships, (
                    f"{case['question_id']}: relationship {target!r} is not in "
                    "the schema snapshot's verified relationship lists"
                )
                continue

            parts = target.split(".")
            assert len(parts) in {2, 3} and parts[0] == schema_name, (
                f"{case['question_id']}: malformed schema target {target!r}"
            )
            table_name = parts[1]
            assert table_name in tables, (
                f"{case['question_id']}: table {table_name!r} is absent from "
                "the schema snapshot"
            )
            if len(parts) == 3:
                assert parts[2] in tables[table_name], (
                    f"{case['question_id']}: column {target!r} is absent from "
                    "the schema snapshot"
                )
