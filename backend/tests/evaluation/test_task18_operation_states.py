import pytest

from opspilot.evaluation.schemas import ExpectedOutcome


@pytest.mark.parametrize(
    "state",
    [
        "CREATED",
        "RETRYING",
        "READY",
        "EXECUTING",
        "OUTCOME_UNKNOWN",
        "RECONCILING",
        "SUCCEEDED",
        "FAILED",
        "WAITING_APPROVAL",
        "MANUAL_REVIEW",
        "REJECTED",
        "DENIED",
    ],
)
def test_operation_intermediate_state_is_preserved_as_fact(state):
    assert ExpectedOutcome(state).value == state
