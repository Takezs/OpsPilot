import json
import subprocess
import sys
import uuid
from pathlib import Path

from opspilot.evaluation.schemas import (
    AgentEvaluationCase,
    AttackCase,
    DatasetManifest,
    EvaluationCase,
    ExpectedOutcome,
    load_jsonl,
    sha256_file,
    verify_frozen_test_dataset,
)
from opspilot.knowledge.models import Chunk, Document

ROOT = Path(__file__).parents[3]
DATASETS = ROOT / "evaluation" / "datasets"


def test_versioned_corpus_counts_hash_and_coverage_are_frozen() -> None:
    dev = load_jsonl(DATASETS / "dev.jsonl", EvaluationCase)
    test = load_jsonl(DATASETS / "test.jsonl", EvaluationCase)
    agents = load_jsonl(DATASETS / "agent_tasks.jsonl", AgentEvaluationCase)
    attacks = load_jsonl(DATASETS / "attacks.jsonl", AttackCase)
    manifest = DatasetManifest.model_validate_json(
        (DATASETS / "manifest.json").read_text(encoding="utf-8")
    )
    review = json.loads((DATASETS / "review.json").read_text(encoding="utf-8"))

    assert (len(dev), len(test), len(agents), len(attacks)) == (60, 140, 60, 40)
    assert manifest.dev_count == len(dev)
    assert manifest.test_count == len(test)
    assert manifest.agent_task_count == len(agents)
    assert manifest.attack_count == len(attacks)
    assert manifest.final_test_executed is False
    verify_frozen_test_dataset(DATASETS / "test.jsonl", manifest)
    assert manifest.generator_sha256 == sha256_file(ROOT / manifest.generator)

    all_regular_ids = [case.case_id for case in (*dev, *test, *agents)]
    assert len(all_regular_ids) == len(set(all_regular_ids))
    assert {case.case_id for case in attacks}.isdisjoint(all_regular_ids)

    combined = (*dev, *test, *agents)
    assert any(case.relevant_chunk_ids for case in combined)
    assert any(not case.relevant_chunk_ids for case in combined)
    assert any(case.required_facts for case in combined)
    assert any(case.forbidden_facts for case in combined)
    assert any(case.expected_tools for case in combined)
    assert any(case.forbidden_tools for case in combined)
    assert {case.follow_up_required for case in combined} == {False, True}
    assert {case.approval_required for case in combined} == {False, True}
    assert {
        ExpectedOutcome.ANSWERED,
        ExpectedOutcome.CLARIFICATION,
        ExpectedOutcome.WAITING_APPROVAL,
        ExpectedOutcome.SUCCEEDED,
        ExpectedOutcome.OUTCOME_UNKNOWN,
        ExpectedOutcome.MANUAL_REVIEW,
        ExpectedOutcome.REJECTED,
    } <= {case.expected_final_state for case in combined}

    assert review["counts"] == {
        "dev": 60,
        "test": 140,
        "agent_tasks": 60,
        "attacks": 40,
    }
    assert review["test_sha256"] == manifest.test_sha256
    assert review["final_test_executed"] is False
    non_operations = [case for case in agents if not case.operation_expected]
    operations = [case for case in agents if case.operation_expected]
    assert review["identity_strategy"] == {
        "algorithm": "UUIDv5",
        "namespace": "a5e57775-fbad-5d63-b7d1-c1bf5a07f1c2",
        "runtime_remap_required": False,
    }
    assert review["agent_operation_counts"] == {
        "operation": len(operations),
        "non_operation": len(non_operations),
    }

    assert {case.category for case in non_operations} == {
        "refund_policy",
        "missing_order_clarification",
    }
    assert all(
        case.order_number is None
        and case.amount is None
        and case.expected_idempotency_key is None
        and case.expected_approval is None
        for case in non_operations
    )
    assert all(
        case.order_number is not None
        and case.amount is not None
        and case.expected_idempotency_key is not None
        and case.expected_approval is not None
        for case in operations
    )


def test_generator_reproduces_frozen_corpus_byte_for_byte(tmp_path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "evaluation" / "generate_datasets.py"),
            "--output",
            str(tmp_path),
        ],
        check=True,
        cwd=ROOT / "backend",
    )

    for name in (
        "dev.jsonl",
        "test.jsonl",
        "agent_tasks.jsonl",
        "attacks.jsonl",
        "manifest.json",
        "review.json",
    ):
        assert (tmp_path / name).read_bytes() == (DATASETS / name).read_bytes()


def test_frozen_retrieval_identities_are_production_uuid_compatible() -> None:
    cases = (
        *load_jsonl(DATASETS / "dev.jsonl", EvaluationCase),
        *load_jsonl(DATASETS / "test.jsonl", EvaluationCase),
        *load_jsonl(DATASETS / "agent_tasks.jsonl", AgentEvaluationCase),
    )

    for case in cases:
        for chunk_id in case.relevant_chunk_ids:
            assert str(uuid.UUID(chunk_id)) == chunk_id
        for citation_id in case.expected_citation_ids:
            document_id, chunk_id = citation_id.removeprefix("[DOC:").removesuffix("]").split("#")
            assert str(uuid.UUID(document_id)) == document_id
            assert str(uuid.UUID(chunk_id)) == chunk_id

    first = next(case for case in cases if case.expected_citation_ids)
    document_id, chunk_id = (
        first.expected_citation_ids[0].removeprefix("[DOC:").removesuffix("]").split("#")
    )
    document = Document(id=uuid.UUID(document_id))
    chunk = Chunk(id=uuid.UUID(chunk_id), document_id=document.id)
    assert isinstance(document.id, uuid.UUID)
    assert isinstance(chunk.id, uuid.UUID)
    assert chunk.document_id == document.id
