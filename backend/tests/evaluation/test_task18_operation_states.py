import pytest

from opspilot.evaluation.schemas import ExpectedOutcome


@pytest.mark.parametrize("state", ["RETRYING", "READY", "EXECUTING", "RECONCILING"])
def test_operation_intermediate_state_is_preserved_as_fact(state):
    assert ExpectedOutcome(state).value == state
