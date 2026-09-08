"""Read-only runtime checks; no evaluation case or receipt is created here."""

import asyncpg  # type: ignore[import-untyped]
import httpx
from redis.asyncio import Redis

from opspilot.config import Settings
from opspilot.evaluation.schemas import EvaluationConfiguration


async def runtime_health(
    settings: Settings, configuration: EvaluationConfiguration
) -> dict[str, bool]:
    result: dict[str, bool] = {}
    try:
        connection = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""), timeout=5)
        try:
            result["postgres"] = await connection.fetchval("SELECT 1") == 1
        finally:
            await connection.close()
    except Exception:
        result["postgres"] = False
    redis = Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5)
    try:
        result["redis"] = bool(await redis.ping())
    except Exception:
        result["redis"] = False
    finally:
        await redis.aclose()

    async def request(
        name: str,
        base: str,
        path: str,
        key: str,
        payload: dict[str, object] | None = None,
        proxy: str | None = None,
    ) -> None:
        try:
            async with httpx.AsyncClient(timeout=30, proxy=proxy) as client:
                response = await client.request(
                    "POST" if payload is not None else "GET",
                    base.rstrip("/") + path,
                    headers={"Authorization": f"Bearer {key}"} if key else {},
                    json=payload,
                )
                result[name] = response.status_code == 200
        except Exception:
            result[name] = False

    await request("api", "http://api:8000", "/health", "")
    await request(
        "deepseek_models",
        settings.deepseek_base_url,
        "/models",
        settings.deepseek_api_key,
        proxy=settings.deepseek_proxy_url,
    )
    await request(
        "deepseek_chat",
        settings.deepseek_base_url,
        "/chat/completions",
        settings.deepseek_api_key,
        {
            "model": configuration.model,
            "messages": [{"role": "user", "content": "Reply OK"}],
            "max_tokens": 1,
            "temperature": configuration.random_parameters["temperature"],
        },
        settings.deepseek_proxy_url,
    )
    await request(
        "bge_embedding",
        settings.bge_base_url,
        "/embeddings",
        settings.bge_api_key,
        {"model": configuration.embedding_model, "input": ["health"]},
    )
    await request(
        "bge_reranker",
        settings.bge_base_url,
        "/rerank",
        settings.bge_api_key,
        {"model": configuration.reranker_model, "query": "health", "documents": ["health"]},
    )
    return result
