"""
EAOC Real-Time Workflow Streaming Event Schemas (Part 2.17)
Defines canonical event types, payload models, and collision-resistant event ID
formatting and parsing rules.
"""

from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

# Canonical event ID regular expression
# Format: evt:v1:{workflow_id}:e{epoch}:s{step_index}:{event_type}:sq{seq}:{discriminator}
EVENT_ID_PATTERN = re.compile(
    r"^evt:v1:(?P<wf>wf-[a-zA-Z0-9_-]{8,64}):e(?P<epoch>\d+):s(?P<step>-?\d+):(?P<type>[a-z_]+):sq(?P<seq>\d+|syn):(?P<disc>[a-zA-Z0-9_-]+)$"
)


class StreamEventType(str, Enum):
    """Supported SSE workflow stream event types."""
    WORKFLOW_STARTED = "workflow_started"
    STEP_STARTED = "step_started"
    TOOL_INVOKED = "tool_invoked"
    WAITING_APPROVAL = "waiting_approval"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    WORKFLOW_COMPLETED = "workflow_completed"
    WORKFLOW_FAILED = "workflow_failed"
    KEEPALIVE = "keepalive"
    ERROR = "error"


class StepEventPayload(BaseModel):
    """Payload for step_started and step_completed events."""
    description: str
    assigned_agent_id: str
    dependencies: List[int] = Field(default_factory=list)
    risk_level: Optional[str] = None
    duration_ms: Optional[float] = None
    status: Optional[str] = None


class ToolEventPayload(BaseModel):
    """Payload for tool_invoked events."""
    tool_id: str
    tool_call_id: str
    action: str  # "attempted", "completed", "failed"
    duration_ms: Optional[float] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)
    output_summary: Optional[str] = None
    error: Optional[str] = None


class ApprovalEventPayload(BaseModel):
    """Payload for waiting_approval events."""
    approval_id: str
    step_index: int
    requested_action: str
    risk_level: str
    details: Dict[str, Any] = Field(default_factory=dict)
    expires_at: Optional[str] = None


class WorkflowTerminalPayload(BaseModel):
    """Payload for workflow_completed and workflow_failed events."""
    workflow_id: str
    status: str
    step_count: Optional[int] = None
    failure_class: Optional[str] = None
    error_type: Optional[str] = None
    reason: Optional[str] = None


class KeepalivePayload(BaseModel):
    """Payload for periodic heartbeat keepalive events."""
    server_time: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class WorkflowStreamEvent(BaseModel):
    """
    Canonical envelope for all Server-Sent Events emitted by EAOC.
    """
    event_id: str = Field(..., description="Canonical monotonic and collision-resistant event ID")
    event_type: StreamEventType = Field(..., description="Standardized event type")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 UTC timestamp"
    )
    workflow_id: str = Field(..., description="Target workflow identifier")
    tenant_id: str = Field(..., description="Authenticated tenant identifier")
    epoch: int = Field(default=1, description="Durable execution lease epoch")
    step_index: int = Field(default=-1, description="Step index or -1 for workflow-level events")
    seq: Optional[int] = Field(default=None, description="Monotonic sequence counter within lease epoch")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Event-specific payload (redacted)")

    def to_sse_frame(self) -> str:
        """
        Serializes this event into standard EventSource wire format:
        event: <event_type>\nid: <event_id>\ndata: <json>\n\n
        """
        import json
        json_data = json.dumps(self.payload, separators=(",", ":"))
        return f"event: {self.event_type.value}\nid: {self.event_id}\ndata: {json_data}\n\n"


def format_event_id(
    workflow_id: str,
    epoch: int,
    step_index: int,
    event_type: StreamEventType,
    seq: Optional[int] = None,
    discriminator: str = "main",
) -> str:
    """
    Constructs a canonical event ID conforming to the approved specification:
    evt:v1:{workflow_id}:e{epoch}:s{step_index}:{event_type}:sq{seq}:{discriminator}
    """
    seq_str = str(seq) if seq is not None else "syn"
    return f"evt:v1:{workflow_id}:e{epoch}:s{step_index}:{event_type.value}:sq{seq_str}:{discriminator}"


def parse_event_id(raw_id: str) -> Optional[Dict[str, Any]]:
    """
    Parses and validates a canonical event ID.
    Returns parsed dictionary or None if invalid or malformed.
    """
    if not raw_id:
        return None
    match = EVENT_ID_PATTERN.match(raw_id.strip())
    if not match:
        return None
    data = match.groupdict()
    seq_val = None if data["seq"] == "syn" else int(data["seq"])
    return {
        "workflow_id": data["wf"],
        "epoch": int(data["epoch"]),
        "step_index": int(data["step"]),
        "event_type": data["type"],
        "seq": seq_val,
        "is_synthesized": data["seq"] == "syn",
        "discriminator": data["disc"],
    }


def is_artificial_error_cursor(raw_id: str) -> bool:
    """
    Detects whether an event ID is an artificial SSE error or reconnect frame
    (which should never be used to advance execution milestones).
    """
    if not raw_id:
        return False
    r = raw_id.strip()
    return r.startswith("err:") or r.startswith("rec:") or ":error:" in r
