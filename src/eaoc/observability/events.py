"""
EAOC Observability Diagnostic Event Representation
Defines diagnostic event schemas and standard event constants.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

# Standard Event Constants
EVENT_REQUEST_STARTED = "request_started"
EVENT_REQUEST_COMPLETED = "request_completed"
EVENT_SERVICE_INITIALIZED = "service_initialized"
EVENT_SERVICE_FAILED = "service_failed"
EVENT_DATABASE_CHECK = "database_health_check"
EVENT_READINESS_CHECK = "readiness_check"


class DiagnosticEvent(BaseModel):
    """
    Standard diagnostic event model.
    Used for structured event tracing across infrastructure services.
    """

    event_name: str = Field(..., description="Name of the diagnostic event")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 UTC timestamp",
    )
    service: str = Field(default="eaoc-core", description="Originating service name")

    # Optional Tracing Identifiers
    request_id: Optional[str] = None
    correlation_id: Optional[str] = None
    workflow_id: Optional[str] = None
    agent_id: Optional[str] = None
    tool_id: Optional[str] = None

    # Performance & Diagnostics
    duration_ms: Optional[float] = None
    error_type: Optional[str] = None
    error_code: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
