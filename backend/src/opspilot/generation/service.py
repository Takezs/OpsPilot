"""Orchestrates the chat provider and citation validation into an answer."""

from opspilot.generation.citations import ValidatedAnswer, validate_citations
from opspilot.generation.provider import GenerationProvider
from opspilot.retrieval.context_builder import BuiltContext


class GenerationService:
    def __init__(self, provider: GenerationProvider) -> None:
        self.provider = provider

    async def answer(self, *, query: str, context: BuiltContext) -> ValidatedAnswer:
        grounded = await self.provider.answer(query=query, context=context)
        return validate_citations(grounded, context)
