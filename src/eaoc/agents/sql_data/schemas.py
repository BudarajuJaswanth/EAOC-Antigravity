"""
EAOC SQL/Data Agent - Strict Input and Output Schemas
Defines typed Pydantic contracts for query requests and structured tabular outcomes.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class SqlDataInput(BaseModel):
    """
    Validated input contract for SQL/Data agent invocations.
    Strictly enforces read-only access and row limits.
    """
    model_config = ConfigDict(extra="forbid")

    query_goal: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Natural language description of data query intent",
    )
    sql_query: Optional[str] = Field(
        default=None,
        description="Explicit SQL SELECT query if formulated or supplied",
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Named bind parameters for parameterized SQL execution",
    )
    permitted_schemas: List[str] = Field(
        default_factory=lambda: ["public", "analytics"],
        description="Allowlisted database schema names authorized for this query",
    )
    read_only_flag: bool = Field(
        default=True,
        description="Strict read-only execution flag; write operations are denied",
    )
    row_limit: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Maximum rows to return, capped between 1 and 1000",
    )
    task_id: Optional[str] = Field(
        default=None,
        description="Task identifier for provenance and audit logging",
    )

    @field_validator("read_only_flag")
    @classmethod
    def validate_read_only(cls, v: bool) -> bool:
        if not v:
            raise ValueError(
                "SQL/Data Agent only permits read-only queries (read_only_flag must be True)."
            )
        return v


class SqlDataOutput(BaseModel):
    """
    Validated output contract for SQL/Data agent responses.
    Presents tabular query results with column metadata, execution time, and provenance.
    """
    status: str = Field(
        ...,
        description="Execution status: 'completed' when rows found, or 'empty_result'",
    )
    query_goal: str = Field(
        ...,
        description="Original query goal for traceability",
    )
    sql_executed: str = Field(
        ...,
        description="Sanitized SQL statement executed against the database",
    )
    columns: List[str] = Field(
        default_factory=list,
        description="Column names returned in the result set",
    )
    rows: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Row records mapping column names to values",
    )
    row_count: int = Field(
        default=0,
        ge=0,
        description="Number of rows returned (capped at row_limit)",
    )
    truncated: bool = Field(
        default=False,
        description="Flag indicating if additional rows exceeded row_limit",
    )
    execution_time_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Execution duration in milliseconds",
    )
    schemas_accessed: List[str] = Field(
        default_factory=list,
        description="List of schemas referenced and verified during query execution",
    )
    provenance_ref: Optional[str] = Field(
        default=None,
        description="Correlation or provenance identifier linking to execution context",
    )
