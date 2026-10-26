"""
EAOC Package Module: research_rag
Provides the Research/RAG specialized agent, retrieval boundaries, and typed schemas.
"""

from eaoc.agents.research_rag.agent import ResearchRagAgent
from eaoc.agents.research_rag.retriever import (
    BaseRetriever,
    DocumentChunk,
    InMemoryRetriever,
)
from eaoc.agents.research_rag.schemas import (
    ResearchRagInput,
    ResearchRagOutput,
    SourceCitation,
)

__all__ = [
    "ResearchRagAgent",
    "BaseRetriever",
    "DocumentChunk",
    "InMemoryRetriever",
    "ResearchRagInput",
    "ResearchRagOutput",
    "SourceCitation",
]
