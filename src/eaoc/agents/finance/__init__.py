"""
EAOC Package Module: finance
Provides the Finance specialized agent, monetary calculation engine, data provider boundary, and typed schemas.
"""

from eaoc.agents.finance.agent import FinanceAgent
from eaoc.agents.finance.calculator import FinanceCalculator
from eaoc.agents.finance.data_provider import (
    BaseFinanceDataProvider,
    InMemoryFinanceDataProvider,
    SqlFinanceDataProvider,
)
from eaoc.agents.finance.schemas import (
    FinanceEntityType,
    FinanceInput,
    FinanceOutput,
    FinanceProvenance,
    FinanceTotals,
    VarianceSummary,
)

__all__ = [
    "FinanceAgent",
    "BaseFinanceDataProvider",
    "InMemoryFinanceDataProvider",
    "SqlFinanceDataProvider",
    "FinanceCalculator",
    "FinanceEntityType",
    "FinanceInput",
    "FinanceOutput",
    "FinanceProvenance",
    "FinanceTotals",
    "VarianceSummary",
]
