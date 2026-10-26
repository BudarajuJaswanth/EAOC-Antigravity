"""
EAOC Tool Base Abstraction & Metadata Definition
Defines provider-neutral tool interfaces, metadata contracts, and parameter validation.
Uses canonical domain RiskLevel and multi-agent allowed_agent_ids.
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eaoc.observability import redact_sensitive_data
from eaoc.schemas.domain import AgentID, RiskLevel, TaskStatus, ToolExecutionResult


class ToolCategory(str, Enum):
    """Categorization for enterprise tools."""
    DATA = "DATA"
    SYSTEM = "SYSTEM"
    ANALYTICS = "ANALYTICS"
    COMMUNICATION = "COMMUNICATION"
    UTILITY = "UTILITY"


class ToolMetadata(BaseModel):
    """
    Declarative metadata contract for an enterprise tool.
    Contains multi-agent permission mapping (allowed_agent_ids) and domain RiskLevel.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool_id: str = Field(..., description="Unique tool identifier")
    name: str = Field(..., description="Human-readable tool name")
    description: str = Field(..., description="Tool capability description")
    category: ToolCategory = Field(default=ToolCategory.UTILITY)
    allowed_agent_ids: List[AgentID] = Field(
        default_factory=list,
        description="List of logical AgentIDs authorized to invoke this tool"
    )
    risk_level: RiskLevel = Field(
        default=RiskLevel.LOW,
        description="Canonical RiskLevel classification (LOW, MEDIUM, HIGH, CRITICAL)"
    )
    required_permissions: List[str] = Field(
        default_factory=list,
        description="Declarative metadata list of required permission strings (no enforcement in Part 1.10)"
    )
    input_schema: Optional[Type[BaseModel]] = Field(
        default=None,
        description="Optional Pydantic model class for input parameter validation"
    )
    output_schema: Optional[Type[BaseModel]] = Field(
        default=None,
        description="Optional Pydantic model class for output payload structure"
    )


class BaseTool(ABC):
    """
    Abstract base class for all EAOC enterprise tools.
    Downstream tools implement execute() in future parts.
    """

    def __init__(self, metadata: ToolMetadata):
        self.metadata = metadata

    @property
    def tool_id(self) -> str:
        """Convenience property for tool identifier."""
        return self.metadata.tool_id

    def validate_parameters(self, raw_input: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates raw input parameter dictionary against metadata.input_schema.
        If input_schema is None, returns raw_input directly.
        Raises ValueError with sanitized error message on validation failure.
        """
        if self.metadata.input_schema is None:
            return raw_input or {}

        try:
            validated = self.metadata.input_schema.model_validate(raw_input or {})
            return validated.model_dump()
        except ValidationError as exc:
            safe_err = redact_sensitive_data(str(exc))
            raise ValueError(
                f"Parameter validation failed for tool '{self.tool_id}': {safe_err}"
            ) from exc

    @abstractmethod
    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        """
        Abstract tool execution method.
        Actual execution logic is implemented in concrete tool classes in future parts.
        """
        pass
