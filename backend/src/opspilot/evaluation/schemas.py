"""Versioned dataset contracts and immutable test-corpus verification."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

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


class ApprovalExpectation(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


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
    rows: list[ModelT] = []
    case_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
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


def verify_frozen_test_dataset(path: Path, manifest: DatasetManifest) -> None:
    actual = sha256_file(path)
    if actual != manifest.test_sha256:
        raise ValueError(
            f"test dataset SHA-256 mismatch: expected {manifest.test_sha256}, got {actual}"
        )
