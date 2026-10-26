"""
EAOC Approval Request Database Model
Defines the SQLAlchemy 2.0 Declarative ORM model for persistent Human-in-the-Loop (HITL)
approval requests, decisions, expiration, and resume-recovery tracking in Supabase PostgreSQL.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from eaoc.db.base import Base
from eaoc.schemas.domain import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
)


class ApprovalRequestModel(Base):
    """
    Durable PostgreSQL ORM model for Human-in-the-Loop (HITL) approval requests.
    Tracks approval lifecycle, approver attribution, expiration, and resume-recovery state.
    """
    __tablename__ = "approval_requests"

    # Primary Identifier (matches RiskGate "appr-[12-hex]")
    approval_id: Mapped[str] = mapped_column(String(50), primary_key=True)

    # Scoping Identifiers
    workflow_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    step_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Request Attributes
    requested_action: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ApprovalStatus.PENDING.value)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    details: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    # Decision Audit Attributes (Populated upon human decision)
    decision: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    approver_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    decision_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Resume-Recovery Lifecycle (Decouples DB commit from Orchestrator resume)
    resume_status: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    resume_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Audit Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')",
            name="chk_approval_requests_status",
        ),
        CheckConstraint(
            "risk_level IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')",
            name="chk_approval_requests_risk_level",
        ),
        CheckConstraint(
            "resume_status IN ('none', 'pending', 'resumed', 'failed')",
            name="chk_approval_requests_resume_status",
        ),
        Index(
            "ix_approval_tenant_status_expires",
            "tenant_id",
            "status",
            "expires_at",
        ),
    )

    def to_domain_request(self) -> ApprovalRequest:
        """
        Converts the database ORM model to the canonical domain ApprovalRequest contract.
        Ensures ISO-8601 UTC string format for requested_at.
        """
        # Ensure UTC timezone formatting
        req_dt = self.requested_at
        if req_dt.tzinfo is None:
            req_dt = req_dt.replace(tzinfo=timezone.utc)
        req_at_str = req_dt.isoformat()

        return ApprovalRequest(
            approval_id=self.approval_id,
            workflow_id=self.workflow_id,
            requested_action=self.requested_action,
            risk_level=RiskLevel(self.risk_level),
            status=ApprovalStatus(self.status),
            requested_at=req_at_str,
            details=dict(self.details) if self.details else {},
        )

    def to_domain_decision(self) -> Optional[ApprovalDecision]:
        """
        Converts recorded decision fields into canonical domain ApprovalDecision contract.
        Returns None if no decision has been recorded yet.
        """
        if not self.decision or not self.approver_id or not self.decided_at:
            return None

        dec_dt = self.decided_at
        if dec_dt.tzinfo is None:
            dec_dt = dec_dt.replace(tzinfo=timezone.utc)
        dec_at_str = dec_dt.isoformat()

        return ApprovalDecision(
            approval_id=self.approval_id,
            workflow_id=self.workflow_id,
            decision=ApprovalStatus(self.decision),
            approver_id=self.approver_id,
            reason=self.decision_reason,
            decided_at=dec_at_str,
        )

    @classmethod
    def from_domain_request(
        cls,
        request: ApprovalRequest,
        tenant_id: str,
        expires_at: datetime,
    ) -> "ApprovalRequestModel":
        """
        Factory creating an ORM model instance from domain ApprovalRequest and tenant metadata.
        """
        # Parse requested_at string into timezone-aware datetime
        try:
            req_dt = datetime.fromisoformat(request.requested_at.replace("Z", "+00:00"))
        except Exception:
            req_dt = datetime.now(timezone.utc)
        if req_dt.tzinfo is None:
            req_dt = req_dt.replace(tzinfo=timezone.utc)

        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        task_id = request.details.get("task_id") if request.details else None
        step_index = request.details.get("step_index") if request.details else None
        if step_index is not None:
            try:
                step_index = int(step_index)
            except (ValueError, TypeError):
                step_index = None

        return cls(
            approval_id=request.approval_id,
            workflow_id=request.workflow_id,
            tenant_id=tenant_id,
            task_id=task_id,
            step_index=step_index,
            requested_action=request.requested_action,
            risk_level=request.risk_level.value,
            status=request.status.value,
            requested_at=req_dt,
            expires_at=expires_at,
            details=dict(request.details) if request.details else {},
            decision=None,
            approver_id=None,
            decision_reason=None,
            decided_at=None,
            resume_status="none",
            resume_error=None,
        )
