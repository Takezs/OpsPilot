from typing import Any

import pytest

from opspilot import worker
from opspilot.agent import runner as agent_runner
from opspilot.generation import provider as generation_provider
from opspilot.knowledge import embedding
from opspilot.retrieval.reranker import BgeReranker


class _ClosableOpenAI:
    def __init__(self, **_: Any) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _ClosableHttpClient:
    def __init__(self) -> None:
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


@pytest.mark.parametrize(
    ("module", "factory"),
    [
        (
            generation_provider,
            lambda: generation_provider.DeepSeekGenerationProvider(api_key="configured"),
        ),
        (
            agent_runner,
            lambda: agent_runner.DeepSeekAgentDecider(api_key="configured"),
        ),
        (
            embedding,
            lambda: embedding.BgeM3EmbeddingProvider(
                base_url="http://bge.invalid/v1", api_key="configured"
            ),
        ),
    ],
)
async def test_openai_compatible_providers_close_owned_client(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    factory: Any,
) -> None:
    monkeypatch.setattr(module, "AsyncOpenAI", _ClosableOpenAI)
    provider = factory()
    client = provider._client

    await provider.aclose()

    assert client.close_calls == 1


async def test_bge_reranker_closes_injected_http_client() -> None:
    client = _ClosableHttpClient()
    reranker = BgeReranker(
        base_url="http://bge.invalid/v1",
        api_key="configured",
        client=client,  # type: ignore[arg-type]
    )

    await reranker.aclose()

    assert client.close_calls == 1


async def test_worker_shutdown_closes_grounding_providers_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = {
        name: _ClosableHttpClient()
        for name in (
            "embedding_provider",
            "reranker_provider",
            "generation_provider",
            "decider",
        )
    }

    async def drained() -> None:
        return None

    monkeypatch.setattr(worker, "drain_detached_provider_tasks", drained)
    monkeypatch.setattr(worker, "drain_detached_run_processor_tasks", drained)

    await worker.shutdown_worker(dict(providers))

    assert all(provider.close_calls == 1 for provider in providers.values())
