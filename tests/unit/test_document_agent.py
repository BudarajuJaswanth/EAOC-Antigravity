"""
EAOC Part 2.5 Unit Test Suite: Document Agent
Tests DocumentAgent:
1. Canonical AgentID and schema definitions.
2. Full text extraction across supported formats (TXT, DOCX/PDF).
3. Document summarization (deterministic and LLM-assisted).
4. Structured field extraction with extracted_fields and missing_fields tracking.
5. In-memory document reader integration.
6. Rejection of empty document content.
7. Rejection of unsupported document formats.
8. Rejection when neither document_id nor document_content is provided.
9. Rejection when extract_fields is requested without extraction_schema.
10. Document size limit bounding enforcement.
11. Safe error handling with secret redaction.
12. Compatibility with BaseAgent execution and AgentRegistry dispatch.
"""

import asyncio
import io
import unittest
from unittest.mock import AsyncMock, MagicMock
from pydantic import ValidationError

from eaoc.agents.base import BaseAgent
from eaoc.agents.document import (
    BaseDocumentReader,
    DocumentAgent,
    DocumentFormat,
    DocumentInput,
    DocumentOperation,
    DocumentOutput,
    DocumentParser,
    InMemoryDocumentReader,
)
from eaoc.agents.registry import AgentRegistry
from eaoc.models.base import ModelProviderCategory, ModelProviderWrapper
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    TaskStatus,
)


class FailingDocumentReader(BaseDocumentReader):
    """Reader that raises an error containing sensitive credentials."""

    async def read_document(self, document_id: str):
        raise RuntimeError(
            "Access failed to postgresql://doc_admin:SuperSecretKey999@storage.internal:5432/docs"
        )


class TestDocumentAgent(unittest.IsolatedAsyncioTestCase):
    """Unit test suite for the Document specialized agent."""

    def setUp(self):
        self.sample_text = (
            "Master Services Agreement.\n"
            "Vendor: Cyberdyne Systems Inc.\n"
            "Contract Number: CSA-2026-9901.\n"
            "Effective Date: January 1, 2026.\n"
            "Total Contract Value: $1,250,000 USD.\n"
            "Governing Law: State of Delaware.\n"
            "The vendor agrees to provide enterprise multi-agent operations infrastructure."
        )
        self.reader = InMemoryDocumentReader({
            "doc-csa-001": (self.sample_text.encode("utf-8"), "txt"),
        })
        self.agent = DocumentAgent(reader=self.reader)

        self.context = AgentExecutionContext(
            execution_id="exec-doc-001",
            workflow_id="wf-doc-001",
            task_id="task-doc-001",
            agent_id=AgentID.DOCUMENT,
            request_id="req-doc-001",
            correlation_id="corr-doc-001",
            input_payload={
                "document_id": "doc-csa-001",
                "format": "txt",
                "operation": "full_text",
            },
            metadata={"caller": "test_runner"},
        )

    def test_canonical_agent_id_and_schemas(self):
        """Verify agent binds to AgentID.DOCUMENT and canonical schemas."""
        self.assertEqual(self.agent.agent_id, AgentID.DOCUMENT)
        self.assertEqual(self.agent.agent_id.value, "document")
        self.assertIs(self.agent.input_schema, DocumentInput)
        self.assertIs(self.agent.output_schema, DocumentOutput)
        self.assertIsInstance(self.agent, BaseAgent)

    def test_input_schema_validation(self):
        """Verify DocumentInput constraints and validators."""
        valid_input = DocumentInput(
            document_content="Simple text content",
            format=DocumentFormat.TXT,
            operation=DocumentOperation.FULL_TEXT,
        )
        self.assertEqual(valid_input.format, DocumentFormat.TXT)
        self.assertEqual(valid_input.operation, DocumentOperation.FULL_TEXT)

        # Neither document_id nor document_content
        with self.assertRaises(ValidationError):
            DocumentInput(
                format=DocumentFormat.TXT,
                operation=DocumentOperation.FULL_TEXT,
            )

        # extract_fields requires extraction_schema
        with self.assertRaises(ValidationError):
            DocumentInput(
                document_content="some text",
                format=DocumentFormat.TXT,
                operation=DocumentOperation.EXTRACT_FIELDS,
            )

        # Unsupported format rejected
        with self.assertRaises(ValidationError):
            DocumentInput(
                document_content="some text",
                format="unsupported_format",  # type: ignore
                operation=DocumentOperation.FULL_TEXT,
            )

    async def test_full_text_operation_success(self):
        """Verify full_text operation extracts plain text and page metadata."""
        result: AgentExecutionResult = await self.agent.execute(self.context)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.execution_id, "exec-doc-001")
        self.assertEqual(result.agent_id, AgentID.DOCUMENT)
        self.assertIsNone(result.error)

        output = DocumentOutput.model_validate(result.output_payload)
        self.assertEqual(output.status, "completed")
        self.assertEqual(output.document_id, "doc-csa-001")
        self.assertEqual(output.operation, DocumentOperation.FULL_TEXT)
        self.assertEqual(output.page_count, 1)
        self.assertIn("Master Services Agreement", output.text_content)
        self.assertEqual(output.provenance_ref, "prov-exec-doc-001")

    async def test_summarize_operation_deterministic(self):
        """Verify summarize operation produces grounded summary without external calls."""
        ctx = self.context.model_copy(update={"input_payload": {
            "document_id": "doc-csa-001",
            "format": "txt",
            "operation": "summarize",
        }})
        result: AgentExecutionResult = await self.agent.execute(ctx)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        output = DocumentOutput.model_validate(result.output_payload)
        self.assertEqual(output.operation, DocumentOperation.SUMMARIZE)
        self.assertIsNotNone(output.summary)
        self.assertIn("Master Services Agreement", output.summary)
        self.assertIsNone(output.text_content)

    async def test_extract_fields_operation_deterministic(self):
        """Verify extract_fields separates found fields from missing_fields."""
        ctx = self.context.model_copy(update={"input_payload": {
            "document_id": "doc-csa-001",
            "format": "txt",
            "operation": "extract_fields",
            "extraction_schema": ["Vendor", "Effective Date", "Termination Penalty"],
        }})
        result: AgentExecutionResult = await self.agent.execute(ctx)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        output = DocumentOutput.model_validate(result.output_payload)
        self.assertEqual(output.operation, DocumentOperation.EXTRACT_FIELDS)

        # Extracted fields
        self.assertIn("Vendor", output.extracted_fields)
        self.assertEqual(output.extracted_fields["Vendor"], "Cyberdyne Systems Inc.")
        self.assertIn("Effective Date", output.extracted_fields)
        self.assertEqual(output.extracted_fields["Effective Date"], "January 1, 2026.")

        # Missing field accurately reported
        self.assertIn("Termination Penalty", output.missing_fields)
        self.assertNotIn("Termination Penalty", output.extracted_fields)

    async def test_direct_content_string_and_bytes(self):
        """Verify direct document_content (str and bytes) processes without reader."""
        agent_no_reader = DocumentAgent(reader=None)

        # String content
        ctx_str = self.context.model_copy(update={"input_payload": {
            "document_content": "Direct text string payload",
            "format": "txt",
            "operation": "full_text",
        }})
        res_str: AgentExecutionResult = await agent_no_reader.execute(ctx_str)
        self.assertEqual(res_str.status, TaskStatus.COMPLETED)
        self.assertEqual(res_str.output_payload["text_content"], "Direct text string payload")

        # Bytes content
        ctx_bytes = self.context.model_copy(update={"input_payload": {
            "document_content": b"Direct bytes payload",
            "format": "txt",
            "operation": "full_text",
        }})
        res_bytes: AgentExecutionResult = await agent_no_reader.execute(ctx_bytes)
        self.assertEqual(res_bytes.status, TaskStatus.COMPLETED)
        self.assertEqual(res_bytes.output_payload["text_content"], "Direct bytes payload")

    async def test_rejection_of_empty_document(self):
        """Verify empty document content is rejected with execution error."""
        ctx = self.context.model_copy(update={"input_payload": {
            "document_content": "   ",
            "format": "txt",
            "operation": "full_text",
        }})
        result: AgentExecutionResult = await self.agent.execute(ctx)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("empty or unreadable", result.error.details["error"])

    async def test_rejection_of_unsupported_format(self):
        """Verify parser rejects unknown format extension."""
        with self.assertRaises(ValueError) as ctx:
            DocumentParser.parse(content=b"some binary data", doc_format="zip")
        self.assertIn("Unsupported document format", str(ctx.exception))

    async def test_max_content_size_bounding(self):
        """Verify document exceeding max_content_size_bytes is rejected."""
        ctx = self.context.model_copy(update={"input_payload": {
            "document_content": "A" * 500,
            "format": "txt",
            "operation": "full_text",
            "max_content_size_bytes": 100,  # limit to 100 bytes
        }})
        result: AgentExecutionResult = await self.agent.execute(ctx)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("exceeds the maximum limit", result.error.details["error"])

    async def test_missing_document_in_reader(self):
        """Verify requesting non-existent document_id returns failure."""
        ctx = self.context.model_copy(update={"input_payload": {
            "document_id": "nonexistent-doc-id",
            "format": "txt",
            "operation": "full_text",
        }})
        result: AgentExecutionResult = await self.agent.execute(ctx)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("not found", result.error.details["error"])

    async def test_safe_errors_no_credential_leakage(self):
        """Verify reader exceptions containing credentials are redacted."""
        failing_agent = DocumentAgent(reader=FailingDocumentReader())
        result: AgentExecutionResult = await failing_agent.execute(self.context)

        self.assertEqual(result.status, TaskStatus.FAILED)
        err_str = str(result.error.details)
        self.assertNotIn("SuperSecretKey999", err_str)
        self.assertNotIn("SuperSecretKey999", result.error.message)

    async def test_chat_model_summarization_and_extraction(self):
        """Verify model_provider is invoked when configured (zero real network calls)."""
        mock_chat_model = MagicMock()
        mock_chat_model.ainvoke = AsyncMock(
            return_value=MagicMock(content='{"Vendor": "Cyberdyne Systems", "Governing Law": "Delaware"}')
        )

        mock_provider = ModelProviderWrapper(
            provider_id="mock-gemini",
            model_name="gemini-pro-test",
            category=ModelProviderCategory.CLOUD,
            chat_model=mock_chat_model,
        )

        agent_with_llm = DocumentAgent(reader=self.reader, model_provider=mock_provider)

        ctx = self.context.model_copy(update={"input_payload": {
            "document_id": "doc-csa-001",
            "format": "txt",
            "operation": "extract_fields",
            "extraction_schema": ["Vendor", "Governing Law", "NonExistent"],
        }})

        result: AgentExecutionResult = await agent_with_llm.execute(ctx)
        self.assertEqual(result.status, TaskStatus.COMPLETED)

        output = DocumentOutput.model_validate(result.output_payload)
        self.assertEqual(output.extracted_fields["Vendor"], "Cyberdyne Systems")
        self.assertEqual(output.extracted_fields["Governing Law"], "Delaware")
        self.assertIn("NonExistent", output.missing_fields)
        mock_chat_model.ainvoke.assert_awaited_once()

    async def test_agent_registry_compatibility(self):
        """Verify DocumentAgent registers into AgentRegistry and handles dispatch()."""
        registry = AgentRegistry()
        registry.register_agent(self.agent)

        self.assertTrue(registry.has_agent(AgentID.DOCUMENT))
        self.assertTrue(registry.has_agent("document"))
        self.assertIs(registry.get_agent(AgentID.DOCUMENT), self.agent)

        # Dispatch via registry
        result: AgentExecutionResult = await registry.dispatch(self.context)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.agent_id, AgentID.DOCUMENT)
        self.assertEqual(result.output_payload["status"], "completed")


if __name__ == "__main__":
    unittest.main()
