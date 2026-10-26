"""
EAOC HR Agent Data Schemas
Defines strict Pydantic models for HR queries, aggregated headcount, org structure,
employee profiles, PII minimization flags, and mandatory provenance.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


class HrQueryType(str, Enum):
    """Supported HR query operations per Phase 0 specification."""
    HEADCOUNT = "headcount"
    ORG_CHART = "org_chart"
    EMPLOYEE_INFO = "employee_info"
    POLICY = "policy"


class HeadcountGroup(BaseModel):
    """Aggregated group count for headcount queries."""
    label: str = Field(..., description="Group label (e.g. department or location name)")
    count: int = Field(..., ge=0, description="Employee count in group")


class HeadcountResult(BaseModel):
    """Aggregated headcount query result without individual employee records."""
    group_by: str = Field(..., description="Grouping dimension ('department', 'location', 'employment_type')")
    groups: List[HeadcountGroup] = Field(default_factory=list, description="Count per category")
    total: int = Field(..., ge=0, description="Total headcount across all groups")
    as_of_date: str = Field(..., description="As-of date for the headcount snapshot")


class HrProvenance(BaseModel):
    """
    Mandatory provenance block attached to every HR response.
    Complies with Phase 0 auditability and Reviewer validation requirements.
    """
    agent_id: str = Field(default="AGT-HR", description="Canonical Agent identifier in Phase 0 specification")
    query_type: str = Field(..., description="HR query type executed")
    department_filter: Optional[str] = Field(default=None, description="Department filter applied, if any")
    pii_redacted: bool = Field(..., description="True if any sensitive PII fields were redacted or minimized")
    data_source: str = Field(..., description="Origin of HR records (e.g. 'in_memory_provider', 'hr_schema')")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timezone-aware UTC timestamp of execution",
    )
    sql_agent_ref: Optional[str] = Field(default=None, description="Correlation reference if delegated to SQL/Data Agent")
    rag_agent_ref: Optional[str] = Field(default=None, description="Correlation reference if delegated to Research/RAG Agent")
    notes: Optional[str] = None


class HrInput(BaseModel):
    """
    Strict input validation contract for the HR Agent.
    
    Security note:
    Caller-supplied fields CANNOT specify authorization roles or PII permissions.
    Authorization and PII claims must be resolved exclusively from trusted execution context.
    """
    query_type: HrQueryType = Field(
        ..., description="HR query type: 'headcount', 'org_chart', 'employee_info', or 'policy'."
    )
    hr_query: str = Field(
        ..., min_length=3, max_length=1000, description="Natural language HR question or goal"
    )
    department: Optional[str] = Field(default=None, max_length=100, description="Optional department filter")
    group_by: Optional[str] = Field(
        default=None,
        description="Grouping field for headcount queries ('department', 'location', 'employment_type')",
    )
    filters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Filter parameters (e.g. employee_id, name, location, status)",
    )
    records: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Optional pre-fetched in-memory HR records for isolated test fixtures (max 2,000 records).",
    )
    max_records: int = Field(
        default=500,
        ge=1,
        le=2000,
        description="Maximum records permitted in calculation or retrieval.",
    )

    @field_validator("group_by")
    @classmethod
    def validate_group_by(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            clean = v.strip().lower()
            allowed = {"department", "location", "employment_type"}
            if clean not in allowed:
                raise ValueError(
                    f"Invalid group_by '{v}'. Headcount grouping must be one of: {sorted(allowed)}."
                )
            return clean
        return None

    @field_validator("records")
    @classmethod
    def validate_records_bound(
        cls, v: Optional[List[Dict[str, Any]]]
    ) -> Optional[List[Dict[str, Any]]]:
        if v is not None and len(v) > 2000:
            raise ValueError(
                f"Input records count ({len(v)}) exceeds maximum allowed limit of 2,000 records."
            )
        return v


class HrOutput(BaseModel):
    """
    Strict output validation contract for the HR Agent.
    Guarantees structured results, accurate PII redaction tracking, and mandatory provenance.
    """
    status: str = Field(..., description="'completed' or 'empty_result'")
    query_type: HrQueryType = Field(..., description="Executed query type")
    hr_query: str = Field(..., description="Original HR query")
    result: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured result payload (headcount summary, org chart hierarchy, employee profile, or policy)",
    )
    pii_redacted: bool = Field(
        ..., description="True if any sensitive PII fields were redacted or excluded"
    )
    record_count: int = Field(..., ge=0, description="Total matching records retrieved or aggregated")
    summary: Optional[str] = Field(default=None, description="Deterministic summary text")
    narrative: Optional[str] = Field(
        default=None, description="Optional LLM-generated narrative interpretation"
    )
    provenance: HrProvenance = Field(
        ..., description="Mandatory provenance block required by Reviewer Agent"
    )
