"""Structured answer provider contract and OpenAI-compatible DeepSeek client."""

from collections.abc import Sequence
from typing import Protocol

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel

from opspilot.generation.prompts import SYSTEM_PROMPT, render_user_prompt
from opspilot.retrieval.context_builder import BuiltContext


class GroundedAnswer(BaseModel):
    answer: str
    citations: list[str]
    insufficient_evidence: bool
    follow_up_question: str | None = None


class GenerationProvider(Protocol):
    async def answer(self, *, query: str, context: BuiltContext) -> GroundedAnswer: ...


class GenerationProviderError(RuntimeError):
    """Raised when the chat provider cannot produce a structured answer."""


class DeepSeekGenerationProvider:
    """Structured answers from DeepSeek V4 Flash via the OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        timeout_seconds: float = 60.0,
        proxy_url: str | None = None,
    ) -> None:
        self.model = model
        http_client = httpx.AsyncClient(proxy=proxy_url) if proxy_url else None
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout_seconds,
            http_client=http_client,
        )

    async def answer(self, *, query: str, context: BuiltContext) -> GroundedAnswer:
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": render_user_prompt(query, context)},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if content is None:
            raise GenerationProviderError("Chat provider returned an empty response")
        return GroundedAnswer.model_validate_json(content)

    async def aclose(self) -> None:
        """Close the owned OpenAI-compatible HTTP client."""
        await self._client.close()


class DeterministicGenerationProvider:
    """Canned answers for unit tests; never calls an external service."""

    def __init__(
        self,
        *,
        answer: str = "Deterministic answer.",
        citations: Sequence[str] = (),
        insufficient_evidence: bool = False,
        follow_up_question: str | None = None,
    ) -> None:
        self._answer = answer
        self._citations = list(citations)
        self._insufficient_evidence = insufficient_evidence
        self._follow_up_question = follow_up_question

    async def answer(self, *, query: str, context: BuiltContext) -> GroundedAnswer:
        return GroundedAnswer(
            answer=self._answer,
            citations=self._citations,
            insufficient_evidence=self._insufficient_evidence,
            follow_up_question=self._follow_up_question,
        )
