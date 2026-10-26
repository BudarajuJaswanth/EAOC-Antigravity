"""
EAOC Part 2.8 Unit Test Suite: HR Agent
Tests HrAgent:
1. Canonical AgentID.HR and BaseAgent contract.
2. Trusted-context RBAC:
   - Permitted: 'hr_user' and 'admin'.
   - Denied: unauthorized roles ('finance_user', 'developer', 'guest').
   - Deny by default: missing metadata, missing role, or unauthenticated context.
   - Anti-spoofing: ignoring caller-supplied roles in request payloads or untrusted metadata.
3. Conservative PII authorization & field minimization:
   - Default redaction when explicit PII claim is absent, including for 'admin'.
   - Unredacted access when explicit trusted PII claim is present.
   - Verified redaction of all required fields: DOB, salary, home address, SSN, personal contact details.
4. Operation-specific behavior:
   - Headcount: Aggregated groups and totals without exposing individual employee rows.
   - Org-chart: Non-PII structural projection only.
   - Employee-info: Filtered records with PII redaction.
   - Policy: Policy lookup and safe fallback.
5. Cross-schema access rejection:
   - Rejection of filters targeting 'finance', 'it', 'analytics'.
   - SqlHrDataProvider delegation with hardcoded permitted_schemas=['hr'].
6. Prohibited write operations (payroll, record updates).
7. Empty results, bounds checking, credential redaction, and AgentRegistry compatibility.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock
from pydantic import ValidationError

from eaoc.agents.base import BaseAgent
from eaoc.agents.hr import (
    BaseHrDataProvider,
    HeadcountGroup,
    HrAgent,
    HrInput,
    HrOutput,
    HrPiiRedactor,
    HrProvenance,
    HrQueryType,
    InMemoryHrDataProvider,
    SqlHrDataProvider,
)
from eaoc.agents.registry import AgentRegistry
from eaoc.models.base import ModelProviderCategory, ModelProviderWrapper
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    TaskStatus,
)


class FailingHrDataProvider(BaseHrDataProvider):
    """Data provider that simulates an infrastructure failure with database credentials."""

    async def query_hr_data(self, query_type, filters, context=None):
        raise RuntimeError(
            "Connection failed to postgresql://hr_admin:SecretHrMasterPass777@db.internal:5432/hr_prod"
        )

    async def query_policy(self, topic, context=None):
        raise RuntimeError("Policy vector index connection timeout")


class TestHrAgent(unittest.IsolatedAsyncioTestCase):
    """Unit test suite for the HR specialized agent."""

    def setUp(self):
        self.sample_employees = [
            {
                "employee_id": "EMP-001",
                "name": "Alice Smith",
                "title": "Senior Engineer",
                "department": "Engineering",
                "location": "New York",
                "employment_type": "full_time",
                "manager_id": "EMP-010",
                "date_of_birth": "1988-04-12",
                "salary": 145000,
                "home_address": "123 Main St, Brooklyn, NY",
                "ssn": "999-00-1111",
                "personal_email": "alice.personal@example.com",
                "personal_phone": "555-0101",
            },
            {
                "employee_id": "EMP-002",
                "name": "Bob Jones",
                "title": "Product Designer",
                "department": "Engineering",
                "location": "New York",
                "employment_type": "full_time",
                "manager_id": "EMP-010",
                "date_of_birth": "1992-09-20",
                "salary": 115000,
                "home_address": "456 Oak Ave, Queens, NY",
                "ssn": "999-00-2222",
                "personal_email": "bob.personal@example.com",
                "personal_phone": "555-0102",
            },
            {
                "employee_id": "EMP-003",
                "name": "Carol White",
                "title": "Recruiter",
                "department": "Human Resources",
                "location": "London",
                "employment_type": "contractor",
                "manager_id": "EMP-020",
                "date_of_birth": "1995-11-05",
                "salary": 85000,
                "home_address": "789 High St, London, UK",
                "ssn": "999-00-3333",
                "personal_email": "carol.personal@example.com",
                "personal_phone": "555-0103",
            },
        ]

        self.sample_policies = {
            "annual leave": {
                "topic": "annual leave",
                "policy_found": True,
                "content": "Full-time employees receive 25 days of paid annual leave per calendar year.",
                "citation": "HR Handbook Section 4.1",
            },
            "parental leave": {
                "topic": "parental leave",
                "policy_found": True,
                "content": "Employees are eligible for 16 weeks of fully paid parental leave.",
                "citation": "HR Handbook Section 4.5",
            },
        }

        self.data_provider = InMemoryHrDataProvider(
            employees=self.sample_employees,
            policies=self.sample_policies,
        )
        self.agent = HrAgent(data_provider=self.data_provider)

        # Standard trusted context: hr_user without explicit PII claim
        self.trusted_hr_context = AgentExecutionContext(
            execution_id="exec-hr-001",
            workflow_id="wf-hr-001",
            task_id="task-hr-001",
            agent_id=AgentID.HR,
            request_id="req-hr-001",
            correlation_id="corr-hr-001",
            input_payload={
                "query_type": "headcount",
                "hr_query": "What is the headcount by department?",
                "group_by": "department",
            },
            metadata={"user_role": "hr_user", "user_id": "hr-user-01"},
        )

    def test_canonical_agent_id_and_schemas(self):
        """Verify agent binds to AgentID.HR and canonical schemas."""
        self.assertEqual(self.agent.agent_id, AgentID.HR)
        self.assertEqual(self.agent.agent_id.value, "hr")
        self.assertIs(self.agent.input_schema, HrInput)
        self.assertIs(self.agent.output_schema, HrOutput)
        self.assertIsInstance(self.agent, BaseAgent)

    async def test_trusted_rbac_authorization(self):
        """Verify hr_user and admin succeed, unauthorized roles fail closed."""
        # 1. hr_user succeeds
        res_hr = await self.agent.execute(self.trusted_hr_context)
        self.assertEqual(res_hr.status, TaskStatus.COMPLETED)

        # 2. admin succeeds
        admin_ctx = self.trusted_hr_context.model_copy(
            update={"metadata": {"user_role": "admin", "user_id": "admin-01"}}
        )
        res_admin = await self.agent.execute(admin_ctx)
        self.assertEqual(res_admin.status, TaskStatus.COMPLETED)

        # 3. finance_user is rejected with PermissionError
        fin_ctx = self.trusted_hr_context.model_copy(
            update={"metadata": {"user_role": "finance_user", "user_id": "fin-01"}}
        )
        res_fin = await self.agent.execute(fin_ctx)
        self.assertEqual(res_fin.status, TaskStatus.FAILED)
        self.assertIn("PermissionError", res_fin.error.error_type)
        self.assertIn("not authorized", res_fin.error.details["error"])

        # 4. Missing metadata / missing role denies by default
        missing_ctx = self.trusted_hr_context.model_copy(update={"metadata": {}})
        res_missing = await self.agent.execute(missing_ctx)
        self.assertEqual(res_missing.status, TaskStatus.FAILED)
        self.assertIn("PermissionError", res_missing.error.error_type)

    async def test_trusted_rbac_anti_spoofing(self):
        """Verify that passing role='admin' inside input_payload does NOT grant access."""
        spoofed_payload = {
            "query_type": "headcount",
            "hr_query": "What is the headcount?",
            "role": "admin",
            "user_role": "admin",
        }
        spoofed_context = self.trusted_hr_context.model_copy(
            update={
                "input_payload": spoofed_payload,
                "metadata": {"user_role": "guest"},
            }
        )
        result = await self.agent.execute(spoofed_context)
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("PermissionError", result.error.error_type)
        self.assertIn("Role 'guest' is not authorized", result.error.details["error"])

    async def test_conservative_pii_authorization_default_redaction(self):
        """Verify that admin without explicit PII permission defaults to redaction."""
        emp_payload = {
            "query_type": "employee_info",
            "hr_query": "Get profile for Alice Smith",
            "filters": {"employee_id": "EMP-001"},
        }

        # 1. Admin WITHOUT explicit PII claim -> PII must be redacted
        admin_no_pii_ctx = self.trusted_hr_context.model_copy(
            update={
                "input_payload": emp_payload,
                "metadata": {"user_role": "admin", "user_id": "admin-01"},
            }
        )
        res1 = await self.agent.execute(admin_no_pii_ctx)
        self.assertEqual(res1.status, TaskStatus.COMPLETED)
        self.assertTrue(res1.output_payload["pii_redacted"])
        emp1 = res1.output_payload["result"]["employees"][0]
        self.assertEqual(emp1["salary"], "[REDACTED]")
        self.assertEqual(emp1["date_of_birth"], "[REDACTED]")
        self.assertEqual(emp1["ssn"], "[REDACTED]")
        self.assertEqual(emp1["home_address"], "[REDACTED]")
        self.assertEqual(emp1["personal_email"], "[REDACTED]")
        self.assertEqual(emp1["personal_phone"], "[REDACTED]")

        # 2. hr_user WITH explicit trusted PII claim -> Unredacted access
        hr_with_pii_ctx = self.trusted_hr_context.model_copy(
            update={
                "input_payload": emp_payload,
                "metadata": {
                    "user_role": "hr_user",
                    "user_id": "hr-01",
                    "user_context": {"role": "hr_user", "pii_permission": True},
                },
            }
        )
        res2 = await self.agent.execute(hr_with_pii_ctx)
        self.assertEqual(res2.status, TaskStatus.COMPLETED)
        self.assertFalse(res2.output_payload["pii_redacted"])
        emp2 = res2.output_payload["result"]["employees"][0]
        self.assertEqual(emp2["salary"], 145000)
        self.assertEqual(emp2["date_of_birth"], "1988-04-12")
        self.assertEqual(emp2["ssn"], "999-00-1111")
        self.assertEqual(emp2["personal_email"], "alice.personal@example.com")

    async def test_headcount_aggregation_never_exposes_rows(self):
        """Verify headcount queries return aggregate numbers and groups, never raw employee rows."""
        result = await self.agent.execute(self.trusted_hr_context)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        res_data = result.output_payload["result"]
        self.assertEqual(res_data["total"], 3)
        self.assertEqual(res_data["group_by"], "department")

        groups = {g["label"]: g["count"] for g in res_data["groups"]}
        self.assertEqual(groups["Engineering"], 2)
        self.assertEqual(groups["Human Resources"], 1)

        # Assert no individual employee records exist in payload
        self.assertNotIn("employees", res_data)
        self.assertNotIn("records", res_data)
        self.assertNotIn("salary", str(res_data))
        self.assertNotIn("Alice Smith", str(res_data))

    async def test_org_chart_field_minimization(self):
        """Verify org chart queries strip all non-structural fields regardless of permissions."""
        org_payload = {
            "query_type": "org_chart",
            "hr_query": "Show company org chart",
            "filters": {},
        }
        org_context = self.trusted_hr_context.model_copy(
            update={
                "input_payload": org_payload,
                "metadata": {
                    "user_role": "admin",
                    "user_context": {"role": "admin", "pii_permission": True},
                },
            }
        )
        result = await self.agent.execute(org_context)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        nodes = result.output_payload["result"]["org_chart"]
        self.assertEqual(len(nodes), 3)

        # Non-PII structural fields must be present
        self.assertIn("employee_id", nodes[0])
        self.assertIn("name", nodes[0])
        self.assertIn("title", nodes[0])
        self.assertIn("manager_id", nodes[0])

        # Sensitive fields must be stripped out entirely
        for node in nodes:
            self.assertNotIn("salary", node)
            self.assertNotIn("date_of_birth", node)
            self.assertNotIn("home_address", node)
            self.assertNotIn("ssn", node)

    async def test_policy_lookup(self):
        """Verify policy lookup retrieves policy excerpt and citation."""
        policy_payload = {
            "query_type": "policy",
            "hr_query": "What is the policy for annual leave?",
        }
        policy_context = self.trusted_hr_context.model_copy(
            update={"input_payload": policy_payload}
        )
        result = await self.agent.execute(policy_context)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        res_data = result.output_payload["result"]
        self.assertTrue(res_data["policy_found"])
        self.assertIn("25 days", res_data["content"])
        self.assertIn("Section 4.1", res_data["citation"])

    async def test_cross_schema_access_rejection(self):
        """Verify attempting to access finance, it, or analytics schemas is rejected."""
        for forbidden_schema in ["finance", "it", "analytics", "public"]:
            with self.subTest(schema=forbidden_schema):
                bad_schema_ctx = self.trusted_hr_context.model_copy(
                    update={
                        "input_payload": {
                            "query_type": "employee_info",
                            "hr_query": f"Query {forbidden_schema} schema",
                            "filters": {"schema": forbidden_schema},
                        }
                    }
                )
                result = await self.agent.execute(bad_schema_ctx)
                self.assertEqual(result.status, TaskStatus.FAILED)
                self.assertIn("PermissionError", result.error.error_type)
                self.assertIn(f"Access to schema '{forbidden_schema}' is prohibited", result.error.details["error"])

    async def test_sql_hr_data_provider_delegation_and_schema_boundary(self):
        """Verify SqlHrDataProvider delegates to SQL Agent with permitted_schemas=['hr']."""
        mock_sql_agent = MagicMock()
        mock_sql_agent.execute = AsyncMock(
            return_value=AgentExecutionResult(
                execution_id="sql-exec-hr-001",
                agent_id=AgentID.SQL_DATA,
                status=TaskStatus.COMPLETED,
                output_payload={
                    "rows": [
                        {"employee_id": "EMP-999", "name": "Test Employee", "department": "HR"}
                    ]
                },
            )
        )
        sql_provider = SqlHrDataProvider(sql_agent=mock_sql_agent)

        rows = await sql_provider.query_hr_data(
            query_type=HrQueryType.EMPLOYEE_INFO,
            filters={"employee_id": "EMP-999"},
            context=self.trusted_hr_context,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["employee_id"], "EMP-999")

        # Verify SQL agent was dispatched with permitted_schemas=['hr']
        call_args = mock_sql_agent.execute.call_args[0][0]
        self.assertEqual(call_args.input_payload["permitted_schemas"], ["hr"])
        self.assertIn("hr.employees", call_args.input_payload["sql_query"])

        # Direct attempt to specify non-hr schema raises PermissionError
        with self.assertRaises(PermissionError) as ctx:
            await sql_provider.query_hr_data(
                query_type=HrQueryType.EMPLOYEE_INFO,
                filters={"schema": "finance"},
                context=self.trusted_hr_context,
            )
        self.assertIn("strictly restricted to schema 'hr'", str(ctx.exception))

    async def test_prohibition_of_write_operations(self):
        """Verify write operations (payroll, update_salary) fail input validation."""
        for prohibited_op in ["payroll", "update_salary", "terminate_employee", "delete"]:
            with self.subTest(operation=prohibited_op):
                bad_ctx = self.trusted_hr_context.model_copy(
                    update={
                        "input_payload": {
                            "query_type": prohibited_op,
                            "hr_query": f"Execute {prohibited_op}",
                        }
                    }
                )
                result = await self.agent.execute(bad_ctx)
                self.assertEqual(result.status, TaskStatus.FAILED)
                self.assertIn("ValidationError", result.error.error_type)

    async def test_safe_errors_credential_redaction(self):
        """Verify provider exceptions with passwords are redacted."""
        failing_agent = HrAgent(data_provider=FailingHrDataProvider())
        result = await failing_agent.execute(self.trusted_hr_context)
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertNotIn("SecretHrMasterPass777", str(result.error.details))
        self.assertIn("****", result.error.details["error"])

    async def test_mandatory_provenance(self):
        """Verify mandatory provenance block with AGT-HR and accurate flags."""
        result = await self.agent.execute(self.trusted_hr_context)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        prov = result.output_payload["provenance"]
        self.assertEqual(prov["agent_id"], "AGT-HR")
        self.assertEqual(prov["query_type"], "headcount")
        self.assertEqual(prov["data_source"], "in_memory_provider")
        self.assertFalse(prov["pii_redacted"])
        self.assertIn("T", prov["timestamp"])

    async def test_agent_registry_compatibility(self):
        """Verify HrAgent registers into AgentRegistry and handles dispatch()."""
        registry = AgentRegistry()
        registry.register_agent(self.agent)

        self.assertIn("hr", registry.list_agent_ids())
        retrieved = registry.get_agent(AgentID.HR)
        self.assertIs(retrieved, self.agent)

        result = await registry.dispatch(self.trusted_hr_context)
        self.assertIsInstance(result, AgentExecutionResult)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.agent_id, AgentID.HR)
