import httpx
from arq import func
from arq.connections import RedisSettings

from opspilot.agent.runner import AgentRunner, DeepSeekAgentDecider
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.execution.executor import drain_detached_provider_tasks
from opspilot.execution.service import build_refund_operation_handler
from opspilot.jobs.run_executor import drain_detached_run_processor_tasks
from opspilot.jobs.tasks import RunMessageResult, process_operation_job, process_run_message
from opspilot.knowledge.tasks import (
    DOCUMENT_INDEX_JOB_TIMEOUT,
    DOCUMENT_INDEX_MAX_TRIES,
    index_document,
)
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


async def shutdown_worker(ctx: dict[str, object]) -> None:
    """Collect cancellation-resistant provider tasks before ARQ exits."""
    await drain_detached_provider_tasks()
    await drain_detached_run_processor_tasks()
    for name in ("order_client", "payment_client", "email_client"):
        client = ctx.get(name)
        if isinstance(client, httpx.AsyncClient):
            await client.aclose()


async def startup_worker(ctx: dict[str, object]) -> None:
    settings = Settings()
    order_client = httpx.AsyncClient(base_url=settings.order_service_url, timeout=5)
    payment_client = httpx.AsyncClient(base_url=settings.payment_service_url, timeout=5)
    email_client = httpx.AsyncClient(base_url=settings.email_service_url, timeout=5)
    ctx.update(order_client=order_client, payment_client=payment_client, email_client=email_client)

    async def unavailable_search(_: SearchKnowledgeArgs) -> ToolResult:
        return ToolResult(ok=False, error="search is unavailable in the operation worker")

    registry = build_tool_registry(
        ToolDependencies(
            search_knowledge=unavailable_search,
            get_order=get_order_adapter(order_client),
            check_refund_eligibility=check_refund_eligibility_adapter(payment_client),
            refund_order=refund_order_adapter(payment_client),
            get_refund_status=get_refund_status_adapter(payment_client),
            send_email=send_email_adapter(email_client),
        )
    )
    decider = DeepSeekAgentDecider(
        settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
    )

    async def process(message: RunMessage) -> RunMessageResult:
        run_id = message.run_id
        runner = AgentRunner(
            registry,
            decider,
            side_effect_handler=await build_refund_operation_handler(async_session_factory, run_id),
        )
        outcome = await runner.run(message.content)
        content = outcome.final_answer or outcome.clarification or "任务已进入受控处理流程。"
        return RunMessageResult(content=content, citation_snapshots=[])

    ctx["run_message_processor"] = process


class WorkerSettings:
    functions = [
        func(
            index_document,
            max_tries=DOCUMENT_INDEX_MAX_TRIES,
            timeout=DOCUMENT_INDEX_JOB_TIMEOUT,
        ),
        func(process_operation_job, max_tries=3, timeout=120),
        func(process_run_message, max_tries=3, timeout=120),
    ]
    on_shutdown = shutdown_worker
    on_startup = startup_worker
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)
