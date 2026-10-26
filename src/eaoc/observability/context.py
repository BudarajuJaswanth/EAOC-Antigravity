"""
EAOC Observability Request & Tracing Context Module
Uses Python contextvars to manage request_id, correlation_id, workflow_id,
agent_id, and tool_id across async call chains.
"""

import uuid
from contextvars import ContextVar
from typing import Dict, Optional

# Context variables for tracing
_request_id_ctx: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
_correlation_id_ctx: ContextVar[Optional[str]] = ContextVar("correlation_id", default=None)
_workflow_id_ctx: ContextVar[Optional[str]] = ContextVar("workflow_id", default=None)
_agent_id_ctx: ContextVar[Optional[str]] = ContextVar("agent_id", default=None)
_tool_id_ctx: ContextVar[Optional[str]] = ContextVar("tool_id", default=None)


def generate_id(prefix: str = "req") -> str:
    """Generates a unique, short tracing ID with a prefix."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def get_request_id() -> Optional[str]:
    """Returns the current request ID from context."""
    return _request_id_ctx.get()


def set_request_id(val: Optional[str]) -> None:
    """Sets the request ID in current context."""
    _request_id_ctx.set(val)


def get_correlation_id() -> Optional[str]:
    """Returns the current correlation ID from context."""
    return _correlation_id_ctx.get()


def set_correlation_id(val: Optional[str]) -> None:
    """Sets the correlation ID in current context."""
    _correlation_id_ctx.set(val)


def get_workflow_id() -> Optional[str]:
    """Returns the current workflow ID from context."""
    return _workflow_id_ctx.get()


def set_workflow_id(val: Optional[str]) -> None:
    """Sets the workflow ID in current context."""
    _workflow_id_ctx.set(val)


def get_agent_id() -> Optional[str]:
    """Returns the current agent ID from context."""
    return _agent_id_ctx.get()


def set_agent_id(val: Optional[str]) -> None:
    """Sets the agent ID in current context."""
    _agent_id_ctx.set(val)


def get_tool_id() -> Optional[str]:
    """Returns the current tool ID from context."""
    return _tool_id_ctx.get()


def set_tool_id(val: Optional[str]) -> None:
    """Sets the tool ID in current context."""
    _tool_id_ctx.set(val)


def clear_tracing_context() -> None:
    """Clears all tracing context variables."""
    _request_id_ctx.set(None)
    _correlation_id_ctx.set(None)
    _workflow_id_ctx.set(None)
    _agent_id_ctx.set(None)
    _tool_id_ctx.set(None)


def get_current_context() -> Dict[str, Optional[str]]:
    """Returns a dictionary containing all active non-None tracing context values."""
    ctx = {}
    if req_id := get_request_id():
        ctx["request_id"] = req_id
    if corr_id := get_correlation_id():
        ctx["correlation_id"] = corr_id
    if wf_id := get_workflow_id():
        ctx["workflow_id"] = wf_id
    if ag_id := get_agent_id():
        ctx["agent_id"] = ag_id
    if tl_id := get_tool_id():
        ctx["tool_id"] = tl_id
    return ctx
