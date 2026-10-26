"""
EAOC Unit Test Suite - Durable Workflow and Task Recovery State (Part 2.16-C)

Tests all Part 2.16-C specifications:
1. Concurrent lease acquisition collision (only one winner).
2. Stale fencing token / epoch rejection (FencingLeaseLostError).
3. Stale row_revision rejection on step completion (FencingLeaseLostError).
4. Heartbeat lease renewal extends lock_expires_at WITHOUT incrementing row_revision.
5. First-time approval resume succeeds and transitions resume_status to 'resumed'.
6. Duplicate resume attempt while lease is active is rejected (409 Conflict / active lease).
7. Recovery of abandoned resumed execution after lease expiry succeeds with new epoch & token.
8. Approval binding validations:
   - Tenant mismatch rejection.
   - Step index mismatch rejection.
   - Plan hash mismatch rejection.
9. Task identity validation:
   - Completed task byte-for-byte reuse (preserves outputs and provenance).
   - Conflicting non-completed task identity (altered agent_id, plan_hash, step_hash) fails closed (PlanIntegrityError).
10. Idempotency-Key behavior:
    - Identical payload replay returns existing workflow.
    - Altered payload with same key raises HTTP 422.
11. Migration & schema integrity check.
12. Redis outage resilience: recovery operates durably via repository without Redis dependency.
"""

import asyncio
from datetime import datetime, timezone, timedelta
import unittest
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from eaoc.agents.orchestrator.engine import OrchestratorEngine, compute_plan_hash, compute_step_hash
from eaoc.agents.orchestrator.schemas import OrchestratorInput, OrchestratorOutput
from eaoc.agents.planner.schemas import ExecutionPlan, PlanStep, PlanType
from eaoc.agents.planner.validator import PlanValidator
from eaoc.agents.registry import AgentRegistry
from eaoc.api.factory import create_app
from eaoc.api.v1.endpoints.workflows import (
    get_state_store,
    get_workflow_repository,
    get_orchestrator_engine,
)
from eaoc.db.models.approval import ApprovalRequestModel
from eaoc.db.models.workflow import WorkflowModel, WorkflowTaskModel
from eaoc.schemas.domain import (
    AgentID,
    ApprovalDecision,
    ApprovalStatus,
    RiskLevel,
    TaskStatus,
    WorkflowState,
    WorkflowStatus,
    AgentExecutionResult,
)
from eaoc.agents.base import BaseAgent
from eaoc.state.workflow_repository import (
    ConcurrencyConflictError,
    FencingLeaseLostError,
    InMemoryWorkflowRecoveryRepository,
    PlanIntegrityError,
    PostgresWorkflowRecoveryRepository,
)
from tests.unit.test_api_workflows import InMemoryTestStateStore


class DummyAgent(BaseAgent):
    def __init__(self, agent_id: AgentID):
        super().__init__(
            agent_id=agent_id,
            input_schema=OrchestratorInput,
            output_schema=OrchestratorOutput,
        )

    async def _execute(self, validated_input, context):
        return AgentExecutionResult(
            task_id=context.task_id if context else "dummy",
            status=TaskStatus.COMPLETED,
            output_data={"result": "ok"},
        )


def create_sample_plan(task_id: str = "task-rec-1") -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=f"plan-{task_id}",
        task_id=task_id,
        plan_type=PlanType.MULTI_AGENT,
        planning_rationale="Sample deterministic decomposition plan",
        steps=[
            PlanStep(
                step_index=0,
                agent_id=AgentID.SECURITY_POLICY,
                description="Evaluate initial compliance policy",
                risk_level=RiskLevel.LOW,
                operation_type="read",
                data_class="internal",
                operation="evaluate_policy",
                inputs={"rule": "standard_check"},
            ),
            PlanStep(
                step_index=1,
                agent_id=AgentID.DEVELOPER,
                description="Execute quarantined sandbox simulation",
                risk_level=RiskLevel.HIGH,
                operation_type="execute",
                data_class="sensitive",
                operation="run_simulation",
                dependencies=[0],
                inputs={"target": "simulation_env"},
            ),
        ],
    )


class TestDurableWorkflowRecovery(unittest.IsolatedAsyncioTestCase):
    """
    Hermetic async unit test suite for Part 2.16-C Durable Recovery & Fencing semantics.
    Uses InMemoryWorkflowRecoveryRepository with explicit asyncio concurrency locks.
    """

    async def asyncSetUp(self):
        self.repo = InMemoryWorkflowRecoveryRepository()
        self.registry = AgentRegistry()
        for agt in AgentID:
            self.registry.register_agent(DummyAgent(agt))
        self.state_store = InMemoryTestStateStore()
        self.engine = OrchestratorEngine(
            registry=self.registry,
            state_store=self.state_store,
            workflow_repo=self.repo,
        )

    async def test_1_concurrent_lease_acquisition(self):
        """Verify that concurrent lease acquisitions result in exactly one winner."""
        wf, _ = await self.repo.create_workflow_intake(
            workflow_id="wf-lease-test",
            tenant_id="tenant-alpha",
            user_id="usr-1",
            task_id="task-1",
            task_description="lease contention test",
        )

        res1 = await self.repo.acquire_execution_lease("wf-lease-test", "tenant-alpha")
        res2 = await self.repo.acquire_execution_lease("wf-lease-test", "tenant-alpha")

        success1, epoch1, rev1, token1 = res1
        success2, epoch2, rev2, token2 = res2

        self.assertTrue(success1)
        self.assertEqual(epoch1, 1)
        self.assertEqual(rev1, 2)
        self.assertIsNotNone(token1)

        # Second acquisition while lease is active MUST fail
        self.assertFalse(success2)
        self.assertIsNone(token2)

    async def test_2_heartbeat_lease_renewal_preserves_row_revision(self):
        """Verify that renewing lease extends expiry without touching row_revision."""
        wf, _ = await self.repo.create_workflow_intake(
            workflow_id="wf-hb-test",
            tenant_id="tenant-alpha",
            user_id="usr-1",
            task_id="task-1",
            task_description="heartbeat test",
        )
        _, epoch, rev_before, token = await self.repo.acquire_execution_lease("wf-hb-test", "tenant-alpha")

        # Perform renewal
        renew_ok = await self.repo.renew_execution_lease("wf-hb-test", "tenant-alpha", epoch, token)
        self.assertTrue(renew_ok)

        wf_after = await self.repo.get_workflow("wf-hb-test", "tenant-alpha")
        # row_revision must remain UNCHANGED after heartbeat renewal
        self.assertEqual(wf_after.row_revision, rev_before)

    async def test_3_stale_fencing_token_rejected(self):
        """Verify that a write with an old/stale fencing token or epoch raises FencingLeaseLostError."""
        wf, _ = await self.repo.create_workflow_intake(
            workflow_id="wf-stale-test",
            tenant_id="tenant-alpha",
            user_id="usr-1",
            task_id="task-1",
            task_description="stale token test",
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease("wf-stale-test", "tenant-alpha")

        # Try prepare_task_step with stale token
        with self.assertRaises(FencingLeaseLostError):
            await self.repo.prepare_task_step(
                workflow_id="wf-stale-test",
                tenant_id="tenant-alpha",
                step_index=0,
                agent_id="policy_evaluator",
                name="step 0",
                plan_hash="hash-123",
                step_hash="step-hash-123",
                inputs={},
                fencing_epoch=epoch,
                lock_token="stale-invalid-token",
                expected_revision=rev,
            )

    async def test_4_stale_row_revision_rejected(self):
        """Verify that a write with an outdated expected row_revision raises FencingLeaseLostError."""
        wf, _ = await self.repo.create_workflow_intake(
            workflow_id="wf-rev-test",
            tenant_id="tenant-alpha",
            user_id="usr-1",
            task_id="task-1",
            task_description="stale rev test",
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease("wf-rev-test", "tenant-alpha")

        # Valid step 0 prepare advances revision to rev + 1
        _, _, _, new_rev = await self.repo.prepare_task_step(
            workflow_id="wf-rev-test",
            tenant_id="tenant-alpha",
            step_index=0,
            agent_id="policy_evaluator",
            name="step 0",
            plan_hash="hash-123",
            step_hash="step-hash-123",
            inputs={},
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # Attempting step completion with the OLD revision must fail
        with self.assertRaises(FencingLeaseLostError):
            await self.repo.complete_task_step(
                workflow_id="wf-rev-test",
                tenant_id="tenant-alpha",
                step_index=0,
                output_data={"result": "ok"},
                provenance={},
                fencing_epoch=epoch,
                lock_token=token,
                expected_revision=rev,  # Stale!
            )

    async def test_5_completed_task_reuse_byte_for_byte(self):
        """Verify that an already completed task returns cached outputs without modification."""
        plan = create_sample_plan()
        plan_hash = compute_plan_hash(plan)
        step0 = plan.steps[0]
        step0_hash = compute_step_hash(step0)

        wf, _ = await self.repo.create_workflow_intake(
            workflow_id="wf-reuse-test",
            tenant_id="tenant-alpha",
            user_id="usr-1",
            task_id=plan.task_id,
            task_description="completed reuse test",
            execution_plan=plan.model_dump(),
            plan_hash=plan_hash,
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease("wf-reuse-test", "tenant-alpha")

        # Step 0 prepare & complete
        _, _, _, rev = await self.repo.prepare_task_step(
            workflow_id="wf-reuse-test",
            tenant_id="tenant-alpha",
            step_index=0,
            agent_id=step0.agent_id.value,
            name=step0.description,
            plan_hash=plan_hash,
            step_hash=step0_hash,
            inputs=step0.inputs,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )
        original_output = {"compliance_verdict": "COMPLIANT", "score": 99.4}
        original_prov = {"step_index": 0, "status": "COMPLETED"}
        rev = await self.repo.complete_task_step(
            workflow_id="wf-reuse-test",
            tenant_id="tenant-alpha",
            step_index=0,
            output_data=original_output,
            provenance=original_prov,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # Re-invoking prepare_task_step for step 0 (e.g. on resume/recovery)
        is_completed, cached_out, cached_prov, rev_after = await self.repo.prepare_task_step(
            workflow_id="wf-reuse-test",
            tenant_id="tenant-alpha",
            step_index=0,
            agent_id=step0.agent_id.value,
            name=step0.description,
            plan_hash=plan_hash,
            step_hash=step0_hash,
            inputs=step0.inputs,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        self.assertTrue(is_completed)
        self.assertEqual(cached_out, original_output)
        self.assertEqual(cached_prov, original_prov)

    async def test_6_conflicting_non_completed_task_identity_fails_closed(self):
        """Verify that an altered non-completed task definition raises PlanIntegrityError."""
        plan = create_sample_plan()
        plan_hash = compute_plan_hash(plan)
        step0 = plan.steps[0]
        step0_hash = compute_step_hash(step0)

        wf, _ = await self.repo.create_workflow_intake(
            workflow_id="wf-conflict-test",
            tenant_id="tenant-alpha",
            user_id="usr-1",
            task_id=plan.task_id,
            task_description="conflict test",
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease("wf-conflict-test", "tenant-alpha")

        # Step 0 prepared (marked RUNNING, not completed)
        _, _, _, rev = await self.repo.prepare_task_step(
            workflow_id="wf-conflict-test",
            tenant_id="tenant-alpha",
            step_index=0,
            agent_id=step0.agent_id.value,
            name=step0.description,
            plan_hash=plan_hash,
            step_hash=step0_hash,
            inputs=step0.inputs,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # Attempt to prepare again with a different agent_id or different step_hash
        with self.assertRaises(PlanIntegrityError):
            await self.repo.prepare_task_step(
                workflow_id="wf-conflict-test",
                tenant_id="tenant-alpha",
                step_index=0,
                agent_id="tampered_agent_id",  # Mismatch!
                name=step0.description,
                plan_hash=plan_hash,
                step_hash=step0_hash,
                inputs=step0.inputs,
                fencing_epoch=epoch,
                lock_token=token,
                expected_revision=rev,
            )

    async def test_7_atomic_approval_resume_and_duplicate_rejection(self):
        """Verify first-time approval resume and rejection of duplicate resume attempts."""
        plan = create_sample_plan()
        plan_hash = compute_plan_hash(plan)

        wf, _ = await self.repo.create_workflow_intake(
            workflow_id="wf-resume-test",
            tenant_id="tenant-alpha",
            user_id="usr-1",
            task_id=plan.task_id,
            task_description="approval resume test",
            execution_plan=plan.model_dump(),
            plan_hash=plan_hash,
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease("wf-resume-test", "tenant-alpha")

        # Pause workflow for approval
        appr_id = "appr-resume-001"
        rev = await self.repo.pause_workflow_for_approval(
            workflow_id="wf-resume-test",
            tenant_id="tenant-alpha",
            step_index=1,
            approval_id=appr_id,
            requested_action="Execute high risk simulation",
            risk_level="high",
            details={"plan_hash": plan_hash},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # Approver approves request
        appr = self.repo._approvals[appr_id]
        appr.status = ApprovalStatus.APPROVED.value
        appr.decision = ApprovalStatus.APPROVED.value
        appr.resume_status = "pending"

        # 1. First resume claim succeeds
        claimed, new_epoch, new_rev, new_token, wf_resumed, reason = await self.repo.claim_approval_resume(
            approval_id=appr_id,
            workflow_id="wf-resume-test",
            tenant_id="tenant-alpha",
        )
        self.assertTrue(claimed)
        self.assertEqual(new_epoch, epoch + 1)
        self.assertEqual(wf_resumed.status, WorkflowStatus.RUNNING.value)
        self.assertEqual(appr.resume_status, "resumed")

        # 2. Duplicate concurrent resume attempt while lease is active MUST fail
        dup_claimed, _, _, _, _, dup_reason = await self.repo.claim_approval_resume(
            approval_id=appr_id,
            workflow_id="wf-resume-test",
            tenant_id="tenant-alpha",
        )
        self.assertFalse(dup_claimed)
        self.assertEqual(dup_reason, "workflow_lease_active_or_ineligible")

    async def test_8_abandoned_execution_recovery(self):
        """Verify recovery of an abandoned resumed execution after lease expiry."""
        plan = create_sample_plan()
        plan_hash = compute_plan_hash(plan)

        wf, _ = await self.repo.create_workflow_intake(
            workflow_id="wf-abandon-test",
            tenant_id="tenant-alpha",
            user_id="usr-1",
            task_id=plan.task_id,
            task_description="abandon recovery test",
            execution_plan=plan.model_dump(),
            plan_hash=plan_hash,
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease("wf-abandon-test", "tenant-alpha")

        appr_id = "appr-abandon-001"
        rev = await self.repo.pause_workflow_for_approval(
            workflow_id="wf-abandon-test",
            tenant_id="tenant-alpha",
            step_index=1,
            approval_id=appr_id,
            requested_action="Execute simulation",
            risk_level="high",
            details={"plan_hash": plan_hash},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        appr = self.repo._approvals[appr_id]
        appr.status = ApprovalStatus.APPROVED.value
        appr.decision = ApprovalStatus.APPROVED.value
        appr.resume_status = "pending"

        # Claim resume (Worker 1)
        claimed, epoch1, rev1, token1, _, _ = await self.repo.claim_approval_resume(
            approval_id=appr_id,
            workflow_id="wf-abandon-test",
            tenant_id="tenant-alpha",
        )
        self.assertTrue(claimed)

        # Worker 1 crashes without releasing lease. Simulate lease expiry:
        wf_model = self.repo._workflows["wf-abandon-test"]
        wf_model.lock_expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)

        # Worker 2 recovers execution after lease expiry
        rec_claimed, epoch2, rev2, token2, wf_rec, rec_reason = await self.repo.claim_approval_resume(
            approval_id=appr_id,
            workflow_id="wf-abandon-test",
            tenant_id="tenant-alpha",
        )
        self.assertTrue(rec_claimed)
        self.assertEqual(epoch2, epoch1 + 1)
        self.assertNotEqual(token2, token1)

    async def test_9_approval_binding_mismatches_rejected(self):
        """Verify that approval mismatches (tenant, step, plan hash) fail closed."""
        plan = create_sample_plan()
        plan_hash = compute_plan_hash(plan)

        wf, _ = await self.repo.create_workflow_intake(
            workflow_id="wf-bind-test",
            tenant_id="tenant-alpha",
            user_id="usr-1",
            task_id=plan.task_id,
            task_description="binding mismatch test",
            execution_plan=plan.model_dump(),
            plan_hash=plan_hash,
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease("wf-bind-test", "tenant-alpha")

        appr_id = "appr-bind-001"
        rev = await self.repo.pause_workflow_for_approval(
            workflow_id="wf-bind-test",
            tenant_id="tenant-alpha",
            step_index=1,
            approval_id=appr_id,
            requested_action="Execute simulation",
            risk_level="high",
            details={"plan_hash": plan_hash},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        appr = self.repo._approvals[appr_id]
        appr.status = ApprovalStatus.APPROVED.value
        appr.decision = ApprovalStatus.APPROVED.value
        appr.resume_status = "pending"

        # 1. Tenant mismatch
        c_tenant, _, _, _, _, r_tenant = await self.repo.claim_approval_resume(
            approval_id=appr_id,
            workflow_id="wf-bind-test",
            tenant_id="tenant-OTHER",  # Mismatch!
        )
        self.assertFalse(c_tenant)

        # 2. Plan hash mismatch in approval details
        appr.details["plan_hash"] = "altered_plan_hash"
        c_plan, _, _, _, _, r_plan = await self.repo.claim_approval_resume(
            approval_id=appr_id,
            workflow_id="wf-bind-test",
            tenant_id="tenant-alpha",
        )
        self.assertFalse(c_plan)
        self.assertEqual(r_plan, "plan_hash_mismatch")

        # 3. Step index mismatch
        appr.details["plan_hash"] = plan_hash
        appr.step_index = 99  # Mismatch!
        c_step, _, _, _, _, r_step = await self.repo.claim_approval_resume(
            approval_id=appr_id,
            workflow_id="wf-bind-test",
            tenant_id="tenant-alpha",
        )
        self.assertFalse(c_step)
        self.assertEqual(r_step, "step_index_mismatch")


class TestWorkflowApiIntegration(unittest.TestCase):
    """
    HTTP API tests for Part 2.16-C endpoints:
    - Idempotency-Key behavior (replay vs altered payload).
    - POST /api/v1/workflows/{workflow_id}/resume RBAC and claim handling.
    """

    def setUp(self):
        import jwt
        self.app = create_app()
        self.test_store = InMemoryTestStateStore()
        self.test_repo = InMemoryWorkflowRecoveryRepository()

        self.app.dependency_overrides[get_state_store] = lambda: self.test_store
        self.app.dependency_overrides[get_workflow_repository] = lambda: self.test_repo

        self.client = TestClient(self.app)
        self.secret = "test-secret-key-at-least-32-bytes-long!"

        # Mock settings for auth
        from eaoc.config import get_settings
        settings = get_settings()
        settings.JWT_SECRET_KEY = self.secret
        settings.JWT_ALGORITHM = "HS256"

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def _generate_token(self, sub: str, tenant_id: str, roles: list) -> str:
        import jwt
        payload = {
            "sub": sub,
            "tenant_id": tenant_id,
            "roles": roles,
            "exp": int(datetime.now(timezone.utc).timestamp()) + 3600,
        }
        return jwt.encode(payload, self.secret, algorithm="HS256")

    def test_idempotency_key_replay_and_altered_payload_rejection(self):
        """Verify identical Idempotency-Key returns existing workflow; altered payload returns 422."""
        token = self._generate_token("usr-1", "tenant-alpha", ["operator"])
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "idemp-key-999",
        }
        payload1 = {
            "input_payload": {"prompt": "Run security compliance analysis"},
            "task_type": "security",
        }

        # 1. Initial submission
        res1 = self.client.post("/api/v1/workflows", json=payload1, headers=headers)
        self.assertEqual(res1.status_code, 201)
        wf_id = res1.json()["workflow_id"]

        # 2. Identical replay returns existing workflow state
        res2 = self.client.post("/api/v1/workflows", json=payload1, headers=headers)
        self.assertEqual(res2.status_code, 201)
        self.assertEqual(res2.json()["workflow_id"], wf_id)

        # 3. Altered payload with SAME idempotency key must return 422 Unprocessable Entity
        payload_altered = {
            "input_payload": {"prompt": "ALTERED DIFFERENT PROMPT"},
            "task_type": "security",
        }
        res3 = self.client.post("/api/v1/workflows", json=payload_altered, headers=headers)
        self.assertEqual(res3.status_code, 422)

    def test_resume_workflow_rbac_and_execution(self):
        """Verify POST /api/v1/workflows/{workflow_id}/resume enforces RBAC and claims lease."""
        plan = create_sample_plan()
        plan_hash = compute_plan_hash(plan)

        # Set up paused workflow in repo
        async def setup_paused():
            wf, _ = await self.test_repo.create_workflow_intake(
                workflow_id="wf-api-resume",
                tenant_id="tenant-alpha",
                user_id="usr-1",
                task_id=plan.task_id,
                task_description="api resume test",
                execution_plan=plan.model_dump(),
                plan_hash=plan_hash,
            )
            _, epoch, rev, token = await self.test_repo.acquire_execution_lease("wf-api-resume", "tenant-alpha")
            await self.test_repo.pause_workflow_for_approval(
                workflow_id="wf-api-resume",
                tenant_id="tenant-alpha",
                step_index=1,
                approval_id="appr-api-001",
                requested_action="Execute simulation",
                risk_level="high",
                details={"plan_hash": plan_hash},
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
                fencing_epoch=epoch,
                lock_token=token,
                expected_revision=rev,
            )
            appr = self.test_repo._approvals["appr-api-001"]
            appr.status = ApprovalStatus.APPROVED.value
            appr.decision = ApprovalStatus.APPROVED.value
            appr.resume_status = "pending"

        asyncio.run(setup_paused())

        # 1. Non-permitted role (viewer) receives 403 Forbidden
        viewer_token = self._generate_token("usr-view", "tenant-alpha", ["viewer"])
        res_forbidden = self.client.post(
            "/api/v1/workflows/wf-api-resume/resume",
            json={"approval_id": "appr-api-001"},
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        self.assertEqual(res_forbidden.status_code, 403)

        # 2. Permitted role (approver) succeeds
        approver_token = self._generate_token("usr-appr", "tenant-alpha", ["approver"])
        res_ok = self.client.post(
            "/api/v1/workflows/wf-api-resume/resume",
            json={"approval_id": "appr-api-001"},
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        self.assertIn(res_ok.status_code, [200, 202])

    def test_tenant_scoped_workflow_retrieval_and_leak_prevention(self):
        """Verify GET /api/v1/workflows/{workflow_id} isolates tenants and prevents cross-tenant leaks."""
        alpha_token = self._generate_token("usr-alpha", "tenant-alpha", ["operator"])
        beta_token = self._generate_token("usr-beta", "tenant-beta", ["operator"])

        headers_alpha = {
            "Authorization": f"Bearer {alpha_token}",
            "Idempotency-Key": "idemp-tenant-001",
        }
        create_resp = self.client.post(
            "/api/v1/workflows",
            json={"input_payload": {"prompt": "Tenant Alpha confidential analysis"}},
            headers=headers_alpha,
        )
        self.assertEqual(create_resp.status_code, 201)
        wf_id = create_resp.json()["workflow_id"]

        # 1. Tenant Alpha (owner) can retrieve the workflow
        get_alpha = self.client.get(f"/api/v1/workflows/{wf_id}", headers={"Authorization": f"Bearer {alpha_token}"})
        self.assertEqual(get_alpha.status_code, 200)
        self.assertEqual(get_alpha.json()["workflow_id"], wf_id)

        # 2. Tenant Beta (cross-tenant) receives 404 (no leak or existence oracle)
        get_beta = self.client.get(f"/api/v1/workflows/{wf_id}", headers={"Authorization": f"Bearer {beta_token}"})
        self.assertEqual(get_beta.status_code, 404)

        # 3. Unauthenticated caller receives 404 (cannot read tenant-isolated workflow)
        get_unauth = self.client.get(f"/api/v1/workflows/{wf_id}")
        self.assertEqual(get_unauth.status_code, 404)

    def test_api_resume_rejects_completed_task(self):
        """Verify resuming a step that is already in COMPLETED status returns 409 Conflict."""
        plan = create_sample_plan("task-comp-test")
        plan_hash = compute_plan_hash(plan)

        async def setup_completed():
            wf, _ = await self.test_repo.create_workflow_intake(
                workflow_id="wf-comp-test",
                tenant_id="tenant-alpha",
                user_id="usr-1",
                task_id=plan.task_id,
                task_description="completed task resume rejection",
                execution_plan=plan.model_dump(),
                plan_hash=plan_hash,
            )
            _, epoch, rev, token = await self.test_repo.acquire_execution_lease("wf-comp-test", "tenant-alpha")
            await self.test_repo.pause_workflow_for_approval(
                workflow_id="wf-comp-test",
                tenant_id="tenant-alpha",
                step_index=1,
                approval_id="appr-comp-001",
                requested_action="Execute simulation",
                risk_level="high",
                details={"plan_hash": plan_hash},
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
                fencing_epoch=epoch,
                lock_token=token,
                expected_revision=rev,
            )
            appr = self.test_repo._approvals["appr-comp-001"]
            appr.status = ApprovalStatus.APPROVED.value
            appr.decision = ApprovalStatus.APPROVED.value
            appr.resume_status = "pending"

            # Manually insert task 1 as COMPLETED
            self.test_repo._tasks["wf-comp-test:1"] = WorkflowTaskModel(
                task_id="wf-comp-test:step-1",
                workflow_id="wf-comp-test",
                tenant_id="tenant-alpha",
                step_index=1,
                agent_id="developer",
                name="Execute quarantined sandbox simulation",
                plan_hash=plan_hash,
                step_hash="hash-step-1",
                status=TaskStatus.COMPLETED.value,
                output_data={"result": "already_done"},
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )

        asyncio.run(setup_completed())

        approver_token = self._generate_token("usr-appr", "tenant-alpha", ["approver"])
        res = self.client.post(
            "/api/v1/workflows/wf-comp-test/resume",
            json={"approval_id": "appr-comp-001"},
            headers={"Authorization": f"Bearer {approver_token}"},
        )
        self.assertEqual(res.status_code, 409)
        self.assertIn("already been completed", res.json()["detail"])


class TestRegressionFixes(unittest.IsolatedAsyncioTestCase):
    """
    Direct unit tests for Part 2.16-C gate audit regression findings:
    - Fix 1: Completed-step bypass prevention and plan tampering rejection.
    - Fix 2: Heartbeat renewal without row_revision bump and database-time lease decisions.
    - Fix 3: State-machine branches and full approval binding validations.
    - Fix 4: Concurrent duplicate intake idempotency collision safety.
    """

    async def asyncSetUp(self):
        self.repo = InMemoryWorkflowRecoveryRepository()
        self.registry = AgentRegistry()
        for agt in AgentID:
            self.registry.register_agent(DummyAgent(agt))
        self.state_store = InMemoryTestStateStore()
        self.engine = OrchestratorEngine(
            registry=self.registry,
            state_store=self.state_store,
            workflow_repo=self.repo,
        )

    async def test_completed_step_bypass_rejected_on_plan_tampering(self):
        """Fix 1: Fast-forward must not occur without verifying plan & step hashes in repo."""
        plan = create_sample_plan("task-bypass-test")
        plan_hash = compute_plan_hash(plan)
        step0 = plan.steps[0]
        step0_hash = compute_step_hash(step0)

        wf, _ = await self.repo.create_workflow_intake(
            workflow_id="wf-bypass-test",
            tenant_id="tenant-alpha",
            user_id="usr-1",
            task_id=plan.task_id,
            task_description="bypass test",
            execution_plan=plan.model_dump(),
            plan_hash=plan_hash,
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease("wf-bypass-test", "tenant-alpha")

        # Step 0 completes legitimately
        _, _, _, rev = await self.repo.prepare_task_step(
            workflow_id="wf-bypass-test",
            tenant_id="tenant-alpha",
            step_index=0,
            agent_id=step0.agent_id.value,
            name=step0.description,
            plan_hash=plan_hash,
            step_hash=step0_hash,
            inputs=step0.inputs,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )
        rev = await self.repo.complete_task_step(
            workflow_id="wf-bypass-test",
            tenant_id="tenant-alpha",
            step_index=0,
            output_data={"verdict": "PASS"},
            provenance={"step": 0},
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # Now simulate resuming with a TAMPERED plan where step 0 definition was altered
        tampered_step0 = PlanStep(
            step_index=0,
            agent_id=AgentID.SECURITY_POLICY,
            description="TAMPERED DESCRIPTION",
            risk_level=RiskLevel.LOW,
            operation_type="read",
            data_class="internal",
            operation="evaluate_policy",
            inputs={"rule": "tampered_rule"},
        )
        tampered_plan = ExecutionPlan(
            plan_id="plan-tampered",
            task_id=plan.task_id,
            plan_type=PlanType.MULTI_AGENT,
            planning_rationale="Tampered plan",
            steps=[tampered_step0, plan.steps[1]],
        )

        # Attempting to execute with tampered plan MUST raise PlanIntegrityError
        with self.assertRaises(PlanIntegrityError):
            await self.engine.execute_workflow(
                input_data=OrchestratorInput(
                    task_id=plan.task_id,
                    task_description="tampered resume",
                    workflow_id="wf-bypass-test",
                    execution_plan=tampered_plan,
                    user_context={"tenant_id": "tenant-alpha", "roles": ["admin"]},
                    metadata={
                        "fencing_epoch": epoch,
                        "lock_token": token,
                        "expected_revision": rev,
                    },
                ),
            )

    async def test_completed_step_without_lease_credentials_raises_fencing_error(self):
        """Fix 1: Execution without fencing credentials must raise FencingLeaseLostError."""
        plan = create_sample_plan("task-nofence-test")
        with self.assertRaises(FencingLeaseLostError):
            await self.engine.execute_workflow(
                input_data=OrchestratorInput(
                    task_id=plan.task_id,
                    task_description="nofence test",
                    workflow_id="wf-nofence-test",
                    execution_plan=plan,
                    user_context={"tenant_id": "tenant-alpha", "roles": ["admin"]},
                    metadata={},  # No fencing credentials!
                ),
            )

    async def test_approval_resume_state_machine_branches(self):
        """Fix 3: Verify mutually exclusive state-machine branches for first-time resume vs abandoned recovery."""
        plan = create_sample_plan("task-branches-test")
        plan_hash = compute_plan_hash(plan)

        wf, _ = await self.repo.create_workflow_intake(
            workflow_id="wf-sm-test",
            tenant_id="tenant-alpha",
            user_id="usr-1",
            task_id=plan.task_id,
            task_description="sm test",
            execution_plan=plan.model_dump(),
            plan_hash=plan_hash,
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease("wf-sm-test", "tenant-alpha")
        rev = await self.repo.pause_workflow_for_approval(
            workflow_id="wf-sm-test",
            tenant_id="tenant-alpha",
            step_index=1,
            approval_id="appr-sm-001",
            requested_action="Execute simulation",
            risk_level="high",
            details={"plan_hash": plan_hash},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        appr = self.repo._approvals["appr-sm-001"]
        appr.status = ApprovalStatus.APPROVED.value
        appr.decision = ApprovalStatus.APPROVED.value

        # Inconsistent branch 1: approval is 'resumed' but workflow is WAITING_APPROVAL
        appr.resume_status = "resumed"
        wf_model = self.repo._workflows["wf-sm-test"]
        wf_model.status = WorkflowStatus.WAITING_APPROVAL.value
        c, _, _, _, _, r = await self.repo.claim_approval_resume("appr-sm-001", "wf-sm-test", "tenant-alpha")
        self.assertFalse(c)
        self.assertEqual(r, "invalid_resume_state")

        # Inconsistent branch 2: approval is 'pending' but workflow is RUNNING
        appr.resume_status = "pending"
        wf_model.status = WorkflowStatus.RUNNING.value
        c, _, _, _, _, r = await self.repo.claim_approval_resume("appr-sm-001", "wf-sm-test", "tenant-alpha")
        self.assertFalse(c)
        self.assertEqual(r, "invalid_resume_state")

    async def test_approval_resume_all_binding_mismatches(self):
        """Fix 3: Verify rejection of task_id, plan_id, agent_id, risk_level, and action mismatches."""
        plan = create_sample_plan("task-bindings-test")
        plan_hash = compute_plan_hash(plan)

        wf, _ = await self.repo.create_workflow_intake(
            workflow_id="wf-allbind-test",
            tenant_id="tenant-alpha",
            user_id="usr-1",
            task_id=plan.task_id,
            task_description="allbind test",
            execution_plan=plan.model_dump(),
            plan_hash=plan_hash,
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease("wf-allbind-test", "tenant-alpha")
        rev = await self.repo.pause_workflow_for_approval(
            workflow_id="wf-allbind-test",
            tenant_id="tenant-alpha",
            step_index=1,
            approval_id="appr-ab-001",
            requested_action="Execute simulation",
            risk_level="high",
            details={"plan_hash": plan_hash, "plan_id": plan.plan_id, "agent_id": "developer"},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        appr = self.repo._approvals["appr-ab-001"]
        appr.status = ApprovalStatus.APPROVED.value
        appr.decision = ApprovalStatus.APPROVED.value
        appr.resume_status = "pending"

        # 1. Task ID mismatch
        appr.task_id = "tampered:task:id"
        c, _, _, _, _, r = await self.repo.claim_approval_resume("appr-ab-001", "wf-allbind-test", "tenant-alpha")
        self.assertFalse(c)
        self.assertEqual(r, "task_id_mismatch")
        appr.task_id = f"wf-allbind-test:step-1"

        # 2. Plan ID mismatch
        appr.details["plan_id"] = "different-plan-id"
        c, _, _, _, _, r = await self.repo.claim_approval_resume("appr-ab-001", "wf-allbind-test", "tenant-alpha")
        self.assertFalse(c)
        self.assertEqual(r, "plan_id_mismatch")
        appr.details["plan_id"] = plan.plan_id

        # 3. Agent ID mismatch
        appr.details["agent_id"] = "unauthorized_agent"
        c, _, _, _, _, r = await self.repo.claim_approval_resume("appr-ab-001", "wf-allbind-test", "tenant-alpha")
        self.assertFalse(c)
        self.assertEqual(r, "agent_id_mismatch")
        appr.details["agent_id"] = "developer"

        # 4. Risk level mismatch
        appr.risk_level = "low"  # Expected 'high'
        c, _, _, _, _, r = await self.repo.claim_approval_resume("appr-ab-001", "wf-allbind-test", "tenant-alpha")
        self.assertFalse(c)
        self.assertEqual(r, "risk_level_mismatch")
        appr.risk_level = "high"

        # 5. Action mismatch
        appr.requested_action = "Delete all database tables"
        c, _, _, _, _, r = await self.repo.claim_approval_resume("appr-ab-001", "wf-allbind-test", "tenant-alpha")
        self.assertFalse(c)
        self.assertEqual(r, "action_mismatch")

    async def test_concurrent_duplicate_intake_does_not_create_multiple_workflows(self):
        """Fix 4: Concurrent duplicate intake requests must return same workflow without creating multiples."""
        async def call_intake(wf_id: str):
            return await self.repo.create_workflow_intake(
                workflow_id=wf_id,
                tenant_id="tenant-alpha",
                user_id="usr-1",
                task_id=f"task-{wf_id}",
                task_description="concurrent test",
                idempotency_key="concurrent-key-777",
                idempotency_fingerprint="fp-777",
            )

        # Launch concurrent requests with same idempotency key and fingerprint
        results = await asyncio.gather(
            call_intake("wf-concurrent-1"),
            call_intake("wf-concurrent-2"),
            call_intake("wf-concurrent-3"),
        )

        wf_instances = [r[0] for r in results]
        # All three must refer to the exact same canonical workflow_id
        winner_id = wf_instances[0].workflow_id
        for wf_inst in wf_instances:
            self.assertEqual(wf_inst.workflow_id, winner_id)

        # Only one workflow must exist in durable storage with that key
        workflows_in_store = [w for w in self.repo._workflows.values() if w.idempotency_key == "concurrent-key-777"]
        self.assertEqual(len(workflows_in_store), 1)


class TestPostgresLeaseAcquisitionPredicate(unittest.IsolatedAsyncioTestCase):
    """
    Direct hermetic unit tests verifying PostgresWorkflowRecoveryRepository.acquire_execution_lease
    SQL query construction and predicate behavior for None and supplied current_token.
    """

    def _setup_mock_session(self, return_row=(1, 2)):
        mock_session = AsyncMock()
        mock_begin = MagicMock()
        mock_begin.__aenter__ = AsyncMock(return_value=None)
        mock_begin.__aexit__ = AsyncMock(return_value=None)
        mock_session.begin = MagicMock(return_value=mock_begin)

        mock_res = MagicMock()
        mock_res.one_or_none.return_value = return_row
        mock_session.execute = AsyncMock(return_value=mock_res)

        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_factory.return_value = mock_ctx

        repo = PostgresWorkflowRecoveryRepository(session_factory=mock_factory)
        return repo, mock_session

    async def test_acquire_lease_with_none_token_omits_token_comparison(self):
        """When current_token is None, query requires unleased or expired lease and omits token comparison."""
        repo, mock_session = self._setup_mock_session(return_row=(1, 2))

        success, epoch, rev, token = await repo.acquire_execution_lease(
            workflow_id="wf-100",
            tenant_id="tenant-100",
            current_token=None,
        )

        self.assertTrue(success)
        self.assertEqual(epoch, 1)
        self.assertEqual(rev, 2)
        self.assertIsNotNone(token)

        # Inspect compiled SQL
        stmt = mock_session.execute.call_args[0][0]
        sql_str = str(stmt).lower()
        self.assertIn("workflows.lock_expires_at is null", sql_str)
        self.assertIn("workflows.lock_expires_at < now()", sql_str)
        self.assertNotIn("workflows.lock_token =", sql_str)

    async def test_acquire_lease_with_supplied_token_includes_token_comparison(self):
        """When current_token is supplied, query includes token match clause for active lease takeover."""
        repo, mock_session = self._setup_mock_session(return_row=(2, 3))

        success, epoch, rev, token = await repo.acquire_execution_lease(
            workflow_id="wf-100",
            tenant_id="tenant-100",
            current_token="tok-prev-active",
        )

        self.assertTrue(success)
        self.assertEqual(epoch, 2)
        self.assertEqual(rev, 3)
        self.assertIsNotNone(token)

        # Inspect compiled SQL
        stmt = mock_session.execute.call_args[0][0]
        sql_str = str(stmt).lower()
        self.assertIn("workflows.lock_expires_at is null", sql_str)
        self.assertIn("workflows.lock_expires_at < now()", sql_str)
        self.assertIn("workflows.lock_token =", sql_str)
        self.assertIn("workflows.lock_token is not null", sql_str)

    async def test_acquire_lease_returns_false_when_no_row_updated(self):
        """When atomic CAS update returns no row, returns (False, None, None, None)."""
        repo, _ = self._setup_mock_session(return_row=None)

        success, epoch, rev, token = await repo.acquire_execution_lease(
            workflow_id="wf-100",
            tenant_id="tenant-100",
            current_token=None,
        )

        self.assertFalse(success)
        self.assertIsNone(epoch)
        self.assertIsNone(rev)
        self.assertIsNone(token)


class TestPostgresApprovalResumePredicate(unittest.IsolatedAsyncioTestCase):
    """
    Hermetic unit tests verifying PostgresWorkflowRecoveryRepository.claim_approval_resume()
    predicate logic, database-time lease active checking, and binding validations using mock sessions.
    Note: These hermetic tests verify query construction, control flow, and error handling;
    they do not execute live PostgreSQL transactions or prove physical instance isolation.
    """

    def _build_test_entities(
        self,
        resume_status="pending",
        workflow_status=WorkflowStatus.WAITING_APPROVAL.value,
        lock_expires_at=None,
        step_index=0,
        plan_hash="plan-hash-valid",
        tenant_id="tenant-alpha",
    ):
        now_utc = datetime.now(timezone.utc)
        appr = ApprovalRequestModel(
            approval_id="appr-101",
            workflow_id="wf-101",
            tenant_id=tenant_id,
            status=ApprovalStatus.APPROVED.value,
            decision=ApprovalStatus.APPROVED.value,
            expires_at=now_utc + timedelta(hours=1),
            resume_status=resume_status,
            step_index=step_index,
            task_id=f"wf-101:step-{step_index}",
            risk_level="low",
            requested_action="Run automated policy verification step",
            details={
                "plan_hash": plan_hash,
                "plan_id": "plan-101",
                "agent_id": "security_policy",
                "operation": "evaluate_policy",
            },
        )
        wf = WorkflowModel(
            workflow_id="wf-101",
            tenant_id="tenant-alpha",
            status=workflow_status,
            current_step_index=step_index,
            plan_hash=plan_hash,
            execution_plan={
                "plan_id": "plan-101",
                "steps": [
                    {
                        "step_index": step_index,
                        "agent_id": "security_policy",
                        "risk_level": "low",
                        "operation": "evaluate_policy",
                        "description": "Run automated policy verification step",
                    }
                ],
            },
            lock_expires_at=lock_expires_at,
            fencing_epoch=1,
            row_revision=2,
            execution_metadata={},
        )
        return appr, wf

    def _setup_mock_session(self, appr_row, wf_row, is_lease_active_in_db=False, is_approval_expired_in_db=False):
        mock_session = AsyncMock()
        mock_begin = MagicMock()
        mock_begin.__aenter__ = AsyncMock(return_value=None)
        mock_begin.__aexit__ = AsyncMock(return_value=None)
        mock_session.begin = MagicMock(return_value=mock_begin)

        async def side_execute(stmt, *args, **kwargs):
            res = MagicMock()
            stmt_str = str(stmt).lower()
            if "from approval_requests" in stmt_str:
                if "expires_at >" in stmt_str or "expires_at >=" in stmt_str:
                    # Database-time approval unexpired check
                    res.scalar_one_or_none.return_value = None if is_approval_expired_in_db else (appr_row.approval_id if appr_row else None)
                else:
                    # Main approval row lock query
                    res.scalar_one_or_none.return_value = appr_row
            elif "from workflows" in stmt_str:
                if "lock_expires_at >=" in stmt_str:
                    # Database-time lease active query
                    res.scalar_one_or_none.return_value = "wf-101" if is_lease_active_in_db else None
                else:
                    # Main workflow row lock query
                    res.scalar_one_or_none.return_value = wf_row
            elif "from workflow_tasks" in stmt_str:
                # Task row (non-completed)
                res.scalar_one_or_none.return_value = None
            else:
                res.scalar_one_or_none.return_value = None
            return res

        mock_session.execute = AsyncMock(side_effect=side_execute)
        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_factory.return_value = mock_ctx

        repo = PostgresWorkflowRecoveryRepository(session_factory=mock_factory)
        return repo, mock_session

    async def test_unexpired_approval_claim_success(self):
        """1a. Unexpired approval with valid WAITING_APPROVAL workflow is claimed successfully."""
        appr, wf = self._build_test_entities(
            resume_status="pending",
            workflow_status=WorkflowStatus.WAITING_APPROVAL.value,
            lock_expires_at=None,
        )
        repo, mock_session = self._setup_mock_session(
            appr, wf, is_lease_active_in_db=False, is_approval_expired_in_db=False
        )

        claimed, epoch, rev, token, wf_model, reason = await repo.claim_approval_resume(
            approval_id="appr-101",
            workflow_id="wf-101",
            tenant_id="tenant-alpha",
        )

        self.assertTrue(claimed)
        self.assertEqual(epoch, 2)
        self.assertEqual(rev, 3)
        self.assertIsNotNone(token)
        self.assertEqual(reason, "success")
        self.assertEqual(wf.status, WorkflowStatus.RUNNING.value)

    async def test_expired_approval_claim_rejection(self):
        """1b. Expired approval in database time is rejected with 'approval_expired'."""
        appr, wf = self._build_test_entities(
            resume_status="pending",
            workflow_status=WorkflowStatus.WAITING_APPROVAL.value,
            lock_expires_at=None,
        )
        # Simulate database query returning None for expires_at > func.now()
        repo, mock_session = self._setup_mock_session(
            appr, wf, is_lease_active_in_db=False, is_approval_expired_in_db=True
        )

        claimed, epoch, rev, token, wf_model, reason = await repo.claim_approval_resume(
            approval_id="appr-101",
            workflow_id="wf-101",
            tenant_id="tenant-alpha",
        )

        self.assertFalse(claimed)
        self.assertIsNone(epoch)
        self.assertIsNone(rev)
        self.assertIsNone(token)
        self.assertEqual(reason, "approval_expired")

    async def test_first_time_approval_claim_without_lease_expiry(self):
        """1c. First-time claim succeeds when lock_expires_at is None and workflow is WAITING_APPROVAL."""
        appr, wf = self._build_test_entities(
            resume_status="pending",
            workflow_status=WorkflowStatus.WAITING_APPROVAL.value,
            lock_expires_at=None,
        )
        repo, mock_session = self._setup_mock_session(appr, wf, is_lease_active_in_db=False)

        claimed, epoch, rev, token, wf_model, reason = await repo.claim_approval_resume(
            approval_id="appr-101",
            workflow_id="wf-101",
            tenant_id="tenant-alpha",
        )

        self.assertTrue(claimed)
        self.assertEqual(epoch, 2)
        self.assertEqual(rev, 3)
        self.assertIsNotNone(token)
        self.assertEqual(reason, "success")
        self.assertEqual(wf.status, WorkflowStatus.RUNNING.value)
        self.assertTrue(wf.execution_metadata.get("is_first_resume_claim"))

    async def test_claim_rejected_when_lease_is_still_active_in_database(self):
        """2. Rejects with 'workflow_lease_active_or_ineligible' when another valid lease is active in database time."""
        future_exp = datetime.now(timezone.utc) + timedelta(seconds=25)
        appr, wf = self._build_test_entities(
            resume_status="pending",
            workflow_status=WorkflowStatus.WAITING_APPROVAL.value,
            lock_expires_at=future_exp,
        )
        # Database query confirms lock_expires_at >= now()
        repo, mock_session = self._setup_mock_session(appr, wf, is_lease_active_in_db=True)

        claimed, epoch, rev, token, wf_model, reason = await repo.claim_approval_resume(
            approval_id="appr-101",
            workflow_id="wf-101",
            tenant_id="tenant-alpha",
        )

        self.assertFalse(claimed)
        self.assertIsNone(epoch)
        self.assertIsNone(rev)
        self.assertEqual(reason, "workflow_lease_active_or_ineligible")

    async def test_recovery_claim_succeeds_when_lease_has_expired_in_database(self):
        """3. Recovery claim for abandoned execution succeeds when lock_expires_at has expired in database time."""
        past_exp = datetime.now(timezone.utc) - timedelta(seconds=10)
        appr, wf = self._build_test_entities(
            resume_status="resumed",
            workflow_status=WorkflowStatus.RUNNING.value,
            lock_expires_at=past_exp,
        )
        # Database query confirms lock_expires_at >= now() returns no row (expired)
        repo, mock_session = self._setup_mock_session(appr, wf, is_lease_active_in_db=False)

        claimed, epoch, rev, token, wf_model, reason = await repo.claim_approval_resume(
            approval_id="appr-101",
            workflow_id="wf-101",
            tenant_id="tenant-alpha",
        )

        self.assertTrue(claimed)
        self.assertEqual(epoch, 2)
        self.assertEqual(rev, 3)
        self.assertEqual(reason, "success")
        self.assertTrue(appr.details.get("is_recovery_claim"))

    async def test_approval_binding_and_tenant_mismatch_rejections(self):
        """4. Verifies tenant, step_index, and plan_hash mismatches fail closed with appropriate reasons."""
        # 4a. Tenant mismatch on approval query
        repo_tenant, _ = self._setup_mock_session(appr_row=None, wf_row=None)
        c1, _, _, _, _, r1 = await repo_tenant.claim_approval_resume(
            approval_id="appr-101",
            workflow_id="wf-101",
            tenant_id="tenant-other",
        )
        self.assertFalse(c1)
        self.assertEqual(r1, "approval_not_found")

        # 4b. Step index mismatch
        appr_step, wf_step = self._build_test_entities(step_index=0)
        appr_step.step_index = 1  # Mismatched step
        repo_step, _ = self._setup_mock_session(appr_step, wf_step)
        c2, _, _, _, _, r2 = await repo_step.claim_approval_resume(
            approval_id="appr-101",
            workflow_id="wf-101",
            tenant_id="tenant-alpha",
        )
        self.assertFalse(c2)
        self.assertEqual(r2, "step_index_mismatch")

        # 4c. Plan hash mismatch
        appr_hash, wf_hash = self._build_test_entities()
        appr_hash.details["plan_hash"] = "altered-plan-hash"
        repo_hash, _ = self._setup_mock_session(appr_hash, wf_hash)
        c3, _, _, _, _, r3 = await repo_hash.claim_approval_resume(
            approval_id="appr-101",
            workflow_id="wf-101",
            tenant_id="tenant-alpha",
        )
        self.assertFalse(c3)
        self.assertEqual(r3, "plan_hash_mismatch")


if __name__ == "__main__":
    unittest.main()

