"""
EAOC Orchestrator Agent (AGT-ORCH / Agent 1)
Authoritative implementation subclassing BaseAgent[OrchestratorInput, OrchestratorOutput].
Coordinates end-to-end task execution without performing direct domain logic.
"""

from typing import Any, Dict, Optional

from eaoc.agents.base import BaseAgent
from eaoc.agents.orchestrator.engine import OrchestratorEngine
from eaoc.agents.orchestrator.schemas import OrchestratorInput, OrchestratorOutput
from eaoc.agents.registry import AgentRegistry
from eaoc.observability import get_logger, redact_sensitive_data
from eaoc.schemas.domain import AgentExecutionContext, AgentID
from eaoc.state.interfaces import IWorkflowStateStore

logger = get_logger("eaoc.agents.orchestrator.agent")


class OrchestratorAgent(BaseAgent[OrchestratorInput, OrchestratorOutput]):
    """
    Canonical Orchestrator Agent (AGT-ORCH, Agent 1).
    Coordinates workflow lifecycle, planning, RBAC verification, deterministic risk gate,
    agent dispatch, retry backoff, re-planning, reviewer gating, and state persistence.
    """

    agent_id = AgentID.ORCHESTRATOR
    input_schema = OrchestratorInput
    output_schema = OrchestratorOutput
    default_timeout_seconds = 300.0

    def __init__(
        self,
        registry: AgentRegistry,
        state_store: Optional[IWorkflowStateStore] = None,
        max_retries: int = 2,
        initial_backoff_seconds: float = 2.0,
        backoff_multiplier: float = 2.0,
        max_replan_attempts: int = 2,
        default_timeout_seconds: Optional[float] = None,
    ):
        super().__init__(
            agent_id=AgentID.ORCHESTRATOR,
            input_schema=OrchestratorInput,
            output_schema=OrchestratorOutput,
            default_timeout_seconds=default_timeout_seconds or self.default_timeout_seconds,
        )
        self.registry = registry
        self.state_store = state_store
        self.engine = OrchestratorEngine(
            registry=registry,
            state_store=state_store,
            max_retries=max_retries,
            initial_backoff_seconds=initial_backoff_seconds,
            backoff_multiplier=backoff_multiplier,
            max_replan_attempts=max_replan_attempts,
        )

    async def _execute(
        self,
        validated_input: OrchestratorInput,
        context: AgentExecutionContext,
    ) -> OrchestratorOutput:
        """
        Executes workflow lifecycle through the OrchestratorEngine.
        Preserves secret redaction across all emitted results.
        """
        logger.info(
            f"OrchestratorAgent executing task {validated_input.task_id}",
            extra={
                "event_name": "orchestrator_execution_started",
                "task_id": validated_input.task_id,
                "execution_id": context.execution_id,
            },
        )

        output = await self.engine.execute_workflow(
            input_data=validated_input,
            context=context,
        )

        return output
