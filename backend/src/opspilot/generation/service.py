"""Orchestrates the chat provider and citation validation into an answer."""

import asyncio
from collections.abc import Callable

from opspilot.generation.citations import ValidatedAnswer, validate_citations
from opspilot.generation.provider import (
    GenerationPrompt,
    GenerationProvider,
    render_generation_prompt,
)
from opspilot.retrieval.context_builder import BuiltContext


class GenerationService:
    def __init__(
        self,
        provider: GenerationProvider,
        *,
        max_validation_attempts: int = 3,
        attempt_timeout_seconds: float = 30.0,
    ) -> None:
        if max_validation_attempts < 1:
            raise ValueError("max validation attempts must be positive")
        if attempt_timeout_seconds <= 0:
            raise ValueError("attempt timeout must be positive")
        self.provider = provider
        self.max_validation_attempts = max_validation_attempts
        self.attempt_timeout_seconds = attempt_timeout_seconds

    async def answer(
        self,
        *,
        query: str,
        context: BuiltContext,
        before_attempt: Callable[[GenerationPrompt], None] | None = None,
    ) -> ValidatedAnswer:
        validated: ValidatedAnswer | None = None
        for _ in range(self.max_validation_attempts):
            prompt = render_generation_prompt(query, context)
            if before_attempt is not None:
                before_attempt(prompt)
            try:
                async with asyncio.timeout(self.attempt_timeout_seconds):
                    grounded = await self.provider.answer(prompt=prompt)
            except TimeoutError:
                continue
            validated = validate_citations(grounded, context)
            if validated.follow_up_question is not None:
                return validated
            if not validated.insufficient_evidence and validated.snapshots:
                return validated
        if validated is not None:
            return validated
        return ValidatedAnswer(
            answer="",
            citations=(),
            snapshots=(),
            insufficient_evidence=True,
            follow_up_question=None,
        )
