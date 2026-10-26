"""
EAOC Enterprise Agent Registry & Capability Discovery
Provides an instantiable, dependency-injectable registry for managing BaseAgent instances,
discovering logical agent capabilities, and safely dispatching execution contexts.
"""

from typing import Dict, List, Optional, Union

from eaoc.agents.base import BaseAgent
from eaoc.observability import get_logger
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    ErrorInfo,
    TaskStatus,
)

logger = get_logger("eaoc.agents.registry")


class AgentRegistry:
    """
    Instantiable, dependency-injectable in-memory registry for EAOC logical agents.
    Provides deterministic lookup, capability discovery, and execution dispatch.
    """

    def __init__(self):
        self._agents: Dict[AgentID, BaseAgent] = {}

    def register_agent(self, agent: BaseAgent) -> None:
        """
        Registers a BaseAgent instance into the registry.
        Accepts only BaseAgent instances.
        Raises TypeError if agent is not an instance of BaseAgent.
        Raises ValueError if an agent with the same AgentID is already registered.
        Does not silently replace existing registrations.
        """
        if not isinstance(agent, BaseAgent):
            raise TypeError(
                f"Expected instance of BaseAgent, got {type(agent).__name__}."
            )

        agent_id = agent.agent_id
        if agent_id in self._agents:
            raise ValueError(
                f"Agent with ID '{agent_id.value}' is already registered."
            )

        self._agents[agent_id] = agent
        logger.info(
            f"Registered agent '{agent_id.value}' in registry.",
            extra={"event_name": "agent_registered", "agent_id": agent_id.value},
        )

    def get_agent(self, agent_id: Union[AgentID, str]) -> Optional[BaseAgent]:
        """
        Retrieves a registered BaseAgent by AgentID (or string value), or returns None if absent.
        """
        target_id = self._resolve_agent_id(agent_id)
        if target_id is None:
            return None
        return self._agents.get(target_id)

    def list_agents(self) -> List[BaseAgent]:
        """
        Returns a list of all currently registered BaseAgent instances.
        """
        return list(self._agents.values())

    def list_agent_ids(self) -> List[AgentID]:
        """
        Returns a list of all currently registered AgentID values.
        """
        return list(self._agents.keys())

    def has_agent(self, agent_id: Union[AgentID, str]) -> bool:
        """
        Checks whether the specified AgentID (or string value) is registered.
        """
        target_id = self._resolve_agent_id(agent_id)
        if target_id is None:
            return False
        return target_id in self._agents

    def clear(self) -> None:
        """
        Clears all registrations from this registry instance only.
        Does not mutate global state or external services.
        """
        self._agents.clear()

    async def dispatch(
        self,
        context: AgentExecutionContext,
        timeout_seconds: Optional[float] = None,
    ) -> AgentExecutionResult:
        """
        Resolves the agent using context.agent_id and dispatches execution.
        If unregistered, returns a structured failed AgentExecutionResult with
        code 'AGENT_NOT_REGISTERED' and retryable=False.
        If registered, delegates to agent.execute(context, timeout_seconds=timeout_seconds).
        """
        agent = self.get_agent(context.agent_id)
        if agent is None:
            agent_id_str = (
                context.agent_id.value
                if isinstance(context.agent_id, AgentID)
                else str(context.agent_id)
            )
            msg = f"Agent '{agent_id_str}' is not registered in the agent registry."
            logger.warning(
                msg,
                extra={
                    "event_name": "agent_not_registered",
                    "agent_id": agent_id_str,
                    "execution_id": context.execution_id,
                },
            )
            return AgentExecutionResult(
                execution_id=context.execution_id,
                agent_id=context.agent_id,
                status=TaskStatus.FAILED,
                output_payload={},
                duration_ms=0.0,
                error=ErrorInfo(
                    code="AGENT_NOT_REGISTERED",
                    message=msg,
                    error_type="LookupError",
                    request_id=context.request_id,
                    correlation_id=context.correlation_id,
                    retryable=False,
                    details={"agent_id": agent_id_str},
                ),
            )

        return await agent.execute(context, timeout_seconds=timeout_seconds)

    @staticmethod
    def _resolve_agent_id(agent_id: Union[AgentID, str]) -> Optional[AgentID]:
        """Helper to resolve AgentID enum or string value safely."""
        if isinstance(agent_id, AgentID):
            return agent_id
        if isinstance(agent_id, str):
            try:
                return AgentID(agent_id)
            except ValueError:
                return None
        return None
