import hashlib
from typing import Protocol

from openai import AsyncOpenAI


class EmbeddingProvider(Protocol):
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class DeterministicEmbeddingProvider:
    model = "deterministic-test-embedding"

    def __init__(self, dimensions: int = 1024) -> None:
        self.dimensions = dimensions
        self.batch_sizes: list[int] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            vectors.append(
                [((digest[index % len(digest)] / 255) * 2) - 1 for index in range(self.dimensions)]
            )
        return vectors


class BgeM3EmbeddingProvider:
    def __init__(self, base_url: str, api_key: str, model: str = "BAAI/bge-m3") -> None:
        self.model = model
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await self._client.embeddings.create(model=self.model, input=texts)
        return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]


def embedding_cache_key(model: str, content: str) -> str:
    return f"{model}:{hashlib.sha256(content.encode()).hexdigest()}"
