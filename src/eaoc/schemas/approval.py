"""
EAOC Approval API Schemas
Defines request and response schemas for Human-in-the-Loop (HITL) approval endpoints.
"""

from datetime import timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator

from eaoc.schemas.domain import (
    ApprovalDecision,
    ApprovalStatus,
    RiskLevel,
)


class ApprovalResponse(BaseModel):
    """Full approval request representation returned by API."""
    approval_id: str = Field(..., description="Unique approval identifier (appr-[12-hex])")
    workflow_id: str = Field(..., description="Associated workflow identifier")
    tenant_id: str = Field(..., description="Tenant organization boundary")
    task_id: Optional[str] = Field(default=None, description="Associated task identifier")
    step_index: Optional[int] = Field(default=None, description="Plan step index requiring approval")
    requested_action: str = Field(..., description="Action description awaiting human review")
    risk_level: RiskLevel = Field(..., description="Assessed risk level")
    status: ApprovalStatus = Field(..., description="Current approval lifecycle status")
    requested_at: str = Field(..., description="ISO-8601 UTC timestamp of request creation")
    expires_at: str = Field(..., description="ISO-8601 UTC timestamp when approval expires")
    details: Dict[str, Any] = Field(default_factory=dict, description="Operational details and parameters")
    decision: Optional[ApprovalDecision] = Field(default=None, description="Committed human decision record, if any")
    resume_status: str = Field(default="none", description="Recovery resume lifecycle status (none, pending, resumed, failed)")


class ApprovalListResponse(BaseModel):
    """Paginated list of pending approval requests."""
    items: List[ApprovalResponse] = Field(..., description="List of pending approval requests")
    total: int = Field(..., ge=0, description="Total count of pending approvals for this tenant")
    limit: int = Field(..., ge=1, le=100, description="Pagination page size limit")
    offset: int = Field(..., ge=0, description="Pagination offset")


class ApprovalDecisionRequest(BaseModel):
    """Input payload for submitting a human approval or rejection decision."""
    decision: ApprovalStatus = Field(
        ...,
        description="Decision status: must be APPROVED or REJECTED"
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Optional human rationale for the approval or rejection decision (max 1000 chars)"
    )

    @field_validator("decision")
    @classmethod
    def validate_decision_enum(cls, v: ApprovalStatus) -> ApprovalStatus:
        if v not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            raise ValueError("Decision must be either 'APPROVED' or 'REJECTED'.")
        return v


class ApprovalDecisionResponse(BaseModel):
    """Response returned upon committing an approval decision."""
    approval_id: str = Field(..., description="Approval identifier")
    workflow_id: str = Field(..., description="Workflow identifier")
    decision: ApprovalStatus = Field(..., description="Recorded human decision (APPROVED or REJECTED)")
    approver_id: str = Field(..., description="Verified approver identity from authenticated token")
    decided_at: str = Field(..., description="ISO-8601 UTC timestamp of decision commit")
    status: ApprovalStatus = Field(..., description="Updated approval status")
    resume_status: str = Field(default="pending", description="Resume lifecycle status (pending)")
    message: str = Field(..., description="Informational message on decision commit and pending resume")


def model_to_approval_response(model: Any) -> ApprovalResponse:
    """Helper converting an ApprovalRequestModel into an ApprovalResponse schema."""
    req_dt = model.requested_at
    if req_dt.tzinfo is None:
        req_dt = req_dt.replace(tzinfo=timezone.utc)
    exp_dt = model.expires_at
    if exp_dt.tzinfo is None:
        exp_dt = exp_dt.replace(tzinfo=timezone.utc)

    decision = model.to_domain_decision() if hasattr(model, "to_domain_decision") else None

    return ApprovalResponse(
        approval_id=model.approval_id,
        workflow_id=model.workflow_id,
        tenant_id=model.tenant_id,
        task_id=model.task_id,
        step_index=model.step_index,
        requested_action=model.requested_action,
        risk_level=RiskLevel(model.risk_level) if isinstance(model.risk_level, str) else model.risk_level,
        status=ApprovalStatus(model.status) if isinstance(model.status, str) else model.status,
        requested_at=req_dt.isoformat(),
        expires_at=exp_dt.isoformat(),
        details=dict(model.details) if model.details else {},
        decision=decision,
        resume_status=model.resume_status,
    )
