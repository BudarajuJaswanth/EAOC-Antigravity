"""
EAOC Workflow & Task Recovery Database Models
Defines SQLAlchemy 2.0 Declarative ORM models for durable workflow recovery state,
cryptographic plan-to-task bindings, fencing lease epochs, and authentic provenance.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from eaoc.db.base import Base
from eaoc.schemas.domain import (
    ErrorInfo,
    ExecutionMetadata,
    RequestContext,
    Task,
    TaskStatus,
    WorkflowState,
    WorkflowStatus,
)


class WorkflowModel(Base):
    """
    Authoritative PostgreSQL ORM model for workflow execution recovery and state management.
    Tracks fencing generation (fencing_epoch), per-write revisions (row_revision),
    lease ownership, accepted execution plan, and deterministic plan hash.
    """
    __tablename__ = "workflows"

    # Primary Identifier
    workflow_id: Mapped[str] = mapped_column(String(50), primary_key=True)

    # Scoping & Attribution
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    # Request Identity & Description
    task_id: Mapped[str] = mapped_column(String(50), nullable=False)
    task_description: Mapped[str] = mapped_column(Text, nullable=False)

    # Lifecycle State
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=WorkflowStatus.PENDING.value
    )
    current_step_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Durable Execution Plan & Cryptographic Hash
    execution_plan: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    plan_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Concurrency Control & Fencing
    fencing_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    row_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lock_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lock_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Intake Idempotency
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    idempotency_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Metadata & Error Details
    execution_metadata: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationship to tasks
    tasks: Mapped[list["WorkflowTaskModel"]] = relationship(
        "WorkflowTaskModel", back_populates="workflow", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'WAITING_APPROVAL', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name="chk_workflows_status",
        ),
        Index("ix_workflows_tenant_status", "tenant_id", "status"),
        Index(
            "ix_workflows_tenant_idempotency",
            "tenant_id",
            "idempotency_key",
            unique=True,
            postgresql_where=(idempotency_key.isnot(None)),
        ),
    )

    def to_domain_state(self, domain_tasks: Optional[Dict[str, Task]] = None) -> WorkflowState:
        """Converts database model to domain WorkflowState contract."""
        created_dt = self.created_at
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        updated_dt = self.updated_at
        if updated_dt.tzinfo is None:
            updated_dt = updated_dt.replace(tzinfo=timezone.utc)

        req_ctx = RequestContext(
            request_id=self.task_id,
            correlation_id=self.workflow_id,
            user_id=self.user_id,
            tenant_id=self.tenant_id,
        )

        tasks_dict = domain_tasks or {}
        if not tasks_dict and self.tasks:
            tasks_dict = {str(t.step_index): t.to_domain_task() for t in self.tasks}

        err_info = None
        if self.error:
            try:
                err_info = ErrorInfo.model_validate(self.error)
            except Exception:
                err_info = ErrorInfo(
                    code="WORKFLOW_ERROR",
                    message=str(self.error),
                    error_type="UnknownError",
                )

        exec_meta = ExecutionMetadata()
        if self.execution_metadata:
            try:
                exec_meta = ExecutionMetadata.model_validate(self.execution_metadata)
            except Exception:
                pass

        return WorkflowState(
            workflow_id=self.workflow_id,
            request_context=req_ctx,
            status=WorkflowStatus(self.status),
            current_task_id=f"step-{self.current_step_index}" if self.current_step_index is not None else None,
            tasks=tasks_dict,
            execution_metadata=exec_meta,
            plan_hash=self.plan_hash,
            execution_plan=self.execution_plan,
            error=err_info,
            created_at=created_dt.isoformat(),
            updated_at=updated_dt.isoformat(),
        )


class WorkflowTaskModel(Base):
    """
    Authoritative PostgreSQL ORM model for individual workflow tasks and step execution.
    Cryptographically binds step definitions to plan_hash and step_hash, and records authentic provenance.
    """
    __tablename__ = "workflow_tasks"

    task_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("workflows.workflow_id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)

    # Cryptographic Step & Plan Binding
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    step_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Execution Status
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TaskStatus.PENDING.value
    )

    # Data Payloads
    input_data: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    output_data: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    provenance: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    duration_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    error: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    workflow: Mapped["WorkflowModel"] = relationship("WorkflowModel", back_populates="tasks")

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'BLOCKED')",
            name="chk_workflow_tasks_status",
        ),
        UniqueConstraint("workflow_id", "step_index", name="uq_workflow_step_index"),
        Index("ix_workflow_tasks_lookup", "tenant_id", "workflow_id", "step_index"),
    )

    def to_domain_task(self) -> Task:
        """Converts database model to domain Task contract."""
        err_info = None
        if self.error:
            try:
                err_info = ErrorInfo.model_validate(self.error)
            except Exception:
                err_info = ErrorInfo(
                    code="TASK_ERROR",
                    message=str(self.error),
                    error_type="TaskExecutionError",
                )

        from eaoc.schemas.domain import AgentID

        return Task(
            task_id=self.task_id,
            workflow_id=self.workflow_id,
            name=self.name,
            assigned_agent_id=AgentID(self.agent_id),
            status=TaskStatus(self.status),
            dependencies=[],
            input_data=self.input_data or {},
            output_data=self.output_data or {},
            error=err_info,
        )
