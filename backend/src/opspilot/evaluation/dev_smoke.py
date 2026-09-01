"""Small public-API smoke runner which is structurally restricted to the dev split."""

import asyncio
from pathlib import Path

import httpx

from opspilot.config import Settings
from opspilot.evaluation.runner import PublicEvaluationApi
from opspilot.evaluation.schemas import EvaluationCase, load_jsonl


def load_dev_smoke_cases(dataset_root: Path, *, limit: int = 3) -> tuple[EvaluationCase, ...]:
    if limit < 1 or limit > 10:
        raise ValueError("dev smoke limit must be between 1 and 10")
    return load_jsonl(dataset_root / "dev.jsonl", EvaluationCase)[:limit]


async def run_dev_smoke() -> None:
    settings = Settings()
    if not settings.evaluation_api_token:
        raise RuntimeError("EVALUATION_API_TOKEN is required for dev smoke")
    cases = load_dev_smoke_cases(Path(settings.evaluation_dataset_root))
    async with httpx.AsyncClient(base_url=settings.evaluation_api_base_url, timeout=30) as client:
        runner = PublicEvaluationApi(client, settings.evaluation_api_token)
        for case in cases:
            await runner(case, 1)


if __name__ == "__main__":
    asyncio.run(run_dev_smoke())
