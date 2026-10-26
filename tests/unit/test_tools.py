"""
EAOC Unit Test Suite - Enterprise Tool Abstraction & Registry Foundation

Tests required Part 1.10 behavior:
1. Tool registration
2. Duplicate tool registration handling
3. Tool lookup by tool_id
4. Tool listing (tools and metadata)
5. Multi-agent capability discovery (allowed_agent_ids)
6. Unknown tool handling
7. Input parameter schema validation
8. Invalid parameter handling
9. Metadata validation (canonical RiskLevel)
10. Sensitive data redaction in error messages
11. Verification that no external execution occurs
"""

import unittest
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

from eaoc.schemas.domain import AgentID, RiskLevel, TaskStatus, ToolExecutionResult
from eaoc.tools.base import BaseTool, ToolCategory, ToolMetadata
from eaoc.tools.registry import ToolRegistry


# Sample input schemas for testing
class SampleQueryInput(BaseModel):
    query: str = Field(..., description="Query text")
    limit: int = Field(default=10, ge=1, le=100)
    api_key_secret: Optional[str] = Field(default=None, description="Secret key parameter")


# Sample stub tool implementation for testing parameter validation & metadata
class SampleQueryTool(BaseTool):
    """Stub tool for testing metadata and validation without external execution."""
    def __init__(self, metadata: ToolMetadata):
        super().__init__(metadata)
        self.executed_count = 0

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        self.executed_count += 1
        return ToolExecutionResult(
            execution_id="exec-stub-1",
            tool_id=self.tool_id,
            status=TaskStatus.COMPLETED,
            output_data={"result": "stub_ok"},
        )


class TestEnterpriseToolAbstraction(unittest.TestCase):
    """Unit test suite for Part 1.10 tool abstraction and instantiable ToolRegistry."""

    def setUp(self):
        self.registry = ToolRegistry()
        self.meta_a = ToolMetadata(
            tool_id="sql_query_tool",
            name="SQL Query Tool",
            description="Executes read-only SQL queries",
            category=ToolCategory.DATA,
            allowed_agent_ids=[AgentID.SQL_DATA, AgentID.ANALYTICS],
            risk_level=RiskLevel.MEDIUM,
            required_permissions=["sql.read"],
            input_schema=SampleQueryInput,
        )
        self.tool_a = SampleQueryTool(self.meta_a)

        self.meta_b = ToolMetadata(
            tool_id="rag_search_tool",
            name="RAG Search Tool",
            description="Retrieves semantic embeddings",
            category=ToolCategory.DATA,
            allowed_agent_ids=[AgentID.RESEARCH_RAG, AgentID.HR, AgentID.IT_SUPPORT],
            risk_level=RiskLevel.LOW,
            required_permissions=["rag.read"],
            input_schema=SampleQueryInput,
        )
        self.tool_b = SampleQueryTool(self.meta_b)

    def test_1_tool_registration(self):
        """Verify successful registration of BaseTool instances."""
        self.registry.register_tool(self.tool_a)
        self.assertEqual(len(self.registry.list_tools()), 1)
        self.assertIsNotNone(self.registry.get_tool("sql_query_tool"))

    def test_2_duplicate_tool_handling(self):
        """Verify duplicate tool registration raises ValueError."""
        self.registry.register_tool(self.tool_a)
        with self.assertRaises(ValueError) as ctx:
            self.registry.register_tool(self.tool_a)
        self.assertIn("already registered", str(ctx.exception))

    def test_3_lookup_by_tool_id(self):
        """Verify tool lookup returns correct instance by tool_id."""
        self.registry.register_tool(self.tool_a)
        self.registry.register_tool(self.tool_b)

        retrieved_a = self.registry.get_tool("sql_query_tool")
        self.assertEqual(retrieved_a.metadata.name, "SQL Query Tool")

        retrieved_b = self.registry.get_tool("rag_search_tool")
        self.assertEqual(retrieved_b.metadata.name, "RAG Search Tool")

    def test_4_listing_tools_and_metadata(self):
        """Verify list_tools and list_tool_metadata return complete collections."""
        self.registry.register_tool(self.tool_a)
        self.registry.register_tool(self.tool_b)

        tools = self.registry.list_tools()
        self.assertEqual(len(tools), 2)

        metadata_list = self.registry.list_tool_metadata()
        self.assertEqual(len(metadata_list), 2)
        tool_ids = [m.tool_id for m in metadata_list]
        self.assertIn("sql_query_tool", tool_ids)
        self.assertIn("rag_search_tool", tool_ids)

    def test_5_multi_agent_capability_discovery(self):
        """Verify get_tools_for_agent filters tools by allowed_agent_ids."""
        self.registry.register_tool(self.tool_a)  # allowed: SQL_DATA, ANALYTICS
        self.registry.register_tool(self.tool_b)  # allowed: RESEARCH_RAG, HR, IT_SUPPORT

        # ANALYTICS agent should find tool_a only
        analytics_tools = self.registry.get_tools_for_agent(AgentID.ANALYTICS)
        self.assertEqual(len(analytics_tools), 1)
        self.assertEqual(analytics_tools[0].tool_id, "sql_query_tool")

        # HR agent should find tool_b only
        hr_tools = self.registry.get_tools_for_agent(AgentID.HR)
        self.assertEqual(len(hr_tools), 1)
        self.assertEqual(hr_tools[0].tool_id, "rag_search_tool")

        # SQL_DATA agent should find tool_a
        sql_tools = self.registry.get_tools_for_agent(AgentID.SQL_DATA)
        self.assertEqual(len(sql_tools), 1)

        # PLANNER agent should find 0 tools
        planner_tools = self.registry.get_tools_for_agent(AgentID.PLANNER)
        self.assertEqual(len(planner_tools), 0)

    def test_6_unknown_tool_handling(self):
        """Verify get_tool returns None and validate_parameters raises KeyError for unknown tools."""
        self.assertIsNone(self.registry.get_tool("non_existent_tool"))

        with self.assertRaises(KeyError) as ctx:
            self.registry.validate_parameters("non_existent_tool", {"query": "test"})
        self.assertIn("not registered", str(ctx.exception))

    def test_7_input_schema_validation_success(self):
        """Verify valid input parameters validate against Pydantic schema."""
        self.registry.register_tool(self.tool_a)
        valid_input = {"query": "SELECT * FROM users", "limit": 25}

        validated = self.registry.validate_parameters("sql_query_tool", valid_input)
        self.assertEqual(validated["query"], "SELECT * FROM users")
        self.assertEqual(validated["limit"], 25)

    def test_8_invalid_parameter_handling(self):
        """Verify invalid parameters (missing required field or out of bounds) raise ValueError."""
        self.registry.register_tool(self.tool_a)

        # Missing required field 'query'
        with self.assertRaises(ValueError) as ctx1:
            self.registry.validate_parameters("sql_query_tool", {"limit": 10})
        self.assertIn("Parameter validation failed", str(ctx1.exception))

        # Invalid type / out of bounds limit (> 100)
        with self.assertRaises(ValueError) as ctx2:
            self.registry.validate_parameters("sql_query_tool", {"query": "test", "limit": 500})
        self.assertIn("Parameter validation failed", str(ctx2.exception))

    def test_9_metadata_validation(self):
        """Verify ToolMetadata strictly uses domain RiskLevel and multi-agent allowed_agent_ids."""
        meta = ToolMetadata(
            tool_id="test_meta_tool",
            name="Meta Tool",
            description="Test metadata validation",
            category=ToolCategory.SYSTEM,
            allowed_agent_ids=[AgentID.DEVELOPER, AgentID.ORCHESTRATOR],
            risk_level=RiskLevel.HIGH,
            required_permissions=["sys.admin"],
        )
        self.assertEqual(meta.risk_level, RiskLevel.HIGH)
        self.assertEqual(len(meta.allowed_agent_ids), 2)
        self.assertIn(AgentID.DEVELOPER, meta.allowed_agent_ids)

    def test_10_sensitive_data_redaction_in_errors(self):
        """Verify parameter validation failure messages redact sensitive fields."""
        self.registry.register_tool(self.tool_a)
        raw_input = {"query": "test", "limit": "invalid_integer_string", "api_key_secret": "SuperSecretPass123"}

        with self.assertRaises(ValueError) as ctx:
            self.registry.validate_parameters("sql_query_tool", raw_input)

        err_msg = str(ctx.exception)
        self.assertNotIn("SuperSecretPass123", err_msg)
        self.assertTrue("****" in err_msg or "redact" in err_msg.lower() or "validation failed" in err_msg.lower())

    def test_11_no_external_execution_occurs(self):
        """Verify registering and retrieving tools performs zero external execution or side effects."""
        self.registry.register_tool(self.tool_a)
        retrieved = self.registry.get_tool("sql_query_tool")

        # Verify execute() was NOT called
        self.assertEqual(self.tool_a.executed_count, 0)
        self.assertEqual(retrieved.executed_count, 0)


if __name__ == "__main__":
    unittest.main()
