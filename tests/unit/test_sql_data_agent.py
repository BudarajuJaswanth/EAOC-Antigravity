"""
EAOC Part 2.4 Unit Test Suite: SQL/Data Agent
Tests SqlDataAgent:
1. Canonical AgentID and schema definitions.
2. Valid query execution and typed tabular output.
3. Strict rejection of write statements (INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, etc.).
4. Strict rejection of multiple statements (semicolon injection defense).
5. Safe semicolon handling inside string literals.
6. Parameterized query handling.
7. Row-limit enforcement and truncation flagging.
8. Unauthorized schema access denial.
9. Safe error handling with secret redaction (no credential leakage).
10. Missing or unconfigured executor error handling.
11. Chat model SQL formulation when raw query is omitted.
12. Compatibility with BaseAgent execution and AgentRegistry dispatch.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock
from pydantic import ValidationError

from eaoc.agents.base import BaseAgent
from eaoc.agents.registry import AgentRegistry
from eaoc.agents.sql_data import (
    BaseSqlExecutor,
    InMemorySqlExecutor,
    SqlDataAgent,
    SqlDataInput,
    SqlDataOutput,
    SqlQueryResult,
)
from eaoc.models.base import ModelProviderCategory, ModelProviderWrapper
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    TaskStatus,
)


class FailingSqlExecutor(BaseSqlExecutor):
    """Executor that raises an exception simulating a database error with credentials."""

    async def execute_query(self, sql: str, parameters=None, row_limit=100) -> SqlQueryResult:
        raise RuntimeError(
            "Connection failed to postgresql://db_admin:UltraSecretPassword123@supabase.internal:5432/eaoc"
        )


class TestSqlDataAgent(unittest.IsolatedAsyncioTestCase):
    """Unit test suite for the SQL/Data specialized agent."""

    def setUp(self):
        self.sample_rows = [
            {"id": 1, "name": "Alpha", "score": 85.5, "active": True},
            {"id": 2, "name": "Beta", "score": 92.0, "active": False},
            {"id": 3, "name": "Gamma", "score": 78.0, "active": True},
            {"id": 4, "name": "Delta", "score": 99.5, "active": True},
        ]
        self.executor = InMemorySqlExecutor({
            "analytics.metrics": self.sample_rows,
            "public.users": [{"user_id": 100, "username": "alice"}],
        })
        self.agent = SqlDataAgent(executor=self.executor)

        self.context = AgentExecutionContext(
            execution_id="exec-sql-001",
            workflow_id="wf-sql-001",
            task_id="task-sql-001",
            agent_id=AgentID.SQL_DATA,
            request_id="req-sql-001",
            correlation_id="corr-sql-001",
            input_payload={
                "query_goal": "Retrieve active metrics from analytics",
                "sql_query": "SELECT * FROM analytics.metrics",
                "permitted_schemas": ["analytics", "public"],
                "row_limit": 10,
            },
            metadata={"caller": "test_runner"},
        )

    def test_canonical_agent_id_and_schemas(self):
        """Verify agent binds to AgentID.SQL_DATA and canonical schemas."""
        self.assertEqual(self.agent.agent_id, AgentID.SQL_DATA)
        self.assertEqual(self.agent.agent_id.value, "sql_data")
        self.assertIs(self.agent.input_schema, SqlDataInput)
        self.assertIs(self.agent.output_schema, SqlDataOutput)
        self.assertIsInstance(self.agent, BaseAgent)

    def test_input_schema_validation(self):
        """Verify SqlDataInput validation and constraints."""
        valid_input = SqlDataInput(
            query_goal="Get metrics",
            sql_query="SELECT id FROM analytics.metrics",
            row_limit=50,
        )
        self.assertEqual(valid_input.query_goal, "Get metrics")
        self.assertEqual(valid_input.row_limit, 50)
        self.assertTrue(valid_input.read_only_flag)

        # read_only_flag=False must be rejected
        with self.assertRaises(ValidationError):
            SqlDataInput(query_goal="Write data", read_only_flag=False)

        # row_limit bounds
        with self.assertRaises(ValidationError):
            SqlDataInput(query_goal="Test", row_limit=0)
        with self.assertRaises(ValidationError):
            SqlDataInput(query_goal="Test", row_limit=1001)

    async def test_successful_query_execution_and_output(self):
        """Verify valid SELECT query executes and returns structured tabular output."""
        result: AgentExecutionResult = await self.agent.execute(self.context)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.execution_id, "exec-sql-001")
        self.assertEqual(result.agent_id, AgentID.SQL_DATA)
        self.assertIsNone(result.error)

        output = SqlDataOutput.model_validate(result.output_payload)
        self.assertEqual(output.status, "completed")
        self.assertEqual(output.row_count, 4)
        self.assertFalse(output.truncated)
        self.assertEqual(output.columns, ["id", "name", "score", "active"])
        self.assertEqual(len(output.rows), 4)
        self.assertEqual(output.schemas_accessed, ["analytics"])
        self.assertEqual(output.provenance_ref, "prov-exec-sql-001")

    async def test_rejection_of_write_statements(self):
        """Verify write statements (INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, etc.) are rejected."""
        forbidden_queries = [
            "INSERT INTO analytics.metrics (id, name) VALUES (5, 'Epsilon')",
            "UPDATE analytics.metrics SET score = 100 WHERE id = 1",
            "DELETE FROM analytics.metrics WHERE id = 1",
            "DROP TABLE analytics.metrics",
            "TRUNCATE TABLE analytics.metrics",
            "ALTER TABLE analytics.metrics ADD COLUMN new_col int",
            "CREATE TABLE analytics.test (id int)",
            "SELECT * INTO analytics.backup FROM analytics.metrics",
        ]

        for query in forbidden_queries:
            ctx = self.context.model_copy(update={"input_payload": {
                "query_goal": "Attempting modification",
                "sql_query": query,
                "permitted_schemas": ["analytics"],
            }})
            result: AgentExecutionResult = await self.agent.execute(ctx)

            self.assertEqual(
                result.status,
                TaskStatus.FAILED,
                f"Query '{query}' was expected to fail validation.",
            )
            self.assertEqual(result.error.code, "EXECUTION_ERROR")
            self.assertIn("Forbidden", result.error.details["error"])

    async def test_rejection_of_multiple_statements(self):
        """Verify multiple statements separated by semicolons are rejected."""
        malicious_query = "SELECT * FROM analytics.metrics; DROP TABLE analytics.metrics;"
        ctx = self.context.model_copy(update={"input_payload": {
            "query_goal": "Multi-statement attack",
            "sql_query": malicious_query,
            "permitted_schemas": ["analytics"],
        }})
        result: AgentExecutionResult = await self.agent.execute(ctx)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("Multiple SQL statements", result.error.details["error"])

    async def test_semicolon_allowed_in_string_literals(self):
        """Verify semicolons within string literals do not trigger false-positive multi-statement rejection."""
        query_with_literal_semicolon = "SELECT * FROM analytics.metrics WHERE name = 'val;with;semi'"
        ctx = self.context.model_copy(update={"input_payload": {
            "query_goal": "Filter by string with semicolon",
            "sql_query": query_with_literal_semicolon,
            "permitted_schemas": ["analytics"],
        }})
        result: AgentExecutionResult = await self.agent.execute(ctx)
        self.assertEqual(result.status, TaskStatus.COMPLETED)

    async def test_parameterized_query_handling(self):
        """Verify query parameters are passed and handled safely."""
        ctx = self.context.model_copy(update={"input_payload": {
            "query_goal": "Fetch Beta user metrics",
            "sql_query": "SELECT * FROM analytics.metrics WHERE id = :id",
            "parameters": {"id": 2},
            "permitted_schemas": ["analytics"],
        }})
        result: AgentExecutionResult = await self.agent.execute(ctx)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        output = SqlDataOutput.model_validate(result.output_payload)
        self.assertEqual(output.row_count, 1)
        self.assertEqual(output.rows[0]["name"], "Beta")

    async def test_row_limit_and_truncation(self):
        """Verify row_limit caps result count and sets truncated=True."""
        ctx = self.context.model_copy(update={"input_payload": {
            "query_goal": "Fetch top 2 rows",
            "sql_query": "SELECT * FROM analytics.metrics",
            "row_limit": 2,
            "permitted_schemas": ["analytics"],
        }})
        result: AgentExecutionResult = await self.agent.execute(ctx)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        output = SqlDataOutput.model_validate(result.output_payload)
        self.assertEqual(output.row_count, 2)
        self.assertTrue(output.truncated)

    async def test_unauthorized_schema_access_denial(self):
        """Verify queries accessing non-permitted schemas are rejected."""
        ctx = self.context.model_copy(update={"input_payload": {
            "query_goal": "Access unauthorized schema",
            "sql_query": "SELECT * FROM confidential_hr.salaries",
            "permitted_schemas": ["analytics"],
        }})
        result: AgentExecutionResult = await self.agent.execute(ctx)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("confidential_hr", result.error.details["error"])

    async def test_safe_errors_no_credential_leakage(self):
        """Verify executor exceptions redact database credentials and passwords."""
        failing_agent = SqlDataAgent(executor=FailingSqlExecutor())
        result: AgentExecutionResult = await failing_agent.execute(self.context)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.error.code, "EXECUTION_ERROR")
        error_details = str(result.error.details)
        # Ensure password is not present anywhere in error details or message
        self.assertNotIn("UltraSecretPassword123", error_details)
        self.assertNotIn("UltraSecretPassword123", result.error.message)

    async def test_unconfigured_executor_handling(self):
        """Verify executing without an executor returns clear error."""
        unconfigured_agent = SqlDataAgent(executor=None)
        result: AgentExecutionResult = await unconfigured_agent.execute(self.context)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("SQL Executor is not configured", result.error.details["error"])

    async def test_chat_model_sql_formulation(self):
        """Verify chat model is invoked to formulate SQL when sql_query is omitted."""
        mock_chat_model = MagicMock()
        mock_chat_model.ainvoke = AsyncMock(
            return_value=MagicMock(content="SELECT * FROM analytics.metrics")
        )

        mock_provider = ModelProviderWrapper(
            provider_id="mock-ollama",
            model_name="llama3-sql",
            category=ModelProviderCategory.LOCAL,
            chat_model=mock_chat_model,
        )

        agent_with_llm = SqlDataAgent(
            executor=self.executor,
            model_provider=mock_provider,
        )

        ctx = self.context.model_copy(update={"input_payload": {
            "query_goal": "Get metrics",
            "permitted_schemas": ["analytics"],
        }})

        result: AgentExecutionResult = await agent_with_llm.execute(ctx)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        output = SqlDataOutput.model_validate(result.output_payload)
        self.assertEqual(output.sql_executed, "SELECT * FROM analytics.metrics")
        mock_chat_model.ainvoke.assert_awaited_once()

    async def test_agent_registry_compatibility(self):
        """Verify SqlDataAgent registers into AgentRegistry and handles dispatch()."""
        registry = AgentRegistry()
        registry.register_agent(self.agent)

        self.assertTrue(registry.has_agent(AgentID.SQL_DATA))
        self.assertTrue(registry.has_agent("sql_data"))
        self.assertIs(registry.get_agent(AgentID.SQL_DATA), self.agent)

        # Dispatch via registry
        result: AgentExecutionResult = await registry.dispatch(self.context)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.agent_id, AgentID.SQL_DATA)
        self.assertEqual(result.output_payload["status"], "completed")


if __name__ == "__main__":
    unittest.main()
