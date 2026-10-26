"""
EAOC Planner Agent Contracts & Data Models
Defines typed Pydantic models for ExecutionPlan, PlanStep, PlanType,
PlannerInput, PlannerOutput, and PlanValidationError per Phase 0 specifications.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator

from eaoc.schemas.domain import AgentID, RiskLevel


class PlanType(str, Enum):
    """
    Classification of execution plan complexity (FR-PLAN-003).
    - single_agent: Simple task requiring exactly one specialized agent.
    - multi_agent: Complex task requiring multiple specialized agents in sequence or DAG.
    """
    SINGLE_AGENT = "single_agent"
    MULTI_AGENT = "multi_agent"


class PlanStep(BaseModel):
    """
    Individual step within an ExecutionPlan.
    """
    step_index: int = Field(..., ge=0, description="Zero-based sequential step index.")
    agent_id: AgentID = Field(..., description="Canonical AgentID enum member for the executing agent.")
    description: str = Field(..., description="Description of the action to be performed in this step.")
    inputs: Dict[str, Any] = Field(default_factory=dict, description="Inputs required for step execution.")
    expected_output_schema: Optional[Dict[str, Any]] = Field(
        default=None, description="Optional expected output schema or field specifications."
    )
    dependencies: List[int] = Field(
        default_factory=list, description="List of step_index values that must complete before this step."
    )
    risk_level: RiskLevel = Field(
        default=RiskLevel.LOW, description="Deterministic risk level annotated per Section 9.1 rules."
    )
    optional: bool = Field(default=False, description="Whether this step can be skipped upon failure.")
    operation_type: Optional[str] = Field(
        default=None, description="Operation category (e.g., read, write, delete, modify, deploy)."
    )
    data_class: Optional[str] = Field(
        default=None, description="Data classification label (e.g., financial_write, hr_pii, cross_domain)."
    )
    operation: Optional[str] = Field(
        default=None, description="Specific operation identifier (e.g., sandbox_execution, read)."
    )

    @field_validator("dependencies")
    @classmethod
    def validate_dependencies_non_negative(cls, deps: List[int]) -> List[int]:
        for d in deps:
            if not isinstance(d, int) or isinstance(d, bool) or d < 0:
                raise ValueError(f"Dependency index must be a non-negative integer, got {d!r}.")
        return deps


class ExecutionPlan(BaseModel):
    """
    Validated multi-step or single-step execution plan produced by the Planner (FR-PLAN-001).
    """
    plan_id: str = Field(..., description="Unique identifier for the generated execution plan.")
    task_id: str = Field(..., description="Associated workflow task identifier.")
    plan_type: PlanType = Field(..., description="single_agent or multi_agent classification.")
    steps: List[PlanStep] = Field(..., description="Ordered list of steps forming the execution DAG.")
    planning_rationale: str = Field(..., description="Deterministic reasoning explaining the decomposition.")
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="UTC timestamp of plan generation.",
    )


class PlannerInput(BaseModel):
    """
    Input payload contract for PlannerAgent.
    """
    task_id: str = Field(..., description="Required workflow task identifier.")
    task_description: str = Field(..., description="Natural language description of the requested task.")
    user_context: Optional[Dict[str, Any]] = Field(
        default=None, description="Optional user context containing roles and authorization claims."
    )
    workflow_context: Optional[Dict[str, Any]] = Field(
        default=None, description="Optional workflow state or historical context."
    )
    failed_step_index: Optional[int] = Field(
        default=None, ge=0, description="Step index that permanently failed if replanning."
    )
    failure_details: Optional[Dict[str, Any]] = Field(
        default=None, description="Error or diagnostic details from failed step if replanning."
    )
    completed_steps: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="List of previously completed steps for context preservation."
    )
    is_replan: bool = Field(default=False, description="Flag indicating this is a replanning request.")


class PlannerOutput(BaseModel):
    """
    Output payload contract returned by PlannerAgent.
    """
    plan_id: str = Field(..., description="Unique identifier for the execution plan.")
    task_id: str = Field(..., description="Associated workflow task identifier.")
    plan: Optional[ExecutionPlan] = Field(default=None, description="The validated ExecutionPlan, if successful.")
    rationale: str = Field(..., description="Planning rationale or explanation of the result.")
    is_replan: bool = Field(default=False, description="Whether this plan represents a contextual replan.")
    provenance: Dict[str, Any] = Field(default_factory=dict, description="Audit provenance metadata.")
    error: Optional[str] = Field(default=None, description="Error message if plan generation failed.")


class PlanValidationError(Exception):
    """Raised when an ExecutionPlan fails structural, topological, registry, or authorization validation."""
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
