"""
EAOC Reviewer / Validation Agent Data Contracts & Schemas
Defines strict Pydantic v2 schemas for the independent quality and compliance gate:
- ReviewFindingType (COMPLETENESS, GROUNDING, CONSISTENCY, SCHEMA, POLICY, RISK)
- FindingSeverity (ERROR, WARNING, INFO)
- ReviewRecommendation (APPROVE, ESCALATE, REJECT)
- ReviewFindingItem
- RiskAssessment
- ReviewerProvenance
- ReviewerInput
- ReviewerOutput

Strictly forbids extra fields to prevent data leakage and ensure schema safety.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class ReviewFindingType(str, Enum):
    """Categorical classification of review findings."""
    COMPLETENESS = "completeness"
    GROUNDING = "grounding"
    CONSISTENCY = "consistency"
    SCHEMA = "schema"
    POLICY = "policy"
    RISK = "risk"


class FindingSeverity(str, Enum):
    """Severity level of review findings."""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ReviewRecommendation(str, Enum):
    """Quality gate recommendation for the Orchestrator."""
    APPROVE = "approve"
    ESCALATE = "escalate"
    REJECT = "reject"


class ReviewFindingItem(BaseModel):
    """Structured machine-readable finding item."""
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(..., description="Unique finding identifier")
    type: ReviewFindingType = Field(..., description="Finding category")
    severity: FindingSeverity = Field(..., description="Finding severity level")
    description: str = Field(..., description="Safe descriptive finding explanation")
    field_reference: Optional[str] = Field(default=None, description="Optional target field reference")


class RiskAssessment(BaseModel):
    """Deterministic risk classification assessment."""
    model_config = ConfigDict(extra="forbid")

    risk_level: str = Field(..., description="Assessed risk level: low, medium, or high")
    risk_factors: List[str] = Field(default_factory=list, description="Enumerated risk factors")
    matched_rule: Optional[str] = Field(default=None, description="Canonical Phase 0 rule matched")


class ReviewerProvenance(BaseModel):
    """Immutable audit provenance for AGT-REV execution."""
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(default="AGT-REV", description="Canonical logical agent ID")
    review_id: str = Field(..., description="Unique review identifier")
    task_id: str = Field(..., description="Target task identifier")
    findings_count: Dict[str, int] = Field(
        default_factory=dict,
        description="Count of findings grouped by severity: error, warning, info",
    )
    risk_level: str = Field(..., description="Assessed risk level")
    recommendation: ReviewRecommendation = Field(..., description="Review recommendation")
    reviewed_at: datetime = Field(..., description="Timezone-aware UTC timestamp")
    llm_used_for_checks: bool = Field(
        default=False, description="Flag indicating if LLM inference assisted review"
    )


class ReviewerInput(BaseModel):
    """Input payload contract for Reviewer / Validation Agent."""
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(..., description="Unique task identifier under review")
    task_description: Optional[str] = Field(default=None, description="Original task description")
    assembled_output: Dict[str, Any] = Field(
        default_factory=dict, description="Assembled workflow output under review"
    )
    required_fields: Optional[List[str]] = Field(
        default_factory=list, description="Required output field names from ExecutionPlan"
    )
    expected_schema: Optional[Dict[str, Any]] = Field(
        default=None, description="Expected schema structure or type constraints"
    )
    source_data: Optional[Any] = Field(
        default_factory=dict, description="Ground truth source data from WorkflowContext"
    )
    step_outputs: Optional[List[Dict[str, Any]]] = Field(
        default_factory=list, description="Prior agent step outputs for consistency checking"
    )
    provenance_chain: Optional[List[Dict[str, Any]]] = Field(
        default_factory=list, description="Provenance chain records from WorkflowContext"
    )
    requires_human_approval: bool = Field(
        default=False,
        description="Whether a prior workflow step required human approval",
    )
    operation_type: Optional[str] = Field(
        default=None,
        description="Operation type (e.g., write, delete, modify, deploy, read)",
    )
    data_class: Optional[str] = Field(
        default=None,
        description="Data classification (e.g., financial_write, hr_pii, restricted_document)",
    )
    agent_id: Optional[str] = Field(
        default=None, description="Agent under review (e.g., AGT-DEV, AGT-FIN, AGT-HR)"
    )
    operation: Optional[str] = Field(
        default=None, description="Operation under review (e.g., sandbox_execution, read)"
    )
    role_context: Optional[str] = Field(
        default=None, description="Caller role context for policy validation"
    )
    role: Optional[str] = Field(
        default=None,
        description="Untrusted caller-supplied role claim; strictly ignored to prevent spoofing",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Additional context metadata"
    )
    validation_rules: Optional[Dict[str, Any]] = Field(
        default=None, description="Validation rules configuration"
    )
    policy_rules: Optional[Dict[str, Any]] = Field(
        default=None, description="Policy rules configuration"
    )


class ReviewerOutput(BaseModel):
    """Output contract returned by Reviewer / Validation Agent."""
    model_config = ConfigDict(extra="forbid")

    review_id: str = Field(..., description="Unique review identifier")
    task_id: str = Field(..., description="Associated task identifier")
    passed: bool = Field(..., description="Whether validation checks passed without error")
    findings: List[ReviewFindingItem] = Field(
        default_factory=list, description="List of structured validation findings"
    )
    risk_assessment: RiskAssessment = Field(..., description="Assessed risk details")
    recommendation: ReviewRecommendation = Field(
        ..., description="Final recommendation: approve, escalate, or reject"
    )
    reviewed_at: datetime = Field(..., description="Timezone-aware UTC review timestamp")
    provenance: ReviewerProvenance = Field(..., description="Audit provenance metadata")
