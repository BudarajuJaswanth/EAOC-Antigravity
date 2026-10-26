"""
EAOC Finance Agent Data Schemas
Defines strict Pydantic models for finance queries, entity records, budget variance,
monetary totals, provenance, and structured outputs with Decimal precision validation.
"""

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


class FinanceEntityType(str, Enum):
    """Supported finance entity query types per Phase 0 specification."""
    INVOICE = "invoice"
    BUDGET = "budget"
    EXPENSE = "expense"
    REPORT = "report"


# ISO 4217 3-letter currency code regex
CURRENCY_REGEX = re.compile(r"^[A-Z]{3}$")


class VarianceSummary(BaseModel):
    """
    Structured budget variance analysis using Decimal arithmetic.
    
    Semantics:
    - variance_amount = allocated - actual
    - favorable: In expense budgets, favorable=True indicates expenditure was at or
      below allocated amount (allocated >= actual). Favorable/unfavorable semantics apply
      strictly to expense budgets and must not be assumed for revenue accounts.
    - percentage_status: 'computed' or 'undefined_zero_allocation' when allocated is 0.
    """
    currency: str = Field(..., description="ISO 4217 currency code")
    allocated: Decimal = Field(..., description="Allocated budget amount")
    actual: Decimal = Field(..., description="Actual expenditure amount")
    variance_amount: Decimal = Field(
        ...,
        description="Budget variance (allocated - actual). Positive indicates remaining budget.",
    )
    variance_percentage: Optional[Decimal] = Field(
        default=None,
        description="Percentage variance ((allocated - actual) / allocated * 100), None if allocated == 0.",
    )
    percentage_status: str = Field(
        default="computed",
        description="'computed' when allocated is nonzero; 'undefined_zero_allocation' when allocated == 0.",
    )
    favorable: bool = Field(
        ...,
        description="True if actual <= allocated (under or on budget for expense accounts).",
    )
    notes: Optional[str] = None


class FinanceTotals(BaseModel):
    """Aggregated financial totals with currency and record count."""
    currency: str = Field(..., description="ISO 4217 currency code")
    total_amount: Decimal = Field(..., description="Total aggregated monetary amount")
    record_count: int = Field(..., ge=0, description="Number of aggregated records")
    average_amount: Optional[Decimal] = Field(
        default=None, description="Average amount per record"
    )


class FinanceProvenance(BaseModel):
    """
    Mandatory provenance block attached to every finance response.
    Complies with Phase 0 auditability and Reviewer validation requirements.
    """
    agent_id: str = Field(
        default="AGT-FIN", description="Canonical Agent identifier in Phase 0 specification"
    )
    entity_type: str = Field(..., description="Finance entity type queried")
    filters_applied: Dict[str, Any] = Field(
        default_factory=dict, description="Query filters applied during execution"
    )
    data_source: str = Field(
        ..., description="Origin of financial records (e.g., 'in_memory_provider', 'finance_schema')"
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timezone-aware UTC timestamp of execution",
    )
    sql_agent_ref: Optional[str] = Field(
        default=None, description="Correlation reference if delegated to SQL/Data Agent"
    )
    document_agent_ref: Optional[str] = Field(
        default=None, description="Correlation reference if delegated to Document Agent"
    )
    notes: Optional[str] = None


class FinanceInput(BaseModel):
    """
    Strict input validation contract for the Finance Agent.
    
    Security note:
    Caller-supplied fields CANNOT specify authorization roles. The user's role
    must be resolved exclusively from the trusted AgentExecutionContext.
    """
    entity_type: FinanceEntityType = Field(
        ..., description="Finance entity type: 'invoice', 'budget', 'expense', or 'report'."
    )
    finance_query: str = Field(
        ..., min_length=3, max_length=1000, description="Natural language finance question or goal"
    )
    filters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Filter parameters (e.g. invoice_id, vendor, department, date_range, period, status)",
    )
    currency: Optional[str] = Field(
        default=None,
        description="Expected ISO 4217 currency code (e.g. 'USD', 'EUR'). Validated against data.",
    )
    records: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Optional pre-fetched in-memory financial records (max 5,000 records).",
    )
    max_records: int = Field(
        default=1000,
        ge=1,
        le=5000,
        description="Maximum records permitted in calculation or retrieval.",
    )

    @field_validator("currency")
    @classmethod
    def validate_currency_code(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v_clean = v.strip().upper()
            if not CURRENCY_REGEX.match(v_clean):
                raise ValueError(
                    f"Invalid currency code '{v}'. Must be a 3-letter uppercase ISO 4217 code (e.g., 'USD', 'EUR')."
                )
            return v_clean
        return None

    @field_validator("records")
    @classmethod
    def validate_records_bound(
        cls, v: Optional[List[Dict[str, Any]]]
    ) -> Optional[List[Dict[str, Any]]]:
        if v is not None and len(v) > 5000:
            raise ValueError(
                f"Input records count ({len(v)}) exceeds maximum allowed limit of 5,000 records."
            )
        return v


class FinanceOutput(BaseModel):
    """
    Strict output validation contract for the Finance Agent.
    Guarantees structured records, Decimal financial calculations, mandatory provenance,
    and separation of LLM narrative from immutable figures.
    """
    status: str = Field(
        ...,
        description="'completed', 'empty_result', or 'no_matching_records'",
    )
    entity_type: FinanceEntityType = Field(..., description="Queried entity type")
    finance_query: str = Field(..., description="Original finance query")
    records: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Formatted finance records matching query filters",
    )
    record_count: int = Field(..., ge=0, description="Total matching records returned")
    currency: Optional[str] = Field(
        default=None, description="ISO 4217 currency code of the result set"
    )
    summary: Optional[str] = Field(
        default=None, description="Deterministic financial summary text"
    )
    totals: Optional[FinanceTotals] = Field(
        default=None, description="Aggregated totals for invoice/expense queries"
    )
    variance: Optional[VarianceSummary] = Field(
        default=None, description="Computed budget variance for budget queries"
    )
    narrative: Optional[str] = Field(
        default=None,
        description="Optional LLM-generated narrative interpretation, strictly separated from figures.",
    )
    provenance: FinanceProvenance = Field(
        ..., description="Mandatory provenance block required by Reviewer Agent"
    )
