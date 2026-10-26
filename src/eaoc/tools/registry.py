"""
EAOC Enterprise Tool Registry
Instantiable and dependency-injectable registry for managing BaseTool instances,
discovering multi-agent capabilities, and validating tool parameters.
"""

import logging
from typing import Dict, List, Optional, Any

from eaoc.schemas.domain import AgentID
from eaoc.tools.base import BaseTool, ToolMetadata

logger = logging.getLogger("eaoc.tools.registry")


class ToolRegistry:
    """
    Instantiable, dependency-injectable in-memory registry for EAOC enterprise tools.
    Provides storage-free, deterministic registration and capability discovery.
    """

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register_tool(self, tool: BaseTool) -> None:
        """
        Registers a BaseTool instance in the registry.
        Raises ValueError if a tool with the same tool_id is already registered.
        """
        if not isinstance(tool, BaseTool):
            raise TypeError("Only instances of BaseTool can be registered.")

        tool_id = tool.tool_id
        if tool_id in self._tools:
            raise ValueError(f"Tool with ID '{tool_id}' is already registered.")

        self._tools[tool_id] = tool
        agents_str = [a.value if hasattr(a, "value") else str(a) for a in tool.metadata.allowed_agent_ids]
        logger.info(
            f"Registered tool '{tool_id}' (Category: {tool.metadata.category.value}, "
            f"Risk: {tool.metadata.risk_level.value}, Allowed Agents: {agents_str})."
        )

    def get_tool(self, tool_id: str) -> Optional[BaseTool]:
        """Retrieves a registered BaseTool by tool_id, or None if absent."""
        return self._tools.get(tool_id)

    def list_tools(self) -> List[BaseTool]:
        """Returns list of all registered BaseTool instances."""
        return list(self._tools.values())

    def list_tool_metadata(self) -> List[ToolMetadata]:
        """Returns list of ToolMetadata for all registered tools."""
        return [t.metadata for t in self._tools.values()]

    def get_tools_for_agent(self, agent_id: AgentID) -> List[BaseTool]:
        """
        Returns all registered tools where agent_id is included in allowed_agent_ids.
        Supports multi-agent capability mapping.
        """
        return [
            tool for tool in self._tools.values()
            if agent_id in tool.metadata.allowed_agent_ids
        ]

    def validate_parameters(self, tool_id: str, raw_input: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates input parameters for the target tool_id.
        Raises KeyError if tool_id is not registered.
        Raises ValueError if parameter validation fails.
        """
        tool = self.get_tool(tool_id)
        if tool is None:
            raise KeyError(f"Tool with ID '{tool_id}' is not registered.")
        return tool.validate_parameters(raw_input)

    def clear(self) -> None:
        """Clears all registered tools from memory."""
        self._tools.clear()
