"""
EAOC Part 2.7 Unit Test Suite: Finance Agent
Tests FinanceAgent:
1. Canonical AgentID.FINANCE and typed schema contracts.
2. Trusted-context RBAC:
   - Authorization for 'finance_user' and 'admin'.
   - Rejection when role is missing (deny by default).
   - Rejection for unauthorized roles ('hr_user', 'developer').
   - Anti-spoofing protection (ignoring input_payload role attempts).
3. Deterministic Decimal arithmetic and rounding (ROUND_HALF_UP).
4. Budget variance calculation:
   - Allocated vs actual variance and percentage.
   - Favorable vs unfavorable expense budget semantics.
   - Zero-allocation division-by-zero defense ('undefined_zero_allocation').
5. Currency validation and mixed-currency rejection.
6. Empty result set handling.
7. Record-count bounding enforcement.
8. Safe error handling with secret redaction.
9. Separation of LLM narrative from immutable deterministic calculations.
10. Prohibition of financial side effects (transfers, payments, refunds).
11. AgentRegistry registration and dispatch compatibility.
"""

from decimal import Decimal
import unittest
from unittest.mock import AsyncMock, MagicMock
from pydantic import ValidationError

from eaoc.agents.base import BaseAgent
from eaoc.agents.finance import (
    BaseFinanceDataProvider,
    FinanceAgent,
    FinanceCalculator,
    FinanceEntityType,
    FinanceInput,
    FinanceOutput,
    InMemoryFinanceDataProvider,
)
from eaoc.agents.registry import AgentRegistry
from eaoc.models.base import ModelProviderCategory, ModelProviderWrapper
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    TaskStatus,
)


class FailingFinanceDataProvider(BaseFinanceDataProvider):
    """Data provider that simulates an infrastructure failure leaking database credentials."""

    async def query_finance_data(self, entity_type, filters, context=None):
        raise RuntimeError(
            "Connection failed to postgresql://finance_admin:TopSecretFinanceDbPassword999@db.internal:5432/finance_prod"
        )


class TestFinanceAgent(unittest.IsolatedAsyncioTestCase):
    """Unit test suite for the Finance specialized agent."""

    def setUp(self):
        self.sample_invoices = [
            {"invoice_id": "INV-001", "vendor": "Acme Corp", "amount": "1250.50", "currency": "USD", "status": "paid"},
            {"invoice_id": "INV-002", "vendor": "Global Tech", "amount": "3400.00", "currency": "USD", "status": "pending"},
            {"invoice_id": "INV-003", "vendor": "Office Supplies Inc", "amount": "150.25", "currency": "USD", "status": "paid"},
        ]
        self.sample_budgets = [
            {"department": "Engineering", "period": "2026-Q3", "allocated": "50000.00", "actual": "42000.00", "currency": "USD"},
            {"department": "Marketing", "period": "2026-Q3", "allocated": "30000.00", "actual": "35000.00", "currency": "USD"},
            {"department": "Operations", "period": "2026-Q3", "allocated": "0.00", "actual": "5000.00", "currency": "USD"},
        ]
        self.sample_expenses = [
            {"expense_id": "EXP-101", "category": "Travel", "amount": "450.75", "currency": "USD", "department": "Sales"},
            {"expense_id": "EXP-102", "category": "Software", "amount": "1200.00", "currency": "USD", "department": "Engineering"},
        ]

        self.data_provider = InMemoryFinanceDataProvider({
            "invoice": self.sample_invoices,
            "budget": self.sample_budgets,
            "expense": self.sample_expenses,
            "report": [],
        })
        self.agent = FinanceAgent(data_provider=self.data_provider)

        # Standard trusted context with finance_user role
        self.trusted_finance_context = AgentExecutionContext(
            execution_id="exec-fin-001",
            workflow_id="wf-fin-001",
            task_id="task-fin-001",
            agent_id=AgentID.FINANCE,
            request_id="req-fin-001",
            correlation_id="corr-fin-001",
            input_payload={
                "entity_type": "invoice",
                "finance_query": "List all invoices for Acme Corp",
                "filters": {"vendor": "Acme Corp"},
            },
            metadata={"user_role": "finance_user", "user_id": "fin-user-01"},
        )

    def test_canonical_agent_id_and_schemas(self):
        """Verify agent binds to AgentID.FINANCE and canonical schemas."""
        self.assertEqual(self.agent.agent_id, AgentID.FINANCE)
        self.assertEqual(self.agent.agent_id.value, "finance")
        self.assertIs(self.agent.input_schema, FinanceInput)
        self.assertIs(self.agent.output_schema, FinanceOutput)
        self.assertIsInstance(self.agent, BaseAgent)

    async def test_trusted_rbac_authorization(self):
        """Verify authorized roles (finance_user, admin) succeed, unauthorized fail."""
        # 1. finance_user succeeds
        result_fin = await self.agent.execute(self.trusted_finance_context)
        self.assertEqual(result_fin.status, TaskStatus.COMPLETED)

        # 2. admin succeeds
        admin_context = self.trusted_finance_context.model_copy(
            update={"metadata": {"user_role": "admin", "user_id": "admin-01"}}
        )
        result_admin = await self.agent.execute(admin_context)
        self.assertEqual(result_admin.status, TaskStatus.COMPLETED)

        # 3. hr_user is rejected with PermissionError
        hr_context = self.trusted_finance_context.model_copy(
            update={"metadata": {"user_role": "hr_user", "user_id": "hr-01"}}
        )
        result_hr = await self.agent.execute(hr_context)
        self.assertEqual(result_hr.status, TaskStatus.FAILED)
        self.assertIn("PermissionError", result_hr.error.error_type)
        self.assertIn("not authorized", result_hr.error.details["error"])

        # 4. Missing metadata / missing role denies by default
        missing_role_context = self.trusted_finance_context.model_copy(update={"metadata": {}})
        result_missing = await self.agent.execute(missing_role_context)
        self.assertEqual(result_missing.status, TaskStatus.FAILED)
        self.assertIn("PermissionError", result_missing.error.error_type)
        self.assertIn("missing", result_missing.error.details["error"])

    async def test_trusted_rbac_anti_spoofing(self):
        """Verify that passing role='admin' inside input_payload does NOT bypass RBAC."""
        spoofed_payload = {
            "entity_type": "invoice",
            "finance_query": "List all invoices",
            "role": "admin",
            "user_role": "admin",
        }
        # Trusted context says caller is guest / unauthorized
        spoofed_context = self.trusted_finance_context.model_copy(
            update={
                "input_payload": spoofed_payload,
                "metadata": {"user_role": "guest"},
            }
        )
        result = await self.agent.execute(spoofed_context)
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("PermissionError", result.error.error_type)
        self.assertIn("Role 'guest' is not authorized", result.error.details["error"])

    async def test_trusted_rbac_untrusted_metadata_spoofing_rejected(self):
        """Verify that supplying admin role in untrusted or unauthenticated metadata fails closed."""
        # 1. Role inside untrusted_metadata container with no trusted role
        untrusted_container_ctx = self.trusted_finance_context.model_copy(
            update={
                "metadata": {
                    "untrusted_metadata": {"user_role": "admin"},
                    "caller_metadata": {"role": "admin"},
                }
            }
        )
        res1 = await self.agent.execute(untrusted_container_ctx)
        self.assertEqual(res1.status, TaskStatus.FAILED)
        self.assertIn("PermissionError", res1.error.error_type)
        self.assertIn("No trusted user role found", res1.error.details["error"])

        # 2. Explicitly unauthenticated or untrusted context
        unauthenticated_ctx = self.trusted_finance_context.model_copy(
            update={
                "metadata": {
                    "user_role": "admin",
                    "is_authenticated": False,
                }
            }
        )
        res2 = await self.agent.execute(unauthenticated_ctx)
        self.assertEqual(res2.status, TaskStatus.FAILED)
        self.assertIn("PermissionError", res2.error.error_type)
        self.assertIn("not authenticated", res2.error.details["error"])

    async def test_finance_schema_lockdown_denies_non_finance_schemas(self):
        """Verify that attempting to query non-finance schemas (hr, it, analytics) is blocked."""
        for forbidden_schema in ["hr", "it", "analytics", "public"]:
            with self.subTest(schema=forbidden_schema):
                forbidden_schema_ctx = self.trusted_finance_context.model_copy(
                    update={
                        "input_payload": {
                            "entity_type": "invoice",
                            "finance_query": f"Query data from {forbidden_schema}",
                            "filters": {"schema": forbidden_schema},
                        }
                    }
                )
                res = await self.agent.execute(forbidden_schema_ctx)
                self.assertEqual(res.status, TaskStatus.FAILED)
                self.assertIn("PermissionError", res.error.error_type)
                self.assertIn(f"Access to schema '{forbidden_schema}' is prohibited", res.error.details["error"])

    async def test_budget_variance_calculation_and_favorable_semantics(self):
        """Verify budget variance: allocated - actual, percentage, and favorable flag."""
        # Engineering: 50,000 allocated, 42,000 actual -> variance = +8,000 (+16.00%), favorable (under budget)
        eng_context = self.trusted_finance_context.model_copy(
            update={
                "input_payload": {
                    "entity_type": "budget",
                    "finance_query": "Engineering Q3 budget vs actuals",
                    "filters": {"department": "Engineering"},
                }
            }
        )
        result_eng = await self.agent.execute(eng_context)
        self.assertEqual(result_eng.status, TaskStatus.COMPLETED)
        payload = result_eng.output_payload
        variance = payload["variance"]
        self.assertEqual(Decimal(str(variance["allocated"])), Decimal("50000.00"))
        self.assertEqual(Decimal(str(variance["actual"])), Decimal("42000.00"))
        self.assertEqual(Decimal(str(variance["variance_amount"])), Decimal("8000.00"))
        self.assertEqual(Decimal(str(variance["variance_percentage"])), Decimal("16.00"))
        self.assertTrue(variance["favorable"])
        self.assertEqual(variance["percentage_status"], "computed")

        # Marketing: 30,000 allocated, 35,000 actual -> variance = -5,000 (-16.67%), unfavorable (over budget)
        mkt_context = self.trusted_finance_context.model_copy(
            update={
                "input_payload": {
                    "entity_type": "budget",
                    "finance_query": "Marketing Q3 budget vs actuals",
                    "filters": {"department": "Marketing"},
                }
            }
        )
        result_mkt = await self.agent.execute(mkt_context)
        self.assertEqual(result_mkt.status, TaskStatus.COMPLETED)
        variance_mkt = result_mkt.output_payload["variance"]
        self.assertEqual(Decimal(str(variance_mkt["variance_amount"])), Decimal("-5000.00"))
        self.assertEqual(Decimal(str(variance_mkt["variance_percentage"])), Decimal("-16.67"))
        self.assertFalse(variance_mkt["favorable"])

    async def test_budget_variance_zero_allocation_defense(self):
        """Verify that zero allocated budget returns undefined status without zero-division error."""
        # Operations: 0.00 allocated, 5,000 actual -> percentage is undefined
        ops_context = self.trusted_finance_context.model_copy(
            update={
                "input_payload": {
                    "entity_type": "budget",
                    "finance_query": "Operations Q3 budget vs actuals",
                    "filters": {"department": "Operations"},
                }
            }
        )
        result_ops = await self.agent.execute(ops_context)
        self.assertEqual(result_ops.status, TaskStatus.COMPLETED)
        variance_ops = result_ops.output_payload["variance"]
        self.assertEqual(Decimal(str(variance_ops["allocated"])), Decimal("0.00"))
        self.assertEqual(Decimal(str(variance_ops["variance_amount"])), Decimal("-5000.00"))
        self.assertIsNone(variance_ops["variance_percentage"])
        self.assertEqual(variance_ops["percentage_status"], "undefined_zero_allocation")
        self.assertFalse(variance_ops["favorable"])

    def test_decimal_rounding_and_precision(self):
        """Verify exact Decimal arithmetic and ROUND_HALF_UP precision."""
        d1 = FinanceCalculator.to_decimal("10.004")
        self.assertEqual(d1, Decimal("10.00"))
        d2 = FinanceCalculator.to_decimal("10.005")
        self.assertEqual(d2, Decimal("10.01"))

        # Test floating point precision safety (0.1 + 0.2 = 0.30, not 0.30000000000000004)
        t = FinanceCalculator.compute_totals([
            {"amount": "0.10", "currency": "USD"},
            {"amount": "0.20", "currency": "USD"},
        ])
        self.assertEqual(t.total_amount, Decimal("0.30"))

    async def test_mixed_currency_rejection(self):
        """Verify that records with mixed currencies are rejected without conversion rate."""
        mixed_context = self.trusted_finance_context.model_copy(
            update={
                "input_payload": {
                    "entity_type": "expense",
                    "finance_query": "Calculate total expenses",
                    "records": [
                        {"expense_id": "E1", "amount": "100.00", "currency": "USD"},
                        {"expense_id": "E2", "amount": "200.00", "currency": "EUR"},
                    ],
                }
            }
        )
        result = await self.agent.execute(mixed_context)
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("ValueError", result.error.error_type)
        self.assertIn("Mixed currencies detected", result.error.details["error"])

    async def test_mandatory_provenance_and_data_source(self):
        """Verify mandatory provenance block with AGT-FIN and accurate data source."""
        result = await self.agent.execute(self.trusted_finance_context)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        prov = result.output_payload["provenance"]
        self.assertEqual(prov["agent_id"], "AGT-FIN")
        self.assertEqual(prov["entity_type"], "invoice")
        self.assertEqual(prov["data_source"], "in_memory_provider")
        self.assertIn("T", prov["timestamp"])  # ISO 8601 UTC timestamp format

    async def test_empty_results_handling(self):
        """Verify that empty search results return status='empty_result' without error."""
        empty_context = self.trusted_finance_context.model_copy(
            update={
                "input_payload": {
                    "entity_type": "invoice",
                    "finance_query": "Nonexistent vendor query",
                    "filters": {"vendor": "Nonexistent Fake Vendor 999"},
                }
            }
        )
        result = await self.agent.execute(empty_context)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.output_payload["status"], "empty_result")
        self.assertEqual(result.output_payload["record_count"], 0)
        self.assertEqual(result.output_payload["records"], [])

    async def test_record_count_bounding(self):
        """Verify that requesting more records than max_records raises ValueError."""
        bounded_context = self.trusted_finance_context.model_copy(
            update={
                "input_payload": {
                    "entity_type": "invoice",
                    "finance_query": "Get invoices",
                    "max_records": 2,  # sample has 3
                    "filters": {},
                }
            }
        )
        result = await self.agent.execute(bounded_context)
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("ValueError", result.error.error_type)
        self.assertIn("exceeds maximum allowed limit of 2 records", result.error.details["error"])

    async def test_safe_errors_credential_redaction(self):
        """Verify provider exceptions with passwords/connection strings are redacted."""
        failing_agent = FinanceAgent(data_provider=FailingFinanceDataProvider())
        result = await failing_agent.execute(self.trusted_finance_context)
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertNotIn("TopSecretFinanceDbPassword999", str(result.error.details))
        self.assertIn("****", result.error.details["error"])

    async def test_prohibition_of_financial_side_effects(self):
        """Verify that unsupported write operations (payments, transfers) fail input validation."""
        for prohibited_op in ["payment", "transfer", "refund", "trade", "approve_invoice"]:
            with self.subTest(operation=prohibited_op):
                bad_context = self.trusted_finance_context.model_copy(
                    update={
                        "input_payload": {
                            "entity_type": prohibited_op,
                            "finance_query": f"Execute {prohibited_op}",
                        }
                    }
                )
                result = await self.agent.execute(bad_context)
                self.assertEqual(result.status, TaskStatus.FAILED)
                self.assertIn("ValidationError", result.error.error_type)

    async def test_llm_narrative_separation(self):
        """Verify that LLM narrative is grounded but cannot alter computed numbers."""
        mock_provider = MagicMock(spec=ModelProviderWrapper)
        mock_provider.category = ModelProviderCategory.LOCAL
        mock_provider.provider_name = "test_llm"
        mock_provider.chat_model = MagicMock()
        mock_provider.generate_chat_completion = AsyncMock(
            return_value="Acme Corp has one verified invoice for 1250.50 USD."
        )

        agent_with_llm = FinanceAgent(
            data_provider=self.data_provider, model_provider=mock_provider
        )
        result = await agent_with_llm.execute(self.trusted_finance_context)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        payload = result.output_payload
        self.assertEqual(
            payload["narrative"],
            "Acme Corp has one verified invoice for 1250.50 USD.",
        )
        # Verify deterministic totals remain intact
        self.assertEqual(Decimal(str(payload["totals"]["total_amount"])), Decimal("1250.50"))

    async def test_agent_registry_compatibility(self):
        """Verify FinanceAgent registers into AgentRegistry and handles dispatch()."""
        registry = AgentRegistry()
        registry.register_agent(self.agent)

        self.assertIn("finance", registry.list_agent_ids())
        retrieved = registry.get_agent(AgentID.FINANCE)
        self.assertIs(retrieved, self.agent)

        result = await registry.dispatch(self.trusted_finance_context)
        self.assertIsInstance(result, AgentExecutionResult)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.agent_id, AgentID.FINANCE)

    async def test_sql_finance_data_provider_delegation_and_schema_boundary(self):
        """Verify that SqlFinanceDataProvider locks queries to finance schema and blocks non-finance requests."""
        from eaoc.agents.finance import SqlFinanceDataProvider

        # 1. Mock SQL/Data Agent returning valid finance rows
        mock_sql_agent = MagicMock()
        mock_sql_agent.execute = AsyncMock(
            return_value=AgentExecutionResult(
                execution_id="sql-exec-001",
                agent_id=AgentID.SQL_DATA,
                status=TaskStatus.COMPLETED,
                output_payload={
                    "rows": [
                        {"invoice_id": "INV-100", "amount": "500.00", "currency": "USD"}
                    ]
                },
            )
        )

        sql_provider = SqlFinanceDataProvider(sql_agent=mock_sql_agent)

        # 2. Legitimate query targeting invoice
        rows = await sql_provider.query_finance_data(
            entity_type=FinanceEntityType.INVOICE,
            filters={"vendor": "Acme"},
            context=self.trusted_finance_context,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["invoice_id"], "INV-100")

        # Verify SQL agent was dispatched with permitted_schemas=['finance']
        call_args = mock_sql_agent.execute.call_args[0][0]
        self.assertEqual(call_args.input_payload["permitted_schemas"], ["finance"])
        self.assertIn("finance.invoices", call_args.input_payload["sql_query"])

        # 3. Direct attempt to query non-finance schema (hr, it, analytics) is blocked
        for forbidden_schema in ["hr", "it", "analytics", "confidential"]:
            with self.subTest(schema=forbidden_schema):
                with self.assertRaises(PermissionError) as ctx:
                    await sql_provider.query_finance_data(
                        entity_type=FinanceEntityType.INVOICE,
                        filters={"schema": forbidden_schema},
                        context=self.trusted_finance_context,
                    )
                self.assertIn("strictly restricted to schema 'finance'", str(ctx.exception))
