"""Public HTTP API-only evaluation case adapter."""

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from opspilot.evaluation.agent_metrics import ActualAgentOutcome, ToolCall, evaluate_agent_outcome
from opspilot.evaluation.answer_metrics import evaluate_answer_facts, evaluate_citation_ids
from opspilot.evaluation.config import configuration_sha256
from opspilot.evaluation.credentials import authenticate_file
from opspilot.evaluation.retrieval_metrics import (
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from opspilot.evaluation.schemas import (
    AgentEvaluationCase,
    ApprovalExpectation,
    EvaluationCase,
    EvaluationConfiguration,
    ExpectedOutcome,
)
from opspilot.evaluation.tasks import EvaluationCaseResult


class PublicEvaluationApi:
    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str = "",
        *,
        poll_seconds: float = 0.25,
        max_polls: int = 2400,
        configuration: EvaluationConfiguration | None = None,
        user_credentials_file: str = "",
        reviewer_credentials_file: str = "",
    ) -> None:
        if not token and not user_credentials_file:
            raise ValueError("evaluation API token is required")
        self._client = client
        self._headers = {"Authorization": f"Bearer {token}"}
        self._poll_seconds = poll_seconds
        self._max_polls = max_polls
        self._configuration = configuration
        self._user_credentials_file = user_credentials_file
        self._reviewer_credentials_file = reviewer_credentials_file
        self._reviewer_headers: dict[str, str] = {}

    async def bind_configuration(
        self, configuration: EvaluationConfiguration, expected_sha: str
    ) -> "PublicEvaluationApi":
        if configuration_sha256(configuration) != expected_sha:
            raise ValueError("receipt configuration identity mismatch")
        if self._configuration != configuration:
            raise ValueError("receipt and production composition do not match")
        await self._authenticate()
        response = await self._client.get("/evaluations/runtime", headers=self._headers)
        if response.status_code != 200:
            raise ValueError("evaluation API runtime unavailable")
        value = response.json()
        if value.get("configuration_sha") != expected_sha or value.get("configuration") != (
            configuration.model_dump(mode="json")
        ):
            raise ValueError("API and receipt configuration do not match")
        return self

    async def _authenticate(self) -> None:
        if self._user_credentials_file:
            self._headers = await authenticate_file(
                self._client, Path(self._user_credentials_file), expected_role="USER"
            )
            self._reviewer_headers = await authenticate_file(
                self._client, Path(self._reviewer_credentials_file), expected_role="REVIEWER"
            )

    async def __call__(self, case: EvaluationCase, repetition: int) -> EvaluationCaseResult:
        # Refresh short-lived credentials between cases, never elevate the USER
        # workflow to ADMIN. Each request retains its own header dict.
        await self._authenticate()
        top_k = self._configuration.top_k if self._configuration is not None else 5
        ranked_ids: list[str] = []
        if case.relevant_chunk_ids:
            retrieval_response = await self._client.post(
                "/retrieval/debug",
                headers=self._headers,
                json={"query": case.query, "top_k": top_k},
            )
            retrieval_response.raise_for_status()
            retrieval = retrieval_response.json()
            stage = retrieval.get("reranker") or retrieval.get("rrf") or []
            ranked_ids = [
                str(item["chunk_id"])
                for item in stage
                if isinstance(item, dict) and item.get("chunk_id") is not None
            ]
        correlation = f"evaluation:{case.case_id}:{repetition}"
        lookup = await self._client.get(
            f"/runs/by-correlation/{correlation}", headers=self._headers
        )
        lookup_value = lookup.json() if lookup.status_code == 200 else {}
        if lookup.status_code == 200 and isinstance(lookup_value.get("run_id"), str):
            run_id = lookup_value["run_id"]
            existing_history = await self._client.get(
                f"/runs/{run_id}/history", headers=self._headers
            )
            existing_history.raise_for_status()
            if not any(
                row.get("event_type") == "user_message_created"
                for row in existing_history.json()
                if isinstance(row, dict)
            ):
                message_response = await self._client.post(
                    f"/runs/{run_id}/messages",
                    headers={**self._headers, "X-Evaluation-Correlation": correlation},
                    json={"content": case.query},
                )
                message_response.raise_for_status()
        elif lookup.status_code in {200, 404}:
            run_response = await self._client.post(
                "/runs", headers={**self._headers, "X-Evaluation-Correlation": correlation}
            )
            if run_response.status_code == 409:
                adopted = await self._client.get(
                    f"/runs/by-correlation/{correlation}", headers=self._headers
                )
                adopted.raise_for_status()
                run_id = str(adopted.json()["run_id"])
            else:
                run_response.raise_for_status()
                run_id = str(run_response.json()["run_id"])
            message_response = await self._client.post(
                f"/runs/{run_id}/messages", headers=self._headers, json={"content": case.query}
            )
            message_response.raise_for_status()
        else:
            lookup.raise_for_status()
        history: list[dict[str, Any]] = []
        detail: dict[str, Any] = {}
        for _ in range(self._max_polls):
            history_response, detail_response = await asyncio.gather(
                self._client.get(f"/runs/{run_id}/history", headers=self._headers),
                self._client.get(f"/runs/{run_id}", headers=self._headers),
            )
            history_response.raise_for_status()
            detail_response.raise_for_status()
            history = history_response.json()
            detail = detail_response.json()
            if any(row.get("event_type") == "assistant_message_created" for row in history):
                break
            await asyncio.sleep(self._poll_seconds)
        else:
            raise TimeoutError("public Run API did not produce an assistant result")
        assistant = next(
            row for row in reversed(history) if row.get("event_type") == "assistant_message_created"
        )
        payload_value = assistant.get("payload")
        payload: dict[str, Any] = payload_value if isinstance(payload_value, dict) else {}
        # Reviewer actions use a separate authenticated Principal. The USER
        # token continues to own all Run, history and retrieval requests.
        if (
            isinstance(case, AgentEvaluationCase)
            and self._reviewer_headers
            and (
                case.expected_approval
                in {ApprovalExpectation.APPROVED, ApprovalExpectation.REJECTED}
            )
        ):
            operations = detail.get("operations", [])
            waiting = [row for row in operations if row.get("status") == "WAITING_APPROVAL"]
            if waiting:
                approvals = await self._client.get(
                    "/approval-requests?status=PENDING", headers=self._reviewer_headers
                )
                approvals.raise_for_status()
                for operation in waiting:
                    approval = next(
                        (row for row in approvals.json() if row["operation_id"] == operation["id"]),
                        None,
                    )
                    if approval is None:
                        raise ValueError("evaluation approval is unavailable")
                    decision = (
                        "APPROVE"
                        if case.expected_approval == ApprovalExpectation.APPROVED
                        else "REJECT"
                    )
                    response = await self._client.post(
                        f"/approval-requests/{approval['id']}/decisions",
                        headers=self._reviewer_headers,
                        json={"decision": decision, "comment": "evaluation scenario"},
                    )
                    response.raise_for_status()
                for _ in range(self._max_polls):
                    response = await self._client.get(f"/runs/{run_id}", headers=self._headers)
                    response.raise_for_status()
                    detail = response.json()
                    if all(
                        row["status"]
                        in {"SUCCEEDED", "FAILED", "REJECTED", "DENIED", "MANUAL_REVIEW"}
                        for row in detail.get("operations", [])
                    ):
                        break
                    await asyncio.sleep(self._poll_seconds)
                else:
                    raise TimeoutError("evaluation operation did not reach a terminal state")
        citation_ids = _citation_ids(payload.get("citations", []))
        citation_score = evaluate_citation_ids(citation_ids, case.expected_citation_ids)
        content = str(payload.get("content", ""))
        fact_score = evaluate_answer_facts(
            content,
            required_facts=case.required_facts,
            forbidden_facts=case.forbidden_facts,
        )
        operations_value = detail.get("operations", []) if isinstance(detail, dict) else []
        operations = [item for item in operations_value if isinstance(item, dict)]
        final_state = (
            str(operations[-1].get("status")) if operations else str(detail.get("status", ""))
        )
        deterministic: dict[str, object] = {
            "citation_precision": citation_score.precision,
            "citation_recall": citation_score.recall,
            "required_fact_recall": fact_score.required_fact_recall,
            "forbidden_fact_hit_rate": fact_score.forbidden_fact_hit_rate,
            "final_state_match": final_state == case.expected_final_state.value,
        }
        if case.relevant_chunk_ids:
            relevant = set(case.relevant_chunk_ids)
            deterministic.update(
                recall_at_5=recall_at_k(ranked_ids, relevant, 5),
                precision_at_5=precision_at_k(ranked_ids, relevant, 5),
                mrr=mean_reciprocal_rank(ranked_ids, relevant),
                ndcg_at_5=ndcg_at_k(ranked_ids, relevant, 5),
            )
        deterministic["task_success"] = (
            fact_score.passed
            and citation_score.precision == 1.0
            and citation_score.recall == 1.0
            and final_state == case.expected_final_state.value
        )
        actual_flags = {"unapproved_execution": False, "duplicate_side_effect": False}
        if isinstance(case, AgentEvaluationCase):
            tool_calls = _tool_calls(payload.get("tool_calls"))
            operation = operations[-1] if operations else {}
            if operation and not any(
                call.name == operation.get("tool_name") for call in tool_calls
            ):
                operation_arguments = operation.get("normalized_arguments")
                if isinstance(operation_arguments, dict):
                    tool_calls = (
                        *tool_calls,
                        ToolCall(str(operation["tool_name"]), operation_arguments),
                    )
            arguments_value = operation.get("normalized_arguments")
            arguments = arguments_value if isinstance(arguments_value, dict) else {}
            status_value = str(operation.get("status", ""))
            if operations:
                actual_state = ExpectedOutcome(status_value)
            else:
                actual_state = ExpectedOutcome(str(payload.get("response_kind", "ANSWERED")))
            approval = _approval(operation)
            actual = ActualAgentOutcome(
                order_number=(
                    str(arguments["order_number"])
                    if isinstance(arguments.get("order_number"), str)
                    else None
                ),
                amount=(
                    Decimal(str(arguments["amount"]))
                    if arguments.get("amount") is not None
                    else None
                ),
                idempotency_key=(
                    str(operation["idempotency_key"])
                    if isinstance(operation.get("idempotency_key"), str)
                    else None
                ),
                tool_calls=tool_calls,
                approval=approval,
                final_state=actual_state,
            )
            agent_scores = evaluate_agent_outcome(actual, case)
            forbidden = {item.signature() for item in case.forbidden_tools}
            forbidden_hit = any(call.signature() in forbidden for call in tool_calls)
            attempts_value = operation.get("attempts")
            attempts = attempts_value if isinstance(attempts_value, list) else []
            succeeded_attempts = sum(
                isinstance(item, dict) and item.get("status") == "SUCCEEDED" for item in attempts
            )
            duplicate = succeeded_attempts > 1
            unapproved = status_value == "SUCCEEDED" and approval not in {
                ApprovalExpectation.APPROVED,
                ApprovalExpectation.NOT_REQUIRED,
            }
            actual_flags = {
                "unapproved_execution": unapproved,
                "duplicate_side_effect": duplicate,
            }
            deterministic.update(
                tool_precision=agent_scores.tools.precision,
                tool_recall=agent_scores.tools.recall,
                tool_f1=agent_scores.tools.f1,
                order_number_match=agent_scores.order_number_match,
                amount_match=agent_scores.amount_match,
                idempotency_key_match=agent_scores.idempotency_key_match,
                approval_match=agent_scores.approval_match,
                forbidden_tool_hit=forbidden_hit,
                unapproved_execution_rate=float(unapproved),
                duplicate_side_effect_rate=float(duplicate),
                task_success=(
                    fact_score.passed
                    and citation_score.precision == 1.0
                    and citation_score.recall == 1.0
                    and agent_scores.passed
                    and not forbidden_hit
                    and not unapproved
                    and not duplicate
                ),
            )
        return EvaluationCaseResult(
            actual_output={
                "run_id": run_id,
                "repetition": repetition,
                "content": content,
                "citation_ids": citation_ids,
                "ranked_chunk_ids": ranked_ids,
                "final_state": final_state,
                "operations": operations,
                **(
                    {"configuration_sha": configuration_sha256(self._configuration)}
                    if self._configuration is not None
                    else {}
                ),
                **actual_flags,
            },
            deterministic_scores=deterministic,
        )


def _citation_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        document_id = item.get("document_id")
        chunk_id = item.get("chunk_id")
        if isinstance(document_id, str) and isinstance(chunk_id, str):
            result.append(f"[DOC:{document_id}#{chunk_id}]")
    return result


def _tool_calls(value: object) -> tuple[ToolCall, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        ToolCall(str(item["name"]), item["arguments"])
        for item in value
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("arguments"), dict)
    )


def _approval(operation: dict[str, Any]) -> ApprovalExpectation | None:
    if not operation:
        return None
    decision = operation.get("policy_decision")
    status = operation.get("status")
    if decision == "ALLOW":
        return ApprovalExpectation.NOT_REQUIRED
    if status == "WAITING_APPROVAL":
        return ApprovalExpectation.PENDING
    if status in {"REJECTED", "DENIED"}:
        return ApprovalExpectation.REJECTED
    if decision == "REQUIRE_APPROVAL":
        return ApprovalExpectation.APPROVED
    return None
