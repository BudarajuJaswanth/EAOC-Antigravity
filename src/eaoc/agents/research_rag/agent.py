"""
EAOC Research/RAG Agent Implementation
Provides grounded, citation-backed answers to natural language queries by performing
semantic retrieval against an abstract retrieval boundary and synthesizing answers
grounded strictly on verified evidence chunks.
"""

from typing import Optional

from eaoc.agents.base import BaseAgent
from eaoc.agents.research_rag.retriever import BaseRetriever
from eaoc.agents.research_rag.schemas import (
    ResearchRagInput,
    ResearchRagOutput,
    SourceCitation,
)
from eaoc.models.base import ModelProviderWrapper
from eaoc.observability import get_logger
from eaoc.schemas.domain import AgentExecutionContext, AgentID

logger = get_logger("eaoc.agents.research_rag")

RAG_SYSTEM_PROMPT = (
    "You are the Enterprise AI Operations Center (EAOC) Research/RAG Agent.\n"
    "Your sole responsibility is to provide accurate, grounded answers based EXCLUSIVELY on the provided source excerpts.\n"
    "Rules:\n"
    "1. Answer using ONLY information found in the sources below.\n"
    "2. Do NOT invent, assume, or extrapolate facts, citations, or document identifiers.\n"
    "3. If the sources do not provide enough information to answer the question, state so clearly.\n"
    "4. Cite sources using their document_id and chunk_id."
)


class ResearchRagAgent(BaseAgent[ResearchRagInput, ResearchRagOutput]):
    """
    Research / RAG specialized agent.
    Performs retrieval through BaseRetriever and synthesizes citation-backed answers.
    Adheres strictly to the BaseAgent generic contract.
    """

    agent_id: AgentID = AgentID.RESEARCH_RAG
    input_schema = ResearchRagInput
    output_schema = ResearchRagOutput

    def __init__(
        self,
        retriever: Optional[BaseRetriever] = None,
        model_provider: Optional[ModelProviderWrapper] = None,
        default_timeout_seconds: Optional[float] = 30.0,
        **kwargs,
    ):
        super().__init__(
            agent_id=AgentID.RESEARCH_RAG,
            input_schema=ResearchRagInput,
            output_schema=ResearchRagOutput,
            default_timeout_seconds=default_timeout_seconds,
            **kwargs,
        )
        self.retriever = retriever
        self.model_provider = model_provider

    async def _execute(
        self, validated_input: ResearchRagInput, context: AgentExecutionContext
    ) -> ResearchRagOutput:
        """
        Executes semantic retrieval and grounded answer synthesis.
        Guarantees:
        1. Retrieval through BaseRetriever without live database coupling.
        2. Explicit handling of empty retrieval results ('no_results').
        3. Strict provenance recording linking evidence to execution context.
        4. Zero model calls if model_provider is not configured.
        """
        if self.retriever is None:
            raise RuntimeError(
                f"Retriever is not configured for agent '{self.agent_id.value}'."
            )

        logger.info(
            f"Executing Research/RAG retrieval for query '{validated_input.query[:50]}...'",
            extra={
                "event_name": "rag_query_received",
                "execution_id": context.execution_id,
                "top_k": validated_input.top_k,
                "namespace": validated_input.namespace,
            },
        )

        chunks = await self.retriever.retrieve(
            query=validated_input.query,
            top_k=validated_input.top_k,
            namespace=validated_input.namespace,
            access_level=validated_input.access_level_filter,
        )

        # Explicit handling of empty retrieval results
        if not chunks:
            logger.info(
                f"Research/RAG found no relevant documents for query.",
                extra={
                    "event_name": "rag_no_results",
                    "execution_id": context.execution_id,
                    "namespace": validated_input.namespace,
                },
            )
            return ResearchRagOutput(
                status="no_results",
                query=validated_input.query,
                answer=None,
                sources=[],
                retrieval_count=0,
                model_used=None,
                provenance_ref=f"prov-{context.execution_id}",
            )

        # Map retrieved chunks to verifiable SourceCitation models
        sources = [
            SourceCitation(
                document_id=c.document_id,
                chunk_id=c.chunk_id,
                score=c.score,
                excerpt=c.content,
                source_title=c.source_title,
                metadata=c.metadata,
            )
            for c in chunks
        ]

        # Synthesize grounded answer
        if self.model_provider is not None and self.model_provider.chat_model is not None:
            evidence_text = "\n\n".join(
                f"Source [{s.document_id}:{s.chunk_id}] (Title: {s.source_title or 'Untitled'}):\n{s.excerpt}"
                for s in sources
            )
            prompt = (
                f"{RAG_SYSTEM_PROMPT}\n\n"
                f"User Query:\n{validated_input.query}\n\n"
                f"Evidence Sources:\n{evidence_text}\n\n"
                f"Answer:"
            )

            chat_model = self.model_provider.chat_model
            if hasattr(chat_model, "ainvoke"):
                resp = await chat_model.ainvoke(prompt)
            elif hasattr(chat_model, "invoke"):
                resp = chat_model.invoke(prompt)
            else:
                resp = str(chat_model)

            answer = resp.content if hasattr(resp, "content") else str(resp)
            model_used = self.model_provider.model_name
        else:
            # Deterministic, provider-free evidence synthesis for zero-network testing
            answer = (
                f"Synthesized research summary based on {len(sources)} source(s):\n"
                + "\n".join(f"- [{s.document_id}:{s.chunk_id}] {s.excerpt}" for s in sources)
            )
            model_used = "deterministic-evidence-synthesis"

        logger.info(
            f"Research/RAG synthesis complete with {len(sources)} source(s).",
            extra={
                "event_name": "rag_synthesis_completed",
                "execution_id": context.execution_id,
                "retrieval_count": len(sources),
                "model_used": model_used,
            },
        )

        return ResearchRagOutput(
            status="completed",
            query=validated_input.query,
            answer=answer,
            sources=sources,
            retrieval_count=len(sources),
            model_used=model_used,
            provenance_ref=f"prov-{context.execution_id}",
        )
