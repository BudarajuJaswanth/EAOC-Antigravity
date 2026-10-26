"""
EAOC Orchestrator Agent Contracts & Schemas
Defines typed Pydantic models for OrchestratorInput, OrchestratorOutput,
StepExecutionRecord, ProvenanceEntry, FinalResponse, and OrchestratorFailureClass
per Phase 0 specifications (AgentSpec Part A, Arch Part A, Arch Part C).
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from eaoc.agents.planner.schemas import ExecutionPlan
from eaoc.schemas.domain import (
    AgentID,
    ApprovalDecision,
    ApprovalRequest,
    ErrorInfo,
    RiskLevel,
    TaskStatus,
    WorkflowStatus,
)


class OrchestratorFailureClass(str, Enum):
    """
    Authoritative failure classifications from Arch Part C Section 12.1.
    """
    LLM_FAILURE = "llm_failure"
    TOOL_ERROR = "tool_error"
    DATABASE_UNAVAILABLE = "database_unavailable"
    EMBEDDING_UNAVAILABLE = "embedding_unavailable"
    VECTOR_STORE_UNAVAILABLE = "vector_store_unavailable"
    SCHEMA_VIOLATION = "schema_violation"
    PERMISSION_DENIED = "permission_denied"
    POLICY_VIOLATION = "policy_violation"
    PLAN_VALIDATION_FAILED = "plan_validation_failed"
    DOCUMENT_NOT_FOUND = "document_not_found"
    APPROVAL_TIMEOUT = "approval_timeout"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    HUMAN_APPROVAL_REJECTED = "human_approval_rejected"
    REVIEWER_REJECTED = "reviewer_rejected"
    UNKNOWN_FAILURE = "unknown_failure"


class ProvenanceEntry(BaseModel):
    """
    Minimum metadata per executed step stage per Arch Part C Section 11.2 and AgentSpec Part A Section P.
    """
    step_index: int = Field(..., description="Zero-based index of the executed step.")
    agent_id: str = Field(..., description="Canonical agent ID that executed the step.")
    tool_used: Optional[str] = Field(default=None, description="Name or ID of tool invoked during step.")
    data_source_accessed: Optional[str] = Field(default=None, description="Data schema, namespace, or store accessed.")
    action_summary: str = Field(..., description="Concise summary of action performed.")
    result_reference: Optional[str] = Field(default=None, description="Pointer or summary hash of step result.")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 UTC timestamp of step completion.",
    )
    status: str = Field(..., description="Terminal status of step execution (e.g. COMPLETED, FAILED, SKIPPED).")


class StepExecutionRecord(BaseModel):
    """
    Internal execution tracking record for an individual plan step.
    """
    step_index: int
    agent_id: AgentID
    status: TaskStatus = TaskStatus.PENDING
    input_data: Dict[str, Any] = Field(default_factory=dict)
    output_data: Dict[str, Any] = Field(default_factory=dict)
    duration_ms: Optional[float] = None
    retry_count: int = 0
    error: Optional[ErrorInfo] = None


class FinalResponse(BaseModel):
    """
    Assembled final workflow response presented to caller upon completion (Arch Part A Section 2.1).
    """
    task_id: str
    status: WorkflowStatus
    result: Dict[str, Any] = Field(default_factory=dict)
    provenance_summary: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    partial_result: bool = False
    api_version: str = "v1"


class OrchestratorInput(BaseModel):
    """
    Input payload contract for OrchestratorAgent.
    """
    task_id: str = Field(..., description="Unique task identifier.")
    task_description: str = Field(..., description="User task description or prompt.")
    execution_plan: Optional[ExecutionPlan] = Field(
        default=None, description="Optional pre-computed ExecutionPlan; if omitted, Planner is invoked."
    )
    user_context: Optional[Dict[str, Any]] = Field(
        default=None, description="Requesting user context containing roles and permissions."
    )
    approval_decision: Optional[ApprovalDecision] = Field(
        default=None, description="Decision record to resume a workflow paused at an approval gate."
    )
    workflow_id: Optional[str] = Field(
        default=None, description="Workflow ID; if omitted, generated deterministically or from task_id."
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Operational tracing and execution metadata."
    )


class OrchestratorOutput(BaseModel):
    """
    Output payload contract returned by OrchestratorAgent.
    """
    task_id: str
    workflow_id: str
    status: WorkflowStatus
    final_response: Optional[FinalResponse] = None
    provenance_chain: List[ProvenanceEntry] = Field(default_factory=list)
    approval_request: Optional[ApprovalRequest] = None
    replan_count: int = 0
    error: Optional[ErrorInfo] = None
