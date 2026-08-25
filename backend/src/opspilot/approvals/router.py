"""HTTP boundary for approval decisions and manual-review resolutions."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from opspilot.approvals.models import ResolutionOutcome
from opspilot.approvals.schemas import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ManualReviewResolutionRequest,
    ManualReviewResolutionResponse,
    RetryRequest,
    RetryResponse,
)
from opspilot.approvals.service import (
    ApprovalDecision,
    ApprovalExpiredError,
    ApprovalForbiddenError,
    ApprovalNotFoundError,
    ApprovalVersionConflictError,
    decide_approval,
    record_manual_review_resolution,
)
from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.models import Role
from opspilot.auth.schemas import Principal
from opspilot.db import async_session_factory
from opspilot.execution.models import Operation, OperationStatus
from opspilot.execution.service import (
    ManualReviewNotTerminalError,
    OperationNotFoundError,
    ResolutionForbiddenError,
    RetryNotAuthorizedError,
    create_refund_operation,
)

router = APIRouter()


@router.post(
    "/approval-requests/{approval_request_id}/decisions",
    response_model=ApprovalDecisionResponse,
)
async def decide(
    approval_request_id: uuid.UUID,
    request: ApprovalDecisionRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> ApprovalDecisionResponse:
    if principal.role not in {Role.REVIEWER, Role.ADMIN}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "reviewer or admin role required")
    async with async_session_factory() as session:
        try:
            result = await decide_approval(
                session,
                approval_request_id,
                ApprovalDecision(request.decision),
                decided_by=principal.user_id,
                role=principal.role,
                comment=request.comment,
            )
        except ApprovalForbiddenError as error:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error
        except ApprovalNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
        except (ApprovalExpiredError, ApprovalVersionConflictError) as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        await session.commit()
    return ApprovalDecisionResponse(
        already_processed=result.already_processed,
        transitioned=result.transitioned,
        approval_request_id=result.approval.id,
        operation_id=result.operation.id,
        operation_status=result.operation.status.value,
        operation_version=result.operation.version,
    )


@router.post(
    "/operations/{operation_id}/manual-review-resolutions",
    response_model=ManualReviewResolutionResponse,
)
async def resolve_manual_review(
    operation_id: uuid.UUID,
    request: ManualReviewResolutionRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> ManualReviewResolutionResponse:
    if principal.role is not Role.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    async with async_session_factory() as session:
        try:
            resolution = await record_manual_review_resolution(
                session,
                operation_id,
                ResolutionOutcome(request.outcome),
                resolved_by=principal.user_id,
                role=principal.role,
                note=request.note,
            )
        except ApprovalForbiddenError as error:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error
        except OperationNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
        except ManualReviewNotTerminalError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        await session.commit()
    return ManualReviewResolutionResponse(resolution_id=resolution.id, operation_id=operation_id)


@router.post("/operations/{operation_id}/retry", response_model=RetryResponse)
async def retry_operation(
    operation_id: uuid.UUID,
    request: RetryRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> RetryResponse:
    if principal.role is not Role.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    async with async_session_factory() as session:
        original = await session.get(Operation, operation_id)
        if original is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "operation not found")
        if original.status is not OperationStatus.MANUAL_REVIEW:
            raise HTTPException(status.HTTP_409_CONFLICT, "operation is not in MANUAL_REVIEW")
        order_number = original.normalized_arguments.get("order_number")
        if not isinstance(order_number, str) or not order_number:
            raise HTTPException(status.HTTP_409_CONFLICT, "operation has no order_number")
        try:
            operation = await create_refund_operation(
                session,
                original.run_id,
                order_number,
                request.amount,
                retry_of_operation_id=operation_id,
                role=principal.role,
            )
        except ResolutionForbiddenError as error:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error
        except RetryNotAuthorizedError as error:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"RETRY_NOT_AUTHORIZED: {error}"
            ) from error
        await session.commit()
    return RetryResponse(
        operation_id=operation.id,
        status=operation.status.value,
        idempotency_key=operation.idempotency_key,
    )
