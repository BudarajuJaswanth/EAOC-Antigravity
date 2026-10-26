"""
EAOC Package Module: hr
Provides the HR specialized agent, PII minimization engine, data provider boundary, and typed schemas.
"""

from eaoc.agents.hr.agent import HrAgent
from eaoc.agents.hr.data_provider import (
    BaseHrDataProvider,
    InMemoryHrDataProvider,
    SqlHrDataProvider,
)
from eaoc.agents.hr.pii_redactor import HrPiiRedactor
from eaoc.agents.hr.schemas import (
    HeadcountGroup,
    HeadcountResult,
    HrInput,
    HrOutput,
    HrProvenance,
    HrQueryType,
)

__all__ = [
    "HrAgent",
    "BaseHrDataProvider",
    "InMemoryHrDataProvider",
    "SqlHrDataProvider",
    "HrPiiRedactor",
    "HrQueryType",
    "HeadcountGroup",
    "HeadcountResult",
    "HrInput",
    "HrOutput",
    "HrProvenance",
]
