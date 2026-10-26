"""
EAOC Unit Test Suite - LangGraph State Contracts & Lifecycle Node Handlers (Task 2.18.2)

Verifies:
1. GraphWorkflowState schema, typing, and security sanitization (lock_token scrub, sensitive redaction).
2. OrchestrationNodes lifecycle node handlers:
   - init_and_recover rehydration
   - plan_resolution validation & plan_hash computation
   - prepare_step fast-forward vs dispatch routing
   - evaluate_risk_gate Section 9.2 approval pause
   - pause_for_approval repository write and WAITING_APPROVAL status
   - dispatch_step RBAC check and retry handling
   - handle_step_failure non-replannable classification
   - reviewer_gate quality gate resolution
   - finalize_workflow and fail_workflow terminal handling
"""

import asyncio
from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from eaoc.agents.orchestrator.schemas import OrchestratorFailureClass
from eaoc.agents.planner.schemas import ExecutionPlan, PlanStep, PlanType
from eaoc.agents.registry import AgentRegistry
from eaoc.orchestration.nodes import OrchestrationNodes, compute_plan_hash, compute_step_hash
from eaoc.orchestration.state import (
    GraphWorkflowState,
    sanitize_state_for_logging,
    CREDENTIAL_FIELDS,
    SENSITIVE_DATA_FIELDS,
)
from eaoc.schemas.domain import (
    AgentID,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
    TaskStatus,
    WorkflowStatus,
)
from eaoc.state.workflow_repository import FencingLeaseLostError
from eaoc.tools.base import BaseTool, ToolCategory, ToolMetadata
from eaoc.tools.registry import ToolRegistry


from eaoc.agents.research_rag.agent import ResearchRagAgent


class DummyTool(BaseTool):
    """Minimal BaseTool stub for tool registry testing."""
    def __init__(self, metadata: ToolMetadata, is_idempotent: bool = False):
        super().__init__(metadata)
        self.is_idempotent = is_idempotent

    async def execute(self, parameters):
        return None


class IdempotentToolMetadata(ToolMetadata):
    """Extended tool metadata supporting explicit idempotency contract."""
    is_idempotent: bool = False



class TestLangGraphStateAndNodes(unittest.IsolatedAsyncioTestCase):
    """Test suite for Part 2.18.2 LangGraph state schema and atomic node handlers."""

    def setUp(self):
        self.registry = AgentRegistry()
        self.registry.register_agent(ResearchRagAgent())
        self.mock_repo = AsyncMock()
        self.mock_event_bus = AsyncMock()
        self.nodes = OrchestrationNodes(
            registry=self.registry,
            workflow_repo=self.mock_repo,
            event_bus=self.mock_event_bus,
            max_retries=1,
            initial_backoff_seconds=0.01,
            backoff_multiplier=1.0,
            max_replan_attempts=2,
        )

    def test_state_sanitization_scrubs_lock_token(self):
        """Test that sanitize_state_for_logging strictly redacts lock_token and sensitive data."""
        state: GraphWorkflowState = {
            "workflow_id": "wf-test-01",
            "tenant_id": "tenant-alpha",
            "user_id": "user-123",
            "lock_token": "secret-lock-tok-12345",
            "fencing_epoch": 2,
            "completed_steps": {0: {"secret_token": "bearer-12345", "api_key": "sk-12345"}},
            "user_context": {"roles": ["admin"], "jwt": "bearer-token-secret"},
        }
        sanitized = sanitize_state_for_logging(state)

        # Lock token must be scrubbed
        self.assertEqual(sanitized["lock_token"], "[REDACTED_CREDENTIAL]")
        # JWT must be scrubbed
        self.assertEqual(sanitized["user_context"]["jwt"], "[REDACTED]")
        # Sensitive credentials in step output must be redacted
        self.assertEqual(sanitized["completed_steps"][0]["secret_token"], "****")
        self.assertEqual(sanitized["completed_steps"][0]["api_key"], "****")

    async def test_init_and_recover_rehydrates_completed_tasks(self):
        """Test that init_and_recover reads completed tasks and fencing epoch from repo."""
        task_mock = MagicMock()
        task_mock.step_index = 0
        task_mock.status = TaskStatus.COMPLETED.value
        task_mock.output_data = {"data": "cached_val"}
        task_mock.provenance = {"action": "step 0 completed"}

        wf_mock = MagicMock()
        wf_mock.fencing_epoch = 3
        wf_mock.row_revision = 5

        self.mock_repo.get_workflow.return_value = wf_mock
        self.mock_repo.get_tasks_for_workflow.return_value = [task_mock]

        state: GraphWorkflowState = {
            "workflow_id": "wf-rec-01",
            "tenant_id": "tenant-alpha",
        }
        res = await self.nodes.node_init_and_recover(state)

        self.assertEqual(res["fencing_epoch"], 3)
        self.assertEqual(res["current_revision"], 5)
        self.assertIn(0, res["completed_steps"])
        self.assertEqual(res["completed_steps"][0], {"data": "cached_val"})

    async def test_plan_resolution_with_supplied_plan(self):
        """Test that plan_resolution validates supplied plan and computes plan_hash."""
        plan = ExecutionPlan(
            plan_id="plan-test-01",
            task_id="task-test-01",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Test research policy rationale",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.RESEARCH_RAG,
                    description="Research company policy",
                    risk_level=RiskLevel.LOW,
                )
            ],
        )
        state: GraphWorkflowState = {
            "workflow_id": "wf-plan-01",
            "tenant_id": "tenant-alpha",
            "execution_plan": plan.model_dump(),
            "user_context": {"roles": ["researcher", "admin"]},
        }

        res = await self.nodes.node_plan_resolution(state)
        self.assertEqual(res["status"], WorkflowStatus.RUNNING.value)
        self.assertEqual(res["next_route"], "prepare_step")
        self.assertTrue(len(res["plan_hash"]) == 64)
        self.mock_event_bus.publish.assert_called_once()

    async def test_prepare_step_fast_forward_branch(self):
        """Test that prepare_step detects completed task and sets is_step_fast_forwarded."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Step 0 description",
            risk_level=RiskLevel.LOW,
        )
        self.mock_repo.prepare_task_step.return_value = (
            True,  # is_completed
            {"out": "step0_result"},
            {"prov": "step0_prov"},
            2,  # new_revision
        )

        state: GraphWorkflowState = {
            "workflow_id": "wf-prep-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
        }

        res = await self.nodes.node_prepare_step(state)
        self.assertTrue(res["is_step_fast_forwarded"])
        self.assertEqual(res["next_route"], "check_more_steps")
        self.assertEqual(res["completed_steps"][0], {"out": "step0_result"})

    async def test_evaluate_risk_gate_pauses_for_high_risk(self):
        """Test that evaluate_risk_gate pauses when high risk step has no approval."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.SQL_DATA,
            description="Drop database table",
            operation_type="delete",
            risk_level=RiskLevel.HIGH,
        )
        state: GraphWorkflowState = {
            "workflow_id": "wf-risk-01",
            "task_id": "task-risk-01",
            "plan_hash": "hash-risk",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "approval_decision": None,
        }

        res = await self.nodes.node_evaluate_risk_gate(state)
        self.assertEqual(res["next_route"], "pause_for_approval")
        self.assertEqual(res["status"], WorkflowStatus.WAITING_APPROVAL.value)
        self.assertIsNotNone(res["approval_request"])

    async def test_pause_for_approval_calls_repository(self):
        """Test that pause_for_approval calls repo.pause_workflow_for_approval and emits event."""
        self.mock_repo.pause_workflow_for_approval.return_value = 3

        state: GraphWorkflowState = {
            "workflow_id": "wf-pause-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 2,
            "current_step_index": 0,
            "approval_request": {
                "approval_id": "appr-123",
                "requested_action": "delete",
                "risk_level": "HIGH",
            },
        }

        res = await self.nodes.node_pause_for_approval(state)
        self.assertEqual(res["status"], WorkflowStatus.WAITING_APPROVAL.value)
        self.assertEqual(res["next_route"], "END")
        self.assertEqual(res["current_revision"], 3)
        self.mock_repo.pause_workflow_for_approval.assert_called_once()
        self.mock_event_bus.publish.assert_called_once()

    async def test_dispatch_step_permission_denied(self):
        """Test that dispatch_step fails closed when role does not permit agent."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.DEVELOPER,
            description="Run developer sandbox",
            risk_level=RiskLevel.LOW,
        )
        state: GraphWorkflowState = {
            "workflow_id": "wf-disp-01",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "user_context": {"roles": ["hr_user"]},  # hr_user cannot dispatch developer
        }

        res = await self.nodes.node_dispatch_step(state)
        self.assertFalse(res["last_dispatch_success"])
        self.assertEqual(res["next_route"], "handle_step_failure")
        self.assertEqual(res["failure_class"], OrchestratorFailureClass.PERMISSION_DENIED.value)

    async def test_handle_step_failure_prohibits_replan_on_permission_denied(self):
        """Test that handle_step_failure routes permission denied directly to fail_workflow."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.DEVELOPER,
            description="Run developer sandbox",
            risk_level=RiskLevel.LOW,
            optional=False,
        )
        state: GraphWorkflowState = {
            "workflow_id": "wf-fail-01",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "error_info": {"code": "PERMISSION_DENIED", "message": "unauthorized"},
            "replan_count": 0,
        }

        res = await self.nodes.node_handle_step_failure(state)
        self.assertEqual(res["next_route"], "fail_workflow")
        self.assertEqual(res["failure_class"], OrchestratorFailureClass.PERMISSION_DENIED.value)

    async def test_finalize_workflow_releases_lease(self):
        """Test that finalize_workflow releases execution lease and returns COMPLETED."""
        state: GraphWorkflowState = {
            "workflow_id": "wf-fin-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 2,
            "lock_token": "tok-fin",
            "task_id": "task-fin-01",
            "completed_steps": {0: {"output": "val0"}},
            "provenance_chain": [{"action": "step 0"}],
        }

        res = await self.nodes.node_finalize_workflow(state)
        self.assertEqual(res["status"], WorkflowStatus.COMPLETED.value)
        self.assertEqual(res["next_route"], "END")
        self.assertIn("final_response", res)
        self.mock_repo.release_execution_lease.assert_called_once()
        self.mock_event_bus.publish.assert_called_once()

    async def test_interrupted_step_read_only_allowed_replay(self):
        """Fix A: Interrupted step with read-only operation and tool is safe to replay."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Read policy documents",
            operation_type="read",
            risk_level=RiskLevel.HIGH,  # High risk does not prevent read-only replay
            inputs={"tool": "postgresql_read"},
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        state: GraphWorkflowState = {
            "workflow_id": "wf-replay-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
        }

        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["next_route"], "evaluate_risk_gate")
        self.assertFalse(res["is_step_fast_forwarded"])
        self.mock_event_bus.publish.assert_called_once()

    async def test_interrupted_step_ambiguous_mutating_fails_closed(self):
        """Fix A: Interrupted step with mutating operation without verified idempotency fails closed."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.SQL_DATA,
            description="Delete user records",
            operation_type="delete",
            risk_level=RiskLevel.LOW,  # Low risk does not permit mutating replay
            inputs={"table": "users"},
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        state: GraphWorkflowState = {
            "workflow_id": "wf-replay-02",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
        }

        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["next_route"], "fail_workflow")
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["failure_class"], OrchestratorFailureClass.UNKNOWN_FAILURE.value)
        self.assertEqual(res["error_info"]["code"], "AMBIGUOUS_SIDE_EFFECT")
        self.mock_event_bus.publish.assert_not_called()

    async def test_interrupted_step_unknown_operation_fails_closed(self):
        """Fix A: Interrupted step with unknown operation type fails closed with AMBIGUOUS_SIDE_EFFECT."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.DEVELOPER,
            description="Custom unspecified action",
            operation_type=None,
            risk_level=RiskLevel.LOW,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        state: GraphWorkflowState = {
            "workflow_id": "wf-replay-03",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
        }

        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["next_route"], "fail_workflow")
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["error_info"]["code"], "AMBIGUOUS_SIDE_EFFECT")

    async def test_pause_for_approval_repo_failure_suppresses_event(self):
        """Fix B: Repository failure during pause suppresses event publication and fails closed."""
        self.mock_repo.pause_workflow_for_approval.side_effect = RuntimeError("DB connection failure")

        state: GraphWorkflowState = {
            "workflow_id": "wf-pause-fail",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 2,
            "current_step_index": 0,
            "approval_request": {
                "approval_id": "appr-123",
                "requested_action": "delete",
                "risk_level": "HIGH",
            },
        }

        res = await self.nodes.node_pause_for_approval(state)
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["failure_class"], OrchestratorFailureClass.DATABASE_UNAVAILABLE.value)
        self.assertEqual(res["next_route"], "fail_workflow")
        self.mock_event_bus.publish.assert_not_called()

    async def test_record_step_success_commit_order_and_failure_suppression(self):
        """Fix B: Task step completion commits to DB before publishing event; suppresses event on DB failure."""
        call_order = []

        async def track_repo_complete(*args, **kwargs):
            call_order.append("repo_commit")
            return 3

        async def track_event_publish(*args, **kwargs):
            call_order.append("event_publish")

        self.mock_repo.complete_task_step.side_effect = track_repo_complete
        self.mock_event_bus.publish.side_effect = track_event_publish

        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Step 0 description",
            risk_level=RiskLevel.LOW,
        )
        state: GraphWorkflowState = {
            "workflow_id": "wf-success-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 2,
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "last_step_output": {"data": "ok"},
        }

        # 1. Successful commit order test
        res = await self.nodes.node_record_step_success(state)
        self.assertEqual(res["next_route"], "check_more_steps")
        self.assertEqual(call_order, ["repo_commit", "event_publish"])

        # 2. Failure suppression test
        self.mock_event_bus.publish.reset_mock()
        self.mock_repo.complete_task_step.side_effect = RuntimeError("DB write error")

        res_fail = await self.nodes.node_record_step_success(state)
        self.assertEqual(res_fail["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res_fail["failure_class"], OrchestratorFailureClass.DATABASE_UNAVAILABLE.value)
        self.assertEqual(res_fail["next_route"], "fail_workflow")
        self.mock_event_bus.publish.assert_not_called()

    # -------------------------------------------------------------------------
    # Fix A: HITL Approval Resumption Tests
    # -------------------------------------------------------------------------
    async def test_valid_approved_mutating_step_resumption_allowed(self):
        """Fix A: Valid approved continuation for mutating step is authorized to resume."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.SQL_DATA,
            description="Delete obsolete audit records",
            operation_type="delete",
            risk_level=RiskLevel.HIGH,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)
        mock_wf = MagicMock()
        mock_wf.plan_hash = "hash-abc"
        self.mock_repo.get_workflow.return_value = mock_wf

        decision = ApprovalDecision(
            approval_id="appr-hitl-01",
            workflow_id="wf-hitl-01",
            decision=ApprovalStatus.APPROVED,
            approver_id="security-officer",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        req = ApprovalRequest(
            approval_id="appr-hitl-01",
            workflow_id="wf-hitl-01",
            requested_action="Execute step 0: Delete obsolete audit records via sql_data",
            risk_level=RiskLevel.HIGH,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc).isoformat(),
            details={
                "step_index": 0,
                "task_id": "wf-hitl-01:step-0",
                "agent_id": "sql_data",
                "plan_hash": "hash-abc",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            },
        )

        state: GraphWorkflowState = {
            "workflow_id": "wf-hitl-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
            "approval_decision": decision.model_dump(),
            "approval_request": req.model_dump(),
        }

        # 1. prepare_step allows valid approved continuation
        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["next_route"], "evaluate_risk_gate")

        # 2. evaluate_risk_gate validates decision and advances to dispatch
        gate_res = await self.nodes.node_evaluate_risk_gate({**state, **res})
        self.assertEqual(gate_res["next_route"], "dispatch_step")
        self.assertEqual(gate_res["status"], WorkflowStatus.RUNNING.value)

    async def test_expired_approval_fails_closed(self):
        """Fix A: Expired approval request fails closed with APPROVAL_TIMEOUT."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.SQL_DATA,
            description="Delete records",
            operation_type="delete",
            risk_level=RiskLevel.HIGH,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        decision = ApprovalDecision(
            approval_id="appr-exp-01",
            workflow_id="wf-exp-01",
            decision=ApprovalStatus.APPROVED,
            approver_id="officer",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        req = ApprovalRequest(
            approval_id="appr-exp-01",
            workflow_id="wf-exp-01",
            requested_action="Execute step 0",
            risk_level=RiskLevel.HIGH,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc).isoformat(),
            details={
                "step_index": 0,
                "task_id": "wf-exp-01:step-0",
                "agent_id": "sql_data",
                "plan_hash": "hash-abc",
                "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),  # EXPIRED
            },
        )

        state: GraphWorkflowState = {
            "workflow_id": "wf-exp-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
            "approval_decision": decision.model_dump(),
            "approval_request": req.model_dump(),
        }

        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["failure_class"], OrchestratorFailureClass.APPROVAL_TIMEOUT.value)
        self.assertEqual(res["next_route"], "fail_workflow")

    async def test_mismatched_approval_fails_closed(self):
        """Fix A: Mismatched plan hash in approval request fails closed with POLICY_VIOLATION."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.SQL_DATA,
            description="Delete records",
            operation_type="delete",
            risk_level=RiskLevel.HIGH,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        decision = ApprovalDecision(
            approval_id="appr-mis-01",
            workflow_id="wf-mis-01",
            decision=ApprovalStatus.APPROVED,
            approver_id="officer",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        req = ApprovalRequest(
            approval_id="appr-mis-01",
            workflow_id="wf-mis-01",
            requested_action="Execute step 0",
            risk_level=RiskLevel.HIGH,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc).isoformat(),
            details={
                "step_index": 0,
                "task_id": "wf-mis-01:step-0",
                "agent_id": "sql_data",
                "plan_hash": "hash-different-tampered",  # MISMATCH
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            },
        )

        state: GraphWorkflowState = {
            "workflow_id": "wf-mis-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
            "approval_decision": decision.model_dump(),
            "approval_request": req.model_dump(),
        }

        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["failure_class"], OrchestratorFailureClass.POLICY_VIOLATION.value)
        self.assertEqual(res["next_route"], "fail_workflow")

    async def test_already_consumed_approval_fails_closed(self):
        """Fix A: Already consumed approval cannot authorize another mutating replay."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.SQL_DATA,
            description="Delete records",
            operation_type="delete",
            risk_level=RiskLevel.HIGH,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        decision = ApprovalDecision(
            approval_id="appr-con-01",
            workflow_id="wf-con-01",
            decision=ApprovalStatus.APPROVED,
            approver_id="officer",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )

        state: GraphWorkflowState = {
            "workflow_id": "wf-con-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
            "approval_decision": decision.model_dump(),
            "approval_consumed": True,  # ALREADY CONSUMED
        }

        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["failure_class"], OrchestratorFailureClass.UNKNOWN_FAILURE.value)
        self.assertEqual(res["error_info"]["code"], "AMBIGUOUS_SIDE_EFFECT")
        self.assertEqual(res["next_route"], "fail_workflow")

    async def test_interrupted_mutating_step_without_approval_fails_closed(self):
        """Fix A: Actual interrupted mutating-step recovery without approval strictly fails closed."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.SQL_DATA,
            description="Delete records",
            operation_type="delete",
            risk_level=RiskLevel.HIGH,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        state: GraphWorkflowState = {
            "workflow_id": "wf-noappr-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
            "approval_decision": None,  # NO APPROVAL
        }

        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["failure_class"], OrchestratorFailureClass.UNKNOWN_FAILURE.value)
        self.assertEqual(res["error_info"]["code"], "AMBIGUOUS_SIDE_EFFECT")
        self.assertEqual(res["next_route"], "fail_workflow")

    async def test_fencing_lease_lost_prevents_dispatch(self):
        """Fix A: Missing or stale lock token immediately raises FencingLeaseLostError."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Read policy",
            operation_type="read",
            risk_level=RiskLevel.LOW,
        )

        state: GraphWorkflowState = {
            "workflow_id": "wf-fence-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": None,  # MISSING LEASE CREDENTIAL
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
        }

        with self.assertRaises(FencingLeaseLostError):
            await self.nodes.node_prepare_step(state)

    # -------------------------------------------------------------------------
    # Fix B: Trusted Tool Idempotency & Metadata Tests
    # -------------------------------------------------------------------------
    async def test_caller_supplied_is_idempotent_cannot_authorize_replay(self):
        """Fix B: Caller-supplied is_idempotent=True in inputs cannot authorize mutating replay."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.DEVELOPER,
            description="Deploy application service",
            operation_type="deploy",
            risk_level=RiskLevel.LOW,
            inputs={"is_idempotent": True, "idempotent": True, "idempotency_key": "caller-key-123"},
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        state: GraphWorkflowState = {
            "workflow_id": "wf-idemp-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
        }

        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["failure_class"], OrchestratorFailureClass.UNKNOWN_FAILURE.value)
        self.assertEqual(res["error_info"]["code"], "AMBIGUOUS_SIDE_EFFECT")
        self.assertEqual(res["next_route"], "fail_workflow")

    async def test_unregistered_tool_cannot_use_read_only_allowlist(self):
        """Fix B: Unregistered or mismatched tool cannot use read-only allowlist."""
        tool_reg = ToolRegistry()
        custom_nodes = OrchestrationNodes(
            registry=self.registry,
            workflow_repo=self.mock_repo,
            tool_registry=tool_reg,
        )

        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Read external data",
            operation_type="read",
            risk_level=RiskLevel.LOW,
            inputs={"tool": "unregistered_read_tool"},  # NOT IN REGISTRY
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        state: GraphWorkflowState = {
            "workflow_id": "wf-unreg-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
        }

        res = await custom_nodes.node_prepare_step(state)
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["error_info"]["code"], "AMBIGUOUS_SIDE_EFFECT")
        self.assertEqual(res["next_route"], "fail_workflow")

    async def test_trusted_registered_read_only_tool_replayed(self):
        """Fix B: Trusted registered read-only tool can be replayed safely."""
        tool_reg = ToolRegistry()
        meta = ToolMetadata(
            tool_id="postgresql_read",
            name="PostgreSQL Read Tool",
            description="Read-only postgres query",
            category=ToolCategory.DATA,
            allowed_agent_ids=[AgentID.RESEARCH_RAG, AgentID.SQL_DATA],
            risk_level=RiskLevel.LOW,
        )
        tool_reg.register_tool(DummyTool(meta))

        custom_nodes = OrchestrationNodes(
            registry=self.registry,
            workflow_repo=self.mock_repo,
            tool_registry=tool_reg,
            event_bus=self.mock_event_bus,
        )

        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Read policy documents",
            operation_type="read",
            risk_level=RiskLevel.LOW,
            inputs={"tool": "postgresql_read"},
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        state: GraphWorkflowState = {
            "workflow_id": "wf-regread-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
        }

        res = await custom_nodes.node_prepare_step(state)
        self.assertEqual(res["next_route"], "evaluate_risk_gate")

    async def test_mutating_tool_replayable_only_with_trusted_idempotency_contract(self):
        """Fix B: Mutating tool is replayable only if trusted contract proves external idempotency."""
        tool_reg = ToolRegistry()
        meta = IdempotentToolMetadata(
            tool_id="idempotent_charge_tool",
            name="Idempotent Payment Charge",
            description="Charges payment with strict provider idempotency",
            category=ToolCategory.SYSTEM,
            allowed_agent_ids=[AgentID.FINANCE],
            risk_level=RiskLevel.HIGH,
            is_idempotent=True,
        )
        tool_reg.register_tool(DummyTool(meta, is_idempotent=True))

        custom_nodes = OrchestrationNodes(
            registry=self.registry,
            workflow_repo=self.mock_repo,
            tool_registry=tool_reg,
            event_bus=self.mock_event_bus,
        )

        # 1. With stable idempotency key -> allowed replay
        step_with_key = PlanStep(
            step_index=0,
            agent_id=AgentID.FINANCE,
            description="Process idempotent charge",
            operation_type="write",
            risk_level=RiskLevel.HIGH,
            inputs={"tool": "idempotent_charge_tool", "idempotency_key": "tx-stable-key-001"},
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        state_with_key: GraphWorkflowState = {
            "workflow_id": "wf-idm-pass",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step_with_key.model_dump()],
            "interrupted_steps": [0],
        }

        res_pass = await custom_nodes.node_prepare_step(state_with_key)
        self.assertEqual(res_pass["next_route"], "evaluate_risk_gate")

        # 2. Without idempotency key -> fails closed
        step_no_key = PlanStep(
            step_index=0,
            agent_id=AgentID.FINANCE,
            description="Process charge missing key",
            operation_type="write",
            risk_level=RiskLevel.HIGH,
            inputs={"tool": "idempotent_charge_tool"},  # Missing idempotency_key
        )
        state_no_key: GraphWorkflowState = {
            **state_with_key,
            "steps": [step_no_key.model_dump()],
        }

        res_fail = await custom_nodes.node_prepare_step(state_no_key)
        self.assertEqual(res_fail["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res_fail["error_info"]["code"], "AMBIGUOUS_SIDE_EFFECT")

    async def test_unknown_metadata_fails_closed(self):
        """Fix B: Unknown operation type metadata fails closed with AMBIGUOUS_SIDE_EFFECT."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.DEVELOPER,
            description="Execute unknown command",
            operation_type=None,
            risk_level=RiskLevel.LOW,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        state: GraphWorkflowState = {
            "workflow_id": "wf-unknown-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
        }

        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["error_info"]["code"], "AMBIGUOUS_SIDE_EFFECT")
    async def test_approval_requested_action_mismatch_fails_closed(self):
        """Fix A: Mismatched requested action fails closed."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.SQL_DATA,
            description="Delete obsolete audit records",
            operation_type="delete",
            risk_level=RiskLevel.HIGH,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)
        decision = ApprovalDecision(
            approval_id="appr-act-01",
            workflow_id="wf-act-01",
            decision=ApprovalStatus.APPROVED,
            approver_id="security-officer",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        req = ApprovalRequest(
            approval_id="appr-act-01",
            workflow_id="wf-act-01",
            requested_action="Execute step 0: Different Action entirely via sql_data",
            risk_level=RiskLevel.HIGH,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc).isoformat(),
            details={
                "step_index": 0,
                "task_id": "wf-act-01:step-0",
                "agent_id": "sql_data",
                "plan_hash": "hash-abc",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            },
        )
        state: GraphWorkflowState = {
            "workflow_id": "wf-act-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
            "approval_decision": decision.model_dump(),
            "approval_request": req.model_dump(),
        }
        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["error_info"]["code"], "REQUESTED_ACTION_MISMATCH")

    async def test_approval_tenant_mismatch_fails_closed(self):
        """Fix A: Mismatched tenant fails closed."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.SQL_DATA,
            description="Delete records",
            operation_type="delete",
            risk_level=RiskLevel.HIGH,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)
        decision = ApprovalDecision(
            approval_id="appr-ten-01",
            workflow_id="wf-ten-01",
            decision=ApprovalStatus.APPROVED,
            approver_id="security-officer",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        req = ApprovalRequest(
            approval_id="appr-ten-01",
            workflow_id="wf-ten-01",
            requested_action="Execute step 0: Delete records via sql_data",
            risk_level=RiskLevel.HIGH,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc).isoformat(),
            details={
                "step_index": 0,
                "task_id": "wf-ten-01:step-0",
                "agent_id": "sql_data",
                "tenant_id": "tenant-other",
                "plan_hash": "hash-abc",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            },
        )
        state: GraphWorkflowState = {
            "workflow_id": "wf-ten-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
            "approval_decision": decision.model_dump(),
            "approval_request": req.model_dump(),
        }
        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["error_info"]["code"], "TENANT_MISMATCH")

    async def test_approval_repo_authoritative_verification_fails_if_unapproved(self):
        """Fix A: Injected approval repository authoritative verification fails if status != APPROVED."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.SQL_DATA,
            description="Delete records",
            operation_type="delete",
            risk_level=RiskLevel.HIGH,
        )
        mock_approval_repo = AsyncMock()
        mock_model = MagicMock()
        mock_model.status = "PENDING"  # Not yet approved in database
        mock_model.workflow_id = "wf-repo-01"
        mock_model.step_index = 0
        mock_model.task_id = "wf-repo-01:step-0"
        mock_approval_repo.get_model_by_id.return_value = mock_model

        custom_nodes = OrchestrationNodes(
            registry=self.registry,
            workflow_repo=self.mock_repo,
            approval_repo=mock_approval_repo,
            event_bus=self.mock_event_bus,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)
        decision = ApprovalDecision(
            approval_id="appr-repo-01",
            workflow_id="wf-repo-01",
            decision=ApprovalStatus.APPROVED,
            approver_id="security-officer",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        req = ApprovalRequest(
            approval_id="appr-repo-01",
            workflow_id="wf-repo-01",
            requested_action="Execute step 0: Delete records via sql_data",
            risk_level=RiskLevel.HIGH,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc).isoformat(),
            details={
                "step_index": 0,
                "task_id": "wf-repo-01:step-0",
                "agent_id": "sql_data",
                "plan_hash": "hash-abc",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            },
        )
        state: GraphWorkflowState = {
            "workflow_id": "wf-repo-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
            "approval_decision": decision.model_dump(),
            "approval_request": req.model_dump(),
        }
        res = await custom_nodes.node_prepare_step(state)
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["error_info"]["code"], "APPROVAL_NOT_APPROVED")

    async def test_first_time_approved_mutating_step_can_resume(self):
        """Part 2.18.2: First-time approved mutating step can resume safely."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.SQL_DATA,
            description="Delete records",
            operation_type="delete",
            risk_level=RiskLevel.HIGH,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)
        mock_wf = MagicMock()
        mock_wf.plan_hash = "hash-abc"
        mock_wf.execution_metadata = {"is_first_resume_claim": True, "approval_claim_count": 1}
        self.mock_repo.get_workflow.return_value = mock_wf

        decision = ApprovalDecision(
            approval_id="appr-first-01",
            workflow_id="wf-first-01",
            decision=ApprovalStatus.APPROVED,
            approver_id="officer",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        req = ApprovalRequest(
            approval_id="appr-first-01",
            workflow_id="wf-first-01",
            requested_action="Execute step 0: Delete records via sql_data",
            risk_level=RiskLevel.HIGH,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc).isoformat(),
            details={
                "step_index": 0,
                "task_id": "wf-first-01:step-0",
                "agent_id": "sql_data",
                "plan_hash": "hash-abc",
                "tenant_id": "tenant-alpha",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                "claim_count": 1,
                "is_first_claim": True,
                "is_recovery_claim": False,
            },
        )

        state: GraphWorkflowState = {
            "workflow_id": "wf-first-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-123",
            "current_revision": 1,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
            "approval_decision": decision.model_dump(),
            "approval_request": req.model_dump(),
            "is_first_claim": True,
            "is_recovery_claim": False,
        }

        res = await self.nodes.node_prepare_step(state)
        self.assertNotEqual(res.get("status"), WorkflowStatus.FAILED.value)
        self.assertIn("next_route", res)
        self.assertNotEqual(res.get("next_route"), "fail_workflow")

    async def test_second_resume_cannot_dispatch_non_idempotent_mutation(self):
        """Part 2.18.2: A second resume cannot dispatch the same non-idempotent mutating operation."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.SQL_DATA,
            description="Delete records",
            operation_type="delete",
            risk_level=RiskLevel.HIGH,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        decision = ApprovalDecision(
            approval_id="appr-sec-01",
            workflow_id="wf-sec-01",
            decision=ApprovalStatus.APPROVED,
            approver_id="officer",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        req = ApprovalRequest(
            approval_id="appr-sec-01",
            workflow_id="wf-sec-01",
            requested_action="Execute step 0: Delete records via sql_data",
            risk_level=RiskLevel.HIGH,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc).isoformat(),
            details={
                "step_index": 0,
                "task_id": "wf-sec-01:step-0",
                "agent_id": "sql_data",
                "plan_hash": "hash-abc",
                "tenant_id": "tenant-alpha",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                "claim_count": 2,
                "is_first_claim": False,
                "is_recovery_claim": True,
            },
        )

        state: GraphWorkflowState = {
            "workflow_id": "wf-sec-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 2,
            "lock_token": "tok-456",
            "current_revision": 2,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
            "approval_decision": decision.model_dump(),
            "approval_request": req.model_dump(),
            "is_first_claim": False,
            "is_recovery_claim": True,
        }

        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["failure_class"], OrchestratorFailureClass.UNKNOWN_FAILURE.value)
        self.assertEqual(res["error_info"]["code"], "AMBIGUOUS_SIDE_EFFECT")
        self.assertEqual(res["next_route"], "fail_workflow")

    async def test_crash_after_claim_before_known_dispatch_fails_closed(self):
        """Part 2.18.2: Crash after claim before known dispatch fails closed without ledger proof."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.FINANCE,
            description="Process payout",
            operation_type="payout",
            risk_level=RiskLevel.HIGH,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        decision = ApprovalDecision(
            approval_id="appr-crash-pre-01",
            workflow_id="wf-crash-pre-01",
            decision=ApprovalStatus.APPROVED,
            approver_id="cfo",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        state: GraphWorkflowState = {
            "workflow_id": "wf-crash-pre-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 2,
            "lock_token": "tok-recover",
            "current_revision": 2,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
            "approval_decision": decision.model_dump(),
            "is_first_claim": False,
            "is_recovery_claim": True,
        }

        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["failure_class"], OrchestratorFailureClass.UNKNOWN_FAILURE.value)
        self.assertEqual(res["error_info"]["code"], "AMBIGUOUS_SIDE_EFFECT")
        self.assertEqual(res["next_route"], "fail_workflow")

    async def test_crash_after_dispatch_fails_closed_for_non_idempotent_mutation(self):
        """Part 2.18.2: Crash after dispatch but before task completion fails closed for non-idempotent mutation."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.FINANCE,
            description="Execute transfer",
            operation_type="transfer",
            risk_level=RiskLevel.HIGH,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        decision = ApprovalDecision(
            approval_id="appr-crash-post-01",
            workflow_id="wf-crash-post-01",
            decision=ApprovalStatus.APPROVED,
            approver_id="cfo",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )

        state: GraphWorkflowState = {
            "workflow_id": "wf-crash-post-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 2,
            "lock_token": "tok-recover-post",
            "current_revision": 2,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
            "approval_decision": decision.model_dump(),
            "is_first_claim": False,
            "is_recovery_claim": True,
        }

        res = await self.nodes.node_prepare_step(state)
        self.assertEqual(res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(res["failure_class"], OrchestratorFailureClass.UNKNOWN_FAILURE.value)
        self.assertEqual(res["error_info"]["code"], "AMBIGUOUS_SIDE_EFFECT")
        self.assertEqual(res["next_route"], "fail_workflow")

    async def test_verified_external_idempotency_permits_replay_on_recovery(self):
        """Part 2.18.2: Verified external idempotency permits replay on recovery claim."""
        tool_reg = ToolRegistry()
        tool_meta = IdempotentToolMetadata(
            tool_id="idempotent_stripe_charge",
            name="idempotent_stripe_charge",
            description="Charges credit card with idempotency key",
            category=ToolCategory.SYSTEM,
            risk_level=RiskLevel.HIGH,
            allowed_agents=["finance"],
            is_idempotent=True,
            timeout_seconds=30,
        )
        mock_tool = DummyTool(tool_meta, is_idempotent=True)
        tool_reg.register_tool(mock_tool)

        nodes = OrchestrationNodes(
            registry=self.registry,
            workflow_repo=self.mock_repo,
            tool_registry=tool_reg,
            event_bus=self.mock_event_bus,
        )
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)

        step = PlanStep(
            step_index=0,
            agent_id=AgentID.FINANCE,
            description="Idempotent charge",
            operation_type="charge",
            inputs={"tool": "idempotent_stripe_charge", "idempotency_key": "idem-key-999"},
            risk_level=RiskLevel.HIGH,
        )

        state: GraphWorkflowState = {
            "workflow_id": "wf-idem-rec-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 2,
            "lock_token": "tok-idem",
            "current_revision": 2,
            "plan_hash": "hash-abc",
            "step_cursor": 0,
            "steps": [step.model_dump()],
            "interrupted_steps": [0],
            "is_first_claim": False,
            "is_recovery_claim": True,
        }

        res = await nodes.node_prepare_step(state)
        self.assertNotEqual(res.get("status"), WorkflowStatus.FAILED.value)
        self.assertIn("next_route", res)
        self.assertNotEqual(res.get("next_route"), "fail_workflow")

    async def test_concurrent_resume_requests_permit_at_most_one_first_time_claim(self):
        """Part 2.18.2: Concurrent resume requests permit at most one first-time claim."""
        from eaoc.state.workflow_repository import InMemoryWorkflowRecoveryRepository
        repo = InMemoryWorkflowRecoveryRepository()

        wf, _ = await repo.create_workflow_intake(
            workflow_id="wf-concurr-claim",
            tenant_id="tenant-alpha",
            user_id="usr-1",
            task_id="task-1",
            task_description="concurrency test",
        )
        _, epoch, rev, token = await repo.acquire_execution_lease("wf-concurr-claim", "tenant-alpha")
        rev = await repo.pause_workflow_for_approval(
            workflow_id="wf-concurr-claim",
            tenant_id="tenant-alpha",
            step_index=0,
            approval_id="appr-conc-01",
            requested_action="Execute payment",
            risk_level="high",
            details={},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        appr = repo._approvals["appr-conc-01"]
        appr.status = ApprovalStatus.APPROVED.value
        appr.decision = ApprovalStatus.APPROVED.value
        appr.resume_status = "pending"

        claimed1, epoch1, rev1, tok1, wf1, reason1 = await repo.claim_approval_resume(
            approval_id="appr-conc-01",
            workflow_id="wf-concurr-claim",
            tenant_id="tenant-alpha",
        )
        self.assertTrue(claimed1)
        self.assertTrue(wf1.execution_metadata.get("is_first_resume_claim"))
        self.assertEqual(wf1.execution_metadata.get("approval_claim_count"), 1)

        claimed2, epoch2, rev2, tok2, wf2, reason2 = await repo.claim_approval_resume(
            approval_id="appr-conc-01",
            workflow_id="wf-concurr-claim",
            tenant_id="tenant-alpha",
        )
        self.assertFalse(claimed2)
        self.assertIn("workflow_lease_active", reason2)

        wf_model = repo._workflows["wf-concurr-claim"]
        wf_model.lock_expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)

        claimed3, epoch3, rev3, tok3, wf3, reason3 = await repo.claim_approval_resume(
            approval_id="appr-conc-01",
            workflow_id="wf-concurr-claim",
            tenant_id="tenant-alpha",
        )
        self.assertTrue(claimed3)
        self.assertFalse(wf3.execution_metadata.get("is_first_resume_claim"))
        self.assertEqual(wf3.execution_metadata.get("approval_claim_count"), 2)
        self.assertEqual(appr.details.get("claim_count"), 2)
        self.assertTrue(appr.details.get("is_recovery_claim"))
        self.assertFalse(appr.details.get("is_first_claim"))


if __name__ == "__main__":

    unittest.main()


