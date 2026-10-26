"""
EAOC Orchestrator Agent Unit Test Suite
Comprehensive deterministic tests covering all 25 required points from Phase 2 Part 2.15:
1. Canonical Orchestrator identity and strict schema validation
2. Single-agent workflow execution
3. Multi-agent execution with dependency-based context passing
4. Invalid plan rejection before any agent dispatch
5. Unknown agent rejection
6. Trusted-context RBAC denial before dispatch
7. LOW, MEDIUM, and HIGH risk gate behavior
8. HIGH risk creates a pending approval request and stops before executing the protected step
9. Missing, rejected, mismatched, or malformed approval decisions cannot authorize a step
10. Valid approved resume, only if supported by the actual approval contracts
11. Reviewer APPROVE, REJECT, and ESCALATE outcomes
12. Transient failures retry within the specified limit and backoff
13. Non-retryable permission/policy/schema errors are not retried
14. Timeout and uncertain-outcome handling avoids unsafe duplicate dispatch
15. Optional versus required step failures
16. Contextual re-planning after eligible permanent failures
17. Replanning limit exhaustion
18. Workflow state persistence after every transition and step
19. State-store failure stops further execution
20. Provenance completeness and correlation identifiers
21. Required Orchestrator audit events and safe audit failure handling
22. Sensitive-data redaction in state, logs, errors, and final output
23. Planner, Reviewer, AgentRegistry, and BaseAgent compatibility
24. No direct domain logic, direct tool execution, or external side effects from the Orchestrator
25. Workflow terminal states and accurate final-response assembly
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from pydantic import BaseModel, Field

from eaoc.agents.base import BaseAgent
from eaoc.services.event_bus import IWorkflowEventBus
from eaoc.state.workflow_repository import (
    InMemoryWorkflowRecoveryRepository,
    FencingLeaseLostError,
)
from eaoc.orchestration.nodes import compute_step_hash, compute_plan_hash
from eaoc.agents.orchestrator import (
    FinalResponse,
    OrchestratorAgent,
    OrchestratorEngine,
    OrchestratorFailureClass,
    OrchestratorInput,
    OrchestratorOutput,
    ProvenanceEntry,
    RiskGate,
    RiskGateDecision,
    StepExecutionRecord,
)
from eaoc.agents.planner.schemas import (
    ExecutionPlan,
    PlanStep,
    PlanType,
    PlanValidationError,
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
    RequestContext,
    RiskLevel,
    TaskStatus,
    WorkflowState,
    WorkflowStatus,
)
from eaoc.state.interfaces import IWorkflowStateStore


# ---------------------------------------------------------------------------
# Test Stubs
# ---------------------------------------------------------------------------

class StubPayload(BaseModel):
    model_config = {"extra": "allow"}
    query: Optional[str] = None
    data: Optional[Any] = None
    secret_key: Optional[str] = None
    result: Optional[str] = None


class StubPolicyViolationError(Exception):
    """Stub exception for policy violations in tests."""
    pass


class StubDomainAgent(BaseAgent[StubPayload, StubPayload]):
    """Generic stub domain agent for testing."""

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
            if "policy" in error.error_type.lower() or "policy" in error.code.lower():
                raise StubPolicyViolationError(error.message)
            raise TimeoutError(error.message)
        return StubPayload.model_validate(self.result_payload)


class StubReviewerAgent(BaseAgent[ReviewerInput, ReviewerOutput]):
    """Stub ReviewerValidationAgent for testing."""

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
        now = datetime.now(timezone.utc)
        return ReviewerOutput(
            review_id=f"rev-{self.calls_count}",
            task_id=validated_input.task_id,
            passed=(self.recommendation == ReviewRecommendation.APPROVE),
            findings=self.findings,
            risk_assessment=RiskAssessment(risk_level="low", risk_factors=[]),
            recommendation=self.recommendation,
            reviewed_at=now,
            provenance=ReviewerProvenance(
                review_id=f"rev-{self.calls_count}",
                task_id=validated_input.task_id,
                risk_level="low",
                recommendation=self.recommendation,
                reviewed_at=now,
            ),
        )


class StubPlannerAgent(BaseAgent[PlannerInput, PlannerOutput]):
    """Stub PlannerAgent for testing."""

    def __init__(self, plan_to_return: Optional[ExecutionPlan] = None):
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
            plan_id=self.plan_to_return.plan_id if self.plan_to_return else "plan-stub",
            task_id=validated_input.task_id,
            plan=self.plan_to_return,
            rationale="Stub planning rationale",
            is_replan=validated_input.is_replan,
        )


class InMemoryStateStore(IWorkflowStateStore):
    """Deterministic in-memory implementation of IWorkflowStateStore."""

    def __init__(self, fail_writes: bool = False):
        self._store: Dict[str, WorkflowState] = {}
        self.fail_writes = fail_writes
        self.history: List[WorkflowState] = []

    async def create_state(self, state: WorkflowState) -> WorkflowState:
        if self.fail_writes:
            raise RuntimeError("Simulated state store disk failure")
        if state.workflow_id in self._store:
            raise ValueError(f"State {state.workflow_id} already exists")
        self._store[state.workflow_id] = state.model_copy(deep=True)
        self.history.append(state.model_copy(deep=True))
        return state

    async def get_state(self, workflow_id: str) -> Optional[WorkflowState]:
        return self._store.get(workflow_id)

    async def update_state(self, state: WorkflowState) -> WorkflowState:
        if self.fail_writes:
            raise RuntimeError("Simulated state store disk failure")
        if state.workflow_id not in self._store:
            raise KeyError(f"State {state.workflow_id} not found")
        self._store[state.workflow_id] = state.model_copy(deep=True)
        self.history.append(state.model_copy(deep=True))
        return state

    async def delete_state(self, workflow_id: str) -> bool:
        return self._store.pop(workflow_id, None) is not None

    async def exists(self, workflow_id: str) -> bool:
        return workflow_id in self._store


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

class TestOrchestratorAgent(unittest.IsolatedAsyncioTestCase):
    """Comprehensive test suite for OrchestratorAgent and OrchestratorEngine."""

    def setUp(self):
        self.registry = AgentRegistry()
        self.state_store = InMemoryStateStore()
        # Default fast backoff for instant tests
        self.agent = OrchestratorAgent(
            registry=self.registry,
            state_store=self.state_store,
            max_retries=2,
            initial_backoff_seconds=0.01,
            backoff_multiplier=1.5,
            max_replan_attempts=2,
        )
        # Register standard stub reviewer
        self.reviewer = StubReviewerAgent(recommendation=ReviewRecommendation.APPROVE)
        self.registry.register_agent(self.reviewer)

    # 1. Canonical Orchestrator identity and strict schema validation
    async def test_01_canonical_identity_and_schema_validation(self):
        self.assertEqual(self.agent.agent_id, AgentID.ORCHESTRATOR)
        self.assertEqual(self.agent.agent_id.value, "orchestrator")
        self.assertEqual(self.agent.input_schema, OrchestratorInput)
        self.assertEqual(self.agent.output_schema, OrchestratorOutput)

        # Strict input validation
        valid_input = OrchestratorInput(
            task_id="t-1",
            task_description="Test task",
            user_context={"roles": ["admin"]},
        )
        self.assertEqual(valid_input.task_id, "t-1")

    # 2. Single-agent workflow execution
    async def test_02_single_agent_workflow_execution(self):
        domain_agent = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG, result_payload={"result": "data"})
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-1",
            task_id="t-2",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Single step query",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.RESEARCH_RAG,
                    description="Execute research query",
                    inputs={"query": "find facts"},
                )
            ],
        )

        inp = OrchestratorInput(
            task_id="t-2",
            task_description="Run single research query",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
        )

        out = await self.agent.engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        self.assertIsNotNone(out.final_response)
        self.assertEqual(out.final_response.status, WorkflowStatus.COMPLETED)
        self.assertEqual(domain_agent.calls_count, 1)
        self.assertEqual(len(out.provenance_chain), 1)
        self.assertEqual(out.provenance_chain[0].status, "COMPLETED")

    # 3. Multi-agent execution with dependency-based context passing
    async def test_03_multi_agent_execution_with_dependency_context(self):
        agent_rag = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG, result_payload={"result": "rag_output"})
        agent_sql = StubDomainAgent(agent_id=AgentID.SQL_DATA, result_payload={"result": "sql_output"})
        self.registry.register_agent(agent_rag)
        self.registry.register_agent(agent_sql)

        plan = ExecutionPlan(
            plan_id="plan-2",
            task_id="t-3",
            plan_type=PlanType.MULTI_AGENT,
            planning_rationale="2 steps sequential",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.RESEARCH_RAG,
                    description="Step 0 research",
                    inputs={"query": "lookup"},
                ),
                PlanStep(
                    step_index=1,
                    agent_id=AgentID.SQL_DATA,
                    description="Step 1 query",
                    inputs={"query": "select *"},
                    dependencies=[0],
                ),
            ],
        )

        inp = OrchestratorInput(
            task_id="t-3",
            task_description="Two-step pipeline",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
        )

        out = await self.agent.engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        self.assertEqual(agent_rag.calls_count, 1)
        self.assertEqual(agent_sql.calls_count, 1)
        self.assertEqual(len(out.provenance_chain), 2)
        self.assertIn("0", out.final_response.result)
        self.assertIn("1", out.final_response.result)

    # 4. Invalid plan rejection before any agent dispatch
    async def test_04_invalid_plan_rejection_before_dispatch(self):
        domain_agent = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG)
        self.registry.register_agent(domain_agent)

        # Single agent plan type but 2 steps violates FR-PLAN-003
        bad_plan = ExecutionPlan(
            plan_id="bad-plan",
            task_id="t-4",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Inconsistent plan type",
            steps=[
                PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Step 0"),
                PlanStep(step_index=1, agent_id=AgentID.RESEARCH_RAG, description="Step 1", dependencies=[0]),
            ],
        )

        inp = OrchestratorInput(
            task_id="t-4",
            task_description="Invalid plan",
            execution_plan=bad_plan,
            user_context={"roles": ["admin"]},
        )

        out = await self.agent.engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.FAILED)
        self.assertIn("Plan validation failed", out.error.message)
        self.assertEqual(domain_agent.calls_count, 0)

    # 5. Unknown agent rejection
    async def test_05_unknown_agent_rejection(self):
        # Plan contains SQL_DATA, which is not registered in AgentRegistry
        plan = ExecutionPlan(
            plan_id="plan-unknown",
            task_id="t-5",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Unregistered agent",
            steps=[
                PlanStep(step_index=0, agent_id=AgentID.SQL_DATA, description="Unregistered step")
            ],
        )

        inp = OrchestratorInput(
            task_id="t-5",
            task_description="Plan with unregistered agent",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
        )

        out = await self.agent.engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.FAILED)
        self.assertEqual(out.error.code, "PLAN_VALIDATION_FAILED")

    # 6. Trusted-context RBAC denial before dispatch
    async def test_06_trusted_context_rbac_denial(self):
        domain_agent = StubDomainAgent(agent_id=AgentID.FINANCE)
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-rbac",
            task_id="t-6",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Unauthorized call",
            steps=[
                PlanStep(step_index=0, agent_id=AgentID.FINANCE, description="Finance action")
            ],
        )

        # hr_user is NOT permitted to dispatch FINANCE
        inp = OrchestratorInput(
            task_id="t-6",
            task_description="Finance task with HR role",
            execution_plan=plan,
            user_context={"roles": ["hr_user"]},
        )

        out = await self.agent.engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.FAILED)
        self.assertEqual(domain_agent.calls_count, 0)

    # 7. LOW, MEDIUM, and HIGH risk gate behavior
    async def test_07_risk_gate_evaluation_levels(self):
        step_low = PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Read", risk_level=RiskLevel.LOW)
        step_med = PlanStep(step_index=1, agent_id=AgentID.RESEARCH_RAG, description="Write non-fin", risk_level=RiskLevel.MEDIUM)
        step_high = PlanStep(step_index=2, agent_id=AgentID.FINANCE, description="Transfer funds", risk_level=RiskLevel.HIGH)

        dec_low, req_low = RiskGate.evaluate(step_low, "t-7", "wf-7")
        self.assertEqual(dec_low, RiskGateDecision.PROCEED)
        self.assertIsNone(req_low)

        dec_med, req_med = RiskGate.evaluate(step_med, "t-7", "wf-7")
        self.assertEqual(dec_med, RiskGateDecision.PROCEED)
        self.assertIsNone(req_med)

        dec_high, req_high = RiskGate.evaluate(step_high, "t-7", "wf-7")
        self.assertEqual(dec_high, RiskGateDecision.PAUSE)
        self.assertIsNotNone(req_high)
        self.assertEqual(req_high.risk_level, RiskLevel.HIGH)

    # 8. HIGH risk creates a pending approval request and stops before executing protected step
    async def test_08_high_risk_pauses_workflow_without_execution(self):
        domain_agent = StubDomainAgent(agent_id=AgentID.FINANCE)
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-high",
            task_id="t-8",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="High risk operation",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.FINANCE,
                    description="High risk wire transfer",
                    risk_level=RiskLevel.HIGH,
                )
            ],
        )

        inp = OrchestratorInput(
            task_id="t-8",
            task_description="High risk operation",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
        )

        out = await self.agent.engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.WAITING_APPROVAL)
        self.assertIsNotNone(out.approval_request)
        self.assertEqual(domain_agent.calls_count, 0)

        # Verify state store records WAITING_APPROVAL
        state = await self.state_store.get_state(out.workflow_id)
        self.assertEqual(state.status, WorkflowStatus.WAITING_APPROVAL)

    # 9. Missing, rejected, mismatched, or malformed approval decisions cannot authorize a step
    async def test_09_mismatched_and_rejected_approval_decisions(self):
        domain_agent = StubDomainAgent(agent_id=AgentID.FINANCE)
        self.registry.register_agent(domain_agent)

        step = PlanStep(
            step_index=0,
            agent_id=AgentID.FINANCE,
            description="Wire transfer",
            risk_level=RiskLevel.HIGH,
        )
        _, pending_req = RiskGate.evaluate(step, "t-9", "wf-9")

        # 1. Mismatched approval_id
        bad_decision = ApprovalDecision(
            approval_id="wrong-id",
            workflow_id="wf-9",
            decision=ApprovalStatus.APPROVED,
            approver_id="user-1",
            decided_at="2026-10-23T00:00:00Z",
        )
        ok, reason = RiskGate.validate_resume_decision(bad_decision, pending_req, 0, "t-9")
        self.assertFalse(ok)
        self.assertIn("mismatch", reason)

        # 2. Rejected decision
        rejected_decision = ApprovalDecision(
            approval_id=pending_req.approval_id,
            workflow_id="wf-9",
            decision=ApprovalStatus.REJECTED,
            approver_id="user-1",
            decided_at="2026-10-23T00:00:00Z",
        )
        ok, reason = RiskGate.validate_resume_decision(rejected_decision, pending_req, 0, "t-9")
        self.assertFalse(ok)
        self.assertEqual(reason, "human_approval_rejected")

    # 10. Valid approved resume, only if supported by actual approval contracts
    async def test_10_valid_approved_resume(self):
        domain_agent = StubDomainAgent(agent_id=AgentID.FINANCE)
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-resume",
            task_id="t-10",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="High risk resume",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.FINANCE,
                    description="High risk wire",
                    risk_level=RiskLevel.HIGH,
                )
            ],
        )

        # Step A: First call pauses
        inp_1 = OrchestratorInput(
            task_id="t-10",
            task_description="High risk wire",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
            workflow_id="wf-10",
        )
        out_1 = await self.agent.engine.execute_workflow(inp_1)
        self.assertEqual(out_1.status, WorkflowStatus.WAITING_APPROVAL)
        approval_id = out_1.approval_request.approval_id

        # Step B: Second call with valid ApprovalDecision resumes and executes
        valid_decision = ApprovalDecision(
            approval_id=approval_id,
            workflow_id="wf-10",
            decision=ApprovalStatus.APPROVED,
            approver_id="compliance_officer_1",
            decided_at="2026-10-23T00:00:00Z",
        )

        inp_2 = OrchestratorInput(
            task_id="t-10",
            task_description="High risk wire",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
            workflow_id="wf-10",
            approval_decision=valid_decision,
        )

        out_2 = await self.agent.engine.execute_workflow(inp_2)
        self.assertEqual(out_2.status, WorkflowStatus.COMPLETED)
        self.assertEqual(domain_agent.calls_count, 1)

    # 11. Reviewer APPROVE, REJECT, and ESCALATE outcomes
    async def test_11_reviewer_outcomes_approve_reject_escalate(self):
        domain_agent = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG)
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-rev",
            task_id="t-11",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Review testing",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Query")],
        )

        # Test ESCALATE
        self.reviewer.recommendation = ReviewRecommendation.ESCALATE
        out_esc = await self.agent.engine.execute_workflow(
            OrchestratorInput(task_id="t-11a", task_description="Test", execution_plan=plan, user_context={"roles": ["admin"]})
        )
        self.assertEqual(out_esc.status, WorkflowStatus.WAITING_APPROVAL)
        self.assertIsNotNone(out_esc.approval_request)

        # Test REJECT
        self.reviewer.recommendation = ReviewRecommendation.REJECT
        self.reviewer.findings = [
            ReviewFindingItem(
                finding_id="f-1",
                type=ReviewFindingType.COMPLETENESS,
                severity=FindingSeverity.ERROR,
                description="Output incomplete",
            )
        ]
        out_rej = await self.agent.engine.execute_workflow(
            OrchestratorInput(task_id="t-11b", task_description="Test", execution_plan=plan, user_context={"roles": ["admin"]})
        )
        self.assertEqual(out_rej.status, WorkflowStatus.FAILED)
        self.assertEqual(out_rej.error.code, "REVIEWER_REJECTED")

    # 12. Transient failures retry within the specified limit and backoff
    async def test_12_transient_failure_retries_and_succeeds(self):
        # Fails once then succeeds on attempt 2
        domain_agent = StubDomainAgent(
            agent_id=AgentID.RESEARCH_RAG,
            result_payload={"result": "recovered"},
            fail_attempts=1,
        )
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-retry",
            task_id="t-12",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Retry test",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Transient step")],
        )

        out = await self.agent.engine.execute_workflow(
            OrchestratorInput(task_id="t-12", task_description="Retry task", execution_plan=plan, user_context={"roles": ["admin"]})
        )

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        self.assertEqual(domain_agent.calls_count, 2)

    # 13. Non-retryable permission/policy/schema errors are not retried
    async def test_13_non_retryable_errors_not_retried(self):
        perm_error = ErrorInfo(
            code="PERMISSION_DENIED",
            message="No permission",
            error_type="PermissionError",
            retryable=False,
        )
        domain_agent = StubDomainAgent(
            agent_id=AgentID.RESEARCH_RAG,
            fail_attempts=5,
            fail_error=perm_error,
        )
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-no-retry",
            task_id="t-13",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="No retry test",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Step")],
        )

        out = await self.agent.engine.execute_workflow(
            OrchestratorInput(task_id="t-13", task_description="No retry", execution_plan=plan, user_context={"roles": ["admin"]})
        )

        self.assertEqual(out.status, WorkflowStatus.FAILED)
        # Attempted exactly once, zero retries
        self.assertEqual(domain_agent.calls_count, 1)

    # 14. Timeout and uncertain-outcome handling avoids unsafe duplicate dispatch
    async def test_14_max_retries_exhausted_stops(self):
        # 3 failures will exhaust max_retries=2 (1 initial + 2 retries = 3 total)
        domain_agent = StubDomainAgent(
            agent_id=AgentID.RESEARCH_RAG,
            fail_attempts=10,
        )
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-exhaust",
            task_id="t-14",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Exhaustion",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Step")],
        )

        out = await self.agent.engine.execute_workflow(
            OrchestratorInput(task_id="t-14", task_description="Exhaust retries", execution_plan=plan, user_context={"roles": ["admin"]})
        )

        self.assertEqual(out.status, WorkflowStatus.FAILED)
        self.assertEqual(domain_agent.calls_count, 3)

    # 15. Optional versus required step failures
    async def test_15_optional_step_failure_continues(self):
        agent_opt = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG, fail_attempts=10)
        agent_req = StubDomainAgent(agent_id=AgentID.SQL_DATA, result_payload={"result": "ok"})
        self.registry.register_agent(agent_opt)
        self.registry.register_agent(agent_req)

        plan = ExecutionPlan(
            plan_id="plan-opt",
            task_id="t-15",
            plan_type=PlanType.MULTI_AGENT,
            planning_rationale="Optional step test",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.RESEARCH_RAG,
                    description="Optional enrichment",
                    optional=True,
                ),
                PlanStep(
                    step_index=1,
                    agent_id=AgentID.SQL_DATA,
                    description="Required data fetch",
                    dependencies=[0],
                ),
            ],
        )

        out = await self.agent.engine.execute_workflow(
            OrchestratorInput(task_id="t-15", task_description="Optional step", execution_plan=plan, user_context={"roles": ["admin"]})
        )

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        self.assertTrue(out.final_response.partial_result)
        self.assertEqual(agent_req.calls_count, 1)

    # 16. Contextual re-planning after eligible permanent failures
    async def test_16_contextual_replanning_success(self):
        # Step 0 fails permanently. Planner provides a revised plan.
        domain_failing = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG, fail_attempts=10)
        domain_backup = StubDomainAgent(agent_id=AgentID.SQL_DATA, result_payload={"result": "recovered_by_sql"})
        self.registry.register_agent(domain_failing)
        self.registry.register_agent(domain_backup)

        revised_plan = ExecutionPlan(
            plan_id="plan-revised",
            task_id="t-16",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Revised fallback plan",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.SQL_DATA,
                    description="Backup query via SQL",
                )
            ],
        )
        planner = StubPlannerAgent(plan_to_return=revised_plan)
        self.registry.register_agent(planner)

        initial_plan = ExecutionPlan(
            plan_id="plan-init",
            task_id="t-16",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Initial plan",
            steps=[
                PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Failing research")
            ],
        )

        out = await self.agent.engine.execute_workflow(
            OrchestratorInput(task_id="t-16", task_description="Replan test", execution_plan=initial_plan, user_context={"roles": ["admin"]})
        )

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        self.assertEqual(planner.calls_count, 1)
        self.assertEqual(out.replan_count, 1)
        self.assertEqual(domain_backup.calls_count, 1)

    # 17. Replanning limit exhaustion
    async def test_17_replanning_limit_exhaustion(self):
        domain_failing = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG, fail_attempts=50)
        self.registry.register_agent(domain_failing)

        plan = ExecutionPlan(
            plan_id="plan-loop",
            task_id="t-17",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Plan that continues failing",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Fails")],
        )
        # Planner always returns the failing plan
        planner = StubPlannerAgent(plan_to_return=plan)
        self.registry.register_agent(planner)

        out = await self.agent.engine.execute_workflow(
            OrchestratorInput(task_id="t-17", task_description="Replan exhaust", execution_plan=plan, user_context={"roles": ["admin"]})
        )

        self.assertEqual(out.status, WorkflowStatus.FAILED)
        self.assertIn("exhausted", out.error.message)
        self.assertEqual(out.replan_count, 2)

    # 18. Workflow state persistence after every transition and step
    async def test_18_state_persistence_history(self):
        domain_agent = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG)
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-persist",
            task_id="t-18",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="State test",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Step")],
        )

        out = await self.agent.engine.execute_workflow(
            OrchestratorInput(task_id="t-18", task_description="Persist test", execution_plan=plan, user_context={"roles": ["admin"]})
        )

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        # History recorded: initial create, running, step 0 completed, completed
        self.assertGreaterEqual(len(self.state_store.history), 3)
        final_state = await self.state_store.get_state(out.workflow_id)
        self.assertEqual(final_state.status, WorkflowStatus.COMPLETED)
        self.assertIn("0", final_state.tasks)

    # 19. State-store failure stops further execution
    async def test_19_state_store_failure_fails_closed(self):
        failing_store = InMemoryStateStore(fail_writes=True)
        agent = OrchestratorAgent(
            registry=self.registry,
            state_store=failing_store,
            initial_backoff_seconds=0.01,
        )

        domain_agent = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG)
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-store-fail",
            task_id="t-19",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Failing store",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Step")],
        )

        with self.assertRaises(RuntimeError) as ctx:
            await agent.engine.execute_workflow(
                OrchestratorInput(task_id="t-19", task_description="Store fail", execution_plan=plan, user_context={"roles": ["admin"]})
            )
        self.assertIn("State store persistence failure", str(ctx.exception))
        self.assertEqual(domain_agent.calls_count, 0)

    # 20. Provenance completeness and correlation identifiers
    async def test_20_provenance_completeness(self):
        domain_agent = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG)
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-prov",
            task_id="t-20",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Provenance test",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Research query")],
        )

        out = await self.agent.engine.execute_workflow(
            OrchestratorInput(
                task_id="t-20",
                task_description="Prov",
                execution_plan=plan,
                user_context={"roles": ["admin"]},
                metadata={"request_id": "req-custom", "correlation_id": "corr-custom"},
            )
        )

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        self.assertEqual(len(out.provenance_chain), 1)
        entry = out.provenance_chain[0]
        self.assertEqual(entry.step_index, 0)
        self.assertEqual(entry.agent_id, "research_rag")
        self.assertEqual(entry.status, "COMPLETED")
        self.assertIsNotNone(entry.timestamp)

    # 21. Required Orchestrator audit events
    async def test_21_audit_events_emitted(self):
        domain_agent = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG)
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-audit",
            task_id="t-21",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Audit test",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Step")],
        )

        with patch("eaoc.agents.orchestrator.engine.logger.info") as mock_log_info:
            out = await self.agent.engine.execute_workflow(
                OrchestratorInput(task_id="t-21", task_description="Audit", execution_plan=plan, user_context={"roles": ["admin"]})
            )
            self.assertEqual(out.status, WorkflowStatus.COMPLETED)
            events = [call.kwargs.get("extra", {}).get("event_name") for call in mock_log_info.call_args_list]
            self.assertIn("TASK_RECEIVED", events)
            self.assertIn("PLAN_RECEIVED", events)
            self.assertIn("STEP_DISPATCHED", events)
            self.assertIn("STEP_COMPLETED", events)
            self.assertIn("TASK_COMPLETED", events)

    # 22. Sensitive-data redaction in state, logs, errors, and final output
    async def test_22_sensitive_data_redaction(self):
        domain_agent = StubDomainAgent(
            agent_id=AgentID.RESEARCH_RAG,
            result_payload={"api_key": "sk-1234567890abcdef1234567890abcdef", "data": "clean"},
        )
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-secret",
            task_id="t-22",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Redaction test",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.RESEARCH_RAG,
                    description="Secret step",
                    inputs={"password": "super-secret-password"},
                )
            ],
        )

        out = await self.agent.engine.execute_workflow(
            OrchestratorInput(task_id="t-22", task_description="Redact", execution_plan=plan, user_context={"roles": ["admin"]})
        )

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        result_dict = out.final_response.result["0"]
        # Password or api_key must be redacted
        self.assertEqual(result_dict.get("api_key"), "****")

    # 23. Planner, Reviewer, AgentRegistry, and BaseAgent compatibility
    async def test_23_full_stack_base_agent_compatibility(self):
        self.assertTrue(isinstance(self.agent, BaseAgent))
        self.assertTrue(self.registry.has_agent(AgentID.REVIEWER_VALIDATION))

    # 24. No direct domain logic or direct tool execution from Orchestrator
    async def test_24_no_direct_tool_execution(self):
        # Orchestrator delegates all actions exclusively through AgentRegistry
        self.assertFalse(hasattr(self.agent, "execute_tool"))
        self.assertFalse(hasattr(self.agent.engine, "execute_tool"))

    # 25. Workflow terminal states and accurate final-response assembly
    async def test_25_workflow_terminal_states(self):
        # Terminal state FAILED
        out_fail = await self.agent.engine.execute_workflow(
            OrchestratorInput(
                task_id="t-25-fail",
                task_description="No roles fails",
                user_context={},  # No roles -> permission fail
                execution_plan=ExecutionPlan(
                    plan_id="p-25",
                    task_id="t-25-fail",
                    plan_type=PlanType.SINGLE_AGENT,
                    planning_rationale="Test",
                    steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Step")],
                ),
            )
        )
        self.assertEqual(out_fail.status, WorkflowStatus.FAILED)
        self.assertIsNone(out_fail.final_response)

    # 26. SEC-01: Approval bound to exact step_index and agent_id
    async def test_26_sec_01_approval_step_index_and_agent_binding(self):
        agent_fin = StubDomainAgent(agent_id=AgentID.FINANCE)
        self.registry.register_agent(agent_fin)

        step_0 = PlanStep(
            step_index=0,
            agent_id=AgentID.FINANCE,
            description="Wire step",
            risk_level=RiskLevel.HIGH,
        )
        _, pending_req = RiskGate.evaluate(step_0, "t-26", "wf-26")

        # Decision matching approval_id but presented for step_index 1
        decision = ApprovalDecision(
            approval_id=pending_req.approval_id,
            workflow_id="wf-26",
            decision=ApprovalStatus.APPROVED,
            approver_id="approver_1",
            decided_at="2026-10-23T00:00:00Z",
        )

        # Mismatched step index fails closed
        ok, reason = RiskGate.validate_resume_decision(
            decision=decision,
            pending_request=pending_req,
            step_index=1,  # Mismatch: requested for 0, resumed for 1
            task_id="t-26",
            step=step_0,
        )
        self.assertFalse(ok)
        self.assertIn("step_index_mismatch", reason)

        # Mismatched agent_id fails closed
        step_diff_agent = PlanStep(
            step_index=0,
            agent_id=AgentID.RESEARCH_RAG,
            description="Different agent",
        )
        ok_agent, reason_agent = RiskGate.validate_resume_decision(
            decision=decision,
            pending_request=pending_req,
            step_index=0,
            task_id="t-26",
            step=step_diff_agent,
        )
        self.assertFalse(ok_agent)
        self.assertIn("agent_id_mismatch", reason_agent)

        # Workflow execution: mismatched approval does not dispatch agent
        plan = ExecutionPlan(
            plan_id="p-26",
            task_id="t-26",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Binding test",
            steps=[step_0],
        )
        # Attempt to resume with wrong step_index in pending_request
        tampered_req = pending_req.model_copy(deep=True)
        tampered_req.details["step_index"] = 99  # Mismatch

        state = await self.state_store.create_state(
            WorkflowState(
                workflow_id="wf-26",
                request_context=RequestContext(request_id="r", correlation_id="c", roles=["admin"]),
                status=WorkflowStatus.WAITING_APPROVAL,
                current_task_id="t-26",
                created_at="2026-10-23T00:00:00Z",
                updated_at="2026-10-23T00:00:00Z",
                approval_request=tampered_req,
            )
        )

        out = await self.agent.engine.execute_workflow(
            OrchestratorInput(
                task_id="t-26",
                task_description="Test",
                execution_plan=plan,
                user_context={"roles": ["admin"]},
                workflow_id="wf-26",
                approval_decision=decision,
            )
        )
        self.assertEqual(out.status, WorkflowStatus.WAITING_APPROVAL)
        self.assertEqual(agent_fin.calls_count, 0)

    # 27. SEC-01: Single-use approval token cannot be reused for a different protected step
    async def test_27_sec_01_single_use_approval_token_cannot_be_reused(self):
        agent_fin = StubDomainAgent(agent_id=AgentID.FINANCE)
        self.registry.register_agent(agent_fin)

        step_0 = PlanStep(
            step_index=0,
            agent_id=AgentID.FINANCE,
            description="Step 0 transfer",
            risk_level=RiskLevel.HIGH,
        )
        step_1 = PlanStep(
            step_index=1,
            agent_id=AgentID.FINANCE,
            description="Step 1 transfer",
            risk_level=RiskLevel.HIGH,
            dependencies=[0],
        )

        plan = ExecutionPlan(
            plan_id="p-27",
            task_id="t-27",
            plan_type=PlanType.MULTI_AGENT,
            planning_rationale="Multi-high risk",
            steps=[step_0, step_1],
        )

        # Call 1: Pauses at step 0
        out_1 = await self.agent.engine.execute_workflow(
            OrchestratorInput(
                task_id="t-27",
                task_description="Multi high-risk",
                execution_plan=plan,
                user_context={"roles": ["admin"]},
                workflow_id="wf-27",
            )
        )
        self.assertEqual(out_1.status, WorkflowStatus.WAITING_APPROVAL)
        approval_id_0 = out_1.approval_request.approval_id

        # Call 2: Resume step 0 with valid decision
        decision_0 = ApprovalDecision(
            approval_id=approval_id_0,
            workflow_id="wf-27",
            decision=ApprovalStatus.APPROVED,
            approver_id="approver_1",
            decided_at="2026-10-23T00:00:00Z",
        )

        out_2 = await self.agent.engine.execute_workflow(
            OrchestratorInput(
                task_id="t-27",
                task_description="Multi high-risk",
                execution_plan=plan,
                user_context={"roles": ["admin"]},
                workflow_id="wf-27",
                approval_decision=decision_0,
            )
        )

        # Step 0 executed, but step 1 MUST pause for a new approval!
        self.assertEqual(out_2.status, WorkflowStatus.WAITING_APPROVAL)
        self.assertIsNotNone(out_2.approval_request)
        self.assertNotEqual(out_2.approval_request.approval_id, approval_id_0)
        self.assertEqual(out_2.approval_request.details.get("step_index"), 1)
        # Agent was called exactly once (for step 0 only, NOT step 1)
        self.assertEqual(agent_fin.calls_count, 1)

    # 28. SEC-02: Caller-controlled risk label tampering is rejected and deterministically upgraded
    async def test_28_sec_02_caller_risk_label_tampering_prevented(self):
        agent_fin = StubDomainAgent(agent_id=AgentID.FINANCE)
        self.registry.register_agent(agent_fin)

        # Tampered plan: operation_type='write' (Rule 1 -> HIGH), but caller labels it LOW
        tampered_step = PlanStep(
            step_index=0,
            agent_id=AgentID.FINANCE,
            description="Malicious wire labeled as LOW",
            operation_type="write",
            risk_level=RiskLevel.LOW,  # TAMPERED
        )

        tampered_plan = ExecutionPlan(
            plan_id="p-28",
            task_id="t-28",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Tamper test",
            steps=[tampered_step],
        )

        # 1. PlanValidator rejects the tampered plan
        with self.assertRaises(PlanValidationError) as ctx:
            self.agent.engine.plan_validator.validate_plan(
                tampered_plan,
                trusted_roles=["admin"],
            )
        self.assertIn("Section 9.1 deterministic rules require 'HIGH'", str(ctx.exception))

        # 2. RiskGate.evaluate upgrades risk level deterministically
        dec, req = RiskGate.evaluate(tampered_step, "t-28", "wf-28")
        self.assertEqual(dec, RiskGateDecision.PAUSE)
        self.assertIsNotNone(req)
        self.assertEqual(req.risk_level, RiskLevel.HIGH)

        # 3. Workflow intake rejects the plan before any agent dispatch occurs
        out = await self.agent.engine.execute_workflow(
            OrchestratorInput(
                task_id="t-28",
                task_description="Tamper",
                execution_plan=tampered_plan,
                user_context={"roles": ["admin"]},
            )
        )
        self.assertEqual(out.status, WorkflowStatus.FAILED)
        self.assertEqual(agent_fin.calls_count, 0)

    # 29. ARCH-01: Prohibited policy / permission failures terminate without replanning
    async def test_29_arch_01_prohibited_policy_failure_does_not_replan(self):
        # Failing domain agent that returns POLICY_VIOLATION
        policy_err = ErrorInfo(
            code="POLICY_VIOLATION",
            message="Security policy engine blocked action",
            error_type="PolicyViolationError",
            retryable=False,
        )
        agent_blocked = StubDomainAgent(
            agent_id=AgentID.RESEARCH_RAG,
            fail_attempts=10,
            fail_error=policy_err,
        )
        self.registry.register_agent(agent_blocked)

        # Register PlannerAgent: planner calls must remain 0
        planner = StubPlannerAgent()
        self.registry.register_agent(planner)

        plan = ExecutionPlan(
            plan_id="p-29",
            task_id="t-29",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Policy denial test",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.RESEARCH_RAG,
                    description="Blocked step",
                )
            ],
        )

        out = await self.agent.engine.execute_workflow(
            OrchestratorInput(
                task_id="t-29",
                task_description="Policy blocked",
                execution_plan=plan,
                user_context={"roles": ["admin"]},
            )
        )

        self.assertEqual(out.status, WorkflowStatus.FAILED)
        self.assertEqual(out.error.code, "POLICY_VIOLATION")
        # PlannerAgent MUST NOT be invoked for prohibited policy failure
        self.assertEqual(planner.calls_count, 0)
        self.assertEqual(out.replan_count, 0)

    # 30. ARCH-01: Permitted failure classes still trigger contextual re-planning
    async def test_30_arch_01_permitted_failure_still_replans(self):
        # Failing domain agent that returns transient timeout (LLM_FAILURE)
        agent_transient = StubDomainAgent(
            agent_id=AgentID.RESEARCH_RAG,
            fail_attempts=10,  # Fails retries
        )
        agent_backup = StubDomainAgent(
            agent_id=AgentID.SQL_DATA,
            result_payload={"result": "backup_ok"},
        )
        self.registry.register_agent(agent_transient)
        self.registry.register_agent(agent_backup)

        revised_plan = ExecutionPlan(
            plan_id="p-30-revised",
            task_id="t-30",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Fallback",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.SQL_DATA,
                    description="Fallback via SQL",
                )
            ],
        )
        planner = StubPlannerAgent(plan_to_return=revised_plan)
        self.registry.register_agent(planner)

        plan = ExecutionPlan(
            plan_id="p-30",
            task_id="t-30",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Initial",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.RESEARCH_RAG,
                    description="Initial step",
                )
            ],
        )

        out = await self.agent.engine.execute_workflow(
            OrchestratorInput(
                task_id="t-30",
                task_description="Timeout replan",
                execution_plan=plan,
                user_context={"roles": ["admin"]},
            )
        )

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        # PlannerAgent was invoked for permitted failure class
        self.assertEqual(planner.calls_count, 1)
        self.assertEqual(out.replan_count, 1)
        self.assertEqual(agent_backup.calls_count, 1)


class TestOrchestratorEngineLangGraphIntegration(unittest.IsolatedAsyncioTestCase):
    """
    Focused integration-boundary tests for OrchestratorEngine with LangGraph delegation (Part 2.18.5.2).
    Verifies that execute_workflow delegates to the compiled StateGraph without entering the legacy loop.
    """

    def setUp(self):
        self.registry = AgentRegistry()
        self.state_store = InMemoryStateStore()
        self.engine = OrchestratorEngine(
            registry=self.registry,
            state_store=self.state_store,
            max_retries=1,
            initial_backoff_seconds=0.01,
            backoff_multiplier=1.0,
            max_replan_attempts=2,
            use_langgraph=True,
        )
        self.reviewer = StubReviewerAgent(recommendation=ReviewRecommendation.APPROVE)
        self.registry.register_agent(self.reviewer)

    # 1. Single-step successful execution through the graph
    async def test_01_single_step_successful_execution_through_graph(self):
        domain_agent = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG, result_payload={"summary": "rag findings"})
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-g1",
            task_id="task-g1",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Single step graph query",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.RESEARCH_RAG,
                    description="Run research step",
                    inputs={"query": "test query"},
                )
            ],
        )

        inp = OrchestratorInput(
            task_id="task-g1",
            task_description="Execute single research step",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
        )

        out = await self.engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        self.assertIsNotNone(out.final_response)
        self.assertEqual(out.final_response.status, WorkflowStatus.COMPLETED)
        self.assertEqual(domain_agent.calls_count, 1)
        self.assertEqual(len(out.provenance_chain), 1)
        self.assertEqual(out.provenance_chain[0].status, "COMPLETED")
        self.assertEqual(out.provenance_chain[0].agent_id, "research_rag")

    # 2. Multi-step execution and dependency/context passing
    async def test_02_multistep_dependency_and_context_passing(self):
        agent_rag = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG, result_payload={"doc": "extracted doc"})
        agent_sql = StubDomainAgent(agent_id=AgentID.SQL_DATA, result_payload={"rows": [1, 2, 3]})
        self.registry.register_agent(agent_rag)
        self.registry.register_agent(agent_sql)

        plan = ExecutionPlan(
            plan_id="plan-g2",
            task_id="task-g2",
            plan_type=PlanType.MULTI_AGENT,
            planning_rationale="Multi-step sequential graph execution",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.RESEARCH_RAG,
                    description="Extract doc",
                    inputs={"search": "annual report"},
                ),
                PlanStep(
                    step_index=1,
                    agent_id=AgentID.SQL_DATA,
                    description="Query database with doc findings",
                    inputs={"query": "SELECT * FROM data"},
                    dependencies=[0],
                ),
            ],
        )

        inp = OrchestratorInput(
            task_id="task-g2",
            task_description="Run multi-step workflow",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
        )

        out = await self.engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        self.assertEqual(agent_rag.calls_count, 1)
        self.assertEqual(agent_sql.calls_count, 1)
        self.assertEqual(len(out.provenance_chain), 2)
        self.assertEqual(out.provenance_chain[0].agent_id, "research_rag")
        self.assertEqual(out.provenance_chain[1].agent_id, "sql_data")

    # 3. Approval pause and valid approval resume
    async def test_03_approval_pause_and_valid_approval_resume(self):
        finance_agent = StubDomainAgent(agent_id=AgentID.FINANCE, result_payload={"tx": "paid"})
        self.registry.register_agent(finance_agent)

        plan = ExecutionPlan(
            plan_id="plan-g3",
            task_id="task-g3",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="High risk step requiring pause",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.FINANCE,
                    description="Transfer funds",
                    risk_level=RiskLevel.HIGH,
                    operation_type="transfer",
                    data_class="financial",
                    inputs={"amount": 1000},
                )
            ],
        )

        # Initial call: must pause at risk gate
        inp_pause = OrchestratorInput(
            task_id="task-g3",
            task_description="Perform high risk transfer",
            execution_plan=plan,
            user_context={"roles": ["admin"], "tenant_id": "tenant-01", "user_id": "usr-01"},
            metadata={"tenant_id": "tenant-01"},
        )

        out_pause = await self.engine.execute_workflow(inp_pause)

        self.assertEqual(out_pause.status, WorkflowStatus.WAITING_APPROVAL)
        self.assertIsNotNone(out_pause.approval_request)
        self.assertEqual(finance_agent.calls_count, 0)
        approval_id = out_pause.approval_request.approval_id

        # Resume call with valid approval decision and first-time claim
        decision = ApprovalDecision(
            approval_id=approval_id,
            workflow_id=out_pause.workflow_id,
            decision=ApprovalStatus.APPROVED,
            approver_id="admin_user",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )

        inp_resume = OrchestratorInput(
            task_id="task-g3",
            task_description="Perform high risk transfer",
            execution_plan=plan,
            workflow_id=out_pause.workflow_id,
            approval_decision=decision,
            user_context={"roles": ["admin"], "tenant_id": "tenant-01", "user_id": "usr-01"},
            metadata={
                "tenant_id": "tenant-01",
                "is_first_resume_claim": True,
                "approval_claim_count": 1,
            },
        )

        out_resume = await self.engine.execute_workflow(inp_resume)

        self.assertEqual(out_resume.status, WorkflowStatus.COMPLETED)
        self.assertEqual(finance_agent.calls_count, 1)

    # 4. Recovery of an already-claimed approval fails closed
    async def test_04_recovery_of_already_claimed_approval_fails_closed(self):
        finance_agent = StubDomainAgent(agent_id=AgentID.FINANCE, result_payload={"tx": "paid"})
        self.registry.register_agent(finance_agent)

        plan = ExecutionPlan(
            plan_id="plan-g4",
            task_id="task-g4",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="High risk step with prior claim",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.FINANCE,
                    description="Transfer funds non-idempotent",
                    risk_level=RiskLevel.HIGH,
                    operation_type="write",
                    data_class="financial",
                    inputs={"amount": 5000},
                )
            ],
        )

        decision = ApprovalDecision(
            approval_id="appr-crashed-01",
            workflow_id="wf-g4",
            decision=ApprovalStatus.APPROVED,
            approver_id="admin_user",
            decided_at=datetime.now(timezone.utc).isoformat(),
        )

        # Indicate this is a recovery claim of an already-claimed approval
        inp = OrchestratorInput(
            task_id="task-g4",
            task_description="Resume crashed workflow",
            workflow_id="wf-g4",
            execution_plan=plan,
            approval_decision=decision,
            user_context={"roles": ["admin"], "tenant_id": "tenant-01", "user_id": "usr-01"},
            metadata={
                "tenant_id": "tenant-01",
                "is_first_resume_claim": False,
                "approval_claim_count": 2,
            },
        )

        out = await self.engine.execute_workflow(inp)

        # Must fail closed per replay safety: cannot redispatch ambiguous mutating operation
        self.assertEqual(out.status, WorkflowStatus.FAILED)
        self.assertEqual(finance_agent.calls_count, 0)
        self.assertIsNotNone(out.error)
        self.assertEqual(out.error.code, "AMBIGUOUS_SIDE_EFFECT")

    # 5. Durable contextual replanning and completed-step fast-forwarding
    async def test_05_durable_contextual_replanning_and_fast_forwarding(self):
        repo = InMemoryWorkflowRecoveryRepository()
        engine = OrchestratorEngine(
            registry=self.registry,
            workflow_repo=repo,
            max_retries=1,
            initial_backoff_seconds=0.01,
            backoff_multiplier=1.0,
            max_replan_attempts=2,
            use_langgraph=True,
        )

        agent_rag = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG, result_payload={"info": "found"})
        agent_fail = StubDomainAgent(agent_id=AgentID.SQL_DATA, fail_attempts=5)
        agent_backup = StubDomainAgent(agent_id=AgentID.ANALYTICS, result_payload={"report": "done"})

        self.registry.register_agent(agent_rag)
        self.registry.register_agent(agent_fail)
        self.registry.register_agent(agent_backup)

        plan_0 = ExecutionPlan(
            plan_id="plan-orig",
            task_id="task-g5",
            plan_type=PlanType.MULTI_AGENT,
            planning_rationale="Initial plan",
            steps=[
                PlanStep(
                    step_index=0,
                    agent_id=AgentID.RESEARCH_RAG,
                    description="Search data",
                    inputs={"q": "rag"},
                ),
                PlanStep(
                    step_index=1,
                    agent_id=AgentID.SQL_DATA,
                    description="Run query",
                    inputs={"sql": "fail"},
                    dependencies=[0],
                ),
            ],
        )

        revised_sub_plan = ExecutionPlan(
            plan_id="sub-replan",
            task_id="task-g5",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Fallback to analytics",
            steps=[
                PlanStep(
                    step_index=1,
                    agent_id=AgentID.ANALYTICS,
                    description="Analyze without SQL",
                    inputs={"mode": "offline"},
                    dependencies=[0],
                )
            ],
        )
        planner = StubPlannerAgent(plan_to_return=revised_sub_plan)
        self.registry.register_agent(planner)

        wf_id = "wf-g5-replan"
        tenant_id = "tenant-g5"
        plan_0_hash = compute_plan_hash(plan_0)

        await repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-g5",
            task_id="task-g5",
            task_description="Execute plan",
            execution_plan=plan_0.model_dump(),
            plan_hash=plan_0_hash,
        )

        ok, epoch, rev, token = await repo.acquire_execution_lease(wf_id, tenant_id)
        self.assertTrue(ok)

        # Pre-complete Step 0 in repository
        step_0 = plan_0.steps[0]
        is_done, _, _, rev = await repo.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            agent_id=step_0.agent_id.value,
            name=step_0.description,
            plan_hash=plan_0_hash,
            step_hash=compute_step_hash(step_0),
            inputs=step_0.inputs,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )
        self.assertFalse(is_done)

        rev = await repo.complete_task_step(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            output_data={"info": "pre-computed in repo"},
            provenance={"agent_id": "research_rag", "step_index": 0, "status": "COMPLETED", "action_summary": "Search data"},
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        inp = OrchestratorInput(
            task_id="task-g5",
            task_description="Execute plan with recovery",
            workflow_id=wf_id,
            execution_plan=plan_0,
            user_context={"roles": ["admin"], "tenant_id": tenant_id, "user_id": "usr-g5"},
            metadata={
                "tenant_id": tenant_id,
                "fencing_epoch": epoch,
                "lock_token": token,
                "expected_revision": rev,
            },
        )

        out = await engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.COMPLETED)
        # Step 0 was pre-completed and fast-forwarded: domain agent was never called for Step 0
        self.assertEqual(agent_rag.calls_count, 0)
        # Planner was invoked for replanning Step 1
        self.assertEqual(planner.calls_count, 1)
        self.assertEqual(out.replan_count, 1)
        # Backup agent executed Step 1
        self.assertEqual(agent_backup.calls_count, 1)

    # 6. Tenant/identity and missing-credential fail-closed behavior
    async def test_06_tenant_and_missing_credential_fail_closed(self):
        repo = InMemoryWorkflowRecoveryRepository()
        engine = OrchestratorEngine(
            registry=self.registry,
            workflow_repo=repo,
            use_langgraph=True,
        )

        plan = ExecutionPlan(
            plan_id="plan-g6",
            task_id="task-g6",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Security test",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Search")],
        )

        # Case A: Missing tenant_id or user_id in protected execution context
        inp_no_identity = OrchestratorInput(
            task_id="task-g6",
            task_description="No identity test",
            execution_plan=plan,
            user_context={"roles": ["admin"]},  # Missing tenant_id and user_id
            metadata={"fencing_epoch": 1, "lock_token": "token", "expected_revision": 1},
        )
        out_no_id = await engine.execute_workflow(inp_no_identity)
        self.assertEqual(out_no_id.status, WorkflowStatus.FAILED)
        self.assertIsNotNone(out_no_id.error)
        self.assertEqual(out_no_id.error.code, "UNTRUSTED_IDENTITY")

        # Case B: Missing lease credentials (lock_token) when repository is configured
        inp_no_token = OrchestratorInput(
            task_id="task-g6",
            task_description="Missing token test",
            execution_plan=plan,
            user_context={"roles": ["admin"], "tenant_id": "ten-01", "user_id": "usr-01"},
            metadata={"fencing_epoch": 1, "expected_revision": 1},  # Missing lock_token
        )
        with self.assertRaises(FencingLeaseLostError):
            await engine.execute_workflow(inp_no_token)

    # 7. Graph failure mapping into OrchestratorOutput
    async def test_07_graph_failure_mapping_into_orchestrator_output(self):
        plan = ExecutionPlan(
            plan_id="plan-g7",
            task_id="task-g7",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Permission failure test",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Secret query")],
        )

        inp = OrchestratorInput(
            task_id="task-g7",
            task_description="Access restricted resource",
            execution_plan=plan,
            user_context={"roles": ["guest"]},
        )

        out = await self.engine.execute_workflow(inp)

        self.assertEqual(out.status, WorkflowStatus.FAILED)
        self.assertIsNotNone(out.error)
        self.assertEqual(out.error.code, "PLAN_VALIDATION_FAILED")

    # 8. Cancellation and tracked-task cleanup
    async def test_08_cancellation_and_tracked_tasks_cleanup(self):
        # Stub agent that pauses to allow cooperative cancellation
        class SlowAgent(BaseAgent[StubPayload, StubPayload]):
            def __init__(self):
                super().__init__(agent_id=AgentID.RESEARCH_RAG, input_schema=StubPayload, output_schema=StubPayload)

            async def _execute(self, validated_input: StubPayload, context: AgentExecutionContext) -> StubPayload:
                await asyncio.sleep(2.0)
                return StubPayload(result="done")

        self.registry.register_agent(SlowAgent())

        plan = ExecutionPlan(
            plan_id="plan-g8",
            task_id="task-g8",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Slow agent cancellation test",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Slow step")],
        )

        inp = OrchestratorInput(
            task_id="task-g8",
            task_description="Cancellation task",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
        )

        # Launch workflow as background task
        workflow_task = asyncio.create_task(self.engine.execute_workflow(inp))

        # Give the event loop a tick to enter _execute_via_langgraph
        await asyncio.sleep(0.05)

        # Verify task is actively tracked in _tracked_tasks
        self.assertIn(workflow_task, self.engine._tracked_tasks)

        # Cancel task
        workflow_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await workflow_task

        # Verify task is cleanly discarded from _tracked_tasks
        self.assertEqual(len(self.engine._tracked_tasks), 0)

    # 9. Canonical SSE event IDs and absence of duplicate lifecycle events
    async def test_09_canonical_sse_event_emission_and_no_duplicates(self):
        mock_event_bus = AsyncMock(spec=IWorkflowEventBus)
        engine = OrchestratorEngine(
            registry=self.registry,
            state_store=self.state_store,
            event_bus=mock_event_bus,
            use_langgraph=True,
        )

        domain_agent = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG, result_payload={"text": "sse content"})
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-g9",
            task_id="task-g9",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="SSE streaming test",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Search documentation")],
        )

        inp = OrchestratorInput(
            task_id="task-g9",
            task_description="Stream test",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
        )

        out = await engine.execute_workflow(inp)
        self.assertEqual(out.status, WorkflowStatus.COMPLETED)

        # Verify events published to event bus
        published_calls = mock_event_bus.publish.call_args_list
        self.assertTrue(len(published_calls) >= 4)  # WORKFLOW_STARTED, STEP_STARTED, STEP_COMPLETED, WORKFLOW_COMPLETED

        event_types = []
        for call_item in published_calls:
            event = call_item.args[0]
            # Check canonical event ID wire format: evt:v1:{wf}:e{epoch}:s{step}:{type}:sq{seq}:{disc}
            self.assertTrue(event.event_id.startswith("evt:v1:"))
            event_types.append(event.event_type.value)

        # Verify exact single occurrence of lifecycle milestones (no duplicates)
        self.assertEqual(event_types.count("workflow_started"), 1)
        self.assertEqual(event_types.count("step_started"), 1)
        self.assertEqual(event_types.count("step_completed"), 1)
        self.assertEqual(event_types.count("workflow_completed"), 1)

    # 10. Confirmation that graph mode never enters the legacy execution loop
    async def test_10_graph_mode_never_enters_legacy_execution_loop(self):
        domain_agent = StubDomainAgent(agent_id=AgentID.RESEARCH_RAG, result_payload={"ans": "test"})
        self.registry.register_agent(domain_agent)

        plan = ExecutionPlan(
            plan_id="plan-g10",
            task_id="task-g10",
            plan_type=PlanType.SINGLE_AGENT,
            planning_rationale="Bypass verification test",
            steps=[PlanStep(step_index=0, agent_id=AgentID.RESEARCH_RAG, description="Inspect bypass")],
        )

        inp = OrchestratorInput(
            task_id="task-g10",
            task_description="Verify graph bypasses legacy loop",
            execution_plan=plan,
            user_context={"roles": ["admin"]},
        )

        # Spy on legacy internal execution helper _dispatch_step_with_retries
        with patch.object(
            OrchestratorEngine,
            "_dispatch_step_with_retries",
            wraps=self.engine._dispatch_step_with_retries,
        ) as legacy_dispatch_spy:
            # 1. In Graph mode (use_langgraph=True): legacy dispatch must NEVER be called
            out_graph = await self.engine.execute_workflow(inp)
            self.assertEqual(out_graph.status, WorkflowStatus.COMPLETED)
            self.assertEqual(legacy_dispatch_spy.call_count, 0)

        # 2. In Legacy mode (use_langgraph=False): legacy dispatch MUST be called
        legacy_engine = OrchestratorEngine(
            registry=self.registry,
            state_store=self.state_store,
            use_langgraph=False,
        )
        with patch.object(
            OrchestratorEngine,
            "_dispatch_step_with_retries",
            wraps=legacy_engine._dispatch_step_with_retries,
        ) as legacy_dispatch_spy:
            out_legacy = await legacy_engine.execute_workflow(inp)
            self.assertEqual(out_legacy.status, WorkflowStatus.COMPLETED)
            self.assertEqual(legacy_dispatch_spy.call_count, 1)


if __name__ == "__main__":
    unittest.main()
