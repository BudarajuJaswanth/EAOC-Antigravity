"""
EAOC Core Shared Domain Models & Data Contracts
Defines platform-level Pydantic data structures, lifecycle enums, and exact 12-agent registry.
"""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class AgentID(str, Enum):
    """
    Registry of approved logical agents in EAOC.
    Contains ONLY the 12 approved logical agents.
    """
    RESEARCH_RAG = "research_rag"
    SQL_DATA = "sql_data"
    DOCUMENT = "document"
    ANALYTICS = "analytics"
    FINANCE = "finance"
    HR = "hr"
    IT_SUPPORT = "it_support"
    DEVELOPER = "developer"
    REVIEWER_VALIDATION = "reviewer_validation"
    SECURITY_POLICY = "security_policy"
    ORCHESTRATOR = "orchestrator"
    PLANNER = "planner"


class WorkflowStatus(str, Enum):
    """Lifecycle status for a workflow execution."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskStatus(str, Enum):
    """Lifecycle status for individual workflow tasks."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class RiskLevel(str, Enum):
    """Risk severity classification for workflow actions."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ApprovalStatus(str, Enum):
    """Status for human-in-the-loop approval workflows."""
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class RequestContext(BaseModel):
    """Platform request context containing tracing identifiers and optional user metadata."""
    request_id: str = Field(..., description="Required primary request identifier")
    correlation_id: str = Field(..., description="Required transaction correlation identifier")
    user_id: Optional[str] = Field(default=None, description="Optional authenticated user identifier")
    tenant_id: Optional[str] = Field(default=None, description="Optional multi-tenant organization identifier")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Platform request metadata")


class ErrorInfo(BaseModel):
    """Structured, safe platform error details."""
    code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Safe human-readable error message")
    error_type: str = Field(..., description="Exception or error classification type")
    request_id: Optional[str] = None
    correlation_id: Optional[str] = None
    retryable: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)


class Task(BaseModel):
    """Represents a discrete task unit within a workflow plan."""
    task_id: str
    workflow_id: str
    name: str
    description: Optional[str] = None
    assigned_agent_id: AgentID
    status: TaskStatus = TaskStatus.PENDING
    dependencies: List[str] = Field(default_factory=list)
    input_data: Dict[str, Any] = Field(default_factory=dict)
    output_data: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[ErrorInfo] = None


class AgentExecutionContext(BaseModel):
    """Input execution context provided to a logical agent."""
    execution_id: str
    workflow_id: str
    task_id: str
    agent_id: AgentID
    request_id: str
    correlation_id: str
    input_payload: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentExecutionResult(BaseModel):
    """Execution output returned by a logical agent."""
    execution_id: str
    agent_id: AgentID
    status: TaskStatus
    output_payload: Dict[str, Any] = Field(default_factory=dict)
    duration_ms: Optional[float] = None
    error: Optional[ErrorInfo] = None


class ToolExecutionContext(BaseModel):
    """Execution context for invoking a platform tool."""
    execution_id: str
    tool_id: str
    workflow_id: str
    task_id: str
    agent_id: AgentID
    parameters: Dict[str, Any] = Field(default_factory=dict)


class ToolExecutionResult(BaseModel):
    """Result payload from a tool invocation."""
    execution_id: str
    tool_id: str
    status: TaskStatus
    output_data: Dict[str, Any] = Field(default_factory=dict)
    duration_ms: Optional[float] = None
    error: Optional[ErrorInfo] = None


class ValidationResult(BaseModel):
    """Validation finding returned by Reviewer/Validation agent."""
    is_valid: bool
    score: Optional[float] = Field(default=None, description="Optional raw score; scale/interpretation is undefined in Part 1.7")
    findings: List[str] = Field(default_factory=list)
    severity: Optional[str] = None
    reason: Optional[str] = None


class ApprovalRequest(BaseModel):
    """Human-in-the-loop approval request contract."""
    approval_id: str
    workflow_id: str
    requested_action: str
    risk_level: RiskLevel
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: str
    details: Dict[str, Any] = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    """Decision record for a human-in-the-loop approval request."""
    approval_id: str
    workflow_id: str
    decision: ApprovalStatus = Field(..., description="Strongly-typed decision status (APPROVED or REJECTED)")
    approver_id: str
    reason: Optional[str] = None
    decided_at: str


class ExecutionMetadata(BaseModel):
    """Operational timing and performance metadata for a workflow."""
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    total_duration_ms: Optional[float] = None
    tokens_used: Optional[int] = None
    # NOTE: Agent Invocation Count (AIC) is intentionally omitted to preserve research metric boundary.


class WorkflowState(BaseModel):
    """Canonical representation of an active or completed workflow state."""
    workflow_id: str
    request_context: RequestContext
    status: WorkflowStatus = WorkflowStatus.PENDING
    current_task_id: Optional[str] = None
    tasks: Dict[str, Task] = Field(default_factory=dict)
    execution_metadata: ExecutionMetadata = Field(default_factory=ExecutionMetadata)
    validation_result: Optional[ValidationResult] = None
    approval_request: Optional[ApprovalRequest] = None
    approval_decision: Optional[ApprovalDecision] = None
    error: Optional[ErrorInfo] = None
    plan_hash: Optional[str] = Field(default=None, description="SHA-256 hash of canonical accepted execution plan")
    execution_plan: Optional[Dict[str, Any]] = Field(default=None, description="Canonical accepted execution plan dictionary")
    created_at: str
    updated_at: str
