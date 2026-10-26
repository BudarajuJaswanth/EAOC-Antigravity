"""
EAOC IT/Support Agent Schemas & Data Contracts
Defines typed Pydantic v2 input, output, filter, and provenance models for AGT-IT.
Enforces strict boundaries: permits only query_tickets, ticket_status,
recommend_resolution, and policy_lookup operations.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ItQueryType(str, Enum):
    """Permitted IT/Support Agent operations per Phase 0 specification."""
    QUERY_TICKETS = "query_tickets"
    TICKET_STATUS = "ticket_status"
    RECOMMEND_RESOLUTION = "recommend_resolution"
    POLICY_LOOKUP = "policy_lookup"


class TicketPriority(str, Enum):
    """Standard IT ticket priority levels."""
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class TicketStatus(str, Enum):
    """Standard IT ticket lifecycle states."""
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    PENDING = "pending"
    RESOLVED = "resolved"
    CLOSED = "closed"


class ItTicketFilter(BaseModel):
    """Validated filter criteria for IT ticket queries."""
    status: Optional[str] = Field(
        default=None,
        description="Filter by ticket status (open, in_progress, pending, resolved, closed)",
    )
    priority: Optional[str] = Field(
        default=None,
        description="Filter by ticket priority (P1, P2, P3, P4)",
    )
    assignee: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Filter by ticket assignee email or identifier",
    )
    category: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Filter by ticket category (e.g. Network, Identity, Hardware)",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum number of ticket records to return",
    )


class ItResolutionStep(BaseModel):
    """Structured resolution step for troubleshooting recommendations."""
    step_number: int = Field(ge=1, description="Sequential step index")
    instruction: str = Field(description="Actionable instruction text")
    target_component: Optional[str] = Field(
        default=None,
        description="Target system or component relevant to this step",
    )


class ItProvenance(BaseModel):
    """
    Mandatory provenance metadata for IT/Support operations.
    Reports the true logical agent ('AGT-IT'), operation, delegation refs,
    data source, and infrastructure redaction flag.
    """
    agent_id: str = Field(
        default="AGT-IT",
        description="Logical agent identifier",
    )
    operation: ItQueryType = Field(
        description="Executed IT operation",
    )
    ticket_id: Optional[str] = Field(
        default=None,
        description="Target ticket identifier when applicable",
    )
    sql_agent_ref: Optional[str] = Field(
        default=None,
        description="Delegated SQL/Data Agent reference when used",
    )
    rag_agent_ref: Optional[str] = Field(
        default=None,
        description="Delegated Research/RAG Agent reference when used",
    )
    infra_redacted: bool = Field(
        description="Flag indicating if infrastructure-sensitive details were masked",
    )
    data_source: str = Field(
        description="Originating data source or provider boundary",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timezone-aware UTC timestamp of query execution",
    )


class ItSupportInput(BaseModel):
    """
    Strict input schema for IT/Support Agent.
    Rejects prohibited write operations, ticket modifications, and command executions.
    """
    it_query: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Natural language query for resolution recommendations or policy lookups",
    )
    operation: ItQueryType = Field(
        default=ItQueryType.QUERY_TICKETS,
        description="Approved IT operation (query_tickets | ticket_status | recommend_resolution | policy_lookup)",
    )
    ticket_id: Optional[str] = Field(
        default=None,
        max_length=100,
        pattern=r"^[A-Za-z0-9_\-]+$",
        description="Specific ticket identifier for status or resolution lookups",
    )
    filters: Optional[ItTicketFilter] = Field(
        default=None,
        description="Filtering parameters for ticket list queries",
    )
    task_id: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Optional workflow correlation task identifier",
    )


class ItSupportOutput(BaseModel):
    """
    Strict output schema for IT/Support Agent results.
    Includes tickets, single ticket details, recommendations, steps, source citations,
    and mandatory AGT-IT provenance.
    """
    operation: ItQueryType = Field(
        description="Executed operation type",
    )
    tickets: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="List of matching ticket records for ticket list queries",
    )
    count: Optional[int] = Field(
        default=None,
        ge=0,
        description="Total count of matching tickets",
    )
    ticket: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Detailed record for a single ticket lookup",
    )
    recommendation: Optional[str] = Field(
        default=None,
        description="Grounded troubleshooting narrative or summary",
    )
    steps: Optional[List[ItResolutionStep]] = Field(
        default=None,
        description="Actionable sequential resolution steps synthesized from knowledge base",
    )
    sources: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Genuine knowledge base article citations and source references",
    )
    policy_excerpts: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Retrieved IT policy excerpts with document citations",
    )
    filters_applied: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Echo of filters applied during ticket query",
    )
    provenance: ItProvenance = Field(
        description="Mandatory AGT-IT execution provenance block",
    )
