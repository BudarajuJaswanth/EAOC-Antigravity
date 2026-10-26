"""
EAOC Security / Policy Agent Data Contracts & Schemas
Defines strict Pydantic v2 schemas for runtime policy enforcement,
agent permission manifests, guardrail checks, and audit provenance:
- PolicyCheckOperation
- PolicyViolationType
- PolicyViolation
- AgentPermissionManifest
- SecurityPolicyProvenance
- SecurityPolicyInput
- SecurityPolicyOutput

Strictly forbids extra fields to prevent data leakage and ensure schema safety.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class PolicyCheckOperation(str, Enum):
    """Permitted Security/Policy Agent evaluation operations."""
    CHECK_TOOL_PERMISSION = "check_tool_permission"
    CHECK_SCHEMA_ACCESS = "check_schema_access"
    CHECK_INPUT_GUARDRAIL = "check_input_guardrail"
    CHECK_OUTPUT_GUARDRAIL = "check_output_guardrail"
    CHECK_MANIFEST = "check_manifest"
    EVALUATE_ACTION = "evaluate_action"


class PolicyViolationType(str, Enum):
    """Categorical classification of security policy violations."""
    TOOL_NOT_PERMITTED = "tool_not_permitted"
    SCHEMA_ACCESS_DENIED = "schema_access_denied"
    PROMPT_INJECTION_DETECTED = "prompt_injection_detected"
    DATA_POLICY_VIOLATION = "data_policy_violation"
    WRITE_NOT_PERMITTED = "write_not_permitted"
    SENSITIVITY_EXCEEDED = "sensitivity_exceeded"
    MANIFEST_NOT_FOUND = "manifest_not_found"
    POLICY_ENGINE_ERROR = "policy_engine_error"


class PolicyViolation(BaseModel):
    """Machine-readable policy violation record."""
    model_config = ConfigDict(extra="forbid")

    policy_id: str = Field(..., description="Canonical policy rule identifier (e.g. POL-TOOL-001)")
    policy_name: str = Field(..., description="Human-readable policy name")
    violation_type: PolicyViolationType = Field(..., description="Specific violation classification")
    description: str = Field(..., description="Safe descriptive explanation of the violation")


class AgentPermissionManifest(BaseModel):
    """
    Machine-readable permission manifest defining runtime capabilities
    and boundary limits for a canonical EAOC logical agent (FR-PERM-001).
    """
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(..., description="Canonical or logical agent identifier (e.g. AGT-HR)")
    permitted_tools: List[str] = Field(
        default_factory=list, description="List of tool_ids permitted for this agent"
    )
    permitted_delegation_targets: List[str] = Field(
        default_factory=list, description="List of logical agent IDs this agent may delegate to"
    )
    permitted_schemas: List[str] = Field(
        default_factory=list, description="Allowlisted PostgreSQL database schemas"
    )
    permitted_namespaces: List[str] = Field(
        default_factory=list, description="Allowlisted vector or document store namespaces"
    )
    max_data_sensitivity: str = Field(
        default="internal", description="Maximum allowable data sensitivity level (public, internal, confidential, restricted)"
    )
    write_permitted: bool = Field(
        default=False, description="Whether database or system write operations are permitted"
    )


class SecurityPolicyProvenance(BaseModel):
    """Audit provenance tracking for AGT-SEC policy decisions."""
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(default="AGT-SEC", description="Logical agent ID for Security/Policy Agent")
    check_id: str = Field(..., description="Unique policy evaluation check identifier")
    action_evaluated: str = Field(..., description="Action or operation evaluated")
    agent_id_evaluated: Optional[str] = Field(default=None, description="Agent ID evaluated")
    tool_id_evaluated: Optional[str] = Field(default=None, description="Tool ID evaluated if applicable")
    allowed: bool = Field(..., description="Final policy check decision")
    violation_count: int = Field(default=0, description="Number of detected violations")
    policy_references_applied: List[str] = Field(
        default_factory=list, description="Applicable Phase 0 policy requirements referenced"
    )
    checked_at: datetime = Field(..., description="Timezone-aware UTC evaluation timestamp")


class SecurityPolicyInput(BaseModel):
    """Input payload contract for Security / Policy Agent evaluation."""
    model_config = ConfigDict(extra="forbid")

    check_id: Optional[str] = Field(default=None, description="Optional caller correlation check ID")
    operation: PolicyCheckOperation = Field(
        default=PolicyCheckOperation.CHECK_TOOL_PERMISSION,
        description="Policy check operation to perform",
    )
    agent_id: Optional[str] = Field(
        default=None, description="Target agent being evaluated (e.g. AGT-HR or hr)"
    )
    tool_id: Optional[str] = Field(
        default=None, description="Target tool ID being checked (e.g. sql_query_tool)"
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict, description="Tool invocation parameters to inspect"
    )
    schema_name: Optional[str] = Field(
        default=None, description="Database schema name requested for access"
    )
    input_content: Optional[str] = Field(
        default=None, description="Task prompt or query input content to check for prompt injection"
    )
    output_content: Optional[Dict[str, Any]] = Field(
        default=None, description="Agent output dictionary to check for PII or domain leakage"
    )
    role_context: Optional[str] = Field(
        default=None, description="Caller role context for output domain verification"
    )
    data_class: Optional[str] = Field(
        default=None, description="Data class classification for risk evaluation"
    )
    operation_type: Optional[str] = Field(
        default=None, description="Operation type (e.g. write, delete, modify, deploy, read)"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Additional context metadata"
    )
    role: Optional[str] = Field(
        default=None,
        description="Untrusted caller-supplied role claim; strictly ignored to prevent spoofing",
    )


class SecurityPolicyOutput(BaseModel):
    """Output contract returned by Security / Policy Agent."""
    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(..., description="Unique policy evaluation check identifier")
    allowed: bool = Field(..., description="Final policy authorization decision: True if allowed, False if blocked")
    violations: List[PolicyViolation] = Field(
        default_factory=list, description="Enumerated policy violations if blocked"
    )
    policy_references: List[str] = Field(
        default_factory=list, description="List of policy rule IDs applied"
    )
    risk_level: str = Field(default="low", description="Assessed risk level: low, medium, or high")
    checked_at: datetime = Field(..., description="Timezone-aware UTC timestamp")
    manifest: Optional[AgentPermissionManifest] = Field(
        default=None, description="Agent permission manifest if check_manifest operation was requested"
    )
    provenance: SecurityPolicyProvenance = Field(..., description="Audit provenance metadata")
