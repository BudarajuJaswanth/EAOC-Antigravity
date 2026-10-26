"""
EAOC Package Module: analytics
Provides the Analytics specialized agent, calculation engine, data provider boundary, and typed schemas.
"""

from eaoc.agents.analytics.agent import AnalyticsAgent
from eaoc.agents.analytics.calculator import AnalyticsCalculator
from eaoc.agents.analytics.data_provider import (
    BaseDataProvider,
    InMemoryDataProvider,
)
from eaoc.agents.analytics.schemas import (
    AggregationSpec,
    AggregationType,
    AnalyticsInput,
    AnalyticsOutput,
    MetricValue,
)

__all__ = [
    "AnalyticsAgent",
    "BaseDataProvider",
    "InMemoryDataProvider",
    "AnalyticsCalculator",
    "AggregationType",
    "AggregationSpec",
    "MetricValue",
    "AnalyticsInput",
    "AnalyticsOutput",
]
