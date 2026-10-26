"""
EAOC Document Agent Implementation
Provides structured document parsing, text extraction, summarization, and field extraction
across PDF, DOCX, and TXT enterprise documents with strict provenance tracking.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from eaoc.agents.base import BaseAgent
from eaoc.agents.document.parser import DocumentParser, ParsedDocument
from eaoc.agents.document.reader import BaseDocumentReader
from eaoc.agents.document.schemas import (
    DocumentFormat,
    DocumentInput,
    DocumentOperation,
    DocumentOutput,
)
from eaoc.models.base import ModelProviderWrapper
from eaoc.observability import get_logger
from eaoc.schemas.domain import AgentExecutionContext, AgentID

logger = get_logger("eaoc.agents.document")


class DocumentAgent(BaseAgent[DocumentInput, DocumentOutput]):
    """
    Document specialized agent.
    Parses unstructured/semi-structured files, performs field extraction against schemas,
    and produces grounded document summaries without fabricating information.
    """

    agent_id: AgentID = AgentID.DOCUMENT
    input_schema = DocumentInput
    output_schema = DocumentOutput

    def __init__(
        self,
        reader: Optional[BaseDocumentReader] = None,
        model_provider: Optional[ModelProviderWrapper] = None,
        default_timeout_seconds: Optional[float] = 30.0,
        **kwargs,
    ):
        super().__init__(
            agent_id=AgentID.DOCUMENT,
            input_schema=DocumentInput,
            output_schema=DocumentOutput,
            default_timeout_seconds=default_timeout_seconds,
            **kwargs,
        )
        self.reader = reader
        self.model_provider = model_provider

    async def _execute(
        self, validated_input: DocumentInput, context: AgentExecutionContext
    ) -> DocumentOutput:
        """
        Executes document retrieval, format parsing, and operation dispatch (summarize, extract_fields, full_text).
        """
        # 1. Resolve document bytes and identifier
        content_bytes, doc_id, doc_format = await self._resolve_document_content(
            validated_input=validated_input, context=context
        )

        # 2. Enforce bounded content size
        if len(content_bytes) > validated_input.max_content_size_bytes:
            raise ValueError(
                f"Document size ({len(content_bytes)} bytes) exceeds the maximum limit of "
                f"{validated_input.max_content_size_bytes} bytes."
            )

        logger.info(
            f"Parsing document '{doc_id}' ({doc_format}) for operation '{validated_input.operation.value}'.",
            extra={
                "event_name": "doc_processing_started",
                "execution_id": context.execution_id,
                "document_id": doc_id,
                "format": doc_format,
                "operation": validated_input.operation.value,
            },
        )

        # 3. Parse document content
        parsed = DocumentParser.parse(content=content_bytes, doc_format=doc_format)

        # 4. Dispatch requested operation
        if validated_input.operation == DocumentOperation.FULL_TEXT:
            return self._handle_full_text(parsed=parsed, doc_id=doc_id, validated_input=validated_input, context=context)
        elif validated_input.operation == DocumentOperation.SUMMARIZE:
            return await self._handle_summarize(parsed=parsed, doc_id=doc_id, validated_input=validated_input, context=context)
        elif validated_input.operation == DocumentOperation.EXTRACT_FIELDS:
            return await self._handle_extract_fields(parsed=parsed, doc_id=doc_id, validated_input=validated_input, context=context)
        else:
            raise ValueError(f"Unsupported operation '{validated_input.operation}'.")

    async def _resolve_document_content(
        self, validated_input: DocumentInput, context: AgentExecutionContext
    ) -> Tuple[bytes, str, str]:
        """Resolves raw document bytes, document_id, and format extension."""
        if validated_input.document_content is not None:
            if isinstance(validated_input.document_content, str):
                content_bytes = validated_input.document_content.encode("utf-8")
            else:
                content_bytes = validated_input.document_content
            doc_id = validated_input.document_id or f"doc-{context.execution_id}"
            doc_format = validated_input.format.value
            return content_bytes, doc_id, doc_format

        if not validated_input.document_id:
            raise ValueError("No document_id or document_content provided.")

        if self.reader is None:
            raise RuntimeError(
                f"Document Reader is not configured for agent '{self.agent_id.value}'."
            )

        content_bytes, fetched_format = await self.reader.read_document(
            validated_input.document_id
        )
        doc_format = fetched_format or validated_input.format.value
        return content_bytes, validated_input.document_id, doc_format

    def _handle_full_text(
        self,
        parsed: ParsedDocument,
        doc_id: str,
        validated_input: DocumentInput,
        context: AgentExecutionContext,
    ) -> DocumentOutput:
        """Returns full text and page count metadata."""
        return DocumentOutput(
            status="completed",
            document_id=doc_id,
            operation=validated_input.operation,
            format=validated_input.format,
            page_count=parsed.page_count,
            text_content=parsed.text,
            metadata=parsed.metadata,
            provenance_ref=f"prov-{context.execution_id}",
        )

    async def _handle_summarize(
        self,
        parsed: ParsedDocument,
        doc_id: str,
        validated_input: DocumentInput,
        context: AgentExecutionContext,
    ) -> DocumentOutput:
        """Generates a document summary via LLM or deterministic fallback."""
        if self.model_provider is not None and self.model_provider.chat_model is not None:
            prompt = (
                "You are the Enterprise AI Operations Center (EAOC) Document Agent.\n"
                "Provide an accurate, concise, grounded summary of the following document:\n\n"
                f"{parsed.text[:4000]}\n\nSummary:"
            )
            chat_model = self.model_provider.chat_model
            if hasattr(chat_model, "ainvoke"):
                resp = await chat_model.ainvoke(prompt)
            elif hasattr(chat_model, "invoke"):
                resp = chat_model.invoke(prompt)
            else:
                resp = str(chat_model)
            summary_text = resp.content if hasattr(resp, "content") else str(resp)
        else:
            # Deterministic, provider-free extractive summary for testing
            sentences = [s.strip() for s in re.split(r"[.\n]+", parsed.text) if s.strip()]
            summary_text = (
                f"Summary ({parsed.page_count} page(s), {len(parsed.text)} chars): "
                + ". ".join(sentences[:3])
            )
            if not summary_text.endswith("."):
                summary_text += "."

        return DocumentOutput(
            status="completed",
            document_id=doc_id,
            operation=validated_input.operation,
            format=validated_input.format,
            page_count=parsed.page_count,
            summary=summary_text,
            metadata=parsed.metadata,
            provenance_ref=f"prov-{context.execution_id}",
        )

    async def _handle_extract_fields(
        self,
        parsed: ParsedDocument,
        doc_id: str,
        validated_input: DocumentInput,
        context: AgentExecutionContext,
    ) -> DocumentOutput:
        """Extracts structured fields, placing unfound keys into missing_fields."""
        schema = validated_input.extraction_schema
        if isinstance(schema, list):
            target_fields = [str(f) for f in schema]
        elif isinstance(schema, dict):
            target_fields = [str(k) for k in schema.keys()]
        else:
            target_fields = []

        extracted_fields: Dict[str, Any] = {}
        missing_fields: List[str] = []

        if self.model_provider is not None and self.model_provider.chat_model is not None:
            prompt = (
                "You are the Enterprise AI Operations Center (EAOC) Document Agent.\n"
                f"Extract the following fields from the document text: {target_fields}.\n"
                "Return a strict JSON object mapping each found field to its value. "
                "Omit fields that are not present in the text.\n\n"
                f"Document Content:\n{parsed.text[:4000]}\n\nJSON:"
            )
            chat_model = self.model_provider.chat_model
            if hasattr(chat_model, "ainvoke"):
                resp = await chat_model.ainvoke(prompt)
            elif hasattr(chat_model, "invoke"):
                resp = chat_model.invoke(prompt)
            else:
                resp = str(chat_model)

            raw_json = resp.content if hasattr(resp, "content") else str(resp)
            try:
                # Clean json blocks if wrapped in markdown
                clean_json = re.sub(r"^```(?:json)?\s*", "", raw_json.strip(), flags=re.IGNORECASE)
                clean_json = re.sub(r"\s*```$", "", clean_json.strip())
                parsed_json = json.loads(clean_json)
                if isinstance(parsed_json, dict):
                    extracted_fields = {k: v for k, v in parsed_json.items() if k in target_fields and v is not None}
            except Exception:
                extracted_fields = {}

            missing_fields = [f for f in target_fields if f not in extracted_fields]
        else:
            # Deterministic regex key-value extraction for zero-network testing
            for field in target_fields:
                # Search for patterns like 'field: value' or 'field = value' or 'field - value'
                pattern = rf"(?i)\b{re.escape(field)}\s*[:=\-]\s*([^\r\n]+)"
                match = re.search(pattern, parsed.text)
                if match:
                    extracted_fields[field] = match.group(1).strip()
                else:
                    missing_fields.append(field)

        return DocumentOutput(
            status="completed",
            document_id=doc_id,
            operation=validated_input.operation,
            format=validated_input.format,
            page_count=parsed.page_count,
            extracted_fields=extracted_fields,
            missing_fields=missing_fields,
            metadata=parsed.metadata,
            provenance_ref=f"prov-{context.execution_id}",
        )
