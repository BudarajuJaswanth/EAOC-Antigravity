"""
EAOC Part 2.3 Unit Test Suite: Research/RAG Agent
Tests ResearchRagAgent:
1. Canonical AgentID and schema definitions.
2. Valid input and output schemas.
3. Malformed input handling and bounds validation.
4. Successful retrieval and evidence citation synthesis.
5. Explicit handling of empty retrieval results ('no_results').
6. Safe handling of retrieval exceptions without unhandled crashes.
7. Unconfigured retriever error handling.
8. ModelProviderWrapper integration with mock chat model (zero external calls).
9. Compatibility with BaseAgent execution and AgentRegistry dispatch.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock
from pydantic import ValidationError

from eaoc.agents.base import BaseAgent
from eaoc.agents.registry import AgentRegistry
from eaoc.agents.research_rag import (
    BaseRetriever,
    DocumentChunk,
    InMemoryRetriever,
    ResearchRagAgent,
    ResearchRagInput,
    ResearchRagOutput,
    SourceCitation,
)
from eaoc.models.base import ModelProviderCategory, ModelProviderWrapper
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    TaskStatus,
)


class ErrorFailingRetriever(BaseRetriever):
    """Retriever stub that raises an exception during retrieval."""

    async def retrieve(self, query: str, top_k: int = 5, namespace=None, access_level=None):
        raise RuntimeError("Simulated vector index connection timeout")


class TestResearchRagAgent(unittest.IsolatedAsyncioTestCase):
    """Unit test suite for the Research/RAG specialized agent."""

    def setUp(self):
        self.chunk_1 = DocumentChunk(
            document_id="doc-hr-001",
            chunk_id="chunk-01",
            content="Employees are entitled to 20 days of paid annual leave per calendar year.",
            score=0.92,
            source_title="Employee Benefits Policy 2026",
            metadata={"namespace": "hr_policy", "access_level": "standard"},
        )
        self.chunk_2 = DocumentChunk(
            document_id="doc-hr-002",
            chunk_id="chunk-02",
            content="Carryover of unused annual leave is capped at 5 days into the subsequent year.",
            score=0.88,
            source_title="Leave Carryover Guidelines",
            metadata={"namespace": "hr_policy", "access_level": "standard"},
        )
        self.retriever = InMemoryRetriever([self.chunk_1, self.chunk_2])
        self.agent = ResearchRagAgent(retriever=self.retriever)

        self.context = AgentExecutionContext(
            execution_id="exec-rag-001",
            workflow_id="wf-rag-001",
            task_id="task-rag-001",
            agent_id=AgentID.RESEARCH_RAG,
            request_id="req-rag-001",
            correlation_id="corr-rag-001",
            input_payload={"query": "annual leave policy", "top_k": 5},
            metadata={"caller": "test_runner"},
        )

    def test_canonical_agent_id_and_schemas(self):
        """Verify agent binds to AgentID.RESEARCH_RAG and canonical schemas."""
        self.assertEqual(self.agent.agent_id, AgentID.RESEARCH_RAG)
        self.assertEqual(self.agent.agent_id.value, "research_rag")
        self.assertIs(self.agent.input_schema, ResearchRagInput)
        self.assertIs(self.agent.output_schema, ResearchRagOutput)
        self.assertIsInstance(self.agent, BaseAgent)

    def test_input_schema_validation(self):
        """Verify ResearchRagInput validation and boundary constraints."""
        valid_input = ResearchRagInput(query="What is the remote work policy?", top_k=10)
        self.assertEqual(valid_input.query, "What is the remote work policy?")
        self.assertEqual(valid_input.top_k, 10)
        self.assertIsNone(valid_input.namespace)

        # Empty query rejected
        with self.assertRaises(ValidationError):
            ResearchRagInput(query="")

        # Query exceeding 2000 chars rejected
        with self.assertRaises(ValidationError):
            ResearchRagInput(query="a" * 2001)

        # top_k < 1 rejected
        with self.assertRaises(ValidationError):
            ResearchRagInput(query="valid", top_k=0)

        # top_k > 50 rejected
        with self.assertRaises(ValidationError):
            ResearchRagInput(query="valid", top_k=51)

        # Extra forbidden fields rejected
        with self.assertRaises(ValidationError):
            ResearchRagInput(query="valid", unauthorized_field=123)  # type: ignore

    async def test_malformed_context_input_payload(self):
        """Verify malformed input payload via execute() returns INVALID_INPUT failure."""
        malformed_context = self.context.model_copy(
            update={"input_payload": {"query": "", "top_k": -1}}
        )
        result: AgentExecutionResult = await self.agent.execute(malformed_context)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.output_payload, {})
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "INVALID_INPUT")
        self.assertFalse(result.error.retryable)

    async def test_successful_execution_with_evidence(self):
        """Verify successful retrieval returns grounded evidence citations without hallucination."""
        result: AgentExecutionResult = await self.agent.execute(self.context)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.execution_id, "exec-rag-001")
        self.assertEqual(result.agent_id, AgentID.RESEARCH_RAG)
        self.assertIsNone(result.error)

        output = ResearchRagOutput.model_validate(result.output_payload)
        self.assertEqual(output.status, "completed")
        self.assertEqual(output.query, "annual leave policy")
        self.assertEqual(output.retrieval_count, 2)
        self.assertEqual(len(output.sources), 2)

        # Check evidence citation fields
        source_1 = output.sources[0]
        self.assertEqual(source_1.document_id, "doc-hr-001")
        self.assertEqual(source_1.chunk_id, "chunk-01")
        self.assertEqual(source_1.score, 0.92)
        self.assertIn("20 days", source_1.excerpt)
        self.assertEqual(source_1.source_title, "Employee Benefits Policy 2026")

        # Grounded answer synthesis
        self.assertIsNotNone(output.answer)
        self.assertIn("doc-hr-001:chunk-01", output.answer)
        self.assertEqual(output.model_used, "deterministic-evidence-synthesis")
        self.assertEqual(output.provenance_ref, "prov-exec-rag-001")

    async def test_empty_retrieval_handling(self):
        """Verify empty retrieval result explicitly returns status='no_results' and answer=None."""
        empty_retriever = InMemoryRetriever([])
        agent = ResearchRagAgent(retriever=empty_retriever)

        result: AgentExecutionResult = await agent.execute(self.context)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertIsNone(result.error)

        output = ResearchRagOutput.model_validate(result.output_payload)
        self.assertEqual(output.status, "no_results")
        self.assertEqual(output.query, "annual leave policy")
        self.assertIsNone(output.answer)
        self.assertEqual(output.sources, [])
        self.assertEqual(output.retrieval_count, 0)
        self.assertIsNone(output.model_used)
        self.assertEqual(output.provenance_ref, "prov-exec-rag-001")

    async def test_retrieval_exception_safety(self):
        """Verify retrieval exceptions are safely caught by BaseAgent contract."""
        failing_agent = ResearchRagAgent(retriever=ErrorFailingRetriever())

        result: AgentExecutionResult = await failing_agent.execute(self.context)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.output_payload, {})
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "EXECUTION_ERROR")
        self.assertEqual(result.error.error_type, "RuntimeError")
        self.assertEqual(result.error.message, "An error occurred during agent execution.")
        self.assertIn("Simulated vector index connection timeout", result.error.details["error"])

    async def test_unconfigured_retriever_raises_safe_error(self):
        """Verify executing without a configured retriever returns clear failure."""
        unconfigured_agent = ResearchRagAgent(retriever=None)

        result: AgentExecutionResult = await unconfigured_agent.execute(self.context)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "EXECUTION_ERROR")
        self.assertIn("Retriever is not configured", result.error.details["error"])

    async def test_mock_model_provider_synthesis(self):
        """Verify model_provider chat_model is invoked when configured (zero real network calls)."""
        mock_chat_model = MagicMock()
        mock_chat_model.ainvoke = AsyncMock(
            return_value=MagicMock(content="According to doc-hr-001, employees receive 20 days annual leave.")
        )

        mock_provider = ModelProviderWrapper(
            provider_id="mock-ollama",
            model_name="llama3-test",
            category=ModelProviderCategory.LOCAL,
            chat_model=mock_chat_model,
        )

        agent_with_llm = ResearchRagAgent(
            retriever=self.retriever,
            model_provider=mock_provider,
        )

        result: AgentExecutionResult = await agent_with_llm.execute(self.context)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        output = ResearchRagOutput.model_validate(result.output_payload)
        self.assertEqual(output.status, "completed")
        self.assertEqual(output.model_used, "llama3-test")
        self.assertIn("According to doc-hr-001", output.answer)

        # Verify chat_model.ainvoke was called with prompt containing citations
        mock_chat_model.ainvoke.assert_awaited_once()
        prompt_arg = mock_chat_model.ainvoke.call_args[0][0]
        self.assertIn("doc-hr-001", prompt_arg)
        self.assertIn("annual leave policy", prompt_arg)

    async def test_agent_registry_compatibility(self):
        """Verify ResearchRagAgent registers into AgentRegistry and handles dispatch()."""
        registry = AgentRegistry()
        registry.register_agent(self.agent)

        self.assertTrue(registry.has_agent(AgentID.RESEARCH_RAG))
        self.assertTrue(registry.has_agent("research_rag"))
        self.assertIs(registry.get_agent(AgentID.RESEARCH_RAG), self.agent)

        # Dispatch execution through registry
        result: AgentExecutionResult = await registry.dispatch(self.context)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.agent_id, AgentID.RESEARCH_RAG)
        self.assertEqual(result.output_payload["status"], "completed")


if __name__ == "__main__":
    unittest.main()
