"""
EAOC Enterprise Tool Abstraction & Registry Package
Defines provider-neutral tool interfaces, metadata contracts, and instantiable ToolRegistry.
"""

from eaoc.tools.base import BaseTool, ToolCategory, ToolMetadata
from eaoc.tools.exceptions import (
    ToolError,
    ToolExecutionError,
    ToolInputValidationError,
    ToolNotFoundError,
    ToolOutputSchemaError,
    ToolPermissionDenied,
    ToolTimeoutError,
)
from eaoc.tools.interceptor import (
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TOOL_TIMEOUTS,
    TOOL_TIMEOUT_RETRYABLE,
    ToolInterceptor,
)
from eaoc.tools.registry import ToolRegistry

__all__ = [
    "BaseTool",
    "ToolCategory",
    "ToolMetadata",
    "ToolRegistry",
    "ToolInterceptor",
    "ToolError",
    "ToolNotFoundError",
    "ToolPermissionDenied",
    "ToolInputValidationError",
    "ToolTimeoutError",
    "ToolExecutionError",
    "ToolOutputSchemaError",
    "DEFAULT_TOOL_TIMEOUTS",
    "TOOL_TIMEOUT_RETRYABLE",
    "DEFAULT_TIMEOUT_SECONDS",
]
