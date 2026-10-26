"""
EAOC API v1 Human-in-the-Loop (HITL) Approvals Endpoints
Exposes REST endpoints for listing pending approvals, inspecting approval details,
and recording human approve/reject decisions into Supabase PostgreSQL.
Decoupled from workflow resumption: commits decisions without invoking the orchestrator.
"""

from datetime import datetime, timezone
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from eaoc.api.dependencies.auth import get_trusted_identity, require_roles
from eaoc.observability import redact_sensitive_data
from eaoc.schemas.approval import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalListResponse,
    ApprovalResponse,
    model_to_approval_response,
)
from eaoc.schemas.auth import AuthenticatedUser
from eaoc.state.approval_repository import IApprovalRepository

logger = logging.getLogger("eaoc.api.v1.approvals")

router = APIRouter(prefix="/approvals", tags=["Approvals"])


def get_approval_repository() -> IApprovalRepository:
    """
    Dependency provider returning the active approval repository instance.
    Defaults to PostgresApprovalRepository. Can be overridden in test suites.
    """
    from eaoc.state.approval_repository import PostgresApprovalRepository
    return PostgresApprovalRepository()


@router.get(
    "",
    response_model=ApprovalListResponse,
    status_code=status.HTTP_200_OK,
    summary="List Pending Approvals",
    description="Retrieves a paginated list of pending, unexpired approvals for the authenticated tenant.",
)
async def list_pending_approvals(
    limit: int = Query(default=50, ge=1, le=100, description="Maximum items per page (1 to 100)"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    user: AuthenticatedUser = Depends(get_trusted_identity),
    repo: IApprovalRepository = Depends(get_approval_repository),
) -> ApprovalListResponse:
    """
    HTTP GET /api/v1/approvals
    Lists pending unexpired approvals scoped to caller's tenant.
    """
    try:
        models = await repo.list_pending_models(
            tenant_id=user.tenant_id,
            limit=limit,
            offset=offset,
        )
        total = await repo.count_pending(tenant_id=user.tenant_id)
        items = [model_to_approval_response(m) for m in models]
        return ApprovalListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )
    except HTTPException:
        raise
    except RuntimeError as r_err:
        safe_msg = redact_sensitive_data(str(r_err))
        logger.error(f"Approval repository unavailable for tenant '{user.tenant_id}': {safe_msg}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Approval persistence store is unavailable.",
        )
    except Exception as exc:
        safe_err = redact_sensitive_data(str(exc))
        logger.error(f"Unexpected error listing approvals for tenant '{user.tenant_id}': {safe_err}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Approval persistence store is unavailable.",
        )


@router.get(
    "/{approval_id}",
    response_model=ApprovalResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Approval Detail",
    description="Retrieves full details of a specific approval request within the authenticated tenant.",
)
async def get_approval_detail(
    approval_id: str,
    user: AuthenticatedUser = Depends(get_trusted_identity),
    repo: IApprovalRepository = Depends(get_approval_repository),
) -> ApprovalResponse:
    """
    HTTP GET /api/v1/approvals/{approval_id}
    Retrieves approval details scoped to caller's tenant.
    """
    try:
        model = await repo.get_model_by_id(
            approval_id=approval_id,
            tenant_id=user.tenant_id,
        )
        if model is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Approval request '{approval_id}' was not found.",
            )
        return model_to_approval_response(model)
    except HTTPException:
        raise
    except RuntimeError as r_err:
        safe_msg = redact_sensitive_data(str(r_err))
        logger.error(f"Approval repository unavailable for '{approval_id}': {safe_msg}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Approval persistence store is unavailable.",
        )
    except Exception as exc:
        safe_err = redact_sensitive_data(str(exc))
        logger.error(f"Unexpected error retrieving approval '{approval_id}': {safe_err}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Approval persistence store is unavailable.",
        )


@router.post(
    "/{approval_id}/decision",
    response_model=ApprovalDecisionResponse,
    status_code=status.HTTP_200_OK,
    summary="Record Approval Decision",
    description="Atomically commits a human approval or rejection decision to Supabase PostgreSQL.",
)
async def record_approval_decision(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    user: AuthenticatedUser = Depends(require_roles("approver", "admin")),
    repo: IApprovalRepository = Depends(get_approval_repository),
) -> ApprovalDecisionResponse:
    """
    HTTP POST /api/v1/approvals/{approval_id}/decision
    Atomically records an APPROVED or REJECTED decision.
    Approver ID and Tenant ID are derived strictly from AuthenticatedUser.
    Decoupled from orchestrator execution: does NOT resume workflow.
    """
    now_utc_str = datetime.now(timezone.utc).isoformat()
    sanitized_reason = redact_sensitive_data(payload.reason) if payload.reason else None

    try:
        success, updated, code = await repo.record_conditional_decision(
            approval_id=approval_id,
            tenant_id=user.tenant_id,
            decision=payload.decision,
            approver_id=user.user_id,
            reason=sanitized_reason,
        )

        if success and updated is not None:
            logger.info(
                f"Committed {payload.decision.value} decision for approval '{approval_id}' by approver '{user.user_id}'.",
                extra={"approval_id": approval_id, "decision": payload.decision.value, "tenant_id": user.tenant_id},
            )
            return ApprovalDecisionResponse(
                approval_id=approval_id,
                workflow_id=updated.workflow_id,
                decision=payload.decision,
                approver_id=user.user_id,
                decided_at=now_utc_str,
                status=updated.status,
                resume_status="pending",
                message="Approval decision committed successfully to database. Workflow resumption is pending.",
            )

        if code == "not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Approval request '{approval_id}' was not found.",
            )
        elif code == "already_decided":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Approval request '{approval_id}' has already been decided.",
            )
        elif code == "expired":
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=f"Approval request '{approval_id}' has expired and can no longer be decided.",
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Unable to commit decision on approval '{approval_id}': {code}.",
            )

    except HTTPException:
        raise
    except RuntimeError as r_err:
        safe_msg = redact_sensitive_data(str(r_err))
        logger.error(f"Approval repository unavailable during decision on '{approval_id}': {safe_msg}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Approval persistence store is unavailable.",
        )
    except Exception as exc:
        safe_err = redact_sensitive_data(str(exc))
        logger.error(f"Unexpected error during decision on '{approval_id}': {safe_err}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Approval persistence store is unavailable.",
        )
