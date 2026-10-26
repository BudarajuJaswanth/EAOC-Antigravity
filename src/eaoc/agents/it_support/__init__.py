"""
EAOC Package Module: it_support
Provides the IT/Support specialized agent, infrastructure redactor, data provider boundaries, and typed schemas.
"""

from eaoc.agents.it_support.agent import ItSupportAgent
from eaoc.agents.it_support.data_provider import (
    BaseItDataProvider,
    InMemoryItDataProvider,
    SqlItDataProvider,
)
from eaoc.agents.it_support.infra_redactor import ItInfraRedactor
from eaoc.agents.it_support.schemas import (
    ItProvenance,
    ItQueryType,
    ItResolutionStep,
    ItSupportInput,
    ItSupportOutput,
    ItTicketFilter,
    TicketPriority,
    TicketStatus,
)

__all__ = [
    "ItSupportAgent",
    "BaseItDataProvider",
    "InMemoryItDataProvider",
    "SqlItDataProvider",
    "ItInfraRedactor",
    "ItQueryType",
    "TicketPriority",
    "TicketStatus",
    "ItTicketFilter",
    "ItResolutionStep",
    "ItProvenance",
    "ItSupportInput",
    "ItSupportOutput",
]
