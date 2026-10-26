"""
EAOC Package Module: planner
Implements the Planner Agent (AGT-PLAN), ExecutionPlan contracts,
plan validation engine, and deterministic task decomposition per Phase 0 specifications.
"""

from eaoc.agents.planner.agent import PlannerAgent
from eaoc.agents.planner.engine import PlannerEngine
from eaoc.agents.planner.schemas import (
    ExecutionPlan,
    PlanStep,
    PlanType,
    PlannerInput,
    PlannerOutput,
    PlanValidationError,
)
from eaoc.agents.planner.validator import PlanValidator, ROLE_PERMITTED_AGENTS

__all__ = [
    "PlannerAgent",
    "PlannerEngine",
    "PlanValidator",
    "ExecutionPlan",
    "PlanStep",
    "PlanType",
    "PlannerInput",
    "PlannerOutput",
    "PlanValidationError",
    "ROLE_PERMITTED_AGENTS",
]
