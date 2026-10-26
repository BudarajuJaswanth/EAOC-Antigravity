"""
EAOC Unit Test Suite - Tool Interceptor & Secure Execution Pipeline (Part 2.13)
Verifies:
1. Unknown tool handling (Step 1 -> TOOL_NOT_FOUND, non-retryable, no execution)
2. Manifest permission denial (Step 2 -> TOOL_PERMISSION_DENIED audit, non-retryable, no execution)
3. Write-operation denial for read-only agent using real SecurityPolicyEngine
4. Forbidden schema denial using real SecurityPolicyEngine
5. Policy engine exception / unavailable dependency -> fail closed
6. Valid input and output -> typed success ToolExecutionResult
7. Invalid input parameter validation failure (Step 3 -> TOOL_INPUT_INVALID audit, no execution)
8. Unique UUID v4 tool_call_id generated across distinct invocations
9. Timeout enforcement (Step 5 -> TOOL_TIMEOUT, Section 7.4 retryable classification)
10. Tool runtime execution exception (Step 5 -> safe redacted diagnostics)
11. Invalid output schema validation failure (Step 6 -> TOOL_OUTPUT_SCHEMA_ERROR, TaskStatus.FAILED)
12. Attempt and completion audit events (TOOL_CALL_ATTEMPTED, TOOL_CALL_COMPLETED with duration_ms)
13. Audit attempt failure behavior (fail-closed before tool execution)
14. Compatibility with ToolExecutionContext and synchronous call_sync() wrapper
"""

import asyncio
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock
import uuid

from pydantic import BaseModel, Field

from eaoc.agents.security_policy.engine import SecurityPolicyEngine
from eaoc.agents.security_policy.manifest_store import ManifestStore
from eaoc.schemas.domain import (
    AgentID,
    RiskLevel,
    TaskStatus,
    ToolExecutionContext,
    ToolExecutionResult,
)
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
from eaoc.tools.interceptor import ToolInterceptor
from eaoc.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Test Schemas
# ---------------------------------------------------------------------------
class QueryInput(BaseModel):
    query: str = Field(..., description="Query text")
    schema_name: Optional[str] = Field(default=None, description="Target database schema")
    operation: Optional[str] = Field(default="read", description="Operation type")
    limit: int = Field(default=10, ge=1, le=100)


class QueryOutput(BaseModel):
    query: str
    row_count: int
    data: List[Dict[str, Any]]


class StrictDocOutput(BaseModel):
    doc_id: str
    content: str


# ---------------------------------------------------------------------------
# Stub Tools
# ---------------------------------------------------------------------------
class StubQueryTool(BaseTool):
    """Deterministic stub tool returning valid QueryOutput."""

    def __init__(self, metadata: ToolMetadata):
        super().__init__(metadata)
        self.call_count = 0

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        self.call_count += 1
        query_val = parameters.get("query", "")
        return ToolExecutionResult(
            execution_id="stub-exec",
            tool_id=self.tool_id,
            status=TaskStatus.COMPLETED,
            output_data={
                "query": query_val,
                "row_count": 1,
                "data": [{"id": 101, "item": "Alpha"}],
            },
        )


class StubSlowTool(BaseTool):
    """Stub tool simulating long-running operation for timeout testing."""

    def __init__(self, metadata: ToolMetadata):
        super().__init__(metadata)
        self.call_count = 0

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        self.call_count += 1
        sleep_sec = float(parameters.get("sleep_sec", 0.3))
        time.sleep(sleep_sec)
        return ToolExecutionResult(
            execution_id="slow-exec",
            tool_id=self.tool_id,
            status=TaskStatus.COMPLETED,
            output_data={"slept": sleep_sec},
        )


class StubFailingTool(BaseTool):
    """Stub tool simulating unhandled internal runtime exception."""

    def __init__(self, metadata: ToolMetadata):
        super().__init__(metadata)
        self.call_count = 0

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        self.call_count += 1
        secret = parameters.get("secret", "SuperSecretPass1234")
        raise RuntimeError(
            f"DB connection failed: postgresql://admin:{secret}@internal.db:5432/finance"
        )


class StubInvalidOutputTool(BaseTool):
    """Stub tool returning output that violates output_schema."""

    def __init__(self, metadata: ToolMetadata):
        super().__init__(metadata)
        self.call_count = 0

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        self.call_count += 1
        # Returns dict missing required 'content' field in StrictDocOutput
        return ToolExecutionResult(
            execution_id="invalid-out",
            tool_id=self.tool_id,
            status=TaskStatus.COMPLETED,
            output_data={"doc_id": "doc-001", "unexpected_field": True},
        )


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------
class TestToolInterceptor(unittest.IsolatedAsyncioTestCase):
    """Unit test suite verifying Part 2.13 ToolInterceptor and execution pipeline."""

    def setUp(self):
        self.registry = ToolRegistry()
        self.policy_engine = SecurityPolicyEngine()
        self.audit_events: List[Tuple[str, Dict[str, Any]]] = []

        def audit_capture(event_name: str, payload: Dict[str, Any]) -> None:
            self.audit_events.append((event_name, payload))

        self.interceptor = ToolInterceptor(
            tool_registry=self.registry,
            policy_engine=self.policy_engine,
            audit_callback=audit_capture,
        )

        # Register tools
        # 1. Query tool permitted for SQL_DATA and ANALYTICS
        self.query_tool = StubQueryTool(
            ToolMetadata(
                tool_id="sql_query_tool",
                name="SQL Query Tool",
                description="Executes SQL queries",
                category=ToolCategory.DATA,
                allowed_agent_ids=[AgentID.SQL_DATA, AgentID.ANALYTICS],
                risk_level=RiskLevel.MEDIUM,
                input_schema=QueryInput,
                output_schema=QueryOutput,
            )
        )
        self.registry.register_tool(self.query_tool)

        # 2. Sandbox execution tool permitted for DEVELOPER
        self.slow_tool = StubSlowTool(
            ToolMetadata(
                tool_id="sandbox_execution",
                name="Sandbox Execution",
                description="Executes sandbox tests",
                category=ToolCategory.SYSTEM,
                allowed_agent_ids=[AgentID.DEVELOPER],
                risk_level=RiskLevel.HIGH,
            )
        )
        self.registry.register_tool(self.slow_tool)

        # 3. Failing tool permitted for SQL_DATA
        self.failing_tool = StubFailingTool(
            ToolMetadata(
                tool_id="postgresql_read",
                name="PostgreSQL Read",
                description="Reads PostgreSQL tables",
                category=ToolCategory.DATA,
                allowed_agent_ids=[AgentID.SQL_DATA],
                risk_level=RiskLevel.LOW,
            )
        )
        self.registry.register_tool(self.failing_tool)

        # 4. Invalid output tool permitted for DOCUMENT
        self.invalid_out_tool = StubInvalidOutputTool(
            ToolMetadata(
                tool_id="document_retrieve",
                name="Document Retrieve",
                description="Retrieves enterprise documents",
                category=ToolCategory.DATA,
                allowed_agent_ids=[AgentID.DOCUMENT],
                risk_level=RiskLevel.LOW,
                output_schema=StrictDocOutput,
            )
        )
        self.registry.register_tool(self.invalid_out_tool)

    async def test_1_unknown_tool_handling(self):
        """Step 1: Unknown tool must fail closed with TOOL_NOT_FOUND, non-retryable, and zero executions."""
        res = await self.interceptor.call(
            agent_id=AgentID.SQL_DATA,
            tool_id="non_existent_tool",
            parameters={"query": "SELECT 1"},
        )
        self.assertEqual(res.status, TaskStatus.FAILED)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.code, "TOOL_NOT_FOUND")
        self.assertEqual(res.error.error_type, "ToolNotFoundError")
        self.assertFalse(res.error.retryable)
        self.assertEqual(self.query_tool.call_count, 0)

    async def test_2_manifest_permission_denial(self):
        """Step 2: Manifest check denial must emit TOOL_PERMISSION_DENIED, fail closed, and not execute tool."""
        # HR agent is not permitted to call sql_query_tool
        res = await self.interceptor.call(
            agent_id=AgentID.HR,
            tool_id="sql_query_tool",
            parameters={"query": "SELECT * FROM employees"},
        )
        self.assertEqual(res.status, TaskStatus.FAILED)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.code, "TOOL_PERMISSION_DENIED")
        self.assertEqual(res.error.error_type, "ToolPermissionDenied")
        self.assertFalse(res.error.retryable)
        self.assertEqual(self.query_tool.call_count, 0)

        # Verify TOOL_PERMISSION_DENIED audit event was captured
        denial_events = [e for e in self.audit_events if e[0] == "TOOL_PERMISSION_DENIED"]
        self.assertEqual(len(denial_events), 1)
        self.assertEqual(denial_events[0][1]["tool_id"], "sql_query_tool")
        self.assertEqual(denial_events[0][1]["agent_id"], "hr")

    async def test_3_write_operation_denial_for_read_only_agent(self):
        """Step 2: Read-only agent (SQL_DATA) attempting a write operation must be blocked fail-closed."""
        res = await self.interceptor.call(
            agent_id=AgentID.SQL_DATA,
            tool_id="sql_query_tool",
            parameters={"query": "INSERT INTO logs VALUES(1)", "operation": "insert"},
        )
        self.assertEqual(res.status, TaskStatus.FAILED)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.code, "TOOL_PERMISSION_DENIED")
        self.assertIn("write", res.error.message.lower())
        self.assertFalse(res.error.retryable)
        self.assertEqual(self.query_tool.call_count, 0)

    async def test_4_forbidden_schema_denial(self):
        """Step 2: Query referencing an unpermitted schema (confidential_hr) must be denied."""
        # SQL_DATA manifest permits only 'analytics' schema
        res = await self.interceptor.call(
            agent_id=AgentID.SQL_DATA,
            tool_id="sql_query_tool",
            parameters={"query": "SELECT * FROM salaries", "schema_name": "confidential_hr"},
        )
        self.assertEqual(res.status, TaskStatus.FAILED)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.code, "TOOL_PERMISSION_DENIED")
        self.assertIn("schema", res.error.message.lower())
        self.assertFalse(res.error.retryable)
        self.assertEqual(self.query_tool.call_count, 0)

    async def test_5_policy_engine_exception_fails_closed(self):
        """Step 2: Policy engine exception or unavailable dependency must fail closed and block execution."""
        mock_engine = MagicMock()
        mock_engine.evaluate_tool_permission.side_effect = RuntimeError("Policy service connection broken")

        broken_interceptor = ToolInterceptor(
            tool_registry=self.registry,
            policy_engine=mock_engine,
        )

        res = await broken_interceptor.call(
            agent_id=AgentID.SQL_DATA,
            tool_id="sql_query_tool",
            parameters={"query": "SELECT 1"},
        )
        self.assertEqual(res.status, TaskStatus.FAILED)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.code, "POLICY_CHECK_FAILED")
        self.assertFalse(res.error.retryable)
        self.assertEqual(self.query_tool.call_count, 0)

    async def test_6_valid_input_and_output_execution(self):
        """Steps 1-8: Permitted tool with valid input and output executes successfully."""
        res = await self.interceptor.call(
            agent_id=AgentID.SQL_DATA,
            tool_id="sql_query_tool",
            parameters={"query": "SELECT * FROM metrics", "limit": 20},
            task_id="task-100",
            workflow_id="wf-100",
        )
        self.assertEqual(res.status, TaskStatus.COMPLETED)
        self.assertIsNone(res.error)
        self.assertEqual(res.output_data["query"], "SELECT * FROM metrics")
        self.assertEqual(res.output_data["row_count"], 1)
        self.assertEqual(len(res.output_data["data"]), 1)
        self.assertEqual(self.query_tool.call_count, 1)
        self.assertIsNotNone(res.duration_ms)
        self.assertGreater(res.duration_ms, 0.0)

        # Verify attempt and completed audit events
        event_names = [e[0] for e in self.audit_events]
        self.assertIn("TOOL_CALL_ATTEMPTED", event_names)
        self.assertIn("TOOL_CALL_COMPLETED", event_names)

    async def test_7_invalid_input_parameter_handling(self):
        """Step 3: Invalid input parameters must emit TOOL_INPUT_INVALID and not execute tool."""
        # Missing required 'query' field, limit out of bounds
        res = await self.interceptor.call(
            agent_id=AgentID.SQL_DATA,
            tool_id="sql_query_tool",
            parameters={"limit": 500},
        )
        self.assertEqual(res.status, TaskStatus.FAILED)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.code, "TOOL_INPUT_INVALID")
        self.assertEqual(res.error.error_type, "ToolInputValidationError")
        self.assertFalse(res.error.retryable)
        self.assertEqual(self.query_tool.call_count, 0)

        # Verify TOOL_INPUT_INVALID audit event was emitted
        input_invalid_events = [e for e in self.audit_events if e[0] == "TOOL_INPUT_INVALID"]
        self.assertEqual(len(input_invalid_events), 1)

    async def test_8_unique_tool_call_id_generation(self):
        """Step 4: Distinct invocations must generate distinct UUID v4 tool_call_ids."""
        res1 = await self.interceptor.call(
            agent_id=AgentID.SQL_DATA,
            tool_id="sql_query_tool",
            parameters={"query": "SELECT 1"},
        )
        res2 = await self.interceptor.call(
            agent_id=AgentID.SQL_DATA,
            tool_id="sql_query_tool",
            parameters={"query": "SELECT 2"},
        )
        self.assertNotEqual(res1.execution_id, res2.execution_id)
        # Validate UUID v4 format
        uuid_obj1 = uuid.UUID(res1.execution_id, version=4)
        uuid_obj2 = uuid.UUID(res2.execution_id, version=4)
        self.assertEqual(uuid_obj1.version, 4)
        self.assertEqual(uuid_obj2.version, 4)

    async def test_9_timeout_enforcement_and_retryability(self):
        """Step 5: Execution timeout returns TOOL_TIMEOUT and honors Section 7.4 retryable rules."""
        # sandbox_execution timeout retryability is False per Section 7.4
        res = await self.interceptor.call(
            agent_id=AgentID.DEVELOPER,
            tool_id="sandbox_execution",
            parameters={"sleep_sec": 0.2},
            timeout_seconds=0.05,  # Force immediate timeout
        )
        self.assertEqual(res.status, TaskStatus.FAILED)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.code, "TOOL_TIMEOUT")
        self.assertEqual(res.error.error_type, "ToolTimeoutError")
        # sandbox_execution is strictly non-retryable
        self.assertFalse(res.error.retryable)

        # Verify TOOL_CALL_FAILED audit event was emitted
        failed_events = [e for e in self.audit_events if e[0] == "TOOL_CALL_FAILED"]
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0][1]["error_code"], "TOOL_TIMEOUT")

    async def test_10_execution_exception_with_credential_redaction(self):
        """Step 5: Runtime exception in tool must redact sensitive credentials from error message and audit."""
        res = await self.interceptor.call(
            agent_id=AgentID.SQL_DATA,
            tool_id="postgresql_read",
            parameters={"secret": "SuperSecretPassword999"},
        )
        self.assertEqual(res.status, TaskStatus.FAILED)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.code, "TOOL_EXECUTION_ERROR")
        self.assertNotIn("SuperSecretPassword999", res.error.message)
        self.assertIn("****", res.error.message)

        # Check failure audit event redaction
        failed_events = [e for e in self.audit_events if e[0] == "TOOL_CALL_FAILED"]
        self.assertEqual(len(failed_events), 1)
        self.assertNotIn("SuperSecretPassword999", str(failed_events[0][1]))

    async def test_11_invalid_output_schema_validation_failure(self):
        """Step 6: Output failing output_schema validation returns TOOL_OUTPUT_SCHEMA_ERROR and TaskStatus.FAILED."""
        res = await self.interceptor.call(
            agent_id=AgentID.DOCUMENT,
            tool_id="document_retrieve",
            parameters={},
        )
        self.assertEqual(res.status, TaskStatus.FAILED)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.code, "TOOL_OUTPUT_SCHEMA_ERROR")
        self.assertEqual(res.error.error_type, "ToolOutputSchemaError")
        self.assertFalse(res.error.retryable)

        # Audit should record TOOL_CALL_FAILED
        failed_events = [e for e in self.audit_events if e[0] == "TOOL_CALL_FAILED"]
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0][1]["error_code"], "TOOL_OUTPUT_SCHEMA_ERROR")

    async def test_12_audit_attempt_failure_blocks_execution(self):
        """Step 4: Audit failure during TOOL_CALL_ATTEMPTED must fail closed and prevent tool execution."""
        failing_audit_interceptor = ToolInterceptor(
            tool_registry=self.registry,
            policy_engine=self.policy_engine,
            audit_callback=MagicMock(side_effect=RuntimeError("Audit disk full")),
        )

        res = await failing_audit_interceptor.call(
            agent_id=AgentID.SQL_DATA,
            tool_id="sql_query_tool",
            parameters={"query": "SELECT 1"},
        )
        self.assertEqual(res.status, TaskStatus.FAILED)
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error.code, "AUDIT_ATTEMPT_FAILED")
        self.assertFalse(res.error.retryable)
        self.assertEqual(self.query_tool.call_count, 0)

    async def test_13_tool_execution_context_compatibility(self):
        """Verify invocation using ToolExecutionContext domain model."""
        ctx = ToolExecutionContext(
            execution_id="ctx-exec-1",
            tool_id="sql_query_tool",
            workflow_id="wf-abc",
            task_id="task-xyz",
            agent_id=AgentID.SQL_DATA,
            parameters={"query": "SELECT count(*) FROM users"},
        )
        res = await self.interceptor.call(ctx)
        self.assertEqual(res.status, TaskStatus.COMPLETED)
        self.assertEqual(res.tool_id, "sql_query_tool")
        self.assertIsNone(res.error)

    def test_14_synchronous_call_sync_helper(self):
        """Verify synchronous call_sync wrapper completes correctly in sync context."""
        res = self.interceptor.call_sync(
            agent_id=AgentID.SQL_DATA,
            tool_id="sql_query_tool",
            parameters={"query": "SELECT 42"},
            task_id="task-sync-1",
        )
        self.assertEqual(res.status, TaskStatus.COMPLETED)
        self.assertIsNone(res.error)
        self.assertEqual(res.output_data["query"], "SELECT 42")


if __name__ == "__main__":
    unittest.main()
