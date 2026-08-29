"""Deterministic assistant rendering from grounded text and durable workflow facts."""

from opspilot.agent.runner import AgentOutcome, WorkflowFact


def _render_fact(fact: WorkflowFact) -> str:
    prefix = f"操作 {fact.operation_id}（{fact.tool}）"
    messages = {
        "WAITING_APPROVAL": "正在等待审批，尚未执行。",
        "READY": "已获准进入执行队列，尚未确认退款成功。",
        "EXECUTING": "正在执行，尚未确认退款成功。",
        "OUTCOME_UNKNOWN": "执行结果暂不确定，正在等待安全核对。",
        "RECONCILING": "正在核对外部执行结果。",
        "MANUAL_REVIEW": "需要人工复核，尚未确认退款成功。",
        "DENIED": "已被策略拒绝，未执行退款。",
        "REJECTED": "审批已拒绝，未执行退款。",
        "FAILED": "执行失败，未确认退款成功。",
        "SUCCEEDED": "退款成功。",
    }
    return f"{prefix}{messages.get(fact.status, '状态待确认。')}"


def render_assistant_reply(outcome: AgentOutcome) -> str:
    """Render one reply without allowing model text to impersonate workflow state."""
    facts = "\n".join(_render_fact(fact) for fact in outcome.workflow_facts)
    if outcome.workflow_facts:
        if outcome.grounded and outcome.final_answer:
            return f"{outcome.final_answer}\n\n{facts}"
        return facts
    return outcome.final_answer or outcome.clarification or "任务已进入受控处理流程。"
