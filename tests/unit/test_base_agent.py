"""
EAOC Part 2.1 Unit Test Suite: Shared Base Agent Contract
Tests BaseAgent abstract contract, typed Pydantic input/output validation,
agent ID mismatch safety, cancellable async timeouts, secret-safe exception handling,
and configuration validation.
"""

import asyncio
import unittest
from typing import Optional
from pydantic import BaseModel, Field

from eaoc.agents.base import BaseAgent
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    TaskStatus,
)


class DummyInput(BaseModel):
    query: str
    max_results: int = Field(default=5, ge=1)


class DummyOutput(BaseModel):
    answer: str
    count: int


class ValidDummyAgent(BaseAgent[DummyInput, DummyOutput]):
    agent_id = AgentID.RESEARCH_RAG
    input_schema = DummyInput
    output_schema = DummyOutput

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.should_raise: Optional[Exception] = None
        self.sleep_duration: Optional[float] = None
        self.custom_output: Optional[dict] = None

    async def _execute(
        self, validated_input: DummyInput, context: AgentExecutionContext
    ) -> DummyOutput:
        if self.sleep_duration is not None:
            await asyncio.sleep(self.sleep_duration)

        if self.should_raise is not None:
            raise self.should_raise

        if self.custom_output is not None:
            return self.custom_output  # type: ignore

        return DummyOutput(
            answer=f"Processed: {validated_input.query}",
            count=validated_input.max_results,
        )


class TestBaseAgentContract(unittest.TestCase):

    def setUp(self):
        self.context = AgentExecutionContext(
            execution_id="exec-test-001",
            workflow_id="wf-test-001",
            task_id="task-test-001",
            agent_id=AgentID.RESEARCH_RAG,
            request_id="req-test-001",
            correlation_id="corr-test-001",
            input_payload={"query": "enterprise AI", "max_results": 3},
            metadata={"caller": "test_runner"},
        )

    def test_successful_execution_and_validated_output(self):
        """Verify successful agent execution validates input, invokes _execute, and returns typed output."""
        agent = ValidDummyAgent()
        result: AgentExecutionResult = asyncio.run(agent.execute(self.context))

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.execution_id, "exec-test-001")
        self.assertEqual(result.agent_id, AgentID.RESEARCH_RAG)
        self.assertEqual(
            result.output_payload,
            {"answer": "Processed: enterprise AI", "count": 3},
        )
        self.assertIsNone(result.error)
        self.assertIsNotNone(result.duration_ms)
        self.assertGreaterEqual(result.duration_ms, 0)

    def test_agent_id_mismatch(self):
        """Verify agent ID mismatch safely aborts execution and returns AGENT_ID_MISMATCH error."""
        agent = ValidDummyAgent()
        mismatched_context = self.context.model_copy(
            update={"agent_id": AgentID.SQL_DATA}
        )

        result: AgentExecutionResult = asyncio.run(agent.execute(mismatched_context))

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.execution_id, "exec-test-001")
        self.assertEqual(result.output_payload, {})
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "AGENT_ID_MISMATCH")
        self.assertFalse(result.error.retryable)
        self.assertEqual(result.error.details["expected_agent_id"], "research_rag")
        self.assertEqual(result.error.details["received_agent_id"], "sql_data")

    def test_invalid_input(self):
        """Verify invalid input payload failing Pydantic schema validation returns INVALID_INPUT error."""
        agent = ValidDummyAgent()
        invalid_context = self.context.model_copy(
            update={"input_payload": {"max_results": "not_an_integer"}}
        )

        result: AgentExecutionResult = asyncio.run(agent.execute(invalid_context))

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.output_payload, {})
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "INVALID_INPUT")
        self.assertEqual(result.error.error_type, "ValidationError")
        self.assertFalse(result.error.retryable)
        self.assertIn("validation_error", result.error.details)

    def test_invalid_output(self):
        """Verify invalid output payload from _execute failing output_schema returns INVALID_OUTPUT error."""
        agent = ValidDummyAgent()
        agent.custom_output = {"answer": "Only answer, missing count"}

        result: AgentExecutionResult = asyncio.run(agent.execute(self.context))

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.output_payload, {})
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "INVALID_OUTPUT")
        self.assertEqual(result.error.error_type, "ValidationError")
        self.assertFalse(result.error.retryable)
        self.assertIn("validation_error", result.error.details)

    def test_agent_execution_exception_and_safe_error_handling(self):
        """Verify unhandled exceptions in _execute are safely captured and credentials are redacted."""
        agent = ValidDummyAgent()
        secret_msg = "Database connection postgresql+asyncpg://admin:SuperSecretPass999@db.local:5432/db failed"
        agent.should_raise = RuntimeError(secret_msg)

        result: AgentExecutionResult = asyncio.run(agent.execute(self.context))

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.output_payload, {})
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "EXECUTION_ERROR")
        self.assertEqual(result.error.error_type, "RuntimeError")
        self.assertFalse(result.error.retryable)

        # Verify sensitive password was redacted
        err_details_str = str(result.error.details)
        self.assertNotIn("SuperSecretPass999", err_details_str)
        self.assertNotIn("SuperSecretPass999", result.error.message)

    def test_timeout_behavior_cancellation(self):
        """Verify cancellable async timeout halts execution and returns EXECUTION_TIMEOUT error."""
        agent = ValidDummyAgent()
        agent.sleep_duration = 2.0  # Would sleep 2 seconds

        result: AgentExecutionResult = asyncio.run(
            agent.execute(self.context, timeout_seconds=0.05)
        )

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.output_payload, {})
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "EXECUTION_TIMEOUT")
        self.assertEqual(result.error.error_type, "TimeoutError")
        self.assertTrue(result.error.retryable)
        self.assertEqual(result.error.details["timeout_seconds"], 0.05)

    def test_default_timeout_behavior(self):
        """Verify default_timeout_seconds configured on agent is applied when no override is given."""
        agent = ValidDummyAgent(default_timeout_seconds=0.05)
        agent.sleep_duration = 2.0

        result: AgentExecutionResult = asyncio.run(agent.execute(self.context))

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.error.code, "EXECUTION_TIMEOUT")
        self.assertTrue(result.error.retryable)
        self.assertEqual(result.error.details["timeout_seconds"], 0.05)

    def test_invalid_timeout_configuration(self):
        """Verify negative or zero timeout overrides return INVALID_TIMEOUT_CONFIG error."""
        agent = ValidDummyAgent()

        for invalid_timeout in [-1.0, 0, -5, "ten"]:
            result: AgentExecutionResult = asyncio.run(
                agent.execute(self.context, timeout_seconds=invalid_timeout)  # type: ignore
            )
            self.assertEqual(result.status, TaskStatus.FAILED)
            self.assertEqual(result.error.code, "INVALID_TIMEOUT_CONFIG")
            self.assertEqual(result.error.error_type, "ValueError")
            self.assertFalse(result.error.retryable)

    def test_agent_initialization_validation(self):
        """Verify BaseAgent validates agent_id, schemas, and default timeout during initialization."""
        # Missing or invalid agent_id
        class NoAgentId(BaseAgent[DummyInput, DummyOutput]):
            input_schema = DummyInput
            output_schema = DummyOutput

            async def _execute(self, i, c):
                pass

        with self.assertRaises(ValueError) as cm:
            NoAgentId()
        self.assertIn("must define a valid AgentID", str(cm.exception))

        # Invalid input_schema
        class InvalidInputSchema(BaseAgent[DummyInput, DummyOutput]):
            agent_id = AgentID.PLANNER
            input_schema = dict  # Not a BaseModel subclass
            output_schema = DummyOutput

            async def _execute(self, i, c):
                pass

        with self.assertRaises(ValueError) as cm:
            InvalidInputSchema()
        self.assertIn("must define 'input_schema' as a Pydantic BaseModel subclass", str(cm.exception))

        # Invalid default timeout during init
        with self.assertRaises(ValueError) as cm:
            ValidDummyAgent(default_timeout_seconds=-10)
        self.assertIn("default_timeout_seconds must be a positive number", str(cm.exception))

        with self.assertRaises(ValueError) as cm:
            ValidDummyAgent(default_timeout_seconds=0)
        self.assertIn("default_timeout_seconds must be a positive number", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
