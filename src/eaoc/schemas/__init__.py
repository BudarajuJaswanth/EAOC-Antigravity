"""
EAOC Schemas Package
Export infrastructure health, system info, and shared domain models.
"""

from eaoc.schemas.auth import AuthenticatedUser
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ErrorInfo,
    ExecutionMetadata,
    RequestContext,
    RiskLevel,
    Task,
    TaskStatus,
    ToolExecutionContext,
    ToolExecutionResult,
    ValidationResult,
    WorkflowState,
    WorkflowStatus,
)
from eaoc.schemas.health import (
    DatabaseHealth,
    HealthStatus,
    LivenessResponse,
    ModelProviderHealth,
    ModelProviderInfo,
    OverallHealthResponse,
    ProviderCategory,
    ReadinessResponse,
    RedisHealth,
    SystemInfoResponse,
)
from eaoc.schemas.events import (
    ApprovalEventPayload,
    KeepalivePayload,
    StepEventPayload,
    StreamEventType,
    ToolEventPayload,
    WorkflowStreamEvent,
    WorkflowTerminalPayload,
    format_event_id,
    is_artificial_error_cursor,
    parse_event_id,
)

__all__ = [
    # Health Schemas
    "HealthStatus",
    "ProviderCategory",
    "DatabaseHealth",
    "RedisHealth",
    "ModelProviderInfo",
    "ModelProviderHealth",
    "SystemInfoResponse",
    "LivenessResponse",
    "ReadinessResponse",
    "OverallHealthResponse",
    # Domain Models & Enums
    "AuthenticatedUser",
    "AgentID",
    "WorkflowStatus",
    "TaskStatus",
    "RiskLevel",
    "ApprovalStatus",
    "RequestContext",
    "ErrorInfo",
    "Task",
    "AgentExecutionContext",
    "AgentExecutionResult",
    "ToolExecutionContext",
    "ToolExecutionResult",
    "ValidationResult",
    "ApprovalRequest",
    "ApprovalDecision",
    "ExecutionMetadata",
    "WorkflowState",
]
