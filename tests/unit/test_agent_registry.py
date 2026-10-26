"""
EAOC Part 2.2 Unit Test Suite: Enterprise Agent Registry & Capability Discovery
Tests AgentRegistry:
1. Successful agent registration.
2. Rejection of non-BaseAgent objects.
3. Duplicate AgentID rejection without replacement.
4. Lookup of registered and missing agents (get_agent, has_agent).
5. Listing registered agents and IDs (list_agents, list_agent_ids).
6. Registry isolation and clear().
7. Successful dispatch to a registered agent.
8. Forwarding of a supported timeout override.
9. Dispatch to an unregistered agent (AGENT_NOT_REGISTERED, retryable=False).
"""

import asyncio
import unittest
from typing import Optional
from unittest.mock import AsyncMock, patch
from pydantic import BaseModel, Field

from eaoc.agents.base import BaseAgent
from eaoc.agents.registry import AgentRegistry
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    TaskStatus,
)


class DummyAgentInput(BaseModel):
    query: str
    limit: int = Field(default=5, ge=1)


class DummyAgentOutput(BaseModel):
    response: str
    items_count: int


class DummyResearchAgent(BaseAgent[DummyAgentInput, DummyAgentOutput]):
    agent_id = AgentID.RESEARCH_RAG
    input_schema = DummyAgentInput
    output_schema = DummyAgentOutput

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sleep_duration: Optional[float] = None

    async def _execute(
        self, validated_input: DummyAgentInput, context: AgentExecutionContext
    ) -> DummyAgentOutput:
        if self.sleep_duration:
            await asyncio.sleep(self.sleep_duration)
        return DummyAgentOutput(
            response=f"Research response for: {validated_input.query}",
            items_count=validated_input.limit,
        )


class DummySqlAgent(BaseAgent[DummyAgentInput, DummyAgentOutput]):
    agent_id = AgentID.SQL_DATA
    input_schema = DummyAgentInput
    output_schema = DummyAgentOutput

    async def _execute(
        self, validated_input: DummyAgentInput, context: AgentExecutionContext
    ) -> DummyAgentOutput:
        return DummyAgentOutput(
            response=f"SQL data response for: {validated_input.query}",
            items_count=validated_input.limit,
        )


class TestAgentRegistry(unittest.IsolatedAsyncioTestCase):
    """Unit test suite for the Enterprise Agent Registry."""

    def setUp(self):
        self.registry = AgentRegistry()
        self.research_agent = DummyResearchAgent()
        self.sql_agent = DummySqlAgent()

    def test_successful_registration(self):
        """Verify registering a valid BaseAgent succeeds and is queryable."""
        self.registry.register_agent(self.research_agent)
        self.assertTrue(self.registry.has_agent(AgentID.RESEARCH_RAG))
        self.assertTrue(self.registry.has_agent("research_rag"))
        self.assertIs(self.registry.get_agent(AgentID.RESEARCH_RAG), self.research_agent)
        self.assertIs(self.registry.get_agent("research_rag"), self.research_agent)

    def test_rejection_of_non_base_agent(self):
        """Verify non-BaseAgent objects are rejected with TypeError."""
        class NotAnAgent:
            agent_id = AgentID.RESEARCH_RAG

        with self.assertRaises(TypeError) as ctx:
            self.registry.register_agent(NotAnAgent())  # type: ignore
        self.assertIn("Expected instance of BaseAgent", str(ctx.exception))

        with self.assertRaises(TypeError):
            self.registry.register_agent("some_string")  # type: ignore

    def test_duplicate_registration_rejected_without_replacement(self):
        """Verify registering duplicate AgentID raises ValueError and does not replace original."""
        self.registry.register_agent(self.research_agent)

        replacement_research_agent = DummyResearchAgent()
        with self.assertRaises(ValueError) as ctx:
            self.registry.register_agent(replacement_research_agent)
        self.assertIn("already registered", str(ctx.exception))

        # Original instance remains untouched
        self.assertIs(self.registry.get_agent(AgentID.RESEARCH_RAG), self.research_agent)
        self.assertIsNot(self.registry.get_agent(AgentID.RESEARCH_RAG), replacement_research_agent)

    def test_lookup_registered_and_missing_agents(self):
        """Verify lookup returns agent or None deterministically for enum, string, or invalid input."""
        self.registry.register_agent(self.sql_agent)

        # Registered
        self.assertIs(self.registry.get_agent(AgentID.SQL_DATA), self.sql_agent)
        self.assertIs(self.registry.get_agent("sql_data"), self.sql_agent)
        self.assertTrue(self.registry.has_agent(AgentID.SQL_DATA))
        self.assertTrue(self.registry.has_agent("sql_data"))

        # Unregistered valid AgentID
        self.assertIsNone(self.registry.get_agent(AgentID.FINANCE))
        self.assertIsNone(self.registry.get_agent("finance"))
        self.assertFalse(self.registry.has_agent(AgentID.FINANCE))
        self.assertFalse(self.registry.has_agent("finance"))

        # Completely invalid ID
        self.assertIsNone(self.registry.get_agent("unknown_nonexistent_agent"))
        self.assertFalse(self.registry.has_agent("unknown_nonexistent_agent"))
        self.assertIsNone(self.registry.get_agent(12345))  # type: ignore
        self.assertFalse(self.registry.has_agent(12345))  # type: ignore

    def test_listing_registered_agents_and_ids(self):
        """Verify list_agents and list_agent_ids return accurate collections."""
        self.assertEqual(self.registry.list_agents(), [])
        self.assertEqual(self.registry.list_agent_ids(), [])

        self.registry.register_agent(self.research_agent)
        self.registry.register_agent(self.sql_agent)

        agents = self.registry.list_agents()
        agent_ids = self.registry.list_agent_ids()

        self.assertEqual(len(agents), 2)
        self.assertIn(self.research_agent, agents)
        self.assertIn(self.sql_agent, agents)

        self.assertEqual(len(agent_ids), 2)
        self.assertIn(AgentID.RESEARCH_RAG, agent_ids)
        self.assertIn(AgentID.SQL_DATA, agent_ids)

    def test_registry_isolation_and_clear(self):
        """Verify multiple registry instances are isolated and clear() resets local state only."""
        registry_b = AgentRegistry()

        self.registry.register_agent(self.research_agent)
        registry_b.register_agent(self.sql_agent)

        self.assertTrue(self.registry.has_agent(AgentID.RESEARCH_RAG))
        self.assertFalse(self.registry.has_agent(AgentID.SQL_DATA))

        self.assertFalse(registry_b.has_agent(AgentID.RESEARCH_RAG))
        self.assertTrue(registry_b.has_agent(AgentID.SQL_DATA))

        self.registry.clear()
        self.assertEqual(self.registry.list_agents(), [])
        self.assertFalse(self.registry.has_agent(AgentID.RESEARCH_RAG))

        # registry_b remains unchanged
        self.assertTrue(registry_b.has_agent(AgentID.SQL_DATA))
        self.assertEqual(len(registry_b.list_agents()), 1)

    async def test_successful_dispatch_to_registered_agent(self):
        """Verify dispatch successfully forwards execution to registered agent."""
        self.registry.register_agent(self.research_agent)

        context = AgentExecutionContext(
            execution_id="exec-reg-001",
            workflow_id="wf-reg-001",
            task_id="task-reg-001",
            agent_id=AgentID.RESEARCH_RAG,
            request_id="req-999",
            correlation_id="corr-999",
            input_payload={"query": "quantum computing", "limit": 10},
            metadata={"caller": "test_runner"},
        )

        result = await self.registry.dispatch(context)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.execution_id, "exec-reg-001")
        self.assertEqual(result.agent_id, AgentID.RESEARCH_RAG)
        self.assertEqual(
            result.output_payload,
            {"response": "Research response for: quantum computing", "items_count": 10},
        )
        self.assertIsNone(result.error)

    async def test_dispatch_forwarding_timeout_override(self):
        """Verify dispatch forwards timeout_seconds override to agent.execute()."""
        self.registry.register_agent(self.research_agent)

        context = AgentExecutionContext(
            execution_id="exec-timeout-001",
            workflow_id="wf-timeout-001",
            task_id="task-timeout-001",
            agent_id=AgentID.RESEARCH_RAG,
            request_id="req-timeout",
            correlation_id="corr-timeout",
            input_payload={"query": "slow task", "limit": 2},
            metadata={"caller": "test_runner"},
        )

        # Agent sleeps 0.3s, timeout override set to 0.05s
        self.research_agent.sleep_duration = 0.3

        result = await self.registry.dispatch(context, timeout_seconds=0.05)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "EXECUTION_TIMEOUT")
        self.assertTrue(result.error.retryable)

    async def test_dispatch_timeout_parameter_passed_to_agent_execute(self):
        """Verify dispatch explicitly passes the timeout_seconds parameter into agent.execute."""
        self.registry.register_agent(self.sql_agent)

        context = AgentExecutionContext(
            execution_id="exec-spy-001",
            workflow_id="wf-spy-001",
            task_id="task-spy-001",
            agent_id=AgentID.SQL_DATA,
            request_id="req-spy",
            correlation_id="corr-spy",
            input_payload={"query": "SELECT 1", "limit": 1},
            metadata={},
        )

        with patch.object(
            self.sql_agent, "execute", wraps=self.sql_agent.execute
        ) as spy_execute:
            result = await self.registry.dispatch(context, timeout_seconds=12.5)
            self.assertEqual(result.status, TaskStatus.COMPLETED)
            spy_execute.assert_awaited_once_with(context, timeout_seconds=12.5)

    async def test_dispatch_unregistered_agent_failure(self):
        """Verify dispatching to unregistered agent returns AGENT_NOT_REGISTERED with retryable=False."""
        context = AgentExecutionContext(
            execution_id="exec-unreg-001",
            workflow_id="wf-unreg-001",
            task_id="task-unreg-001",
            agent_id=AgentID.FINANCE,
            request_id="req-unreg",
            correlation_id="corr-unreg",
            input_payload={"amount": 1000},
            metadata={"caller": "test_runner"},
        )

        result = await self.registry.dispatch(context)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.execution_id, "exec-unreg-001")
        self.assertEqual(result.agent_id, AgentID.FINANCE)
        self.assertEqual(result.output_payload, {})
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "AGENT_NOT_REGISTERED")
        self.assertFalse(result.error.retryable)
        self.assertEqual(result.error.request_id, "req-unreg")
        self.assertEqual(result.error.correlation_id, "corr-unreg")
        self.assertIn("finance", result.error.message)


if __name__ == "__main__":
    unittest.main()
