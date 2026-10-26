"""
EAOC Package Module: agents
Provides the shared base agent contract and implementations for the 12 approved logical agents.
"""

from eaoc.agents.analytics import AnalyticsAgent
from eaoc.agents.base import BaseAgent
from eaoc.agents.developer import DeveloperAgent
from eaoc.agents.document import DocumentAgent
from eaoc.agents.finance import FinanceAgent
from eaoc.agents.hr import HrAgent
from eaoc.agents.it_support import ItSupportAgent
from eaoc.agents.orchestrator import OrchestratorAgent
from eaoc.agents.planner import PlannerAgent
from eaoc.agents.registry import AgentRegistry
from eaoc.agents.research_rag import ResearchRagAgent
from eaoc.agents.reviewer_validation import ReviewerValidationAgent
from eaoc.agents.security_policy import SecurityPolicyAgent
from eaoc.agents.sql_data import SqlDataAgent

__all__ = [
    "BaseAgent",
    "AgentRegistry",
    "OrchestratorAgent",
    "PlannerAgent",
    "ResearchRagAgent",
    "SqlDataAgent",
    "DocumentAgent",
    "AnalyticsAgent",
    "FinanceAgent",
    "HrAgent",
    "ItSupportAgent",
    "DeveloperAgent",
    "ReviewerValidationAgent",
    "SecurityPolicyAgent",
]
