from opspilot.evaluation.models import (
    EvaluationCaseRecord,
    EvaluationJobOutbox,
    EvaluationTestExecution,
)


def test_task17_models_reserve_durable_audit_tables_and_repetition() -> None:
    assert EvaluationTestExecution.__tablename__ == "evaluation_test_executions"
    assert EvaluationJobOutbox.__tablename__ == "evaluation_job_outbox"
    assert "repetition" in EvaluationCaseRecord.__table__.columns
