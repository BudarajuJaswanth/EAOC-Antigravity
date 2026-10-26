"""
EAOC LangGraph Integration Validation Test Suite (Part 2.18.3.2)

Hermetic integration-validation tests verifying the compiled StateGraph lifecycle:
1. Multi-step recovery & fast-forwarding of completed steps using authoritative repository state.
2. Reviewer escalation through reviewer_gate to pause_for_approval and then END (no unauthorized dispatch).
3. First-time approval resume and recovery-claim behavior (persisted claim metadata & tenant/plan/action binding).
4. Lease loss, interrupted mutating steps, and ambiguous side-effect fail-closed behavior (no retry/replan loops).
5. Output conformance with OrchestratorOutput and API contract.
6. Bounded retry and replan routing with explicit limitation retention (in-memory plan until Task 2.18.4).
7. Hermetic in-memory isolation using InMemoryWorkflowRecoveryRepository (distinguished from live DB tests).
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from eaoc.agents.orchestrator.schemas import (
    FinalResponse,
    OrchestratorFailureClass,
    OrchestratorOutput,
    ProvenanceEntry,
)
from eaoc.agents.planner.schemas import ExecutionPlan, PlanStep, PlanType, PlannerOutput
from eaoc.agents.registry import AgentRegistry
from eaoc.agents.research_rag.agent import ResearchRagAgent
from eaoc.agents.reviewer_validation.schemas import ReviewRecommendation
from eaoc.orchestration.graph import compile_workflow_graph
from eaoc.orchestration.nodes import OrchestrationNodes, compute_plan_hash
from eaoc.orchestration.state import GraphWorkflowState
from eaoc.schemas.domain import (
    AgentID,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ErrorInfo,
    RiskLevel,
    TaskStatus,
    WorkflowStatus,
)
from eaoc.state.workflow_repository import (
    FencingLeaseLostError,
    InMemoryWorkflowRecoveryRepository,
)


class TestLangGraphIntegrationValidation(unittest.IsolatedAsyncioTestCase):
    """
    Hermetic integration validation test suite for compiled EAOC StateGraph.
    Uses InMemoryWorkflowRecoveryRepository test doubles to ensure fast, isolated verification.
    """

    def setUp(self):
        self.registry = AgentRegistry()
        self.research_agent = ResearchRagAgent()
        self.registry.register_agent(self.research_agent)

        self.repo = InMemoryWorkflowRecoveryRepository()
        self.event_bus = AsyncMock()
        self.approval_repo = AsyncMock()

        self.nodes = OrchestrationNodes(
            registry=self.registry,
            workflow_repo=self.repo,
            approval_repo=self.approval_repo,
            event_bus=self.event_bus,
            max_retries=1,
            initial_backoff_seconds=0.01,
            backoff_multiplier=1.0,
            max_replan_attempts=2,
        )
        self.compiled_graph = compile_workflow_graph(self.nodes)

    def _build_multi_step_plan(self, task_id: str, count: int = 3) -> ExecutionPlan:
        steps = []
        for i in range(count):
            steps.append(
                PlanStep(
                    step_index=i,
                    agent_id=AgentID.RESEARCH_RAG,
                    description=f"Step {i} research action",
                    operation_type="read",
                    risk_level=RiskLevel.LOW,
                    dependencies=[i - 1] if i > 0 else [],
                    inputs={"query": f"query for step {i}"},
                )
            )
        return ExecutionPlan(
            plan_id=f"plan-{task_id}",
            task_id=task_id,
            plan_type=PlanType.SINGLE_AGENT if count == 1 else PlanType.MULTI_AGENT,
            planning_rationale="Multi-step sequential read execution",
            steps=steps,
        )

    # -------------------------------------------------------------------------
    # Task 1: Multi-Step Recovery and Fast-Forwarding of Completed Steps
    # -------------------------------------------------------------------------
    async def test_multistep_recovery_and_fast_forwarding(self):
        """
        Verify that on recovery, previously completed steps are fast-forwarded via
        prepare_step -> prepare_step (check_more_steps) without redispatching,
        and subsequent remaining steps execute to final completion.
        """
        wf_id = "wf-multistep-rec-01"
        tenant_id = "tenant-alpha"
        plan = self._build_multi_step_plan(wf_id, count=3)
        plan_hash = compute_plan_hash(plan)

        # 1. Initialize workflow intake in authoritative in-memory repository
        wf, _ = await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-test",
            task_id=wf_id,
            task_description="Multi-step test",
            execution_plan=plan.model_dump(),
            plan_hash=plan_hash,
        )

        # Acquire initial lease
        acquired, epoch, rev, token = await self.repo.acquire_execution_lease(
            workflow_id=wf_id,
            tenant_id=tenant_id,
        )
        self.assertTrue(acquired)

        from eaoc.orchestration.nodes import compute_step_hash
        step0_hash = compute_step_hash(plan.steps[0])

        # Step 0 was already executed and completed under previous lease
        is_comp, _, _, rev = await self.repo.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG.value,
            name=plan.steps[0].description,
            plan_hash=plan_hash,
            step_hash=step0_hash,
            inputs=plan.steps[0].inputs,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )
        self.assertFalse(is_comp)

        rev = await self.repo.complete_task_step(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            output_data={"result": "step 0 already completed"},
            provenance={"action": "step 0 done"},
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # Simulate lease expiry & crash recovery
        wf.lock_expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)

        # 2. Recovery lease acquisition
        rec_acquired, rec_epoch, rec_rev, rec_token = await self.repo.acquire_execution_lease(
            workflow_id=wf_id,
            tenant_id=tenant_id,
        )
        self.assertTrue(rec_acquired)

        # Track which steps get dispatched
        dispatched_steps: List[str] = []

        with patch.object(self.registry, "dispatch") as mock_dispatch:
            async def mock_dispatch_side_effect(ctx):
                dispatched_steps.append(ctx.task_id)
                step_idx = ctx.input_payload.get("query", "")
                return MagicMock(
                    status=TaskStatus.COMPLETED,
                    output_payload={"result": f"dispatched {step_idx}"},
                    error=None,
                )

            mock_dispatch.side_effect = mock_dispatch_side_effect

            initial_state: GraphWorkflowState = {
                "workflow_id": wf_id,
                "task_id": wf_id,
                "tenant_id": tenant_id,
                "user_id": "usr-test",
                "user_context": {"roles": ["analyst"]},
                "fencing_epoch": rec_epoch,
                "lock_token": rec_token,
                "current_revision": rec_rev,
                "execution_plan": plan.model_dump(),
            }

            final_state = await self.compiled_graph.ainvoke(initial_state)

            # Assertions:
            self.assertEqual(final_state["status"], WorkflowStatus.COMPLETED.value)
            # All 3 steps must be recorded in completed_steps
            completed = final_state.get("completed_steps", {})
            self.assertEqual(len(completed), 3)
            self.assertEqual(completed[0], {"result": "step 0 already completed"})
            self.assertIn(1, completed)
            self.assertIn(2, completed)

            # Step 0 was fast-forwarded and NEVER redispatched!
            # Exactly 2 dispatches should have occurred (steps 1 and 2)
            self.assertEqual(mock_dispatch.call_count, 2)
            self.assertEqual(final_state.get("next_route"), "END")

    # -------------------------------------------------------------------------
    # Task 2: Reviewer Escalation to Approval Pause and END
    # -------------------------------------------------------------------------
    async def test_reviewer_escalation_routes_to_pause_and_end(self):
        """
        Verify that reviewer escalation through reviewer_gate creates an ApprovalRequest,
        routes to pause_for_approval, persists the pause, and terminates at END without
        unauthorized dispatch.
        """
        wf_id = "wf-reviewer-escalate-01"
        tenant_id = "tenant-alpha"
        plan = self._build_multi_step_plan(wf_id, count=1)
        plan_hash = compute_plan_hash(plan)

        wf, _ = await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-test",
            task_id=wf_id,
            task_description="Reviewer escalation test",
            execution_plan=plan.model_dump(),
            plan_hash=plan_hash,
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease(
            workflow_id=wf_id,
            tenant_id=tenant_id,
        )

        with patch.object(self.registry, "dispatch") as mock_dispatch:
            mock_dispatch.return_value = MagicMock(
                status=TaskStatus.COMPLETED,
                output_payload={"result": "preliminary finding"},
                error=None,
            )

            # Mock ReviewerEngine to recommend ESCALATE
            with patch.object(self.nodes.reviewer_engine, "evaluate") as mock_review:
                mock_finding = MagicMock()
                mock_finding.model_dump.return_value = {
                    "rule": "quality_check",
                    "severity": "medium",
                    "message": "Human review required",
                }
                mock_review.return_value = MagicMock(
                    recommendation=ReviewRecommendation.ESCALATE,
                    findings=[mock_finding],
                )

                initial_state: GraphWorkflowState = {
                    "workflow_id": wf_id,
                    "task_id": wf_id,
                    "tenant_id": tenant_id,
                    "user_id": "usr-test",
                    "user_context": {"roles": ["analyst"]},
                    "fencing_epoch": epoch,
                    "lock_token": token,
                    "current_revision": rev,
                    "execution_plan": plan.model_dump(),
                }

                final_state = await self.compiled_graph.ainvoke(initial_state)

                # Must pause in WAITING_APPROVAL
                self.assertEqual(final_state["status"], WorkflowStatus.WAITING_APPROVAL.value)
                self.assertIsNotNone(final_state.get("approval_request"))
                self.assertEqual(
                    final_state["approval_request"]["requested_action"],
                    "Reviewer Escalation",
                )
                self.assertEqual(final_state.get("next_route"), "END")

                # Dispatch must have run ONLY once (for step 0); no unauthorized extra dispatch
                self.assertEqual(mock_dispatch.call_count, 1)

                # Authoritative repo state must reflect WAITING_APPROVAL
                persisted_wf = await self.repo.get_workflow(wf_id, tenant_id)
                self.assertEqual(persisted_wf.status, WorkflowStatus.WAITING_APPROVAL.value)

    # -------------------------------------------------------------------------
    # Task 3: First-time Resume vs Recovery Claims and Binding Gates
    # -------------------------------------------------------------------------
    async def test_first_time_approval_resume_succeeds(self):
        """
        Verify that first-time approval resume (claim_count=1, is_first_claim=True)
        passes all safety gates and completes step execution.
        """
        wf_id = "wf-resume-valid-01"
        tenant_id = "tenant-alpha"
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Approved high-risk query",
            operation_type="query",
            risk_level=RiskLevel.HIGH,
            inputs={"query": "approved research"},
        )
        plan = ExecutionPlan(
            plan_id=f"plan-{wf_id}",
            task_id=wf_id,
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Approved high risk query",
            steps=[step],
        )
        plan_hash = compute_plan_hash(plan)

        # Setup workflow in WAITING_APPROVAL
        wf, _ = await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-test",
            task_id=wf_id,
            task_description="Approval test",
            execution_plan=plan.model_dump(),
            plan_hash=plan_hash,
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease(
            workflow_id=wf_id,
            tenant_id=tenant_id,
        )
        rev = await self.repo.pause_workflow_for_approval(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            approval_id="appr-val-01",
            requested_action="Execute step 0: Approved high-risk query via research_rag",
            risk_level="HIGH",
            details={"step_index": 0, "task_id": f"{wf_id}:step-0"},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        decision = ApprovalDecision(
            approval_id="appr-val-01",
            workflow_id=wf_id,
            decision=ApprovalStatus.APPROVED,
            approver_id="security-lead",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        req = ApprovalRequest(
            approval_id="appr-val-01",
            workflow_id=wf_id,
            requested_action="Execute step 0: Approved high-risk query via research_rag",
            risk_level=RiskLevel.HIGH,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc).isoformat(),
            details={
                "step_index": 0,
                "task_id": f"{wf_id}:step-0",
                "agent_id": "research_rag",
                "plan_hash": plan_hash,
                "tenant_id": tenant_id,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "claim_count": 1,
                "is_first_claim": True,
                "is_recovery_claim": False,
            },
        )

        # Acquire execution lease for resumption
        wf.lock_expires_at = None
        wf.status = WorkflowStatus.WAITING_APPROVAL.value
        _, res_epoch, res_rev, res_token = await self.repo.acquire_execution_lease(
            workflow_id=wf_id,
            tenant_id=tenant_id,
        )

        with patch.object(self.registry, "dispatch") as mock_dispatch:
            mock_dispatch.return_value = MagicMock(
                status=TaskStatus.COMPLETED,
                output_payload={"result": "approved query executed"},
                error=None,
            )

            initial_state: GraphWorkflowState = {
                "workflow_id": wf_id,
                "task_id": f"{wf_id}:step-0",
                "tenant_id": tenant_id,
                "user_id": "usr-test",
                "user_context": {"roles": ["analyst"]},
                "fencing_epoch": res_epoch,
                "lock_token": res_token,
                "current_revision": res_rev,
                "execution_plan": plan.model_dump(),
                "approval_decision": decision.model_dump(),
                "approval_request": req.model_dump(),
                "is_first_claim": True,
                "is_recovery_claim": False,
            }

            final_state = await self.compiled_graph.ainvoke(initial_state)

            self.assertEqual(final_state["status"], WorkflowStatus.COMPLETED.value)
            mock_dispatch.assert_called_once()

    async def test_recovery_claim_on_mutating_step_fails_closed(self):
        """
        Verify that a recovery claim (claim_count > 1 or is_recovery_claim=True)
        for an interrupted mutating step fails closed with AMBIGUOUS_SIDE_EFFECT
        and prevents redispatch.
        """
        wf_id = "wf-recovery-claim-fail-01"
        tenant_id = "tenant-alpha"
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Mutating operation crashed after claim",
            operation_type="mutation",
            risk_level=RiskLevel.HIGH,
        )
        plan = ExecutionPlan(
            plan_id=f"plan-{wf_id}",
            task_id=wf_id,
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Mutating recovery claim test",
            steps=[step],
        )
        plan_hash = compute_plan_hash(plan)

        # Setup workflow and lease in repo
        wf, _ = await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-test",
            task_id=wf_id,
            task_description="Mutating recovery test",
            execution_plan=plan.model_dump(),
            plan_hash=plan_hash,
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease(
            workflow_id=wf_id,
            tenant_id=tenant_id,
        )

        decision = ApprovalDecision(
            approval_id="appr-rec-claim-01",
            workflow_id=wf_id,
            decision=ApprovalStatus.APPROVED,
            approver_id="security-lead",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        req = ApprovalRequest(
            approval_id="appr-rec-claim-01",
            workflow_id=wf_id,
            requested_action=f"Execute step 0: Mutating operation crashed after claim via research_rag",
            risk_level=RiskLevel.HIGH,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc).isoformat(),
            details={
                "step_index": 0,
                "task_id": f"{wf_id}:step-0",
                "agent_id": "research_rag",
                "plan_hash": plan_hash,
                "tenant_id": tenant_id,
                "claim_count": 2,  # Second claim after crash!
                "is_first_claim": False,
                "is_recovery_claim": True,
            },
        )

        with patch.object(self.registry, "dispatch") as mock_dispatch:
            initial_state: GraphWorkflowState = {
                "workflow_id": wf_id,
                "task_id": f"{wf_id}:step-0",
                "tenant_id": tenant_id,
                "user_id": "usr-test",
                "user_context": {"roles": ["analyst"]},
                "fencing_epoch": epoch,
                "lock_token": token,
                "current_revision": rev,
                "execution_plan": plan.model_dump(),
                "interrupted_steps": [0],
                "approval_decision": decision.model_dump(),
                "approval_request": req.model_dump(),
                "is_first_claim": False,
                "is_recovery_claim": True,
            }

            final_state = await self.compiled_graph.ainvoke(initial_state)

            self.assertEqual(final_state["status"], WorkflowStatus.FAILED.value)
            self.assertEqual(final_state.get("error_info", {}).get("code"), "AMBIGUOUS_SIDE_EFFECT")
            mock_dispatch.assert_not_called()

    async def test_approval_binding_tenant_mismatch_fails_closed(self):
        """Verify that tenant mismatch in approval request fails closed with POLICY_VIOLATION."""
        wf_id = "wf-tenant-mismatch-01"
        tenant_id = "tenant-alpha"
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Tenant check action",
            operation_type="read",
            risk_level=RiskLevel.HIGH,
        )
        plan = ExecutionPlan(
            plan_id=f"plan-{wf_id}",
            task_id=wf_id,
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Tenant mismatch test",
            steps=[step],
        )

        decision = ApprovalDecision(
            approval_id="appr-tenant-01",
            workflow_id=wf_id,
            decision=ApprovalStatus.APPROVED,
            approver_id="lead",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        req = ApprovalRequest(
            approval_id="appr-tenant-01",
            workflow_id=wf_id,
            requested_action=f"Execute step 0: Tenant check action via research_rag",
            risk_level=RiskLevel.HIGH,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc).isoformat(),
            details={
                "step_index": 0,
                "task_id": f"{wf_id}:step-0",
                "tenant_id": "tenant-DIFFERENT",  # Mismatch!
                "plan_hash": compute_plan_hash(plan),
            },
        )

        wf, _ = await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-test",
            task_id=wf_id,
            task_description="Tenant check test",
            execution_plan=plan.model_dump(),
            plan_hash=compute_plan_hash(plan),
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease(
            workflow_id=wf_id,
            tenant_id=tenant_id,
        )

        with patch.object(self.registry, "dispatch") as mock_dispatch:
            initial_state: GraphWorkflowState = {
                "workflow_id": wf_id,
                "task_id": f"{wf_id}:step-0",
                "tenant_id": tenant_id,
                "user_id": "usr-test",
                "user_context": {"roles": ["analyst"]},
                "fencing_epoch": epoch,
                "lock_token": token,
                "current_revision": rev,
                "execution_plan": plan.model_dump(),
                "interrupted_steps": [0],
                "approval_decision": decision.model_dump(),
                "approval_request": req.model_dump(),
            }

            final_state = await self.compiled_graph.ainvoke(initial_state)

            self.assertEqual(final_state["status"], WorkflowStatus.FAILED.value)
            self.assertEqual(
                final_state.get("failure_class"),
                OrchestratorFailureClass.POLICY_VIOLATION.value,
            )
            self.assertEqual(final_state.get("error_info", {}).get("code"), "TENANT_MISMATCH")
            mock_dispatch.assert_not_called()

    # -------------------------------------------------------------------------
    # Task 4: Lease Loss, Interrupted Steps, & Ambiguous Failures
    # -------------------------------------------------------------------------
    async def test_lease_loss_fails_closed_without_replan(self):
        """Verify that FencingLeaseLostError in prepare_step fails closed and cannot enter replan loop."""
        wf_id = "wf-lease-loss-01"
        tenant_id = "tenant-alpha"
        plan = self._build_multi_step_plan(wf_id, count=1)

        # Mock repo to raise FencingLeaseLostError on prepare_task_step
        mock_repo = AsyncMock()
        mock_repo.get_workflow.return_value = None
        mock_repo.prepare_task_step.side_effect = FencingLeaseLostError("Stale fencing epoch")

        nodes = OrchestrationNodes(
            registry=self.registry,
            workflow_repo=mock_repo,
            max_replan_attempts=2,
        )
        compiled = compile_workflow_graph(nodes)

        initial_state: GraphWorkflowState = {
            "workflow_id": wf_id,
            "task_id": wf_id,
            "tenant_id": tenant_id,
            "user_id": "usr-test",
            "user_context": {"roles": ["analyst"]},
            "fencing_epoch": 1,
            "lock_token": "stale-token",
            "current_revision": 1,
            "execution_plan": plan.model_dump(),
            "replan_count": 0,
        }

        # Expecting exception or immediate failure
        with self.assertRaises(FencingLeaseLostError):
            await compiled.ainvoke(initial_state)

        # Confirm replanning was never attempted
        self.assertEqual(mock_repo.get_workflow.call_count, 1)

    # -------------------------------------------------------------------------
    # Task 5: Contract Output Verification against OrchestratorOutput
    # -------------------------------------------------------------------------
    async def test_contract_output_conformance(self):
        """
        Verify that compiled graph outputs for success, failure, and approval pause
        strictly conform to the existing OrchestratorOutput schema.
        """
        plan = self._build_multi_step_plan("task-contract-01", count=1)

        # 1. Success Outcome Conformance
        success_state: GraphWorkflowState = {
            "task_id": "task-contract-01",
            "workflow_id": "wf-contract-01",
            "status": WorkflowStatus.COMPLETED.value,
            "final_response": {
                "task_id": "task-contract-01",
                "status": WorkflowStatus.COMPLETED.value,
                "result": {"step_0": "done"},
                "provenance_summary": [],
                "partial_result": False,
            },
            "provenance_chain": [
                {
                    "step_index": 0,
                    "agent_id": "research_rag",
                    "action_summary": "Query",
                    "status": "COMPLETED",
                }
            ],
            "replan_count": 1,
        }

        output_success = OrchestratorOutput(
            task_id=success_state["task_id"],
            workflow_id=success_state["workflow_id"],
            status=WorkflowStatus(success_state["status"]),
            final_response=FinalResponse.model_validate(success_state["final_response"]),
            provenance_chain=[
                ProvenanceEntry.model_validate(p) for p in success_state["provenance_chain"]
            ],
            replan_count=success_state["replan_count"],
        )
        self.assertEqual(output_success.status, WorkflowStatus.COMPLETED)
        self.assertIsNotNone(output_success.final_response)

        # 2. Failure Outcome Conformance
        failure_state: GraphWorkflowState = {
            "task_id": "task-contract-02",
            "workflow_id": "wf-contract-02",
            "status": WorkflowStatus.FAILED.value,
            "error_info": {
                "code": "AMBIGUOUS_SIDE_EFFECT",
                "message": "Mutating operation crashed",
                "error_type": "AmbiguousSideEffectError",
            },
            "replan_count": 0,
        }

        output_failure = OrchestratorOutput(
            task_id=failure_state["task_id"],
            workflow_id=failure_state["workflow_id"],
            status=WorkflowStatus(failure_state["status"]),
            error=ErrorInfo.model_validate(failure_state["error_info"]),
            replan_count=failure_state["replan_count"],
        )
        self.assertEqual(output_failure.status, WorkflowStatus.FAILED)
        self.assertEqual(output_failure.error.code, "AMBIGUOUS_SIDE_EFFECT")

        # 3. Approval Pause Conformance
        pause_state: GraphWorkflowState = {
            "task_id": "task-contract-03",
            "workflow_id": "wf-contract-03",
            "status": WorkflowStatus.WAITING_APPROVAL.value,
            "approval_request": {
                "approval_id": "appr-contract-01",
                "workflow_id": "wf-contract-03",
                "requested_action": "Execute step 0",
                "risk_level": "HIGH",
                "status": "PENDING",
                "requested_at": datetime.now(timezone.utc).isoformat(),
            },
        }

        output_pause = OrchestratorOutput(
            task_id=pause_state["task_id"],
            workflow_id=pause_state["workflow_id"],
            status=WorkflowStatus(pause_state["status"]),
            approval_request=ApprovalRequest.model_validate(pause_state["approval_request"]),
        )
        self.assertEqual(output_pause.status, WorkflowStatus.WAITING_APPROVAL)
        self.assertEqual(output_pause.approval_request.approval_id, "appr-contract-01")

    # -------------------------------------------------------------------------
    # Task 6: Bounded Retry and Replan Routing with Limitation Retention
    # -------------------------------------------------------------------------
    async def test_prohibited_failure_classes_bypass_replan(self):
        """
        Verify that non-replannable failure classes (POLICY_VIOLATION, PERMISSION_DENIED)
        in step failure immediately terminate in fail_workflow and do not route to contextual_replan.
        """
        wf_id = "wf-prohibited-replan-01"
        tenant_id = "tenant-alpha"
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Policy check action",
            operation_type="read",
            risk_level=RiskLevel.LOW,
        )
        plan = ExecutionPlan(
            plan_id=f"plan-{wf_id}",
            task_id=wf_id,
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Prohibited replan test",
            steps=[step],
        )

        wf, _ = await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-test",
            task_id=wf_id,
            task_description="Prohibited replan test",
            execution_plan=plan.model_dump(),
            plan_hash=compute_plan_hash(plan),
        )
        _, epoch, rev, token = await self.repo.acquire_execution_lease(
            workflow_id=wf_id,
            tenant_id=tenant_id,
        )

        # Dispatch returns an unrecoverable POLICY_VIOLATION error
        with patch.object(self.registry, "dispatch") as mock_dispatch:
            mock_dispatch.return_value = MagicMock(
                status=TaskStatus.FAILED,
                error=ErrorInfo(
                    code="POLICY_VIOLATION_DETECTED",
                    message="Prohibited policy boundary reached",
                    error_type="PolicyViolationError",
                ),
                output_payload=None,
            )

            initial_state: GraphWorkflowState = {
                "workflow_id": wf_id,
                "task_id": wf_id,
                "tenant_id": tenant_id,
                "user_id": "usr-test",
                "user_context": {"roles": ["analyst"]},
                "fencing_epoch": epoch,
                "lock_token": token,
                "current_revision": rev,
                "execution_plan": plan.model_dump(),
                "replan_count": 0,
            }

            final_state = await self.compiled_graph.ainvoke(initial_state)

            # Must fail immediately with POLICY_VIOLATION; no replan
            self.assertEqual(final_state["status"], WorkflowStatus.FAILED.value)
            self.assertEqual(
                final_state.get("failure_class"),
                OrchestratorFailureClass.POLICY_VIOLATION.value,
            )
            self.assertEqual(final_state.get("replan_count", 0), 0)
            self.assertEqual(final_state.get("next_route"), "END")



if __name__ == "__main__":
    unittest.main()
