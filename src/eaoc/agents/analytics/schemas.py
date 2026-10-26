"""
EAOC Analytics Agent - Strict Input and Output Schemas
Defines typed Pydantic contracts for analysis goals, aggregations, and quantitative outcomes.
"""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AggregationType(str, Enum):
    """Approved aggregation operations."""
    SUM = "sum"
    MEAN = "mean"
    COUNT = "count"
    MIN = "min"
    MAX = "max"
    STD = "std"


class AggregationSpec(BaseModel):
    """
    Specification for a single metric aggregation calculation.
    """
    model_config = ConfigDict(extra="forbid")

    field: str = Field(..., min_length=1, description="Target column or field name to aggregate")
    op: AggregationType = Field(default=AggregationType.SUM, description="Aggregation operation to execute")
    alias: Optional[str] = Field(default=None, description="Optional custom metric key name")
    unit: Optional[str] = Field(default=None, description="Unit of measurement (e.g. USD, ms, headcount, %)")


class MetricValue(BaseModel):
    """
    Typed metric value result with descriptive measurement units and period metadata.
    """
    model_config = ConfigDict(frozen=True)

    value: Optional[float] = Field(default=None, description="Computed numeric metric value, or None if undefined")
    unit: Optional[str] = Field(default=None, description="Unit of measurement")
    period: Optional[str] = Field(default=None, description="Analysis time period")
    group_label: Optional[str] = Field(default=None, description="Group label if computed as part of a group breakdown")


class AnalyticsInput(BaseModel):
    """
    Validated input contract for Analytics Agent invocations.
    Requires either direct tabular dataset records or a data_source reference.
    """
    model_config = ConfigDict(extra="forbid")

    analysis_goal: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Natural language description of the analysis objective",
    )
    dataset: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Direct tabular dataset as a list of row records",
    )
    data_source: Optional[str] = Field(
        default=None,
        description="Table or dataset reference to resolve via BaseDataProvider",
    )
    aggregations: List[AggregationSpec] = Field(
        default_factory=list,
        description="List of aggregation specifications to compute",
    )
    group_by: Optional[List[str]] = Field(
        default=None,
        description="Optional list of column names to group metrics by",
    )
    time_range: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional time filter range: {from_date, to_date}",
    )
    max_rows: int = Field(
        default=10000,
        ge=1,
        le=50000,
        description="Maximum permissible rows in dataset to protect memory bounds",
    )
    task_id: Optional[str] = Field(
        default=None,
        description="Task identifier for provenance and audit logging",
    )

    @model_validator(mode="after")
    def validate_dataset_source(self) -> "AnalyticsInput":
        if self.dataset is None and not self.data_source:
            raise ValueError(
                "Either 'dataset' (row records) or 'data_source' (reference name) must be provided."
            )
        return self


class AnalyticsOutput(BaseModel):
    """
    Validated output contract for Analytics Agent responses.
    Strictly enforces PII aggregation: returns only computed metrics and narrative, never raw records.
    """
    status: str = Field(
        ...,
        description="Execution status: 'completed' or 'empty_dataset'",
    )
    analysis_goal: str = Field(
        ...,
        description="Original analysis goal for traceability",
    )
    metrics: Dict[str, MetricValue] = Field(
        default_factory=dict,
        description="Dictionary mapping metric names to structured MetricValue models",
    )
    grouped_metrics: Optional[Dict[str, Dict[str, Optional[float]]]] = Field(
        default=None,
        description="Optional breakdowns grouped by target dimensions",
    )
    computation_notes: str = Field(
        ...,
        description="Explanatory notes on missing values, zero denominators, or sample counts",
    )
    narrative: Optional[str] = Field(
        default=None,
        description="Human-readable narrative summary grounded strictly in the computed metrics",
    )
    data_reference: str = Field(
        ...,
        description="Reference to dataset or data_source analyzed",
    )
    row_count: int = Field(
        default=0,
        ge=0,
        description="Number of rows analyzed in the dataset",
    )
    pii_aggregated: bool = Field(
        default=True,
        description="Affirmation that individual records are omitted to satisfy PII privacy constraints",
    )
    provenance_ref: Optional[str] = Field(
        default=None,
        description="Correlation or provenance identifier linking to execution context",
    )
