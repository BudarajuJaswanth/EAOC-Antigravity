"""
EAOC Research/RAG Agent - Retrieval Interface & Dependency Boundary
Defines abstract BaseRetriever and DocumentChunk data contracts to isolate semantic retrieval
from concrete vector databases (e.g. pgvector), enabling deterministic testing and provenance tracking.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class DocumentChunk(BaseModel):
    """
    Representation of a retrieved document chunk from knowledge storage.
    Strictly preserves citation fields and metadata without fabrication.
    """
    model_config = ConfigDict(frozen=True)

    document_id: str = Field(..., description="Unique document identifier")
    chunk_id: str = Field(..., description="Unique chunk identifier within document")
    content: str = Field(..., description="Raw text excerpt/content of the chunk")
    score: float = Field(default=0.0, ge=0.0, le=1.0, description="Similarity or relevance score")
    source_title: Optional[str] = Field(default=None, description="Human-readable title of source document")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Metadata tags, namespace, access level, etc.")


class BaseRetriever(ABC):
    """
    Dependency boundary / abstraction for semantic and vector retrieval.
    Decouples ResearchRagAgent from concrete storage implementations.
    """

    @abstractmethod
    async def retrieve(
        self,
        query: str,
        top_k: int = 5,
        namespace: Optional[str] = None,
        access_level: Optional[str] = None,
    ) -> List[DocumentChunk]:
        """
        Retrieves top_k relevant DocumentChunks matching the query and access criteria.
        Must be implemented by concrete vector-store adapters or test fakes.
        """
        pass


class InMemoryRetriever(BaseRetriever):
    """
    In-memory retriever implementation for deterministic testing and local capability verification.
    Filters candidate chunks by query keywords, namespace, and top_k.
    """

    def __init__(self, chunks: Optional[List[DocumentChunk]] = None):
        self._chunks: List[DocumentChunk] = list(chunks) if chunks else []

    def add_chunk(self, chunk: DocumentChunk) -> None:
        self._chunks.append(chunk)

    async def retrieve(
        self,
        query: str,
        top_k: int = 5,
        namespace: Optional[str] = None,
        access_level: Optional[str] = None,
    ) -> List[DocumentChunk]:
        results = self._chunks

        if namespace is not None:
            results = [c for c in results if c.metadata.get("namespace") == namespace]

        if access_level is not None:
            results = [c for c in results if c.metadata.get("access_level") == access_level]

        # Simple keyword presence filter if query keywords exist
        query_lower = query.lower()
        matched = [
            c for c in results
            if any(term in c.content.lower() for term in query_lower.split())
        ]

        # If no keyword matches, fallback to namespace-filtered results
        selected = matched if matched else results
        return selected[:top_k]
