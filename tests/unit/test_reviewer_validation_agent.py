"""
Unit tests for EAOC Reviewer / Validation Agent (AGT-REV).
Verifies:
1. Canonical AgentID and schema validation.
2. Trusted RBAC, missing identity, unauthorized roles, and input-payload role spoofing.
3. Completeness pass.
4. Missing and null required fields.
5. Grounding match.
6. Grounding discrepancy ($500K vs $400K).
7. Missing source evidence triggers ESCALATE.
8. Internal consistency conflict.
9. Policy finding for apparent unredacted PII and cross-domain disclosure.
10. All eight risk rules and precedence verification.
11. Human approval prevents automatic approval (triggers ESCALATE).
12. LLM failure/timeout fails to ESCALATE rather than approve.
13. Read-only behavior for nested workflow output and context.
14. Safe error redaction.
15. AgentRegistry registration and dispatch compatibility.
16. Provenance fields, findings count, and UTC-aware timestamps.
"""

import copy
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock

from pydantic import ValidationError

from eaoc.agents.registry import AgentRegistry
from eaoc.agents.reviewer_validation import (
    AUTHORIZED_REVIEWER_ROLES,
    FindingSeverity,
    ReviewFindingItem,
    ReviewFindingType,
    ReviewRecommendation,
    ReviewerEngine,
    ReviewerInput,
    ReviewerOutput,
    ReviewerProvenance,
    ReviewerValidationAgent,
    RiskAssessment,
)
from eaoc.models.base import ModelProviderCategory, ModelProviderWrapper
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    TaskStatus,
)


def _make_context(
    role: str = "orchestrator",
    is_authenticated: bool = True,
    metadata: dict = None,
    execution_id: str = "exec-test-rev-01",
    task_id: str = "task-rev-01",
) -> AgentExecutionContext:
    meta = {
        "role": role,
        "is_authenticated": is_authenticated,
        "is_trusted": is_authenticated,
        "user_context": {"role": role},
    }
    if metadata:
        meta.update(metadata)
    return AgentExecutionContext(
        execution_id=execution_id,
        workflow_id="wf-test-01",
        task_id=task_id,
        agent_id=AgentID.REVIEWER_VALIDATION,
        request_id="req-test-01",
        correlation_id="corr-test-01",
        metadata=meta,
    )


class TestReviewerValidationAgent(unittest.IsolatedAsyncioTestCase):
    """Test suite for Reviewer / Validation Agent (AGT-REV)."""

    def setUp(self):
        self.agent = ReviewerValidationAgent()

    # 1. Canonical AgentID and schema validation
    def test_01_canonical_agent_id_and_schemas(self):
        self.assertEqual(self.agent.agent_id, AgentID.REVIEWER_VALIDATION)
        self.assertEqual(self.agent.agent_id.value, "reviewer_validation")
        self.assertEqual(self.agent.input_schema, ReviewerInput)
        self.assertEqual(self.agent.output_schema, ReviewerOutput)

        # Extra fields rejection
        with self.assertRaises(ValidationError):
            ReviewerInput(
                task_id="task-1",
                unexpected_field="disallowed",
            )

    # 2. Trusted RBAC, missing identity, unauthorized roles, and anti-spoofing
    async def test_02_trusted_rbac_and_anti_spoofing(self):
        payload = {
            "task_id": "task-01",
            "assembled_output": {"status": "complete"},
            "source_data": {"status": "complete"},
        }

        # Authorized roles: orchestrator, system, admin, workflow_owner
        for role in ["orchestrator", "system", "admin", "workflow_owner"]:
            ctx = _make_context(role=role)
            ctx.input_payload = payload
            res = await self.agent.execute(ctx)
            self.assertEqual(res.status, TaskStatus.COMPLETED)

        # Unauthorized roles: developer, hr_user, it_user, finance_user, guest
        for unauth_role in ["developer", "hr_user", "it_user", "finance_user", "guest"]:
            ctx = _make_context(role=unauth_role)
            ctx.input_payload = payload
            res = await self.agent.execute(ctx)
            self.assertEqual(res.status, TaskStatus.FAILED)
            self.assertEqual(res.error.code, "EXECUTION_ERROR")
            self.assertEqual(res.error.error_type, "PermissionError")
            self.assertIn("not authorized", res.error.details.get("error", "").lower())

        # Unauthenticated context
        ctx_unauth = _make_context(role="orchestrator", is_authenticated=False)
        ctx_unauth.input_payload = payload
        res_unauth = await self.agent.execute(ctx_unauth)
        self.assertEqual(res_unauth.status, TaskStatus.FAILED)
        self.assertEqual(res_unauth.error.error_type, "PermissionError")

        # Missing metadata
        ctx_no_meta = AgentExecutionContext(
            execution_id="exec-err",
            workflow_id="wf-1",
            task_id="t-1",
            agent_id=AgentID.REVIEWER_VALIDATION,
            request_id="r-1",
            correlation_id="c-1",
            input_payload=payload,
            metadata={},
        )
        res_no_meta = await self.agent.execute(ctx_no_meta)
        self.assertEqual(res_no_meta.status, TaskStatus.FAILED)
        self.assertEqual(res_no_meta.error.error_type, "PermissionError")

        # Input payload spoofing attempt: guest context claims admin in input
        ctx_spoof = _make_context(role="guest")
        ctx_spoof.input_payload = {"role": "admin", "task_id": "task-01"}
        res_spoof = await self.agent.execute(ctx_spoof)
        self.assertEqual(res_spoof.status, TaskStatus.FAILED)
        self.assertEqual(res_spoof.error.error_type, "PermissionError")
        self.assertIn("not authorized", res_spoof.error.details.get("error", "").lower())

    # 3. Completeness pass
    async def test_03_completeness_pass(self):
        ctx = _make_context()
        ctx.input_payload = {
            "task_id": "task-comp-pass",
            "assembled_output": {
                "summary": "Analytics completed.",
                "total_count": 42,
                "status": "success",
            },
            "required_fields": ["summary", "total_count", "status"],
            "source_data": {"total_count": 42},
        }
        res = await self.agent.execute(ctx)
        self.assertEqual(res.status, TaskStatus.COMPLETED)
        out = ReviewerOutput.model_validate(res.output_payload)
        self.assertTrue(out.passed)
        self.assertEqual(out.recommendation, ReviewRecommendation.APPROVE)
        self.assertEqual(len(out.findings), 0)

    # 4. Missing and null required fields
    async def test_04_missing_and_null_required_fields(self):
        ctx = _make_context()

        # Missing required field
        ctx.input_payload = {
            "task_id": "task-comp-fail-1",
            "assembled_output": {"summary": "Report text"},
            "required_fields": ["summary", "metrics_table"],
        }
        res = await self.agent.execute(ctx)
        self.assertEqual(res.status, TaskStatus.COMPLETED)
        out = ReviewerOutput.model_validate(res.output_payload)
        self.assertFalse(out.passed)
        self.assertEqual(out.recommendation, ReviewRecommendation.REJECT)
        comp_errs = [f for f in out.findings if f.type == ReviewFindingType.COMPLETENESS]
        self.assertEqual(len(comp_errs), 1)
        self.assertEqual(comp_errs[0].field_reference, "metrics_table")

        # Explicitly null required field
        ctx.input_payload = {
            "task_id": "task-comp-fail-2",
            "assembled_output": {"summary": "Report text", "metrics_table": None},
            "required_fields": ["summary", "metrics_table"],
        }
        res2 = await self.agent.execute(ctx)
        out2 = ReviewerOutput.model_validate(res2.output_payload)
        self.assertFalse(out2.passed)
        self.assertEqual(out2.recommendation, ReviewRecommendation.REJECT)

    # 5. Grounding match
    async def test_05_grounding_match(self):
        ctx = _make_context()
        ctx.input_payload = {
            "task_id": "task-grnd-match",
            "assembled_output": {
                "revenue": 500000,
                "summary": "Q3 Revenue reached $500K.",
            },
            "source_data": {"revenue": 500000},
        }
        res = await self.agent.execute(ctx)
        self.assertEqual(res.status, TaskStatus.COMPLETED)
        out = ReviewerOutput.model_validate(res.output_payload)
        self.assertTrue(out.passed)
        self.assertEqual(out.recommendation, ReviewRecommendation.APPROVE)

    # 6. Grounding discrepancy ($500K claim conflicting with a $400K source)
    async def test_06_grounding_discrepancy_500k_vs_400k(self):
        ctx = _make_context()
        ctx.input_payload = {
            "task_id": "task-grnd-conflict",
            "assembled_output": {
                "narrative": "Revenue Q3 was $500K according to results.",
            },
            "source_data": {
                "revenue": "$400K",
            },
        }
        res = await self.agent.execute(ctx)
        self.assertEqual(res.status, TaskStatus.COMPLETED)
        out = ReviewerOutput.model_validate(res.output_payload)
        self.assertFalse(out.passed)
        self.assertEqual(out.recommendation, ReviewRecommendation.REJECT)
        grnd_errs = [f for f in out.findings if f.type == ReviewFindingType.GROUNDING]
        self.assertGreaterEqual(len(grnd_errs), 1)
        self.assertEqual(grnd_errs[0].severity, FindingSeverity.ERROR)
        self.assertIn("conflicts with source data", grnd_errs[0].description)

    # 7. Missing source evidence triggers ESCALATE
    async def test_07_missing_source_evidence_triggers_escalate(self):
        ctx = _make_context()
        ctx.input_payload = {
            "task_id": "task-missing-evidence",
            "assembled_output": {
                "summary": "Projected growth is 25% with 100 new accounts.",
            },
            "source_data": {},  # Missing/empty source evidence
        }
        res = await self.agent.execute(ctx)
        self.assertEqual(res.status, TaskStatus.COMPLETED)
        out = ReviewerOutput.model_validate(res.output_payload)
        self.assertTrue(out.passed)  # Checks completed without fatal syntax error
        self.assertEqual(out.recommendation, ReviewRecommendation.ESCALATE)
        warnings = [f for f in out.findings if f.severity == FindingSeverity.WARNING]
        self.assertGreaterEqual(len(warnings), 1)
        self.assertIn("Missing source evidence", warnings[0].description)

    # 8. Internal consistency conflict
    async def test_08_internal_consistency_conflict(self):
        ctx = _make_context()
        ctx.input_payload = {
            "task_id": "task-consistency-fail",
            "assembled_output": {"status": "success"},
            "step_outputs": [
                {"step_1_id": "step-1", "user_status": "active"},
                {"step_2_id": "step-2", "user_status": "inactive"},
            ],
            "source_data": {"user_status": "active"},
        }
        res = await self.agent.execute(ctx)
        self.assertEqual(res.status, TaskStatus.COMPLETED)
        out = ReviewerOutput.model_validate(res.output_payload)
        self.assertFalse(out.passed)
        self.assertEqual(out.recommendation, ReviewRecommendation.REJECT)
        cons_errs = [f for f in out.findings if f.type == ReviewFindingType.CONSISTENCY]
        self.assertEqual(len(cons_errs), 1)
        self.assertEqual(cons_errs[0].field_reference, "user_status")

    # 9. Policy finding for apparent unredacted PII / cross-domain disclosure
    async def test_09_policy_finding_pii_and_cross_domain(self):
        ctx = _make_context()

        # Unredacted SSN
        ctx.input_payload = {
            "task_id": "task-pii-ssn",
            "assembled_output": {
                "employee_name": "Jane Doe",
                "ssn": "123-45-6789",
            },
            "source_data": {"employee_name": "Jane Doe"},
        }
        res = await self.agent.execute(ctx)
        out = ReviewerOutput.model_validate(res.output_payload)
        self.assertFalse(out.passed)
        self.assertEqual(out.recommendation, ReviewRecommendation.REJECT)
        pol_errs = [f for f in out.findings if f.type == ReviewFindingType.POLICY]
        self.assertGreaterEqual(len(pol_errs), 1)

        # Cross-domain financial disclosure to non-finance role
        ctx.input_payload = {
            "task_id": "task-cross-domain",
            "role_context": "it_user",
            "assembled_output": {
                "financial_records": [{"account": "operating_cash", "balance": 1000000}],
            },
            "source_data": {"financial_records": []},
        }
        res2 = await self.agent.execute(ctx)
        out2 = ReviewerOutput.model_validate(res2.output_payload)
        self.assertFalse(out2.passed)
        self.assertEqual(out2.recommendation, ReviewRecommendation.REJECT)
        cross_errs = [f for f in out2.findings if f.type == ReviewFindingType.POLICY]
        self.assertGreaterEqual(len(cross_errs), 1)
        self.assertIn("restricted financial data", cross_errs[0].description)

    # 10. All eight risk rules and precedence verification
    def test_10_all_eight_risk_rules_and_precedence(self):
        # Rule 1: operation_type in [write, delete, modify, deploy] -> HIGH
        for op in ["write", "delete", "modify", "deploy"]:
            res = ReviewerEngine.evaluate_risk(operation_type=op)
            self.assertEqual(res.risk_level, "high", f"Rule 1 failed for {op}")
            self.assertIn("Rule 1", res.matched_rule)

        # Rule 1 precedence over Rule 6 (operation_type=write + data_class=hr_personal_non_pii)
        res_overlap_1 = ReviewerEngine.evaluate_risk(
            operation_type="write", data_class="hr_personal_non_pii"
        )
        self.assertEqual(res_overlap_1.risk_level, "high")
        self.assertIn("Rule 1", res_overlap_1.matched_rule)

        # Rule 2: data_class = financial_write -> HIGH
        res_2 = ReviewerEngine.evaluate_risk(data_class="financial_write")
        self.assertEqual(res_2.risk_level, "high")
        self.assertIn("Rule 2", res_2.matched_rule)

        # Rule 2 precedence over Rule 5
        res_overlap_2 = ReviewerEngine.evaluate_risk(
            data_class="financial_write",
            agent_id="AGT-DEV",
            operation="sandbox_execution",
        )
        self.assertEqual(res_overlap_2.risk_level, "high")
        self.assertIn("Rule 2", res_overlap_2.matched_rule)

        # Rule 3: data_class = hr_pii -> HIGH
        res_3 = ReviewerEngine.evaluate_risk(data_class="hr_pii")
        self.assertEqual(res_3.risk_level, "high")
        self.assertIn("Rule 3", res_3.matched_rule)

        # Rule 4: data_class = restricted_document -> HIGH
        res_4 = ReviewerEngine.evaluate_risk(data_class="restricted_document")
        self.assertEqual(res_4.risk_level, "high")
        self.assertIn("Rule 4", res_4.matched_rule)

        # Rule 5: agent_id = AGT-DEV and operation = sandbox_execution -> MEDIUM
        res_5 = ReviewerEngine.evaluate_risk(
            agent_id="AGT-DEV", operation="sandbox_execution"
        )
        self.assertEqual(res_5.risk_level, "medium")
        self.assertIn("Rule 5", res_5.matched_rule)

        # Rule 6: data_class in [financial_read, hr_personal_non_pii, cross_domain] -> MEDIUM
        for dc in ["financial_read", "hr_personal_non_pii", "cross_domain"]:
            res_6 = ReviewerEngine.evaluate_risk(data_class=dc)
            self.assertEqual(res_6.risk_level, "medium", f"Rule 6 failed for {dc}")
            self.assertIn("Rule 6", res_6.matched_rule)

        # Rule 6 precedence over Rule 7 (data_class=cross_domain + AGT-FIN read)
        res_overlap_6 = ReviewerEngine.evaluate_risk(
            data_class="cross_domain", agent_id="AGT-FIN", operation="read"
        )
        self.assertEqual(res_overlap_6.risk_level, "medium")
        self.assertIn("Rule 6", res_overlap_6.matched_rule)

        # Rule 7: agent_id in [AGT-FIN, AGT-HR] and operation = read -> MEDIUM
        for agt in ["AGT-FIN", "AGT-HR", "finance", "hr"]:
            res_7 = ReviewerEngine.evaluate_risk(agent_id=agt, operation="read")
            self.assertEqual(res_7.risk_level, "medium", f"Rule 7 failed for {agt}")
            self.assertIn("Rule 7", res_7.matched_rule)

        # Rule 8: default -> LOW
        res_8 = ReviewerEngine.evaluate_risk(agent_id="AGT-DOC", operation="parse")
        self.assertEqual(res_8.risk_level, "low")
        self.assertIn("Rule 8", res_8.matched_rule)

    # 11. Human approval prevents automatic approval
    async def test_11_human_approval_prevents_automatic_approval(self):
        ctx = _make_context()
        ctx.input_payload = {
            "task_id": "task-hitl-required",
            "assembled_output": {"summary": "Execution passed completely."},
            "source_data": {"summary": "Execution passed completely."},
            "requires_human_approval": True,
        }
        res = await self.agent.execute(ctx)
        self.assertEqual(res.status, TaskStatus.COMPLETED)
        out = ReviewerOutput.model_validate(res.output_payload)
        self.assertTrue(out.passed)
        # MUST NOT auto-approve: recommendation must be ESCALATE
        self.assertEqual(out.recommendation, ReviewRecommendation.ESCALATE)

    # 12. LLM failure/timeout fails to ESCALATE rather than approve
    async def test_12_llm_failure_fails_to_escalate(self):
        mock_chat = MagicMock()
        mock_chat.ainvoke = AsyncMock(side_effect=RuntimeError("LLM timeout"))
        mock_wrapper = ModelProviderWrapper(
            provider_id="mock-prov",
            model_name="mock-model",
            category=ModelProviderCategory.LOCAL,
            chat_model=mock_chat,
        )
        agent_with_llm = ReviewerValidationAgent(model_provider=mock_wrapper)

        ctx = _make_context()
        ctx.input_payload = {
            "task_id": "task-llm-fail",
            "assembled_output": {"summary": "Valid output."},
            "source_data": {"summary": "Valid output."},
        }
        res = await agent_with_llm.execute(ctx)
        self.assertEqual(res.status, TaskStatus.COMPLETED)
        out = ReviewerOutput.model_validate(res.output_payload)
        # Check fail-escalate guarantee: does not approve, returns ESCALATE
        self.assertEqual(out.recommendation, ReviewRecommendation.ESCALATE)
        self.assertFalse(out.provenance.llm_used_for_checks)
        llm_findings = [f for f in out.findings if "LLM" in f.description]
        self.assertEqual(len(llm_findings), 1)

    # 13. Read-only behavior for nested workflow output and provenance
    async def test_13_read_only_behavior_for_nested_output(self):
        ctx = _make_context()
        nested_output = {
            "root_key": "val",
            "nested_dict": {"k1": [1, 2, 3]},
            "nested_list": [{"id": 1}],
        }
        orig_copy = copy.deepcopy(nested_output)
        ctx.input_payload = {
            "task_id": "task-read-only",
            "assembled_output": nested_output,
            "source_data": {"root_key": "val"},
        }
        res = await self.agent.execute(ctx)
        self.assertEqual(res.status, TaskStatus.COMPLETED)
        self.assertEqual(nested_output, orig_copy)

    # 14. Safe error redaction
    async def test_14_safe_error_redaction(self):
        ctx = _make_context(
            role="unauthorized_role",
            metadata={"secret_token": "api_key=sk-12345secretpassword"},
        )
        ctx.input_payload = {
            "task_id": "task-safe-err",
            "assembled_output": {"summary": "Safe error test"},
            "source_data": {"summary": "Safe error test"},
        }
        res = await self.agent.execute(ctx)
        self.assertEqual(res.status, TaskStatus.FAILED)
        self.assertNotIn("sk-12345secretpassword", str(res.error))

    # 15. AgentRegistry registration and dispatch compatibility
    async def test_15_registry_registration_and_dispatch(self):
        registry = AgentRegistry()
        registry.register_agent(self.agent)

        self.assertTrue(registry.has_agent(AgentID.REVIEWER_VALIDATION))
        self.assertIs(registry.get_agent(AgentID.REVIEWER_VALIDATION), self.agent)

        ctx = _make_context()
        ctx.input_payload = {
            "task_id": "task-dispatch",
            "assembled_output": {"summary": "Registry test."},
            "source_data": {"summary": "Registry test."},
        }
        res = await registry.dispatch(ctx)
        self.assertEqual(res.status, TaskStatus.COMPLETED)
        out = ReviewerOutput.model_validate(res.output_payload)
        self.assertEqual(out.task_id, "task-dispatch")

    # 16. Provenance fields, findings count, and UTC-aware timestamps
    async def test_16_provenance_and_timestamps(self):
        ctx = _make_context()
        ctx.input_payload = {
            "task_id": "task-prov",
            "assembled_output": {"summary": "Provenance check."},
            "source_data": {"summary": "Provenance check."},
        }
        res = await self.agent.execute(ctx)
        out = ReviewerOutput.model_validate(res.output_payload)
        prov = out.provenance

        self.assertEqual(prov.agent_id, "AGT-REV")
        self.assertEqual(prov.task_id, "task-prov")
        self.assertIn("error", prov.findings_count)
        self.assertIn("warning", prov.findings_count)
        self.assertIn("info", prov.findings_count)
        self.assertEqual(prov.risk_level, "low")
        self.assertEqual(prov.recommendation, ReviewRecommendation.APPROVE)
        self.assertIsNotNone(prov.reviewed_at.tzinfo)
        self.assertEqual(prov.reviewed_at.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
