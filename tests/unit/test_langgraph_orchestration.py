"""
EAOC Part 2.18.6.2: LangGraph Stateful Orchestration Acceptance Test Suite
Implements formal acceptance test scenarios A18-01 through A18-16.

Exercises the public OrchestratorEngine boundary in graph mode (use_langgraph=True)
verifying end-to-end lifecycle, fencing, crash recovery, replay safety, HITL approvals,
durable replanning, reviewer gating, and canonical SSE event emission.
"""

import asyncio
import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

from pydantic import BaseModel

from eaoc.agents.base import BaseAgent
from eaoc.agents.orchestrator.engine import OrchestratorEngine
from eaoc.agents.orchestrator.schemas import (
    FinalResponse,
    OrchestratorFailureClass,
    OrchestratorInput,
    OrchestratorOutput,
    ProvenanceEntry,
)
from eaoc.agents.planner.schemas import (
    ExecutionPlan,
    PlanStep,
    PlanType,
    PlannerInput,
    PlannerOutput,
)
from eaoc.agents.registry import AgentRegistry
from eaoc.agents.reviewer_validation.schemas import (
    FindingSeverity,
    ReviewFindingItem,
    ReviewFindingType,
    ReviewRecommendation,
    ReviewerInput,
    ReviewerOutput,
    ReviewerProvenance,
    RiskAssessment,
)
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ErrorInfo,
    RiskLevel,
    TaskStatus,
    WorkflowStatus,
)
from eaoc.schemas.events import StreamEventType, WorkflowStreamEvent
from eaoc.services.event_bus import IWorkflowEventBus
from eaoc.state.interfaces import IWorkflowStateStore
from eaoc.agents.orchestrator.risk_gate import RiskGate, RiskGateDecision
from eaoc.orchestration.nodes import compute_plan_hash, compute_step_hash
from eaoc.state.workflow_repository import (
    FencingLeaseLostError,
    InMemoryWorkflowRecoveryRepository,
)


# ---------------------------------------------------------------------------
# Test Stubs
# ---------------------------------------------------------------------------

class StubPayload(BaseModel):
    model_config = {"extra": "allow"}
    query: Optional[str] = None
    data: Optional[Any] = None
    result: Optional[str] = None


class StubDomainAgent(BaseAgent[StubPayload, StubPayload]):
    """Generic stub domain agent with configurable results and failure behavior."""

    def __init__(
        self,
        agent_id: AgentID,
        result_payload: Optional[Dict[str, Any]] = None,
        fail_attempts: int = 0,
        fail_error: Optional[ErrorInfo] = None,
    ):
        super().__init__(
            agent_id=agent_id,
            input_schema=StubPayload,
            output_schema=StubPayload,
        )
        self.result_payload = result_payload or {"result": "success"}
        self.fail_attempts = fail_attempts
        self.fail_error = fail_error
        self.calls_count = 0

    async def _execute(
        self, validated_input: StubPayload, context: AgentExecutionContext
    ) -> StubPayload:
        self.calls_count += 1
        if self.calls_count <= self.fail_attempts:
            error = self.fail_error or ErrorInfo(
                code="LLM_TIMEOUT",
                message="Temporary timeout",
                error_type="TimeoutError",
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                retryable=True,
            )
            if error.error_type == "PermissionError":
                raise PermissionError(error.message)
            raise TimeoutError(error.message)
        return StubPayload.model_validate(self.result_payload)


class StubPlannerAgent(BaseAgent[PlannerInput, PlannerOutput]):
    """Stub PlannerAgent for unsupplied plan resolution and replanning."""

    def __init__(self, plan_to_return: ExecutionPlan):
        super().__init__(
            agent_id=AgentID.PLANNER,
            input_schema=PlannerInput,
            output_schema=PlannerOutput,
        )
        self.plan_to_return = plan_to_return
        self.calls_count = 0

    async def _execute(
        self, validated_input: PlannerInput, context: AgentExecutionContext
    ) -> PlannerOutput:
        self.calls_count += 1
        return PlannerOutput(
            plan_id=self.plan_to_return.plan_id,
            task_id=validated_input.task_id,
            plan=self.plan_to_return,
            rationale="Deterministic stub plan",
        )


class StubReviewerAgent(BaseAgent[ReviewerInput, ReviewerOutput]):
    """Stub ReviewerValidationAgent returning configurable recommendations."""

    def __init__(
        self,
        recommendation: ReviewRecommendation = ReviewRecommendation.APPROVE,
        findings: Optional[List[ReviewFindingItem]] = None,
    ):
        super().__init__(
            agent_id=AgentID.REVIEWER_VALIDATION,
            input_schema=ReviewerInput,
            output_schema=ReviewerOutput,
        )
        self.recommendation = recommendation
        self.findings = findings or []
        self.calls_count = 0

    async def _execute(
        self, validated_input: ReviewerInput, context: AgentExecutionContext
    ) -> ReviewerOutput:
        self.calls_count += 1
        return ReviewerOutput(
            review_id=f"rev-{uuid.uuid4().hex[:8]}",
            task_id=validated_input.task_id,
            passed=(self.recommendation == ReviewRecommendation.APPROVE),
            findings=self.findings,
            risk_assessment=RiskAssessment(
                risk_level="low" if self.recommendation == ReviewRecommendation.APPROVE else "high",
                risk_factors=[],
            ),
            recommendation=self.recommendation,
            reviewed_at=datetime.now(timezone.utc),
            provenance=ReviewerProvenance(
                review_id=f"rev-{uuid.uuid4().hex[:8]}",
                task_id=validated_input.task_id,
                risk_level="low" if self.recommendation == ReviewRecommendation.APPROVE else "high",
                recommendation=self.recommendation,
                reviewed_at=datetime.now(timezone.utc),
            ),
        )


class MockStateStore(IWorkflowStateStore):
    """In-memory state store stub implementing all IWorkflowStateStore methods."""

    def __init__(self):
        self._store: Dict[str, Any] = {}

    async def create_state(self, state: Any) -> Any:
        self._store[state.workflow_id] = state
        return state

    async def get_state(self, workflow_id: str) -> Optional[Any]:
        return self._store.get(workflow_id)

    async def update_state(self, state: Any) -> Any:
        self._store[state.workflow_id] = state
        return state

    async def delete_state(self, workflow_id: str) -> bool:
        return self._store.pop(workflow_id, None) is not None

    async def exists(self, workflow_id: str) -> bool:
        return workflow_id in self._store


# ---------------------------------------------------------------------------
# Acceptance Test Suite: A18-01 through A18-16
# ---------------------------------------------------------------------------

class TestLangGraphOrchestrationAcceptance(unittest.IsolatedAsyncioTestCase):
    """Formal Acceptance Test Suite for LangGraph Stateful Orchestration (Part 2.18)."""

    def setUp(self):
        self.registry = AgentRegistry()
        self.state_store = MockStateStore()
        # Default reviewer approving all steps
        self.registry.register_agent(StubReviewerAgent(ReviewRecommendation.APPROVE))

    def _create_engine(
        self,
        workflow_repo: Optional[InMemoryWorkflowRecoveryRepository] = None,
        event_bus: Optional[IWorkflowEventBus] = None,
        max_retries: int = 2,
        max_replan_attempts: int = 2,
    ) -> OrchestratorEngine:
        return OrchestratorEngine(
            registry=self.registry,
            state_store=self.state_store,
            workflow_repo=workflow_repo,
            event_bus=event_bus,
            max_retries=max_retries,
            initial_backoff_seconds=0.01,
            backoff_multiplier=1.0,
            max_replan_attempts=max_replan_attempts,
            use_langgraph=True,
        )

    # -------------------------------------------------------------------------
    # A18-01: Graph Compilation & Topology
    # -------------------------------------------------------------------------
    async def test_a18_01_graph_compilation_and_topology(self):
        """A18-01: Validates StateGraph compiles cleanly with all 12 canonical nodes and START/END."""
        engine = self._create_engine()
        self.assertTrue(engine.use_langgraph)
        self.assertIsNotNone(engine._compiled_graph)

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
        graph_nodes = set(engine._compiled_graph.nodes.keys())
        for node in expected_nodes:
            self.assertIn(node, graph_nodes, f"Node '{node}' missing from compiled StateGraph.")

    # -------------------------------------------------------------------------
    # A18-02: End-to-End Happy Path
    # -------------------------------------------------------------------------
    async def test_a18_02_multistep_happy_path_with_reviewer_and_lease_release(self):
        """A18-02: Multi-step plan executes sequentially, completes reviewer gate, releases lease, returns FinalResponse."""
        repo = InMemoryWorkflowRecoveryRepository()
        engine = self._create_engine(workflow_repo=repo)

        agent_rag = StubDomainAgent(AgentID.RESEARCH_RAG, result_payload={"docs": ["item1", "item2"]})
        agent_sql = StubDomainAgent(AgentID.SQL_DATA, result_payload={"rows": [10, 20]})
        agent_analytics = StubDomainAgent(AgentID.ANALYTICS, result_payload={"report": "summary"})
        self.registry.register_agent(agent_rag)
        self.registry.register_agent(agent_sql)
        self.registry.register_agent(agent_analytics)

        plan = ExecutionPlan(
            plan_id="plan-a18-02",
            task_id="task-a18-02",
            plan_type=PlanType.MULTI_AGENT,
            planning_rationale="3-step pipeline",
            steps=[
                PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Step 0: Retrieve"),
                PlanStep(step_index=1, agent_id=AgentID.SQL_DATA, description="Step 1: Query", dependencies=[0]),
                PlanStep(step_index=2, agent_id=AgentID.ANALYTICS, description="Step 2: Summarize", dependencies=[1]),
            ],
        )

        wf_id = "wf-a18-02"
        tenant_id = "tenant-a18"
        await repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-01",
            task_id="task-a18-02",
            task_description="Execute 3-step pipeline",
            execution_plan=plan.model_dump(),
            plan_hash=compute_plan_hash(plan),
        )
        ok, epoch, rev, token = await repo.acquire_execution_lease(wf_id, tenant_id)
        self.assertTrue(ok)

        inp = OrchestratorInput(
            task_id="task-a18-02",
            task_description="Execute 3-step pipeline",
            workflow_id=wf_id,
            execution_plan=plan,
            user_context={"roles": ["admin"], "tenant_id": tenant_id, "user_id": "usr-01"},
            metadata={"tenant_id": tenant_id, "fencing_epoch": epoch, "lock_token": token, "expected_revision": rev},
        )

        out = await engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        self.assertIsNotNone(out.final_response)
        self.assertEqual(agent_rag.calls_count, 1)
        self.assertEqual(agent_sql.calls_count, 1)
        self.assertEqual(agent_analytics.calls_count, 1)
        self.assertEqual(len(out.provenance_chain), 3)

        # Confirm lease released in repository with status COMPLETED
        db_wf = await repo.get_workflow(wf_id, tenant_id)
        self.assertEqual(db_wf.status, WorkflowStatus.COMPLETED.value)
        self.assertIsNone(db_wf.lock_token)

    # -------------------------------------------------------------------------
    # A18-03: Plan Generation & Validation Node
    # -------------------------------------------------------------------------
    async def test_a18_03_unsupplied_plan_dispatches_planner_and_validates(self):
        """A18-03: Unsupplied plan (execution_plan=None) dispatches PlannerAgent, validates, and proceeds to completion."""
        engine = self._create_engine()
        agent_rag = StubDomainAgent(AgentID.RESEARCH_RAG, result_payload={"res": "ok"})
        self.registry.register_agent(agent_rag)

        generated_plan = ExecutionPlan(
            plan_id="plan-gen-01",
            task_id="task-a18-03",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Dynamically resolved plan",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Dynamic research step")],
        )
        planner = StubPlannerAgent(plan_to_return=generated_plan)
        self.registry.register_agent(planner)

        inp = OrchestratorInput(
            task_id="task-a18-03",
            task_description="Solve research query dynamically",
            execution_plan=None,  # Unsupplied plan
            user_context={"roles": ["admin"]},
        )

        out = await engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        self.assertEqual(planner.calls_count, 1)
        self.assertEqual(agent_rag.calls_count, 1)
        self.assertEqual(len(out.provenance_chain), 1)

    # -------------------------------------------------------------------------
    # A18-04: Byte-for-Byte Fast-Forward Resumption
    # -------------------------------------------------------------------------
    async def test_a18_04_byte_for_byte_fast_forward_resumption(self):
        """A18-04: Pre-completed steps in repository are fast-forwarded byte-for-byte without redispatching."""
        repo = InMemoryWorkflowRecoveryRepository()
        engine = self._create_engine(workflow_repo=repo)

        agent_rag = StubDomainAgent(AgentID.RESEARCH_RAG)
        agent_sql = StubDomainAgent(AgentID.SQL_DATA, result_payload={"sql_result": "done"})
        self.registry.register_agent(agent_rag)
        self.registry.register_agent(agent_sql)

        plan = ExecutionPlan(
            plan_id="plan-a18-04",
            task_id="task-a18-04",
            plan_type=PlanType.MULTI_AGENT,
            planning_rationale="Fast forward step 0",
            steps=[
                PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Search data"),
                PlanStep(step_index=1, agent_id=AgentID.SQL_DATA, description="Query SQL", dependencies=[0]),
            ],
        )

        wf_id = "wf-a18-04"
        tenant_id = "tenant-a18"
        p_hash = compute_plan_hash(plan)
        await repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-01",
            task_id="task-a18-04",
            task_description="Execute plan",
            execution_plan=plan.model_dump(),
            plan_hash=p_hash,
        )
        ok, epoch, rev, token = await repo.acquire_execution_lease(wf_id, tenant_id)
        self.assertTrue(ok)

        # Pre-complete step 0 in repository
        is_done, _, _, rev = await repo.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG.value,
            name="Search data",
            plan_hash=p_hash,
            step_hash=compute_step_hash(plan.steps[0]),
            inputs={},
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )
        self.assertFalse(is_done)
        rev = await repo.complete_task_step(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            output_data={"cached_text": "byte_for_byte_exact_output"},
            provenance={"agent_id": "research_rag", "step_index": 0, "status": "COMPLETED", "action_summary": "Search"},
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        inp = OrchestratorInput(
            task_id="task-a18-04",
            task_description="Resume fast-forward",
            workflow_id=wf_id,
            execution_plan=plan,
            user_context={"roles": ["admin"], "tenant_id": tenant_id, "user_id": "usr-01"},
            metadata={"tenant_id": tenant_id, "fencing_epoch": epoch, "lock_token": token, "expected_revision": rev},
        )

        out = await engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        # Step 0 was pre-completed and fast-forwarded: agent_rag was NOT called
        self.assertEqual(agent_rag.calls_count, 0)
        # Step 1 dispatched and succeeded
        self.assertEqual(agent_sql.calls_count, 1)

    # -------------------------------------------------------------------------
    # A18-05: Deterministic Risk Gate (Low/Med)
    # -------------------------------------------------------------------------
    async def test_a18_05_deterministic_risk_gate_low_and_medium(self):
        """A18-05: Low- and medium-risk steps proceed automatically without human-in-the-loop pause."""
        engine = self._create_engine()
        agent_rag = StubDomainAgent(AgentID.RESEARCH_RAG)
        agent_sql = StubDomainAgent(AgentID.SQL_DATA)
        self.registry.register_agent(agent_rag)
        self.registry.register_agent(agent_sql)

        plan = ExecutionPlan(
            plan_id="plan-a18-05",
            task_id="task-a18-05",
            plan_type=PlanType.MULTI_AGENT,
            planning_rationale="Low and medium steps",
            steps=[
                PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, risk_level=RiskLevel.LOW, description="Read docs"),
                PlanStep(step_index=1, agent_id=AgentID.SQL_DATA, risk_level=RiskLevel.MEDIUM, data_class="internal", description="Internal query"),
            ],
        )

        inp = OrchestratorInput(
            task_id="task-a18-05",
            task_description="Run low and med steps",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
        )

        out = await engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        self.assertEqual(agent_rag.calls_count, 1)
        self.assertEqual(agent_sql.calls_count, 1)

    # -------------------------------------------------------------------------
    # A18-06: Human Approval Pause (High Risk)
    # -------------------------------------------------------------------------
    async def test_a18_06_human_approval_pause_with_lease_release(self):
        """A18-06: High-risk step creates ApprovalRequest, releases lease in repository, emits WAITING_APPROVAL, and halts."""
        repo = InMemoryWorkflowRecoveryRepository()
        engine = self._create_engine(workflow_repo=repo)
        agent_finance = StubDomainAgent(AgentID.FINANCE)
        self.registry.register_agent(agent_finance)

        plan = ExecutionPlan(
            plan_id="plan-a18-06",
            task_id="task-a18-06",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="High risk transfer",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.FINANCE,
                    risk_level=RiskLevel.HIGH,
                    operation_type="transfer",
                    data_class="financial",
                    description="Transfer $50,000",
                    inputs={"amount": 50000},
                )
            ],
        )

        wf_id = "wf-a18-06"
        tenant_id = "tenant-a18"
        await repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-01",
            task_id="task-a18-06",
            task_description="Perform transfer",
            execution_plan=plan.model_dump(),
            plan_hash=compute_plan_hash(plan),
        )
        ok, epoch, rev, token = await repo.acquire_execution_lease(wf_id, tenant_id)
        self.assertTrue(ok)

        inp = OrchestratorInput(
            task_id="task-a18-06",
            task_description="Perform transfer",
            workflow_id=wf_id,
            execution_plan=plan,
            user_context={"roles": ["admin"], "tenant_id": tenant_id, "user_id": "usr-01"},
            metadata={"tenant_id": tenant_id, "fencing_epoch": epoch, "lock_token": token, "expected_revision": rev},
        )

        out = await engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.WAITING_APPROVAL)
        self.assertIsNotNone(out.approval_request)
        self.assertEqual(agent_finance.calls_count, 0)

        # Confirm lease released in repository and status is WAITING_APPROVAL
        db_wf = await repo.get_workflow(wf_id, tenant_id)
        self.assertEqual(db_wf.status, WorkflowStatus.WAITING_APPROVAL.value)
        self.assertIsNone(db_wf.lock_token)

    # -------------------------------------------------------------------------
    # A18-07: Approval Resume with Monotonic Epoch
    # -------------------------------------------------------------------------
    async def test_a18_07_approval_resume_with_monotonic_epoch(self):
        """A18-07: Valid ApprovalDecision resumes execution with incremented fencing epoch and completes successfully."""
        repo = InMemoryWorkflowRecoveryRepository()
        engine = self._create_engine(workflow_repo=repo)
        agent_finance = StubDomainAgent(AgentID.FINANCE, result_payload={"tx_id": "tx-12345"})
        self.registry.register_agent(agent_finance)

        plan = ExecutionPlan(
            plan_id="plan-a18-07",
            task_id="task-a18-07",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Approved high risk transfer",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.FINANCE,
                    risk_level=RiskLevel.HIGH,
                    operation_type="transfer",
                    operation="transfer",
                    data_class="financial",
                    description="Transfer $10,000",
                    inputs={"amount": 10000},
                )
            ],
        )

        wf_id = "wf-a18-07"
        tenant_id = "tenant-a18"
        p_hash = compute_plan_hash(plan)
        await repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-01",
            task_id="task-a18-07",
            task_description="Execute transfer",
            execution_plan=plan.model_dump(),
            plan_hash=p_hash,
        )
        ok, epoch_0, rev_0, token_0 = await repo.acquire_execution_lease(wf_id, tenant_id)
        self.assertTrue(ok)

        # Pause workflow for approval
        appr_id = "appr-a18-07"
        rev_1 = await repo.pause_workflow_for_approval(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            approval_id=appr_id,
            requested_action="Execute step 0: Transfer $10,000 via finance",
            risk_level="high",
            details={
                "agent_id": "finance",
                "plan_hash": p_hash,
                "operation": "transfer",
                "step_index": 0,
                "task_id": f"{wf_id}:step-0",
            },
            fencing_epoch=epoch_0,
            lock_token=token_0,
            expected_revision=rev_0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
        )

        # Approver records APPROVED decision in repository
        appr = repo._approvals[appr_id]
        appr.status = ApprovalStatus.APPROVED.value
        appr.decision = ApprovalStatus.APPROVED.value
        appr.approver_id = "admin-approver"
        appr.decided_at = datetime.now(timezone.utc)
        appr.resume_status = "pending"

        # Atomic claim approval resume: bumps fencing epoch and row revision
        claimed, epoch_1, rev_2, token_1, _, reason = await repo.claim_approval_resume(
            approval_id=appr_id,
            workflow_id=wf_id,
            tenant_id=tenant_id,
        )
        self.assertTrue(claimed, f"claim_approval_resume failed: {reason}")
        self.assertEqual(epoch_1, epoch_0 + 1)

        decision = ApprovalDecision(
            approval_id=appr_id,
            workflow_id=wf_id,
            decision=ApprovalStatus.APPROVED,
            approver_id="admin-approver",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )

        inp = OrchestratorInput(
            task_id="task-a18-07",
            task_description="Resume transfer",
            workflow_id=wf_id,
            execution_plan=plan,
            approval_decision=decision,
            user_context={"roles": ["admin"], "tenant_id": tenant_id, "user_id": "usr-01"},
            metadata={
                "tenant_id": tenant_id,
                "fencing_epoch": epoch_1,
                "lock_token": token_1,
                "expected_revision": rev_2,
                "is_first_resume_claim": True,
                "approval_claim_count": 1,
            },
        )

        out = await engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        self.assertEqual(agent_finance.calls_count, 1)

    # -------------------------------------------------------------------------
    # A18-08: Approval Rejection Termination
    # -------------------------------------------------------------------------
    async def test_a18_08_approval_rejection_terminates_workflow(self):
        """A18-08: Explicit approval rejection terminates workflow with HUMAN_APPROVAL_REJECTED without dispatching tool."""
        engine = self._create_engine()
        agent_finance = StubDomainAgent(AgentID.FINANCE)
        self.registry.register_agent(agent_finance)

        plan = ExecutionPlan(
            plan_id="plan-a18-08",
            task_id="task-a18-08",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Transfer rejected by human approver",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.FINANCE,
                    risk_level=RiskLevel.HIGH,
                    operation_type="transfer",
                    data_class="financial",
                    description="Transfer funds",
                )
            ],
        )

        decision = ApprovalDecision(
            approval_id="appr-rej-01",
            workflow_id="wf-a18-08",
            decision=ApprovalStatus.REJECTED,
            approver_id="security_officer",
            reason="Suspected fraudulent destination account",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )

        inp = OrchestratorInput(
            task_id="task-a18-08",
            task_description="Perform transfer",
            workflow_id="wf-a18-08",
            execution_plan=plan,
            approval_decision=decision,
            user_context={"roles": ["admin"]},
            metadata={"is_first_resume_claim": True, "approval_claim_count": 1},
        )

        out = await engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.FAILED)
        self.assertIsNotNone(out.error)
        self.assertEqual(out.error.code, "HUMAN_APPROVAL_REJECTED")
        self.assertEqual(agent_finance.calls_count, 0)

    # -------------------------------------------------------------------------
    # A18-09: Expired Approval Rejection
    # -------------------------------------------------------------------------
    async def test_a18_09_expired_approval_fails_closed_in_graph(self):
        """A18-09: An approval decision referencing an expired approval request fails closed with APPROVAL_EXPIRED."""
        engine = self._create_engine()
        agent_finance = StubDomainAgent(AgentID.FINANCE)
        self.registry.register_agent(agent_finance)

        plan = ExecutionPlan(
            plan_id="plan-a18-09",
            task_id="task-a18-09",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Expired approval test",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.FINANCE,
                    risk_level=RiskLevel.HIGH,
                    operation_type="transfer",
                    data_class="financial",
                    description="Transfer funds",
                )
            ],
        )

        p_hash = compute_plan_hash(plan)
        expired_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        decision = ApprovalDecision(
            approval_id="appr-exp-01",
            workflow_id="wf-a18-09",
            decision=ApprovalStatus.APPROVED,
            approver_id="admin_user",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )

        inp = OrchestratorInput(
            task_id="task-a18-09",
            task_description="Execute with expired approval",
            workflow_id="wf-a18-09",
            execution_plan=plan,
            approval_decision=decision,
            user_context={"roles": ["admin"]},
            metadata={
                "is_first_resume_claim": True,
                "approval_claim_count": 1,
                "interrupted_steps": [0],
            },
        )

        mock_req = ApprovalRequest(
            approval_id="appr-exp-01",
            workflow_id="wf-a18-09",
            step_index=0,
            requested_action="Execute step 0: Transfer funds via finance",
            risk_level=RiskLevel.HIGH,
            status=ApprovalStatus.PENDING,
            requested_at=expired_time,
            details={
                "expires_at": expired_time,
                "agent_id": "finance",
                "step_index": 0,
                "plan_hash": p_hash,
            },
        )

        # Supply approval request with past expiry date via RiskGate.evaluate
        with patch.object(RiskGate, "evaluate", return_value=(RiskGateDecision.PAUSE, mock_req)):
            out = await engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.FAILED)
        self.assertIsNotNone(out.error)
        self.assertEqual(out.error.code, "APPROVAL_EXPIRED")
        self.assertEqual(agent_finance.calls_count, 0)

    # -------------------------------------------------------------------------
    # A18-10: Stale Fencing Lease Rejection
    # -------------------------------------------------------------------------
    async def test_a18_10_stale_fencing_lease_prevents_dispatch(self):
        """A18-10: Stale lock token or missing credentials raises FencingLeaseLostError, preventing split-brain writes."""
        repo = InMemoryWorkflowRecoveryRepository()
        engine = self._create_engine(workflow_repo=repo)
        agent_rag = StubDomainAgent(AgentID.RESEARCH_RAG)
        self.registry.register_agent(agent_rag)

        plan = ExecutionPlan(
            plan_id="plan-a18-10",
            task_id="task-a18-10",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Fencing lease test",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Search data")],
        )

        # Missing lock token in input metadata
        inp_missing_token = OrchestratorInput(
            task_id="task-a18-10",
            task_description="Test stale lease",
            workflow_id="wf-a18-10",
            execution_plan=plan,
            user_context={"roles": ["admin"], "tenant_id": "ten-01", "user_id": "usr-01"},
            metadata={"tenant_id": "ten-01", "fencing_epoch": 1, "expected_revision": 1},  # Missing lock_token
        )

        with self.assertRaises(FencingLeaseLostError):
            await engine.execute_workflow(inp_missing_token)

        self.assertEqual(agent_rag.calls_count, 0)

    # -------------------------------------------------------------------------
    # A18-11: Exponential Backoff Dispatch Retries
    # -------------------------------------------------------------------------
    async def test_a18_11_exponential_backoff_retry_and_recovery(self):
        """A18-11: Transient agent failure triggers exponential backoff retry and recovers on attempt 2 without replanning."""
        engine = self._create_engine(max_retries=2)
        # Fails attempt 1 with TimeoutError, succeeds on attempt 2
        agent_rag = StubDomainAgent(AgentID.RESEARCH_RAG, result_payload={"recovered": True}, fail_attempts=1)
        self.registry.register_agent(agent_rag)

        plan = ExecutionPlan(
            plan_id="plan-a18-11",
            task_id="task-a18-11",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Retry recovery test",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Search data")],
        )

        inp = OrchestratorInput(
            task_id="task-a18-11",
            task_description="Test transient retry",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
        )

        out = await engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        self.assertEqual(agent_rag.calls_count, 2)
        self.assertEqual(out.replan_count, 0)

    # -------------------------------------------------------------------------
    # A18-12: Contextual Re-Planning on Step Failure
    # -------------------------------------------------------------------------
    async def test_a18_12_contextual_replanning_end_to_end(self):
        """A18-12: Permanent step failure triggers PlannerAgent replan, commits whole-plan revision, and completes."""
        repo = InMemoryWorkflowRecoveryRepository()
        engine = self._create_engine(workflow_repo=repo, max_replan_attempts=2)

        agent_rag = StubDomainAgent(AgentID.RESEARCH_RAG, result_payload={"info": "found"})
        agent_fail = StubDomainAgent(AgentID.SQL_DATA, fail_attempts=5)  # Exhausts retries
        agent_backup = StubDomainAgent(AgentID.ANALYTICS, result_payload={"report": "analytics_done"})

        self.registry.register_agent(agent_rag)
        self.registry.register_agent(agent_fail)
        self.registry.register_agent(agent_backup)

        plan_0 = ExecutionPlan(
            plan_id="plan-orig-12",
            task_id="task-a18-12",
            plan_type=PlanType.MULTI_AGENT,
            planning_rationale="Initial plan",
            steps=[
                PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Step 0: Search"),
                PlanStep(step_index=1, agent_id=AgentID.SQL_DATA, description="Step 1: SQL", dependencies=[0]),
            ],
        )

        revised_sub_plan = ExecutionPlan(
            plan_id="sub-replan-12",
            task_id="task-a18-12",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Fallback to analytics",
            steps=[PlanStep(step_index=1, agent_id=AgentID.ANALYTICS, description="Step 1: Analytics", dependencies=[0])],
        )
        planner = StubPlannerAgent(plan_to_return=revised_sub_plan)
        self.registry.register_agent(planner)

        wf_id = "wf-a18-12"
        tenant_id = "tenant-a18"
        p_hash = compute_plan_hash(plan_0)
        await repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-01",
            task_id="task-a18-12",
            task_description="Execute plan",
            execution_plan=plan_0.model_dump(),
            plan_hash=p_hash,
        )
        ok, epoch, rev, token = await repo.acquire_execution_lease(wf_id, tenant_id)
        self.assertTrue(ok)

        inp = OrchestratorInput(
            task_id="task-a18-12",
            task_description="Execute with replan",
            workflow_id=wf_id,
            execution_plan=plan_0,
            user_context={"roles": ["admin"], "tenant_id": tenant_id, "user_id": "usr-01"},
            metadata={"tenant_id": tenant_id, "fencing_epoch": epoch, "lock_token": token, "expected_revision": rev},
        )

        out = await engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        self.assertEqual(planner.calls_count, 1)
        self.assertEqual(out.replan_count, 1)
        self.assertEqual(agent_backup.calls_count, 1)

        # Confirm repository holds revised plan and incremented replan counter
        db_wf = await repo.get_workflow(wf_id, tenant_id)
        self.assertEqual(db_wf.execution_metadata.get("replan_count"), 1)

    # -------------------------------------------------------------------------
    # A18-13: Non-Replannable Failure Halting
    # -------------------------------------------------------------------------
    async def test_a18_13_non_replannable_failure_halts_immediately(self):
        """A18-13: RBAC permission denial fails closed immediately with PLAN_VALIDATION_FAILED and bypasses replanning."""
        engine = self._create_engine()
        agent_rag = StubDomainAgent(AgentID.RESEARCH_RAG)
        self.registry.register_agent(agent_rag)

        plan = ExecutionPlan(
            plan_id="plan-a18-13",
            task_id="task-a18-13",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Restricted access",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Secret query")],
        )

        inp = OrchestratorInput(
            task_id="task-a18-13",
            task_description="Attempt unauthorized action",
            execution_plan=plan,
            user_context={"roles": ["guest"]},  # Guest has no permission for research_rag
        )

        out = await engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.FAILED)
        self.assertIsNotNone(out.error)
        self.assertEqual(out.error.code, "PLAN_VALIDATION_FAILED")
        self.assertEqual(out.replan_count, 0)
        self.assertEqual(agent_rag.calls_count, 0)

    # -------------------------------------------------------------------------
    # A18-14: Reviewer Quality Gate Outcomes
    # -------------------------------------------------------------------------
    async def test_a18_14_reviewer_quality_gate_escalate_and_reject(self):
        """A18-14: Validates ReviewerValidationAgent quality gate outcomes: REJECT fails workflow, ESCALATE pauses."""
        # Case A: Reviewer returns REJECT -> Workflow fails immediately with REVIEWER_REJECTED
        agent_rag = StubDomainAgent(AgentID.RESEARCH_RAG, result_payload={"bad_data": True})
        self.registry.register_agent(agent_rag)

        plan = ExecutionPlan(
            plan_id="plan-a18-14",
            task_id="task-a18-14",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Reviewer gate test",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Search data")],
        )

        inp = OrchestratorInput(
            task_id="task-a18-14",
            task_description="Execute and review",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
        )

        engine_reject = self._create_engine()
        with patch.object(
            engine_reject._graph_nodes.reviewer_engine,
            "evaluate",
            return_value=ReviewerOutput(
                review_id="rev-rej-01",
                task_id="task-a18-14",
                passed=False,
                findings=[ReviewFindingItem(finding_id="f1", type=ReviewFindingType.POLICY, severity=FindingSeverity.ERROR, description="Bad data")],
                risk_assessment=RiskAssessment(risk_level="high", risk_factors=["policy"]),
                recommendation=ReviewRecommendation.REJECT,
                reviewed_at=datetime.now(timezone.utc),
                provenance=ReviewerProvenance(review_id="rev-rej-01", task_id="task-a18-14", risk_level="high", recommendation=ReviewRecommendation.REJECT, reviewed_at=datetime.now(timezone.utc)),
            ),
        ):
            out_reject = await engine_reject.execute_workflow(inp)

        self.assertEqual(out_reject.status, WorkflowStatus.FAILED)
        self.assertIsNotNone(out_reject.error)
        self.assertEqual(out_reject.error.code, "REVIEWER_REJECTED")

        # Case B: Reviewer returns ESCALATE -> Workflow pauses for human approval
        engine_escalate = self._create_engine()
        with patch.object(
            engine_escalate._graph_nodes.reviewer_engine,
            "evaluate",
            return_value=ReviewerOutput(
                review_id="rev-esc-01",
                task_id="task-a18-14",
                passed=False,
                findings=[ReviewFindingItem(finding_id="f2", type=ReviewFindingType.RISK, severity=FindingSeverity.WARNING, description="Needs human review")],
                risk_assessment=RiskAssessment(risk_level="high", risk_factors=["risk"]),
                recommendation=ReviewRecommendation.ESCALATE,
                reviewed_at=datetime.now(timezone.utc),
                provenance=ReviewerProvenance(review_id="rev-esc-01", task_id="task-a18-14", risk_level="high", recommendation=ReviewRecommendation.ESCALATE, reviewed_at=datetime.now(timezone.utc)),
            ),
        ):
            out_escalate = await engine_escalate.execute_workflow(inp)

        self.assertEqual(out_escalate.status, WorkflowStatus.WAITING_APPROVAL)
        self.assertIsNotNone(out_escalate.approval_request)
        self.assertEqual(out_escalate.approval_request.requested_action, "Reviewer Escalation")

    # -------------------------------------------------------------------------
    # A18-15: SSE Event Emission & Identity
    # -------------------------------------------------------------------------
    async def test_a18_15_canonical_sse_event_emission_and_identity(self):
        """A18-15: Emitted SSE events match canonical event_id format, sequence monotonicity, and stage discriminators."""
        mock_bus = AsyncMock(spec=IWorkflowEventBus)
        engine = self._create_engine(event_bus=mock_bus)
        agent_rag = StubDomainAgent(AgentID.RESEARCH_RAG, result_payload={"info": "done"})
        self.registry.register_agent(agent_rag)

        plan = ExecutionPlan(
            plan_id="plan-a18-15",
            task_id="task-a18-15",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="SSE audit test",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Search docs")],
        )

        inp = OrchestratorInput(
            task_id="task-a18-15",
            task_description="Execute and emit events",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
        )

        out = await engine.execute_workflow(inp)
        self.assertEqual(out.status, WorkflowStatus.COMPLETED)

        # Inspect published events
        published_events: List[WorkflowStreamEvent] = [
            call.args[0] for call in mock_bus.publish.call_args_list if call.args and isinstance(call.args[0], WorkflowStreamEvent)
        ]
        self.assertGreaterEqual(len(published_events), 2)

        # Canonical format regex: evt:v1:{wf}:e{epoch}:s{step}:{type}:sq{seq}:{disc}
        event_regex = re.compile(r"^evt:v1:[^:]+:e\d+:s-?\d+:[a-zA-Z0-9_]+:sq\d+:[a-z0-9_-]+$")

        seen_ids = set()
        last_seq = 0
        for evt in published_events:
            self.assertTrue(event_regex.match(evt.event_id), f"Event ID '{evt.event_id}' does not match canonical format.")
            self.assertNotIn(evt.event_id, seen_ids, f"Duplicate event ID detected: '{evt.event_id}'.")
            seen_ids.add(evt.event_id)
            self.assertGreater(evt.seq, last_seq, f"Sequence number {evt.seq} is not strictly monotonically increasing.")
            last_seq = evt.seq

    # -------------------------------------------------------------------------
    # A18-16: Zero Dual-Mastery / Process Crash Recovery
    # -------------------------------------------------------------------------
    async def test_a18_16_process_crash_recovery_read_only_vs_mutating(self):
        """A18-16: Process crash recovery allows safe replay for read-only tools, but fails closed for ambiguous mutating steps."""
        # Case A: Read-Only tool crash recovery allows safe replay
        engine_a = self._create_engine()
        agent_read = StubDomainAgent(AgentID.RESEARCH_RAG, result_payload={"rag_data": "fresh"})
        self.registry.register_agent(agent_read)

        plan_read = ExecutionPlan(
            plan_id="plan-a18-16-read",
            task_id="task-a18-16-a",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Read-only recovery",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.RESEARCH_RAG,
                    operation_type="read",
                    operation="rag_search_tool",
                    description="Read query",
                    inputs={"tool": "rag_search_tool"},
                )
            ],
        )

        inp_read = OrchestratorInput(
            task_id="task-a18-16-a",
            task_description="Recover read step",
            execution_plan=plan_read,
            user_context={"roles": ["admin"]},
            metadata={"interrupted_steps": [0]},
        )

        out_read = await engine_a.execute_workflow(inp_read)
        self.assertEqual(out_read.status, WorkflowStatus.COMPLETED)
        self.assertEqual(agent_read.calls_count, 1)

        # Case B: Mutating non-idempotent tool crash recovery fails closed per replay safety
        engine_b = self._create_engine()
        agent_write = StubDomainAgent(AgentID.FINANCE)
        self.registry.register_agent(agent_write)

        plan_write = ExecutionPlan(
            plan_id="plan-a18-16-write",
            task_id="task-a18-16-b",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Mutating recovery",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.FINANCE,
                    risk_level=RiskLevel.HIGH,
                    operation_type="write",
                    data_class="financial",
                    description="Transfer funds",
                    inputs={"amount": 50000},
                )
            ],
        )

        decision = ApprovalDecision(
            approval_id="appr-crashed-16",
            workflow_id="wf-a18-16-b",
            decision=ApprovalStatus.APPROVED,
            approver_id="admin_user",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )

        # Recovery claim of an already-claimed approval after crash
        inp_write = OrchestratorInput(
            task_id="task-a18-16-b",
            task_description="Recover crashed mutating step",
            workflow_id="wf-a18-16-b",
            execution_plan=plan_write,
            approval_decision=decision,
            user_context={"roles": ["admin"], "tenant_id": "ten-01", "user_id": "usr-01"},
            metadata={
                "tenant_id": "ten-01",
                "is_first_resume_claim": False,
                "approval_claim_count": 2,
            },
        )

        out_write = await engine_b.execute_workflow(inp_write)

        # Must fail closed per replay safety: ambiguous mutating side effects cannot be replayed
        self.assertEqual(out_write.status, WorkflowStatus.FAILED)
        self.assertIsNotNone(out_write.error)
        self.assertEqual(out_write.error.code, "AMBIGUOUS_SIDE_EFFECT")
        self.assertEqual(agent_write.calls_count, 0)


if __name__ == "__main__":
    unittest.main()
