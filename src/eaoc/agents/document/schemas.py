"""
EAOC Document Agent - Strict Input and Output Schemas
Defines typed Pydantic contracts for document operations: summarization, field extraction, and full text retrieval.
"""

from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, model_validator


class DocumentFormat(str, Enum):
    """Approved enterprise document formats."""
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"


class DocumentOperation(str, Enum):
    """Approved document operations."""
    SUMMARIZE = "summarize"
    EXTRACT_FIELDS = "extract_fields"
    FULL_TEXT = "full_text"


class DocumentInput(BaseModel):
    """
    Validated input contract for Document Agent invocations.
    Requires either document_id or document_content and validates operation-specific schemas.
    """
    model_config = ConfigDict(extra="forbid")

    document_id: Optional[str] = Field(
        default=None,
        description="Unique document identifier for document store retrieval",
    )
    document_content: Optional[Union[str, bytes]] = Field(
        default=None,
        description="Raw document content as bytes or plain text string",
    )
    format: DocumentFormat = Field(
        default=DocumentFormat.TXT,
        description="Document format: pdf, docx, or txt",
    )
    operation: DocumentOperation = Field(
        ...,
        description="Requested operation: summarize, extract_fields, or full_text",
    )
    extraction_schema: Optional[Union[List[str], Dict[str, Any]]] = Field(
        default=None,
        description="List of field names or schema dict required when operation is extract_fields",
    )
    max_content_size_bytes: int = Field(
        default=10 * 1024 * 1024,  # 10 MB default limit
        ge=1,
        le=50 * 1024 * 1024,  # 50 MB upper bound
        description="Maximum permissible document size in bytes",
    )
    task_id: Optional[str] = Field(
        default=None,
        description="Task identifier for provenance and audit logging",
    )

    @model_validator(mode="after")
    def validate_inputs(self) -> "DocumentInput":
        if not self.document_id and self.document_content is None:
            raise ValueError(
                "Either 'document_id' or 'document_content' must be provided."
            )

        if self.operation == DocumentOperation.EXTRACT_FIELDS and not self.extraction_schema:
            raise ValueError(
                "Operation 'extract_fields' requires an 'extraction_schema' specifying target fields."
            )

        return self


class DocumentOutput(BaseModel):
    """
    Validated output contract for Document Agent responses.
    Captures structured operation outcomes, page counts, metadata, and execution provenance.
    """
    status: str = Field(
        ...,
        description="Execution status: 'completed' on success",
    )
    document_id: str = Field(
        ...,
        description="Resolved document identifier",
    )
    operation: DocumentOperation = Field(
        ...,
        description="Operation executed",
    )
    format: DocumentFormat = Field(
        ...,
        description="Document format processed",
    )
    page_count: int = Field(
        default=1,
        ge=1,
        description="Document page or section count",
    )
    text_content: Optional[str] = Field(
        default=None,
        description="Full extracted text (populated on full_text operation)",
    )
    summary: Optional[str] = Field(
        default=None,
        description="Synthesized document summary (populated on summarize operation)",
    )
    extracted_fields: Dict[str, Any] = Field(
        default_factory=dict,
        description="Dictionary of extracted field names to values (populated on extract_fields)",
    )
    missing_fields: List[str] = Field(
        default_factory=list,
        description="Target fields from extraction_schema that could not be found in the document",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Document metadata such as format, character count, or parse attributes",
    )
    provenance_ref: Optional[str] = Field(
        default=None,
        description="Provenance tracking reference linking to execution context",
    )
