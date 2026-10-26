"""
EAOC Research/RAG Agent - Strict Input and Output Schemas
Defines typed Pydantic contracts for query requests, evidence citations, and grounded responses.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class ResearchRagInput(BaseModel):
    """
    Validated input contract for Research/RAG agent invocations.
    """
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Natural language search or research query",
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Maximum number of document chunks to retrieve",
    )
    namespace: Optional[str] = Field(
        default=None,
        description="Optional knowledge base namespace or collection to restrict search",
    )
    access_level_filter: Optional[str] = Field(
        default=None,
        description="Maximum access level permitted for chunk retrieval",
    )
    task_id: Optional[str] = Field(
        default=None,
        description="Task identifier for provenance and audit logging",
    )


class SourceCitation(BaseModel):
    """
    Structured provenance and citation contract for a single retrieved piece of evidence.
    Guarantees no fabricated citations or hallucinated source references.
    """
    model_config = ConfigDict(frozen=True)

    document_id: str = Field(..., description="Unique source document identifier")
    chunk_id: str = Field(..., description="Unique chunk identifier within document")
    score: float = Field(default=0.0, ge=0.0, le=1.0, description="Similarity or relevance score")
    excerpt: str = Field(..., description="Verbatim textual excerpt from the source chunk")
    source_title: Optional[str] = Field(default=None, description="Human-readable title of source document")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Metadata tags, namespace, access level, etc.")


class ResearchRagOutput(BaseModel):
    """
    Validated output contract for Research/RAG agent responses.
    Explicitly represents both successful grounded synthesis and empty retrieval ('no_results').
    """
    status: str = Field(
        ...,
        description="Execution status: 'completed' when evidence found, or 'no_results' when empty",
    )
    query: str = Field(
        ...,
        description="Echo of the input query for traceability",
    )
    answer: Optional[str] = Field(
        default=None,
        description="Synthesized grounded answer, or None if no sources were found",
    )
    sources: List[SourceCitation] = Field(
        default_factory=list,
        description="List of verified source citations backing the synthesized answer",
    )
    retrieval_count: int = Field(
        default=0,
        ge=0,
        description="Total number of relevant sources retrieved",
    )
    model_used: Optional[str] = Field(
        default=None,
        description="Identifier of the model used for synthesis, or None",
    )
    provenance_ref: Optional[str] = Field(
        default=None,
        description="Correlation or provenance identifier linking to execution context",
    )
