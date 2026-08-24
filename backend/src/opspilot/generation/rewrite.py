"""Query rewrite lives in Generation and is called on demand by the orchestrator."""

from typing import Protocol

from openai import AsyncOpenAI

from opspilot.generation.prompts import REWRITE_SYSTEM_PROMPT


class QueryRewriter(Protocol):
    async def rewrite(self, query: str) -> str: ...


class IdentityQueryRewriter:
    """No-op rewrite used when there is no history or rewriting is disabled."""

    async def rewrite(self, query: str) -> str:
        return query.strip()


class DeepSeekQueryRewriter:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.model = model
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout_seconds)

    async def rewrite(self, query: str) -> str:
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
        )
        return (response.choices[0].message.content or query).strip()
