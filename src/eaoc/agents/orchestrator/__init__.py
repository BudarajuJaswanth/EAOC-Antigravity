"""
EAOC Package Module: orchestrator
Exports OrchestratorAgent, OrchestratorEngine, RiskGate, and typed data contracts.
"""

from eaoc.agents.orchestrator.agent import OrchestratorAgent
from eaoc.agents.orchestrator.engine import OrchestratorEngine
from eaoc.agents.orchestrator.risk_gate import RiskGate, RiskGateDecision
from eaoc.agents.orchestrator.schemas import (
    FinalResponse,
    OrchestratorFailureClass,
    OrchestratorInput,
    OrchestratorOutput,
    ProvenanceEntry,
    StepExecutionRecord,
)

__all__ = [
    "OrchestratorAgent",
    "OrchestratorEngine",
    "RiskGate",
    "RiskGateDecision",
    "OrchestratorInput",
    "OrchestratorOutput",
    "FinalResponse",
    "ProvenanceEntry",
    "StepExecutionRecord",
    "OrchestratorFailureClass",
]
