"""
EAOC LangGraph Assembly & Wiring Test Suite (Part 2.18.3.1)

Verifies the isolated LangGraph assembly, compilation, lifecycle node registration,
and conditional routing topologies:
- Successful graph construction and compilation (no competing checkpointer)
- Valid initial routing and terminal success (COMPLETED)
- Failure and terminal failure routing (FAILED)
- Approval pause without unauthorized dispatch (WAITING_APPROVAL)
- Approved resume passing through all mandatory safety gates
- Bounded retry and replan routing (bounded cycles)
- No dispatch after fencing or replay-safety failure (AMBIGUOUS_SIDE_EFFECT)
- No unintended cycles or duplicate terminal processing
"""

import asyncio
from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from langgraph.graph import StateGraph, START, END
from langgraph.graph.state import CompiledStateGraph

from eaoc.agents.planner.schemas import ExecutionPlan, PlanStep, PlanType
from eaoc.agents.registry import AgentRegistry
from eaoc.agents.research_rag.agent import ResearchRagAgent
from eaoc.orchestration.graph import (
    build_workflow_graph,
    compile_workflow_graph,
    route_init_and_recover,
    route_plan_resolution,
    route_prepare_step,
    route_evaluate_risk_gate,
    route_pause_for_approval,
    route_dispatch_step,
    route_record_step_success,
    route_handle_step_failure,
    route_contextual_replan,
    route_reviewer_gate,
)
from eaoc.orchestration.nodes import OrchestrationNodes
from eaoc.orchestration.state import GraphWorkflowState
from eaoc.schemas.domain import (
    AgentID,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
    TaskStatus,
    WorkflowStatus,
)
from eaoc.agents.orchestrator.schemas import OrchestratorFailureClass
from eaoc.agents.reviewer_validation.schemas import ReviewRecommendation


class TestLangGraphAssemblyAndRouting(unittest.IsolatedAsyncioTestCase):
    """Isolated unit test suite for LangGraph StateGraph construction, compilation, and routing."""

    def setUp(self):
        self.registry = AgentRegistry()
        self.mock_researcher = ResearchRagAgent()
        self.registry.register_agent(self.mock_researcher)

        self.mock_repo = AsyncMock()
        self.mock_event_bus = AsyncMock()
        self.mock_approval_repo = AsyncMock()

        self.nodes = OrchestrationNodes(
            registry=self.registry,
            workflow_repo=self.mock_repo,
            approval_repo=self.mock_approval_repo,
            event_bus=self.mock_event_bus,
            max_retries=1,
            initial_backoff_seconds=0.01,
            backoff_multiplier=1.0,
            max_replan_attempts=2,
        )

    # -------------------------------------------------------------------------
    # 1. Successful Graph Construction and Compilation
    # -------------------------------------------------------------------------
    def test_graph_construction_and_compilation(self):
        """Verify StateGraph registers all 12 lifecycle nodes and compiles without a checkpointer."""
        builder = build_workflow_graph(self.nodes)
        self.assertIsInstance(builder, StateGraph)

        compiled = compile_workflow_graph(self.nodes)
        self.assertIsInstance(compiled, CompiledStateGraph)

        # Confirm all 12 operational nodes exist in the graph
        expected_nodes = {
            "init_and_recover",
            "plan_resolution",
            "prepare_step",
            "evaluate_risk_gate",
            "pause_for_approval",
            "dispatch_step",
            "record_step_success",
            "handle_step_failure",
            "contextual_replan",
            "reviewer_gate",
            "finalize_workflow",
            "fail_workflow",
        }
        actual_nodes = set(compiled.nodes.keys())
        for expected in expected_nodes:
            self.assertIn(expected, actual_nodes)

        # Confirm PostgreSQL remains canonical: checkpointer must be absent / None
        self.assertIsNone(compiled.checkpointer)

    # -------------------------------------------------------------------------
    # 2. Valid Initial Routing and Terminal Success
    # -------------------------------------------------------------------------
    async def test_end_to_end_successful_execution(self):
        """Verify that a valid plan flows cleanly through all nodes to terminal COMPLETED status."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Perform query",
            operation_type="read",
            risk_level=RiskLevel.LOW,
            inputs={"query": "test compliance"},
        )
        plan = ExecutionPlan(
            plan_id="plan-success-01",
            task_id="task-success-01",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Read query",
            steps=[step],
        )

        # Mock repository responses
        self.mock_repo.prepare_task_step.return_value = (False, None, None, 1)
        self.mock_repo.complete_task_step.return_value = 2
        mock_wf = MagicMock()
        mock_wf.plan_hash = None
        mock_wf.execution_metadata = {}
        self.mock_repo.get_workflow.return_value = mock_wf

        # Mock agent execution
        with patch.object(self.registry, "dispatch") as mock_dispatch:
            mock_dispatch.return_value = MagicMock(
                status=TaskStatus.COMPLETED,
                output_payload={"result": "data retrieved"},
                error=None,
            )

            compiled = compile_workflow_graph(self.nodes)

            initial_state: GraphWorkflowState = {
                "workflow_id": "wf-success-01",
                "task_id": "task-success-01",
                "tenant_id": "tenant-alpha",
                "user_id": "usr-01",
                "user_context": {"roles": ["analyst"]},
                "fencing_epoch": 1,
                "lock_token": "tok-success",
                "current_revision": 1,
                "execution_plan": plan.model_dump(),
            }

            final_state = await compiled.ainvoke(initial_state)

            self.assertEqual(final_state["status"], WorkflowStatus.COMPLETED.value)
            self.assertIsNotNone(final_state.get("final_response"))
            self.assertIn(0, final_state.get("completed_steps", {}))
            self.assertEqual(final_state.get("next_route"), "END")

    # -------------------------------------------------------------------------
    # 3. Failure and Terminal Failure Routing
    # -------------------------------------------------------------------------
    async def test_plan_validation_failure_routes_to_terminal_fail(self):
        """Verify that plan validation failure in plan_resolution routes directly to fail_workflow."""
        compiled = compile_workflow_graph(self.nodes)

        # Malformed plan (empty steps violates plan validator)
        bad_plan = {
            "plan_id": "plan-bad-01",
            "task_id": "task-bad-01",
            "plan_type": "single_agent",
            "planning_rationale": "Invalid empty plan",
            "steps": [],
        }

        initial_state: GraphWorkflowState = {
            "workflow_id": "wf-fail-01",
            "tenant_id": "tenant-alpha",
            "fencing_epoch": 1,
            "lock_token": "tok-fail",
            "current_revision": 1,
            "execution_plan": bad_plan,
        }

        final_state = await compiled.ainvoke(initial_state)

        self.assertEqual(final_state["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(
            final_state.get("failure_class"),
            OrchestratorFailureClass.PLAN_VALIDATION_FAILED.value,
        )
        self.assertEqual(final_state.get("next_route"), "END")

    async def test_reviewer_rejection_routes_to_terminal_fail(self):
        """Verify that reviewer rejection routes to fail_workflow and terminates."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Perform query",
            operation_type="read",
            risk_level=RiskLevel.LOW,
            inputs={"query": "test query"},
        )
        plan = ExecutionPlan(
            plan_id="plan-rev-fail",
            task_id="task-rev-fail",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Reviewer rejection test",
            steps=[step],
        )

        self.mock_repo.prepare_task_step.return_value = (False, None, None, 1)
        self.mock_repo.complete_task_step.return_value = 2

        with patch.object(self.registry, "dispatch") as mock_dispatch:
            mock_dispatch.return_value = MagicMock(
                status=TaskStatus.COMPLETED,
                output_payload={"result": "data"},
                error=None,
            )
            # Mock reviewer engine rejecting the output
            with patch.object(self.nodes.reviewer_engine, "evaluate") as mock_review:
                mock_review.return_value = MagicMock(
                    recommendation=ReviewRecommendation.REJECT,
                    findings=[],
                )
                compiled = compile_workflow_graph(self.nodes)

                initial_state: GraphWorkflowState = {
                    "workflow_id": "wf-rev-01",
                    "task_id": "task-rev-01",
                    "tenant_id": "tenant-alpha",
                    "user_id": "usr-01",
                    "user_context": {"roles": ["analyst"]},
                    "fencing_epoch": 1,
                    "lock_token": "tok-rev",
                    "current_revision": 1,
                    "execution_plan": plan.model_dump(),
                }

                final_state = await compiled.ainvoke(initial_state)

                self.assertEqual(final_state["status"], WorkflowStatus.FAILED.value)
                self.assertEqual(
                    final_state.get("failure_class"),
                    OrchestratorFailureClass.REVIEWER_REJECTED.value,
                )
                self.assertEqual(final_state.get("next_route"), "END")

    # -------------------------------------------------------------------------
    # 4. Approval Pause Without Unauthorized Dispatch
    # -------------------------------------------------------------------------
    async def test_approval_pause_terminates_without_dispatch(self):
        """Verify that a high-risk step requiring approval pauses and does NOT reach dispatch_step."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="High risk delete action",
            operation_type="delete",
            risk_level=RiskLevel.HIGH,
            inputs={"target": "sensitive_data"},
        )
        plan = ExecutionPlan(
            plan_id="plan-pause-01",
            task_id="task-pause-01",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Approval pause test",
            steps=[step],
        )

        self.mock_repo.prepare_task_step.return_value = (False, None, None, 1)
        self.mock_repo.pause_workflow_for_approval.return_value = 2
        mock_wf = MagicMock()
        mock_wf.plan_hash = None
        mock_wf.execution_metadata = {}
        self.mock_repo.get_workflow.return_value = mock_wf

        with patch.object(self.registry, "dispatch") as mock_dispatch:
            compiled = compile_workflow_graph(self.nodes)

            initial_state: GraphWorkflowState = {
                "workflow_id": "wf-pause-01",
                "task_id": "task-pause-01",
                "tenant_id": "tenant-alpha",
                "user_id": "usr-01",
                "user_context": {"roles": ["analyst"]},
                "fencing_epoch": 1,
                "lock_token": "tok-pause",
                "current_revision": 1,
                "execution_plan": plan.model_dump(),
            }

            final_state = await compiled.ainvoke(initial_state)

            # Workflow must pause in WAITING_APPROVAL
            self.assertEqual(final_state["status"], WorkflowStatus.WAITING_APPROVAL.value)
            self.assertIsNotNone(final_state.get("approval_request"))
            self.assertEqual(final_state.get("next_route"), "END")

            # Agent dispatch must NOT have occurred
            mock_dispatch.assert_not_called()

    # -------------------------------------------------------------------------
    # 5. Approved Resume Passing Through Mandatory Safety Gates
    # -------------------------------------------------------------------------
    async def test_approved_resume_passes_all_safety_gates(self):
        """Verify that resuming with an approved decision passes through preparation and risk gate."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Approved high-risk query",
            operation_type="query",
            risk_level=RiskLevel.HIGH,
            inputs={"query": "approved query"},
        )
        plan = ExecutionPlan(
            plan_id="plan-resume-01",
            task_id="task-resume-01",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Approved resume test",
            steps=[step],
        )
        from eaoc.agents.orchestrator.engine import compute_plan_hash
        plan_hash = compute_plan_hash(plan)

        decision = ApprovalDecision(
            approval_id="appr-gate-01",
            workflow_id="wf-resume-01",
            decision=ApprovalStatus.APPROVED,
            approver_id="security-lead",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        req = ApprovalRequest(
            approval_id="appr-gate-01",
            workflow_id="wf-resume-01",
            requested_action="Execute step 0: Approved high-risk query via research_rag",
            risk_level=RiskLevel.HIGH,
            status=ApprovalStatus.PENDING,
            requested_at=datetime.now(timezone.utc).isoformat(),
            details={
                "step_index": 0,
                "task_id": "task-resume-01:step-0",
                "agent_id": "research_rag",
                "plan_hash": plan_hash,
                "tenant_id": "tenant-alpha",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                "claim_count": 1,
                "is_first_claim": True,
                "is_recovery_claim": False,
            },
        )

        self.mock_repo.prepare_task_step.return_value = (False, None, None, 2)
        self.mock_repo.complete_task_step.return_value = 3
        mock_wf = MagicMock()
        mock_wf.plan_hash = plan_hash
        mock_wf.execution_metadata = {"is_first_resume_claim": True, "approval_claim_count": 1}
        self.mock_repo.get_workflow.return_value = mock_wf

        with patch.object(self.registry, "dispatch") as mock_dispatch:
            mock_dispatch.return_value = MagicMock(
                status=TaskStatus.COMPLETED,
                output_payload={"result": "approved execution completed"},
                error=None,
            )

            compiled = compile_workflow_graph(self.nodes)

            initial_state: GraphWorkflowState = {
                "workflow_id": "wf-resume-01",
                "task_id": "task-resume-01:step-0",
                "tenant_id": "tenant-alpha",
                "user_id": "usr-01",
                "user_context": {"roles": ["analyst"]},
                "fencing_epoch": 1,
                "lock_token": "tok-resume",
                "current_revision": 2,
                "execution_plan": plan.model_dump(),
                "approval_decision": decision.model_dump(),
                "approval_request": req.model_dump(),
                "is_first_claim": True,
                "is_recovery_claim": False,
            }

            final_state = await compiled.ainvoke(initial_state)

            self.assertEqual(final_state["status"], WorkflowStatus.COMPLETED.value)
            mock_dispatch.assert_called_once()

    # -------------------------------------------------------------------------
    # 6. Bounded Retry and Replan Routing
    # -------------------------------------------------------------------------
    async def test_bounded_replan_exhaustion_terminates_workflow(self):
        """Verify that replan cycles are bounded and terminate in FAILED when max_replan_attempts is reached."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Replannable step",
            operation_type="read",
            risk_level=RiskLevel.LOW,
            optional=False,
        )
        plan = ExecutionPlan(
            plan_id="plan-replan-01",
            task_id="task-replan-01",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Replan test",
            steps=[step],
        )

        self.mock_repo.prepare_task_step.return_value = (False, None, None, 1)
        mock_wf = MagicMock()
        mock_wf.plan_hash = None
        mock_wf.execution_metadata = {}
        self.mock_repo.get_workflow.return_value = mock_wf

        # Dispatch always fails with STEP_FAILED (retryable)
        with patch.object(self.registry, "dispatch") as mock_dispatch:
            from eaoc.schemas.domain import ErrorInfo
            # Mock step dispatch failing, and Planner dispatch returning revised plan
            def dispatch_side_effect(ctx):
                if ctx.agent_id == AgentID.PLANNER:
                    from eaoc.agents.planner.schemas import PlannerOutput
                    return MagicMock(
                        status=TaskStatus.COMPLETED,
                        output_payload=PlannerOutput(
                            plan_id=plan.plan_id,
                            task_id=plan.task_id,
                            rationale="Revised plan after step failure",
                            plan=plan,
                            confidence_score=0.9,
                        ).model_dump(),
                    )
                return MagicMock(
                    status=TaskStatus.FAILED,
                    error=ErrorInfo(code="STEP_FAILED", message="Temporary error"),
                    output_payload=None,
                )

            mock_dispatch.side_effect = dispatch_side_effect

            compiled = compile_workflow_graph(self.nodes)

            initial_state: GraphWorkflowState = {
                "workflow_id": "wf-replan-01",
                "task_id": "task-replan-01",
                "tenant_id": "tenant-alpha",
                "user_id": "usr-01",
                "user_context": {"roles": ["analyst"]},
                "fencing_epoch": 1,
                "lock_token": "tok-replan",
                "current_revision": 1,
                "execution_plan": plan.model_dump(),
                "replan_count": 0,
            }

            final_state = await compiled.ainvoke(initial_state)

            # Workflow must terminate in FAILED once max_replan_attempts is exhausted
            self.assertEqual(final_state["status"], WorkflowStatus.FAILED.value)
            self.assertGreaterEqual(final_state.get("replan_count", 0), 2)
            self.assertEqual(final_state.get("next_route"), "END")

    # -------------------------------------------------------------------------
    # 7. No Dispatch After Fencing or Replay-Safety Failure
    # -------------------------------------------------------------------------
    async def test_no_dispatch_after_replay_safety_failure(self):
        """Verify that an interrupted mutating step without idempotency fails closed without dispatch."""
        step = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Interrupted mutating operation",
            operation_type="mutation",
            risk_level=RiskLevel.HIGH,
        )
        plan = ExecutionPlan(
            plan_id="plan-replay-fail",
            task_id="task-replay-fail",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Replay safety failure test",
            steps=[step],
        )

        self.mock_repo.prepare_task_step.return_value = (False, None, None, 1)
        mock_wf = MagicMock()
        mock_wf.plan_hash = None
        mock_wf.execution_metadata = {}
        self.mock_repo.get_workflow.return_value = mock_wf

        with patch.object(self.registry, "dispatch") as mock_dispatch:
            compiled = compile_workflow_graph(self.nodes)

            initial_state: GraphWorkflowState = {
                "workflow_id": "wf-replay-fail-01",
                "task_id": "task-replay-fail-01",
                "tenant_id": "tenant-alpha",
                "user_id": "usr-01",
                "user_context": {"roles": ["analyst"]},
                "fencing_epoch": 1,
                "lock_token": "tok-replay-fail",
                "current_revision": 1,
                "execution_plan": plan.model_dump(),
                "interrupted_steps": [0],  # Interrupted mutating step
                "is_first_claim": False,
                "is_recovery_claim": True,
            }

            final_state = await compiled.ainvoke(initial_state)

            self.assertEqual(final_state["status"], WorkflowStatus.FAILED.value)
            self.assertEqual(
                final_state.get("failure_class"),
                OrchestratorFailureClass.UNKNOWN_FAILURE.value,
            )
            self.assertEqual(final_state.get("error_info", {}).get("code"), "AMBIGUOUS_SIDE_EFFECT")
            mock_dispatch.assert_not_called()

    # -------------------------------------------------------------------------
    # 8. No Unintended Cycles or Duplicate Terminal Processing
    # -------------------------------------------------------------------------
    def test_terminal_nodes_have_no_outbound_graph_edges(self):
        """Verify that finalize_workflow and fail_workflow only route to END."""
        compiled = compile_workflow_graph(self.nodes)

        # Inspect compiled graph edges from terminal nodes
        finalize_node = compiled.builder.nodes.get("finalize_workflow")
        fail_node = compiled.builder.nodes.get("fail_workflow")

        self.assertIsNotNone(finalize_node)
        self.assertIsNotNone(fail_node)

        # Ensure outbound edges from finalize_workflow and fail_workflow lead exclusively to END
        outbound_finalize = compiled.builder.edges
        self.assertIn(("finalize_workflow", END), outbound_finalize)
        self.assertIn(("fail_workflow", END), outbound_finalize)

        # Confirm neither terminal node has any outgoing edge back into any other node
        for src, dst in outbound_finalize:
            if src in ("finalize_workflow", "fail_workflow"):
                self.assertEqual(dst, END)


if __name__ == "__main__":
    unittest.main()
