"""
EAOC Tool Exception Hierarchy
Defines canonical structured exceptions for enterprise tool execution and interception.
Conforms to Phase 0 Arch Part B Section 7.2.
"""

from typing import Any, Dict, Optional
from eaoc.observability import redact_sensitive_data


class ToolError(Exception):
    """Base exception for all EAOC tool interception and execution errors."""

    def __init__(
        self,
        message: str,
        code: str = "TOOL_ERROR",
        tool_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        retryable: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.raw_message = message
        self.message = redact_sensitive_data(message)
        self.code = code
        self.tool_id = tool_id
        self.agent_id = agent_id
        self.retryable = retryable
        self.details = details or {}
        super().__init__(self.message)


class ToolNotFoundError(ToolError):
    """Raised when target tool_id cannot be found in ToolRegistry (Step 1)."""

    def __init__(
        self,
        tool_id: str,
        agent_id: Optional[str] = None,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        msg = message or f"Tool with ID '{tool_id}' is not registered in ToolRegistry."
        super().__init__(
            message=msg,
            code="TOOL_NOT_FOUND",
            tool_id=tool_id,
            agent_id=agent_id,
            retryable=False,
            details=details,
        )


class ToolPermissionDenied(ToolError):
    """Raised when calling agent is not authorized by AGT-SEC manifest to execute tool (Step 2)."""

    def __init__(
        self,
        tool_id: str,
        agent_id: Optional[str] = None,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        msg = (
            message
            or f"Permission denied: Agent '{agent_id}' is not authorized to invoke tool '{tool_id}'."
        )
        super().__init__(
            message=msg,
            code="TOOL_PERMISSION_DENIED",
            tool_id=tool_id,
            agent_id=agent_id,
            retryable=False,
            details=details,
        )


class ToolInputValidationError(ToolError):
    """Raised when parameters fail tool input schema validation (Step 3)."""

    def __init__(
        self,
        tool_id: str,
        agent_id: Optional[str] = None,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        msg = message or f"Input parameter validation failed for tool '{tool_id}'."
        super().__init__(
            message=msg,
            code="TOOL_INPUT_INVALID",
            tool_id=tool_id,
            agent_id=agent_id,
            retryable=False,
            details=details,
        )


class ToolTimeoutError(ToolError):
    """Raised when tool execution exceeds configured timeout limit (Step 5)."""

    def __init__(
        self,
        tool_id: str,
        timeout_seconds: float,
        agent_id: Optional[str] = None,
        retryable: bool = False,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        msg = message or f"Tool '{tool_id}' execution timed out after {timeout_seconds}s."
        super().__init__(
            message=msg,
            code="TOOL_TIMEOUT",
            tool_id=tool_id,
            agent_id=agent_id,
            retryable=retryable,
            details=details,
        )


class ToolExecutionError(ToolError):
    """Raised when tool execution fails with an internal runtime exception (Step 5)."""

    def __init__(
        self,
        tool_id: str,
        message: str,
        agent_id: Optional[str] = None,
        retryable: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            message=message,
            code="TOOL_EXECUTION_ERROR",
            tool_id=tool_id,
            agent_id=agent_id,
            retryable=retryable,
            details=details,
        )


class ToolOutputSchemaError(ToolError):
    """Raised when tool execution result fails output schema validation (Step 6)."""

    def __init__(
        self,
        tool_id: str,
        agent_id: Optional[str] = None,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        msg = message or f"Output schema validation failed for tool '{tool_id}'."
        super().__init__(
            message=msg,
            code="TOOL_OUTPUT_SCHEMA_ERROR",
            tool_id=tool_id,
            agent_id=agent_id,
            retryable=False,
            details=details,
        )
