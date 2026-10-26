"""
EAOC Part 2.6 Unit Test Suite: Analytics Agent
Tests AnalyticsAgent:
1. Canonical AgentID and schema definitions.
2. Valid calculations and expected outputs (sum, mean, min, max, count, std).
3. Grouped aggregations (group_by).
4. Missing values and non-numeric value handling.
5. Empty dataset explicit handling ('empty_dataset').
6. Division by zero and calculation edge cases.
7. Row-limit bounding enforcement.
8. PII aggregation constraint enforcement (no raw row records in output).
9. Data provider dependency injection and resolution.
10. Safe error handling with secret redaction.
11. LLM narrative separation (calculated metrics remain immutable).
12. Compatibility with BaseAgent execution and AgentRegistry dispatch.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock
from pydantic import ValidationError

from eaoc.agents.analytics import (
    AggregationSpec,
    AggregationType,
    AnalyticsAgent,
    AnalyticsCalculator,
    AnalyticsInput,
    AnalyticsOutput,
    BaseDataProvider,
    InMemoryDataProvider,
)
from eaoc.agents.base import BaseAgent
from eaoc.agents.registry import AgentRegistry
from eaoc.models.base import ModelProviderCategory, ModelProviderWrapper
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    TaskStatus,
)


class FailingDataProvider(BaseDataProvider):
    """Data provider that simulates an infrastructure failure with credentials."""

    async def get_data(self, data_source: str, query_spec=None):
        raise RuntimeError(
            "Connection failed to postgresql://analytics_svc:SuperSecretAnalyticsPass888@db.internal:5432/analytics"
        )


class TestAnalyticsAgent(unittest.IsolatedAsyncioTestCase):
    """Unit test suite for the Analytics specialized agent."""

    def setUp(self):
        self.sample_rows = [
            {"region": "North", "sales": 100.0, "units": 10},
            {"region": "North", "sales": 200.0, "units": 20},
            {"region": "South", "sales": 300.0, "units": 15},
            {"region": "South", "sales": 400.0, "units": 25},
        ]
        self.data_provider = InMemoryDataProvider({
            "regional_sales": self.sample_rows,
        })
        self.agent = AnalyticsAgent(data_provider=self.data_provider)

        self.context = AgentExecutionContext(
            execution_id="exec-ana-001",
            workflow_id="wf-ana-001",
            task_id="task-ana-001",
            agent_id=AgentID.ANALYTICS,
            request_id="req-ana-001",
            correlation_id="corr-ana-001",
            input_payload={
                "analysis_goal": "Calculate total and average sales",
                "data_source": "regional_sales",
                "aggregations": [
                    {"field": "sales", "op": "sum", "alias": "total_sales", "unit": "USD"},
                    {"field": "sales", "op": "mean", "alias": "avg_sales", "unit": "USD"},
                    {"field": "units", "op": "sum", "alias": "total_units", "unit": "items"},
                ],
            },
            metadata={"caller": "test_runner"},
        )

    def test_canonical_agent_id_and_schemas(self):
        """Verify agent binds to AgentID.ANALYTICS and canonical schemas."""
        self.assertEqual(self.agent.agent_id, AgentID.ANALYTICS)
        self.assertEqual(self.agent.agent_id.value, "analytics")
        self.assertIs(self.agent.input_schema, AnalyticsInput)
        self.assertIs(self.agent.output_schema, AnalyticsOutput)
        self.assertIsInstance(self.agent, BaseAgent)

    def test_input_schema_validation(self):
        """Verify AnalyticsInput validation constraints."""
        valid_input = AnalyticsInput(
            analysis_goal="Summarize revenue",
            dataset=[{"val": 10}],
        )
        self.assertEqual(valid_input.analysis_goal, "Summarize revenue")
        self.assertEqual(len(valid_input.dataset), 1)

        # Neither dataset nor data_source
        with self.assertRaises(ValidationError):
            AnalyticsInput(analysis_goal="Summarize revenue")

        # Empty analysis_goal
        with self.assertRaises(ValidationError):
            AnalyticsInput(analysis_goal="", dataset=[{"val": 10}])

        # max_rows bounds
        with self.assertRaises(ValidationError):
            AnalyticsInput(analysis_goal="Goal", dataset=[], max_rows=0)
        with self.assertRaises(ValidationError):
            AnalyticsInput(analysis_goal="Goal", dataset=[], max_rows=50001)

    async def test_valid_calculation_and_expected_outputs(self):
        """Verify aggregations compute accurately and PII aggregation is enforced."""
        result: AgentExecutionResult = await self.agent.execute(self.context)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.execution_id, "exec-ana-001")
        self.assertEqual(result.agent_id, AgentID.ANALYTICS)
        self.assertIsNone(result.error)

        output = AnalyticsOutput.model_validate(result.output_payload)
        self.assertEqual(output.status, "completed")
        self.assertEqual(output.row_count, 4)
        self.assertTrue(output.pii_aggregated)  # No raw row records in payload

        # Check metrics
        self.assertEqual(output.metrics["total_sales"].value, 1000.0)
        self.assertEqual(output.metrics["total_sales"].unit, "USD")
        self.assertEqual(output.metrics["avg_sales"].value, 250.0)
        self.assertEqual(output.metrics["total_units"].value, 70.0)

        # Provenance reference
        self.assertEqual(output.provenance_ref, "prov-exec-ana-001")

    async def test_grouped_aggregations(self):
        """Verify group_by correctly partitions aggregations."""
        ctx = self.context.model_copy(update={"input_payload": {
            "analysis_goal": "Sales by region",
            "data_source": "regional_sales",
            "aggregations": [
                {"field": "sales", "op": "sum", "alias": "regional_sales_sum"},
            ],
            "group_by": ["region"],
        }})
        result: AgentExecutionResult = await self.agent.execute(ctx)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        output = AnalyticsOutput.model_validate(result.output_payload)
        self.assertIsNotNone(output.grouped_metrics)

        breakdown = output.grouped_metrics["regional_sales_sum"]
        self.assertEqual(breakdown["North"], 300.0)
        self.assertEqual(breakdown["South"], 700.0)

    async def test_missing_and_non_numeric_values_handling(self):
        """Verify calculator skips nulls and non-numeric values while logging notes."""
        dirty_dataset = [
            {"val": 10.0},
            {"val": None},           # null
            {"val": "20.0"},          # string float
            {"val": "invalid_str"},   # non-numeric
            {"val": 30.0},
        ]
        agent_dirty = AnalyticsAgent(data_provider=None)
        ctx = self.context.model_copy(update={"input_payload": {
            "analysis_goal": "Aggregate dirty data",
            "dataset": dirty_dataset,
            "aggregations": [
                {"field": "val", "op": "sum", "alias": "val_sum"},
                {"field": "val", "op": "mean", "alias": "val_mean"},
            ],
        }})
        result: AgentExecutionResult = await agent_dirty.execute(ctx)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        output = AnalyticsOutput.model_validate(result.output_payload)
        # Sum of 10.0 + 20.0 + 30.0 = 60.0
        self.assertEqual(output.metrics["val_sum"].value, 60.0)
        self.assertEqual(output.metrics["val_mean"].value, 20.0)
        self.assertIn("skipped 2 null or non-numeric", output.computation_notes)

    async def test_empty_dataset_handling(self):
        """Verify empty dataset returns explicit 'empty_dataset' status without error."""
        agent = AnalyticsAgent(data_provider=None)
        ctx = self.context.model_copy(update={"input_payload": {
            "analysis_goal": "Summarize empty data",
            "dataset": [],
        }})
        result: AgentExecutionResult = await agent.execute(ctx)

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        output = AnalyticsOutput.model_validate(result.output_payload)
        self.assertEqual(output.status, "empty_dataset")
        self.assertEqual(output.row_count, 0)
        self.assertEqual(output.metrics, {})
        self.assertIsNone(output.narrative)
        self.assertIn("empty", output.computation_notes)

    def test_calculator_division_by_zero_defense(self):
        """Verify AnalyticsCalculator.compute_ratio guards against zero denominators."""
        ratio, note = AnalyticsCalculator.compute_ratio(50.0, 0.0)
        self.assertIsNone(ratio)
        self.assertIn("Division by zero avoided", note)

        # Standard deviation with single element
        std_val = AnalyticsCalculator.compute_aggregation([42.0], "std")
        self.assertEqual(std_val, 0.0)

    async def test_max_rows_bounding(self):
        """Verify dataset exceeding max_rows is rejected."""
        large_dataset = [{"sales": i} for i in range(20)]
        agent = AnalyticsAgent(data_provider=None)
        ctx = self.context.model_copy(update={"input_payload": {
            "analysis_goal": "Analyze capped dataset",
            "dataset": large_dataset,
            "max_rows": 10,
        }})
        result: AgentExecutionResult = await agent.execute(ctx)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("exceeds maximum allowed limit", result.error.details["error"])

    async def test_safe_errors_no_credential_leakage(self):
        """Verify data provider exceptions redact credentials."""
        failing_agent = AnalyticsAgent(data_provider=FailingDataProvider())
        result: AgentExecutionResult = await failing_agent.execute(self.context)

        self.assertEqual(result.status, TaskStatus.FAILED)
        error_details = str(result.error.details)
        self.assertNotIn("SuperSecretAnalyticsPass888", error_details)
        self.assertNotIn("SuperSecretAnalyticsPass888", result.error.message)

    async def test_llm_narrative_separation(self):
        """Verify LLM narrative is grounded in metrics but cannot alter computed numbers."""
        mock_chat_model = MagicMock()
        mock_chat_model.ainvoke = AsyncMock(
            return_value=MagicMock(content="Sales reached a robust total of 1000 USD with an average of 250 USD.")
        )

        mock_provider = ModelProviderWrapper(
            provider_id="mock-ollama",
            model_name="llama3-analytics",
            category=ModelProviderCategory.LOCAL,
            chat_model=mock_chat_model,
        )

        agent_with_llm = AnalyticsAgent(
            data_provider=self.data_provider,
            model_provider=mock_provider,
        )

        result: AgentExecutionResult = await agent_with_llm.execute(self.context)
        self.assertEqual(result.status, TaskStatus.COMPLETED)

        output = AnalyticsOutput.model_validate(result.output_payload)
        # Numbers remain the exact computed figures
        self.assertEqual(output.metrics["total_sales"].value, 1000.0)
        self.assertEqual(output.metrics["avg_sales"].value, 250.0)
        self.assertIn("robust total of 1000 USD", output.narrative)
        mock_chat_model.ainvoke.assert_awaited_once()

    async def test_agent_registry_compatibility(self):
        """Verify AnalyticsAgent registers into AgentRegistry and handles dispatch()."""
        registry = AgentRegistry()
        registry.register_agent(self.agent)

        self.assertTrue(registry.has_agent(AgentID.ANALYTICS))
        self.assertTrue(registry.has_agent("analytics"))
        self.assertIs(registry.get_agent(AgentID.ANALYTICS), self.agent)

        # Dispatch via registry
        result: AgentExecutionResult = await registry.dispatch(self.context)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.agent_id, AgentID.ANALYTICS)
        self.assertEqual(result.output_payload["status"], "completed")


if __name__ == "__main__":
    unittest.main()
