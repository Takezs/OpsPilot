import uuid

from opspilot.agent.reply import render_assistant_reply
from opspilot.agent.runner import AgentOutcome, WorkflowFact


def test_workflow_only_reply_uses_server_fact_not_model_answer() -> None:
    operation_id = str(uuid.uuid4())
    outcome = AgentOutcome(
        final_answer="I already refunded it successfully",
        workflow_facts=(WorkflowFact(operation_id, "refund_order", "WAITING_APPROVAL"),),
    )

    reply = render_assistant_reply(outcome)

    assert "I already refunded" not in reply
    assert operation_id in reply
    assert "等待审批" in reply
    assert "退款成功" not in reply


def test_ready_or_executing_workflow_never_claims_refund_succeeded() -> None:
    for status in ("READY", "EXECUTING", "OUTCOME_UNKNOWN", "RECONCILING"):
        outcome = AgentOutcome(
            workflow_facts=(WorkflowFact(str(uuid.uuid4()), "refund_order", status),)
        )
        assert "已退款成功" not in render_assistant_reply(outcome)


def test_grounded_answer_and_workflow_fact_form_one_typed_reply() -> None:
    operation_id = str(uuid.uuid4())
    outcome = AgentOutcome(
        final_answer="政策要求审批 [DOC:d1#c1]。",
        grounded=True,
        workflow_facts=(WorkflowFact(operation_id, "refund_order", "WAITING_APPROVAL"),),
    )

    reply = render_assistant_reply(outcome)

    assert reply.startswith("政策要求审批")
    assert operation_id in reply
    assert reply.count(operation_id) == 1
