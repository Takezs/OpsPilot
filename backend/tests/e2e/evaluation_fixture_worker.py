"""Immutable synthetic-only smoke entrypoint; never used by the release Worker."""

import hashlib
import json
import os

from arq.worker import run_worker
from pydantic import Field

from opspilot.evaluation import tasks
from opspilot.evaluation.schemas import AgentEvaluationCase, EvaluationCase, FrozenDatasetSnapshot
from opspilot.worker import WorkerSettings, startup_worker


class SyntheticRefundCase(AgentEvaluationCase):
    # This namespace belongs only to the test image, never the frozen schema.
    order_number: str | None = Field(default=None, pattern=r"^E2E-[0-9a-f]{32}$")


def refund_case(repetition):
    namespace = os.environ["OPSPILOT_SYNTHETIC_REFUND_NAMESPACE"]
    order = "E2E-" + hashlib.sha256(f"{namespace}:{repetition}".encode()).hexdigest()[:32]
    return SyntheticRefundCase(
        schema_version="1.0.0",
        case_id="synthetic-refund",
        category="synthetic-refund",
        query=(
            f"Call check_refund_eligibility for {order}, then create the "
            f"refund_order Operation for {order} amount 350."
        ),
        relevant_chunk_ids=(),
        expected_citation_ids=(),
        required_facts=(),
        forbidden_facts=(),
        expected_tools=(),
        forbidden_tools=(),
        follow_up_required=False,
        approval_required=True,
        expected_final_state="SUCCEEDED",
        operation_expected=True,
        order_number=order,
        amount="350",
        expected_idempotency_key=f"refund:{order}",
        expected_approval="APPROVED",
    )


def snapshot():
    base = {
        "schema_version": "1.0.0",
        "category": "synthetic-preflight",
        "query": "Hello. Please reply with a short greeting.",
        "relevant_chunk_ids": [],
        "expected_citation_ids": [],
        "required_facts": [],
        "forbidden_facts": [],
        "expected_tools": [],
        "forbidden_tools": [],
        "follow_up_required": False,
        "approval_required": False,
        "expected_final_state": "ANSWERED",
    }
    regular = tuple(
        EvaluationCase.model_validate(base | {"case_id": f"synthetic-{i}"}) for i in range(3)
    )
    agent = AgentEvaluationCase.model_validate(
        base
        | {
            "case_id": "synthetic-agent",
            "operation_expected": False,
        }
    )
    if os.getenv("OPSPILOT_SYNTHETIC_REFUND_NAMESPACE"):
        agent = refund_case(1)
    identity = {
        "synthetic_fixture_sha": hashlib.sha256(
            json.dumps(
                [case.model_dump(mode="json") for case in (*regular, agent)], sort_keys=True
            ).encode()
        ).hexdigest()
    }
    return FrozenDatasetSnapshot(identity, regular, (agent,))


class MeasuredProcessor:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.active = self.peak = 0

    async def bind_configuration(self, configuration, expected_sha):
        await self.wrapped.bind_configuration(configuration, expected_sha)
        return self

    async def __call__(self, case, repetition):
        if case.case_id == "synthetic-refund":
            case = refund_case(repetition)
        self.active += 1
        self.peak = max(self.active, self.peak)
        print("SYNTHETIC_ACTIVE " + str(self.active), flush=True)
        try:
            return await self.wrapped(case, repetition)
        finally:
            self.active -= 1


async def startup(ctx):
    await startup_worker(ctx)
    ctx["evaluation_case_processor"] = MeasuredProcessor(ctx["evaluation_case_processor"])
    # A test-only image supplies immutable synthetic data to the unmodified
    # production executor. It never loads or executes the held-out corpus.
    tasks.capture_frozen_dataset = lambda _root: snapshot()
    print("SYNTHETIC_PREFLIGHT_PASSED true", flush=True)


if __name__ == "__main__":
    WorkerSettings.on_startup = startup
    run_worker(WorkerSettings)
