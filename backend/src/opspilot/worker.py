import httpx
from arq import func
from arq.connections import RedisSettings

from opspilot.agent.knowledge import RunKnowledgeSearch
from opspilot.agent.reply import render_assistant_reply
from opspilot.agent.runner import AgentRunner, DeepSeekAgentDecider
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.execution.executor import drain_detached_provider_tasks
from opspilot.execution.service import build_refund_operation_handler
from opspilot.generation.citations import ValidatedAnswer
from opspilot.generation.provider import DeepSeekGenerationProvider
from opspilot.generation.service import GenerationService
from opspilot.jobs.run_executor import RunJobFence, drain_detached_run_processor_tasks
from opspilot.jobs.tasks import RunMessageResult, process_operation_job, process_run_message
from opspilot.knowledge.embedding import BgeM3EmbeddingProvider
from opspilot.knowledge.tasks import (
    DOCUMENT_INDEX_JOB_TIMEOUT,
    DOCUMENT_INDEX_MAX_TRIES,
    index_document,
)
from opspilot.retrieval.context_builder import BuiltContext
from opspilot.retrieval.reranker import BgeReranker
from opspilot.runs.models import RunMessage
from opspilot.tools.adapters.python import (
    check_refund_eligibility_adapter,
    get_order_adapter,
    get_refund_status_adapter,
    refund_order_adapter,
    send_email_adapter,
)
from opspilot.tools.registry import ToolDependencies, build_tool_registry
from opspilot.tools.schemas import SearchKnowledgeArgs
from opspilot.tools.types import ToolResult

RUN_MESSAGE_JOB_TIMEOUT = 600


async def shutdown_worker(ctx: dict[str, object]) -> None:
    """Collect cancellation-resistant provider tasks before ARQ exits."""
    await drain_detached_provider_tasks()
    await drain_detached_run_processor_tasks()
    for name in ("order_client", "payment_client", "email_client"):
        client = ctx.get(name)
        if isinstance(client, httpx.AsyncClient):
            await client.aclose()
    for name in ("embedding_provider", "reranker_provider", "generation_provider", "decider"):
        provider = ctx.get(name)
        close = getattr(provider, "aclose", None)
        if callable(close):
            await close()


async def startup_worker(ctx: dict[str, object]) -> None:
    settings = Settings()
    order_client = httpx.AsyncClient(base_url=settings.order_service_url, timeout=5)
    payment_client = httpx.AsyncClient(base_url=settings.payment_service_url, timeout=5)
    email_client = httpx.AsyncClient(base_url=settings.email_service_url, timeout=5)
    ctx.update(order_client=order_client, payment_client=payment_client, email_client=email_client)
    embedding_provider = BgeM3EmbeddingProvider(
        settings.bge_base_url, settings.bge_api_key, settings.bge_embedding_model
    )
    reranker_provider = BgeReranker(
        settings.bge_base_url,
        settings.bge_api_key,
        settings.bge_reranker_model,
    )
    generation_provider = DeepSeekGenerationProvider(
        settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        proxy_url=settings.deepseek_proxy_url,
    )
    generation_service = GenerationService(generation_provider)
    decider = DeepSeekAgentDecider(
        settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        proxy_url=settings.deepseek_proxy_url,
    )
    ctx.update(
        embedding_provider=embedding_provider,
        reranker_provider=reranker_provider,
        generation_provider=generation_provider,
        decider=decider,
    )

    async def process(message: RunMessage, fence: RunJobFence) -> RunMessageResult:
        run_id = message.run_id
        knowledge_search = RunKnowledgeSearch(
            async_session_factory,
            run_id,
            embedding_provider,
            reranker_provider,
            context_token_budget=settings.generation_context_token_budget,
            reranker_timeout_seconds=settings.retrieval_reranker_timeout_seconds,
        )

        async def search_tool(arguments: SearchKnowledgeArgs) -> ToolResult:
            return (await knowledge_search(arguments)).summary

        registry = build_tool_registry(
            ToolDependencies(
                search_knowledge=search_tool,
                get_order=get_order_adapter(order_client),
                check_refund_eligibility=check_refund_eligibility_adapter(payment_client),
                refund_order=refund_order_adapter(payment_client),
                get_refund_status=get_refund_status_adapter(payment_client),
                send_email=send_email_adapter(email_client),
            )
        )

        async def grounded_answer(query: str, context: BuiltContext) -> ValidatedAnswer:
            return await generation_service.answer(query=query, context=context)

        runner = AgentRunner(
            registry,
            decider,
            side_effect_handler=await build_refund_operation_handler(
                async_session_factory, run_id, transaction_guard=fence.lock
            ),
            knowledge_search_handler=knowledge_search,
            grounded_answer_handler=grounded_answer,
        )
        outcome = await runner.run(message.content)
        snapshots: list[dict[str, object]] = [
            {
                "document_id": snapshot.document_id,
                "document_version": snapshot.document_version,
                "chunk_id": snapshot.chunk_id,
                "section_path": list(snapshot.section_path),
                "page": snapshot.page,
            }
            for snapshot in outcome.citation_snapshots
        ]
        return RunMessageResult(
            content=render_assistant_reply(outcome), citation_snapshots=snapshots
        )

    ctx["run_message_processor"] = process


class WorkerSettings:
    functions = [
        func(
            index_document,
            max_tries=DOCUMENT_INDEX_MAX_TRIES,
            timeout=DOCUMENT_INDEX_JOB_TIMEOUT,
        ),
        func(process_operation_job, max_tries=3, timeout=120),
        func(process_run_message, max_tries=3, timeout=RUN_MESSAGE_JOB_TIMEOUT),
    ]
    on_shutdown = shutdown_worker
    on_startup = startup_worker
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)
