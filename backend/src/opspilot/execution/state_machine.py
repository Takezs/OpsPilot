"""Legal Operation state transitions, enforced fail-closed (Task 10).

The table mirrors design §6.2: CREATED/WAITING_APPROVAL flow into READY (or a
terminal denial), READY and RETRYING are the only claimable statuses
(-> EXECUTING), an EXECUTING lease may commit a result, fail definitively, be
recovered for retry, or become OUTCOME_UNKNOWN when a side-effect outcome is
uncertain. The uncertainty bucket (OUTCOME_UNKNOWN -> RECONCILING and the
reconciliation verdicts) is defined here for completeness but the actual
reconciliation flow belongs to task 11.

MANUAL_REVIEW / SUCCEEDED / FAILED / DENIED / REJECTED are terminal: no
transition leaves them (a MANUAL_REVIEW retry is a *new* Operation via task 9's
resolution gate, not a transition of this row). The terminal statuses are also
not claimable, so nothing here can re-invoke a side-effect provider.
"""

from opspilot.execution.models import OperationStatus
from opspilot.execution.service import OperationError

TERMINAL_STATUSES = frozenset(
    {
        OperationStatus.MANUAL_REVIEW,
        OperationStatus.SUCCEEDED,
        OperationStatus.FAILED,
        OperationStatus.DENIED,
        OperationStatus.REJECTED,
    }
)

CLAIMABLE_STATUSES = frozenset({OperationStatus.READY, OperationStatus.RETRYING})

_LEGAL_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.CREATED: frozenset(
        {OperationStatus.WAITING_APPROVAL, OperationStatus.READY, OperationStatus.DENIED}
    ),
    OperationStatus.WAITING_APPROVAL: frozenset(
        {OperationStatus.READY, OperationStatus.DENIED, OperationStatus.REJECTED}
    ),
    OperationStatus.READY: frozenset({OperationStatus.EXECUTING}),
    OperationStatus.EXECUTING: frozenset(
        {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.RETRYING,
            OperationStatus.OUTCOME_UNKNOWN,
            OperationStatus.RECONCILING,
            OperationStatus.MANUAL_REVIEW,
        }
    ),
    OperationStatus.RETRYING: frozenset({OperationStatus.EXECUTING}),
    OperationStatus.OUTCOME_UNKNOWN: frozenset(
        {
            OperationStatus.RECONCILING,
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.RETRYING,
            OperationStatus.MANUAL_REVIEW,
        }
    ),
    OperationStatus.RECONCILING: frozenset(
        {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.RETRYING,
            OperationStatus.MANUAL_REVIEW,
            OperationStatus.OUTCOME_UNKNOWN,
        }
    ),
    OperationStatus.MANUAL_REVIEW: frozenset(),
    OperationStatus.SUCCEEDED: frozenset(),
    OperationStatus.FAILED: frozenset(),
    OperationStatus.DENIED: frozenset(),
    OperationStatus.REJECTED: frozenset(),
}


class IllegalStateTransitionError(OperationError):
    """Raised when a transition is not allowed by the state machine."""


def assert_transition(current: OperationStatus, target: OperationStatus) -> None:
    """Raise ``IllegalStateTransitionError`` unless the transition is legal."""
    allowed = _LEGAL_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise IllegalStateTransitionError(
            f"illegal operation transition {current.value} -> {target.value}"
        )
