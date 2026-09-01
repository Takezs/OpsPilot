"""Public HTTP API-only evaluation case adapter."""

import asyncio
from typing import Any

import httpx

from opspilot.evaluation.answer_metrics import evaluate_answer_facts, evaluate_citation_ids
from opspilot.evaluation.retrieval_metrics import (
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from opspilot.evaluation.schemas import EvaluationCase
from opspilot.evaluation.tasks import EvaluationCaseResult


class PublicEvaluationApi:
    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        *,
        poll_seconds: float = 0.25,
        max_polls: int = 240,
    ) -> None:
        if not token:
            raise ValueError("evaluation API token is required")
        self._client = client
        self._headers = {"Authorization": f"Bearer {token}"}
        self._poll_seconds = poll_seconds
        self._max_polls = max_polls

    async def __call__(self, case: EvaluationCase, repetition: int) -> EvaluationCaseResult:
        ranked_ids: list[str] = []
        if case.relevant_chunk_ids:
            retrieval_response = await self._client.post(
                "/retrieval/debug",
                headers=self._headers,
                json={"query": case.query, "top_k": 5},
            )
            retrieval_response.raise_for_status()
            retrieval = retrieval_response.json()
            stage = retrieval.get("reranker") or retrieval.get("rrf") or []
            ranked_ids = [
                str(item["chunk_id"])
                for item in stage
                if isinstance(item, dict) and item.get("chunk_id") is not None
            ]
        run_response = await self._client.post("/runs", headers=self._headers)
        run_response.raise_for_status()
        run_id = str(run_response.json()["run_id"])
        message_response = await self._client.post(
            f"/runs/{run_id}/messages",
            headers=self._headers,
            json={"content": case.query},
        )
        message_response.raise_for_status()
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
        return EvaluationCaseResult(
            actual_output={
                "run_id": run_id,
                "repetition": repetition,
                "content": content,
                "citation_ids": citation_ids,
                "ranked_chunk_ids": ranked_ids,
                "final_state": final_state,
                "operations": operations,
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
