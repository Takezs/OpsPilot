"""Versioned dataset contracts and immutable test-corpus verification."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExpectedOutcome(StrEnum):
    ANSWERED = "ANSWERED"
    CLARIFICATION = "CLARIFICATION"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    REJECTED = "REJECTED"
    DENIED = "DENIED"
    RETRYING = "RETRYING"
    READY = "READY"
    EXECUTING = "EXECUTING"
    RECONCILING = "RECONCILING"


class ApprovalExpectation(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class EvaluationConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str = Field(min_length=1, max_length=128)
    embedding_model: str = Field(min_length=1, max_length=128)
    reranker_model: str = Field(min_length=1, max_length=128)
    top_k: int = Field(ge=1, le=100)
    prompt_version: str = Field(min_length=1, max_length=128)
    random_parameters: dict[str, object]
    concurrency: int = Field(ge=1, le=32)
    repetitions: int = Field(ge=3, le=20)


class FreezeEvaluationRequest(BaseModel):
    dataset_version: str = Field(min_length=1, max_length=64)
    dataset_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration: EvaluationConfiguration


class EvaluationExecutionResponse(BaseModel):
    id: uuid.UUID
    evaluation_run_id: uuid.UUID
    dataset_version: str
    dataset_sha: str
    configuration_sha: str
    status: str
    frozen_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_summary: str | None


class EvaluationFaultPlanRequest(BaseModel):
    matrix_run_id: uuid.UUID
    operation_id: uuid.UUID
    fault_point: str


class ToolExpectation(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, object]

    def signature(self) -> str:
        arguments = json.dumps(
            self.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return f"{self.name}:{arguments}"


class EvaluationCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    case_id: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=2000)
    relevant_chunk_ids: tuple[str, ...]
    expected_citation_ids: tuple[str, ...]
    required_facts: tuple[str, ...]
    forbidden_facts: tuple[str, ...]
    expected_tools: tuple[ToolExpectation, ...]
    forbidden_tools: tuple[ToolExpectation, ...]
    follow_up_required: bool
    approval_required: bool
    expected_final_state: ExpectedOutcome

    @model_validator(mode="after")
    def validate_unambiguous_expectations(self) -> EvaluationCase:
        if len(set(self.relevant_chunk_ids)) != len(self.relevant_chunk_ids):
            raise ValueError("relevant_chunk_ids must be unique")
        if len(set(self.expected_citation_ids)) != len(self.expected_citation_ids):
            raise ValueError("expected_citation_ids must be unique")
        for chunk_id in self.relevant_chunk_ids:
            if str(uuid.UUID(chunk_id)) != chunk_id:
                raise ValueError("relevant chunk id must be a canonical UUID")
        for citation in self.expected_citation_ids:
            if (
                not citation.startswith("[DOC:")
                or "#" not in citation
                or not citation.endswith("]")
            ):
                raise ValueError("expected citation id is not canonical")
            chunk_id = citation.rsplit("#", 1)[1][:-1]
            document_id = citation.removeprefix("[DOC:").split("#", 1)[0]
            if str(uuid.UUID(document_id)) != document_id:
                raise ValueError("citation document id must be a canonical UUID")
            if chunk_id not in self.relevant_chunk_ids:
                raise ValueError("expected citation must reference a relevant chunk")
        required = {item.strip().casefold() for item in self.required_facts}
        forbidden = {item.strip().casefold() for item in self.forbidden_facts}
        if "" in required or "" in forbidden:
            raise ValueError("fact expectations must be non-empty")
        if required & forbidden:
            raise ValueError("required and forbidden facts must be disjoint")
        expected_tools = {item.signature() for item in self.expected_tools}
        forbidden_tools = {item.signature() for item in self.forbidden_tools}
        if expected_tools & forbidden_tools:
            raise ValueError("expected and forbidden tools must be disjoint")
        if (
            self.follow_up_required
            and self.expected_final_state is not ExpectedOutcome.CLARIFICATION
        ):
            raise ValueError("follow-up cases must end in CLARIFICATION")
        if (
            self.expected_final_state is ExpectedOutcome.CLARIFICATION
            and not self.follow_up_required
        ):
            raise ValueError("CLARIFICATION requires follow_up_required")
        return self


class AgentEvaluationCase(EvaluationCase):
    operation_expected: bool
    order_number: str | None = Field(default=None, pattern=r"^ORD-\d{3,}$")
    amount: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    expected_idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    expected_approval: ApprovalExpectation | None = None

    @model_validator(mode="after")
    def validate_approval_expectation(self) -> AgentEvaluationCase:
        operation_values = (
            self.order_number,
            self.amount,
            self.expected_idempotency_key,
            self.expected_approval,
        )
        if self.operation_expected and any(value is None for value in operation_values):
            raise ValueError("operation cases require all durable business expectations")
        if not self.operation_expected and any(value is not None for value in operation_values):
            raise ValueError("non-operation cases must not fabricate business expectations")
        if not self.operation_expected and self.approval_required:
            raise ValueError("non-operation cases cannot require operation approval")
        if not self.operation_expected:
            return self
        if self.approval_required == (self.expected_approval is ApprovalExpectation.NOT_REQUIRED):
            raise ValueError("approval expectation must match approval_required")
        return self


class AttackCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    case_id: str = Field(min_length=1, max_length=128)
    attack_type: str = Field(min_length=1, max_length=64)
    input: str = Field(min_length=1, max_length=4000)
    required_safe_behavior: str = Field(min_length=1, max_length=500)
    forbidden_tools: tuple[ToolExpectation, ...]
    forbidden_facts: tuple[str, ...]


class DatasetManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    corpus_version: str = Field(min_length=1, max_length=64)
    generator: str = Field(min_length=1, max_length=255)
    generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_seed: int = Field(ge=0)
    dev_count: int = Field(ge=0)
    test_count: int = Field(ge=0)
    agent_task_count: int = Field(ge=0)
    attack_count: int = Field(ge=0)
    test_frozen: bool
    test_frozen_at: datetime
    test_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    final_test_executed: bool

    @model_validator(mode="after")
    def validate_frozen_test(self) -> DatasetManifest:
        if not self.test_frozen:
            raise ValueError("test corpus manifest must be frozen")
        if self.dev_count != 60 or self.test_count != 140:
            raise ValueError("v1 corpus requires dev=60 and test=140")
        if self.final_test_executed:
            raise ValueError("Task 16 must not execute the frozen final test corpus")
        return self


def load_jsonl[ModelT: BaseModel](path: Path, model: type[ModelT]) -> tuple[ModelT, ...]:
    return load_jsonl_bytes(path.read_bytes(), model)


def load_jsonl_bytes[ModelT: BaseModel](content: bytes, model: type[ModelT]) -> tuple[ModelT, ...]:
    rows: list[ModelT] = []
    case_ids: set[str] = set()
    for line_number, line in enumerate(content.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"blank JSONL line at {line_number}")
        row = model.model_validate_json(line)
        case_id = getattr(row, "case_id", None)
        if isinstance(case_id, str):
            if case_id in case_ids:
                raise ValueError(f"duplicate case_id: {case_id}")
            case_ids.add(case_id)
        rows.append(row)
    return tuple(rows)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def verify_frozen_test_dataset(path: Path, manifest: DatasetManifest) -> None:
    actual = sha256_file(path)
    if actual != manifest.test_sha256:
        raise ValueError(
            f"test dataset SHA-256 mismatch: expected {manifest.test_sha256}, got {actual}"
        )


@dataclass(frozen=True)
class FrozenDatasetSnapshot:
    identity: dict[str, str]
    test_cases: tuple[EvaluationCase, ...]
    agent_cases: tuple[AgentEvaluationCase, ...]


def _trusted_generator_path(dataset_root: Path, declared: str) -> Path:
    pure = PurePosixPath(declared)
    if (
        pure.is_absolute()
        or PureWindowsPath(declared).is_absolute()
        or ".." in pure.parts
        or not pure.parts
    ):
        raise ValueError("generator path must stay within the trusted evaluation root")
    evaluation_root = dataset_root.resolve().parent
    if pure.parts[0] != evaluation_root.name:
        raise ValueError("generator path must stay within the trusted evaluation root")
    candidate = evaluation_root.joinpath(*pure.parts[1:])
    current = evaluation_root
    for part in pure.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValueError("generator path must not contain symlinks")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(evaluation_root):
        raise ValueError("generator path must stay within the trusted evaluation root")
    return resolved


def capture_frozen_dataset(root: Path) -> FrozenDatasetSnapshot:
    manifest_bytes = (root / "manifest.json").read_bytes()
    test_bytes = (root / "test.jsonl").read_bytes()
    agent_bytes = (root / "agent_tasks.jsonl").read_bytes()
    manifest = DatasetManifest.model_validate_json(manifest_bytes)
    generator_bytes = _trusted_generator_path(root, manifest.generator).read_bytes()
    test_sha = _sha256_bytes(test_bytes)
    if test_sha != manifest.test_sha256:
        raise ValueError(
            f"test dataset SHA-256 mismatch: expected {manifest.test_sha256}, got {test_sha}"
        )
    generator_sha = _sha256_bytes(generator_bytes)
    if generator_sha != manifest.generator_sha256:
        raise ValueError(
            f"generator SHA-256 mismatch: expected {manifest.generator_sha256}, got {generator_sha}"
        )
    test_cases = load_jsonl_bytes(test_bytes, EvaluationCase)
    agent_cases = load_jsonl_bytes(agent_bytes, AgentEvaluationCase)
    if len(test_cases) != manifest.test_count or len(agent_cases) != manifest.agent_task_count:
        raise ValueError("captured evaluation case counts do not match manifest")
    return FrozenDatasetSnapshot(
        identity={
            "manifest_sha256": _sha256_bytes(manifest_bytes),
            "test_sha256": test_sha,
            "agent_tasks_sha256": _sha256_bytes(agent_bytes),
            "schema_version": manifest.schema_version,
            "generator_sha256": generator_sha,
        },
        test_cases=test_cases,
        agent_cases=agent_cases,
    )


def frozen_dataset_identity(root: Path) -> dict[str, str]:
    # Build/startup identity checks must not deserialize held-out cases.
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = DatasetManifest.model_validate_json(manifest_bytes)
    test_sha = sha256_file(root / "test.jsonl")
    generator_sha = sha256_file(_trusted_generator_path(root, manifest.generator))
    if test_sha != manifest.test_sha256:
        raise ValueError("test dataset SHA-256 mismatch")
    if generator_sha != manifest.generator_sha256:
        raise ValueError("generator SHA-256 mismatch")
    return {
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "test_sha256": test_sha,
        "agent_tasks_sha256": sha256_file(root / "agent_tasks.jsonl"),
        "schema_version": manifest.schema_version,
        "generator_sha256": generator_sha,
    }
