"""
EAOC Part 2.18.4.2: Durable Execution Plan and Replanning Persistence Unit Tests
Validates atomic durable plan update, whole-workflow replan construction,
task index collision prevention, completed-step fast-forwarding, lease fencing,
OCC CAS conflict rejection, and crash recovery.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import AsyncMock, patch

from eaoc.agents.orchestrator.schemas import OrchestratorFailureClass
from eaoc.agents.planner.agent import PlannerAgent
from eaoc.agents.planner.engine import PlannerEngine
from eaoc.agents.planner.schemas import (
    ExecutionPlan,
    PlanStep,
    PlanType,
    PlannerOutput,
)
from eaoc.orchestration.nodes import (
    OrchestrationNodes,
    compute_plan_hash,
    compute_step_hash,
)
from eaoc.agents.registry import AgentRegistry
from eaoc.agents.research_rag.agent import ResearchRagAgent
from eaoc.agents.document.agent import DocumentAgent
from eaoc.agents.sql_data.agent import SqlDataAgent
from eaoc.agents.analytics.agent import AnalyticsAgent
from eaoc.orchestration.graph import compile_workflow_graph
from eaoc.orchestration.state import GraphWorkflowState
from eaoc.schemas.domain import (
    AgentID,
    RiskLevel,
    TaskStatus,
    WorkflowStatus,
)
from eaoc.services.event_bus import IWorkflowEventBus
from eaoc.state.workflow_repository import (
    ConcurrencyConflictError,
    FencingLeaseLostError,
    InMemoryWorkflowRecoveryRepository,
    PlanIntegrityError,
)


class TestDurableReplanning(unittest.IsolatedAsyncioTestCase):
    """Hermetic test suite for atomic durable plan update, collision prevention, and recovery."""

    def setUp(self):
        self.registry = AgentRegistry()
        self.research_agent = ResearchRagAgent()
        self.document_agent = DocumentAgent()
        self.sql_agent = SqlDataAgent()
        self.analytics_agent = AnalyticsAgent()
        self.planner_agent = PlannerAgent()

        self.registry.register_agent(self.research_agent)
        self.registry.register_agent(self.document_agent)
        self.registry.register_agent(self.sql_agent)
        self.registry.register_agent(self.analytics_agent)
        self.registry.register_agent(self.planner_agent)

        self.repo = InMemoryWorkflowRecoveryRepository()
        self.mock_event_bus = AsyncMock(spec=IWorkflowEventBus)

        self.nodes = OrchestrationNodes(
            registry=self.registry,
            workflow_repo=self.repo,
            approval_repo=AsyncMock(),
            event_bus=self.mock_event_bus,
            max_retries=1,
            initial_backoff_seconds=0.01,
            backoff_multiplier=1.0,
            max_replan_attempts=2,
        )

    def _build_multi_step_plan(
        self,
        task_id: str,
        step_definitions: List[Dict[str, Any]],
    ) -> ExecutionPlan:
        steps: List[PlanStep] = []
        for idx, s in enumerate(step_definitions):
            agent_id = s.get("agent_id", AgentID.RESEARCH_RAG)
            step = PlanStep(
                step_index=idx,
                agent_id=agent_id,
                description=s.get("description", f"Step {idx}"),
                inputs=s.get("inputs", {"query": f"query {idx}", "task_id": task_id}),
                dependencies=s.get("dependencies", []),
                operation_type=s.get("operation_type", "read"),
                data_class=s.get("data_class", "internal"),
                operation="read",
            )
            step.risk_level = RiskLevel.LOW
            steps.append(step)

        plan_type = PlanType.SINGLE_AGENT if len(steps) == 1 else PlanType.MULTI_AGENT
        return ExecutionPlan(
            plan_id=f"plan-{task_id}",
            task_id=task_id,
            plan_type=plan_type,
            steps=steps,
            planning_rationale="Multi-step test plan",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    # -------------------------------------------------------------------------
    # 1. Atomic plan/hash/counter/revision persistence
    # -------------------------------------------------------------------------
    async def test_atomic_plan_update_persists_state_and_increments_revision(self):
        """Verifies update_workflow_plan atomically commits revised plan, hash, replan count, and bumps revision."""
        wf_id = "wf-persist-01"
        tenant_id = "tenant-test"

        plan_0 = self._build_multi_step_plan("task-01", [
            {"agent_id": AgentID.RESEARCH_RAG, "description": "Step 0"},
            {"agent_id": AgentID.DOCUMENT, "description": "Step 1"},
        ])
        plan_0_hash = compute_plan_hash(plan_0)

        # Create intake
        wf, _ = await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-01",
            task_id="task-01",
            task_description="Execute multi-step task",
            execution_plan=plan_0.model_dump(),
            plan_hash=plan_0_hash,
        )

        ok, epoch, rev, token = await self.repo.acquire_execution_lease(wf_id, tenant_id)
        self.assertTrue(ok)
        self.assertEqual(rev, 2)

        # Execute Step 0 to COMPLETED
        step_0 = plan_0.steps[0]
        s0_hash = compute_step_hash(step_0)
        is_done, _, _, rev = await self.repo.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            agent_id=step_0.agent_id.value,
            name=step_0.description,
            plan_hash=plan_0_hash,
            step_hash=s0_hash,
            inputs=step_0.inputs,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )
        self.assertFalse(is_done)

        rev = await self.repo.complete_task_step(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            outputs={"findings": "step 0 results"},
            provenance={"agent": "research_rag", "exec_id": "exec-0"},
            duration_ms=12.5,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # Prepare Step 1 (which will be superseded / non-completed)
        step_1 = plan_0.steps[1]
        s1_hash = compute_step_hash(step_1)
        is_done, _, _, rev = await self.repo.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=1,
            agent_id=step_1.agent_id.value,
            name=step_1.description,
            plan_hash=plan_0_hash,
            step_hash=s1_hash,
            inputs=step_1.inputs,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # Revised plan replaces Step 1 with ANALYTICS
        plan_1 = self._build_multi_step_plan("task-01", [
            {"agent_id": AgentID.RESEARCH_RAG, "description": "Step 0"},
            {"agent_id": AgentID.ANALYTICS, "description": "Fallback Step 1"},
        ])
        plan_1_hash = compute_plan_hash(plan_1)

        new_rev = await self.repo.update_workflow_plan(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            execution_plan=plan_1.model_dump(),
            plan_hash=plan_1_hash,
            replan_count=1,
            replan_metadata={"reason": "Step 1 document agent failed"},
            current_step_index=1,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        self.assertEqual(new_rev, rev + 1)

        # Verify workflow model updated in repo
        wf_updated = await self.repo.get_workflow(wf_id, tenant_id)
        self.assertIsNotNone(wf_updated)
        self.assertEqual(wf_updated.plan_hash, plan_1_hash)
        self.assertEqual(wf_updated.row_revision, new_rev)
        self.assertEqual(wf_updated.current_step_index, 1)
        exec_meta = wf_updated.execution_metadata
        self.assertEqual(exec_meta.get("replan_count"), 1)
        self.assertIn(plan_0_hash, exec_meta.get("previous_plan_hashes", []))
        self.assertEqual(len(exec_meta.get("replan_history", [])), 1)

        # Verify superseded Step 1 task was deleted, but COMPLETED Step 0 remains
        tasks = await self.repo.get_tasks_for_workflow(wf_id, tenant_id)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].step_index, 0)
        self.assertEqual(tasks[0].status, TaskStatus.COMPLETED.value)
        self.assertEqual(tasks[0].plan_hash, plan_0_hash)  # Immutable original plan hash preserved

    # -------------------------------------------------------------------------
    # 2. Rejection of Stale Lease and OCC Conflict
    # -------------------------------------------------------------------------
    async def test_update_workflow_plan_enforces_lease_and_occ(self):
        """Verifies update_workflow_plan rejects stale fencing epoch, token, expired lease, or revision mismatch."""
        wf_id = "wf-occ-01"
        tenant_id = "tenant-test"

        plan = self._build_multi_step_plan("task-occ", [{"agent_id": AgentID.RESEARCH_RAG}])
        wf, _ = await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-01",
            task_id="task-occ",
            task_description="OCC test",
            execution_plan=plan.model_dump(),
            plan_hash=compute_plan_hash(plan),
        )

        ok, epoch, rev, token = await self.repo.acquire_execution_lease(wf_id, tenant_id)
        self.assertTrue(ok)

        # 1. Revision mismatch raises ConcurrencyConflictError
        with self.assertRaises(ConcurrencyConflictError):
            await self.repo.update_workflow_plan(
                workflow_id=wf_id,
                tenant_id=tenant_id,
                execution_plan=plan.model_dump(),
                plan_hash="new-hash",
                replan_count=1,
                replan_metadata={},
                current_step_index=0,
                fencing_epoch=epoch,
                lock_token=token,
                expected_revision=rev + 99,  # Stale revision
            )

        # 2. Fencing epoch mismatch raises FencingLeaseLostError
        with self.assertRaises(FencingLeaseLostError):
            await self.repo.update_workflow_plan(
                workflow_id=wf_id,
                tenant_id=tenant_id,
                execution_plan=plan.model_dump(),
                plan_hash="new-hash",
                replan_count=1,
                replan_metadata={},
                current_step_index=0,
                fencing_epoch=epoch + 1,  # Bad epoch
                lock_token=token,
                expected_revision=rev,
            )

        # 3. Lock token mismatch raises FencingLeaseLostError
        with self.assertRaises(FencingLeaseLostError):
            await self.repo.update_workflow_plan(
                workflow_id=wf_id,
                tenant_id=tenant_id,
                execution_plan=plan.model_dump(),
                plan_hash="new-hash",
                replan_count=1,
                replan_metadata={},
                current_step_index=0,
                fencing_epoch=epoch,
                lock_token="stale-token",  # Bad token
                expected_revision=rev,
            )

    # -------------------------------------------------------------------------
    # 3. Fast-forward completed step under historical plan hash
    # -------------------------------------------------------------------------
    async def test_completed_step_fast_forwards_with_historical_plan_hash(self):
        """Verifies prepare_task_step recognizes historical plan hash in previous_plan_hashes and fast-forwards without PlanIntegrityError."""
        wf_id = "wf-ff-01"
        tenant_id = "tenant-test"

        plan_0 = self._build_multi_step_plan("task-ff", [
            {"agent_id": AgentID.RESEARCH_RAG, "description": "Step 0 research"},
            {"agent_id": AgentID.DOCUMENT, "description": "Step 1 doc"},
        ])
        h0 = compute_plan_hash(plan_0)

        wf, _ = await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-01",
            task_id="task-ff",
            task_description="Fast forward test",
            execution_plan=plan_0.model_dump(),
            plan_hash=h0,
        )

        ok, epoch, rev, token = await self.repo.acquire_execution_lease(wf_id, tenant_id)
        step_0 = plan_0.steps[0]
        s0_hash = compute_step_hash(step_0)

        # Prepare and complete Step 0 under plan_0
        _, _, _, rev = await self.repo.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            agent_id=step_0.agent_id.value,
            name=step_0.description,
            plan_hash=h0,
            step_hash=s0_hash,
            inputs=step_0.inputs,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        rev = await self.repo.complete_task_step(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            outputs={"key": "step0_output_payload"},
            provenance={"agent": "research_rag", "prov_id": "prov-0"},
            duration_ms=20.0,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # Replan to plan_1 (Step 0 preserved, Step 1 replaced with SQL_DATA)
        plan_1 = self._build_multi_step_plan("task-ff", [
            {"agent_id": AgentID.RESEARCH_RAG, "description": "Step 0 research"},
            {"agent_id": AgentID.SQL_DATA, "description": "Step 1 sql"},
        ])
        h1 = compute_plan_hash(plan_1)

        rev = await self.repo.update_workflow_plan(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            execution_plan=plan_1.model_dump(),
            plan_hash=h1,
            replan_count=1,
            replan_metadata={"reason": "replan test"},
            current_step_index=1,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # Now call prepare_task_step for Step 0 using plan_1's hash h1!
        # It must fast-forward and return the preserved output and provenance!
        is_completed, task_out, task_prov, rev = await self.repo.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            agent_id=step_0.agent_id.value,
            name=step_0.description,
            plan_hash=h1,  # New plan hash passed!
            step_hash=s0_hash,  # Step definition is identical
            inputs=step_0.inputs,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        self.assertTrue(is_completed)
        self.assertEqual(task_out, {"key": "step0_output_payload"})
        self.assertEqual(task_prov.get("prov_id"), "prov-0")

    # -------------------------------------------------------------------------
    # 4. End-to-end compiled-graph multi-step replan and collision prevention
    # -------------------------------------------------------------------------
    async def test_compiled_graph_multistep_replan_and_collision_prevention(self):
        """
        Executes a 3-step workflow via compiled LangGraph:
        - Step 0 (RESEARCH_RAG) completes.
        - Step 1 (DOCUMENT) fails on first attempt.
        - contextual_replan constructs Whole-Workflow Plan (0: RESEARCH, 1: RESEARCH_RAG fallback, 2: ANALYTICS).
        - Revision is persisted to repo.
        - Step 0 fast-forwards.
        - Step 1 executes fallback and completes.
        - Step 2 completes.
        - Workflow reaches COMPLETED with replan_count=1.
        """
        wf_id = "wf-e2e-replan-01"
        tenant_id = "tenant-alpha"

        plan_0 = self._build_multi_step_plan("task-e2e", [
            {"agent_id": AgentID.RESEARCH_RAG, "description": "Step 0 research"},
            {"agent_id": AgentID.DOCUMENT, "description": "Step 1 doc"},
            {"agent_id": AgentID.ANALYTICS, "description": "Step 2 analytics", "dependencies": [0, 1]},
        ])
        h0 = compute_plan_hash(plan_0)

        wf, _ = await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-01",
            task_id="task-e2e",
            task_description="Research and analyze document data",
            execution_plan=plan_0.model_dump(),
            plan_hash=h0,
        )

        ok, epoch, rev, token = await self.repo.acquire_execution_lease(wf_id, tenant_id)
        self.assertTrue(ok)

        doc_failed_once = False

        async def dispatch_mock(ctx):
            nonlocal doc_failed_once
            from eaoc.agents.base import AgentExecutionResult
            from eaoc.schemas.domain import ErrorInfo
            if ctx.agent_id == AgentID.PLANNER:
                fallback_step = PlanStep(
                    step_index=0,
                    agent_id=AgentID.ANALYTICS,
                    description="Fallback Step 1 analytics",
                    inputs={"query": "fallback query", "task_id": "task-e2e"},
                    dependencies=[],
                    operation_type="read",
                    data_class="cross_domain",
                    operation="read",
                )
                fallback_step.risk_level = PlannerEngine.annotate_step_risk(fallback_step)
                fallback_plan = ExecutionPlan(
                    plan_id="plan-replan-sub",
                    task_id="task-e2e",
                    plan_type=PlanType.SINGLE_AGENT,
                    steps=[fallback_step],
                    planning_rationale="Reroute failed doc step to analytics",
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                return AgentExecutionResult(
                    execution_id=ctx.execution_id,
                    agent_id=ctx.agent_id,
                    status=TaskStatus.COMPLETED,
                    output_payload=PlannerOutput(
                        plan_id="plan-replan-sub",
                        task_id="task-e2e",
                        rationale="Reroute failed doc step to analytics",
                        plan=fallback_plan,
                        confidence_score=0.9,
                    ).model_dump(),
                )
            elif ctx.agent_id == AgentID.DOCUMENT:
                return AgentExecutionResult(
                    execution_id=ctx.execution_id,
                    agent_id=ctx.agent_id,
                    status=TaskStatus.FAILED,
                    error=ErrorInfo(code="DOCUMENT_TIMEOUT", message="Document agent timed out"),
                )
            return AgentExecutionResult(
                execution_id=ctx.execution_id,
                agent_id=ctx.agent_id,
                status=TaskStatus.COMPLETED,
                output_payload={"output": f"{ctx.agent_id.value} succeeded"},
            )

        compiled = compile_workflow_graph(self.nodes)

        initial_state: GraphWorkflowState = {
            "workflow_id": wf_id,
            "tenant_id": tenant_id,
            "task_id": "task-e2e",
            "task_description": "Research and analyze document data",
            "user_id": "usr-01",
            "user_context": {"roles": ["analyst"]},
            "fencing_epoch": epoch,
            "lock_token": token,
            "current_revision": rev,
            "execution_plan": plan_0.model_dump(),
        }

        with patch.object(self.registry, "dispatch", side_effect=dispatch_mock):
            final_state = await compiled.ainvoke(initial_state)

        self.assertEqual(final_state["status"], WorkflowStatus.COMPLETED.value)
        self.assertEqual(final_state.get("replan_count"), 1)
        self.assertIn(0, final_state.get("completed_steps", {}))
        self.assertIn(1, final_state.get("completed_steps", {}))
        self.assertIn(2, final_state.get("completed_steps", {}))

        # Check repository state
        wf_final = await self.repo.get_workflow(wf_id, tenant_id)
        self.assertIsNotNone(wf_final)
        self.assertEqual(wf_final.status, WorkflowStatus.COMPLETED.value)
        self.assertEqual(wf_final.execution_metadata.get("replan_count"), 1)
        self.assertIn(h0, wf_final.execution_metadata.get("previous_plan_hashes", []))

        # Check all 3 tasks persisted without index collision
        tasks = await self.repo.get_tasks_for_workflow(wf_id, tenant_id)
        self.assertEqual(len(tasks), 3)
        indices = [t.step_index for t in tasks]
        self.assertEqual(indices, [0, 1, 2])
        for t in tasks:
            self.assertEqual(t.status, TaskStatus.COMPLETED.value)

    # -------------------------------------------------------------------------
    # 5. Process restart recovery after a committed plan update
    # -------------------------------------------------------------------------
    async def test_restart_recovery_rehydrates_revised_plan_and_replan_counter(self):
        """Verifies that after replanning and updating the plan in DB, a new node instance recovers the revised plan."""
        wf_id = "wf-restart-01"
        tenant_id = "tenant-beta"

        plan_0 = self._build_multi_step_plan("task-restart", [
            {"agent_id": AgentID.RESEARCH_RAG, "description": "Step 0"},
            {"agent_id": AgentID.DOCUMENT, "description": "Step 1"},
        ])
        h0 = compute_plan_hash(plan_0)

        await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-01",
            task_id="task-restart",
            task_description="Restart recovery test",
            execution_plan=plan_0.model_dump(),
            plan_hash=h0,
        )

        ok, epoch, rev, token = await self.repo.acquire_execution_lease(wf_id, tenant_id)

        # Complete Step 0
        step_0 = plan_0.steps[0]
        _, _, _, rev = await self.repo.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            agent_id=step_0.agent_id.value,
            name=step_0.description,
            plan_hash=h0,
            step_hash=compute_step_hash(step_0),
            inputs=step_0.inputs,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )
        rev = await self.repo.complete_task_step(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            step_index=0,
            outputs={"res": "recovered data"},
            provenance={"agent": "research_rag"},
            duration_ms=10.0,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # Replan and commit plan_1
        plan_1 = self._build_multi_step_plan("task-restart", [
            {"agent_id": AgentID.RESEARCH_RAG, "description": "Step 0"},
            {"agent_id": AgentID.RESEARCH_RAG, "description": "Step 1 replacement"},
        ])
        h1 = compute_plan_hash(plan_1)

        rev = await self.repo.update_workflow_plan(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            execution_plan=plan_1.model_dump(),
            plan_hash=h1,
            replan_count=1,
            replan_metadata={"note": "crash simulated after commit"},
            current_step_index=1,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # SIMULATE PROCESS RESTART: create completely new OrchestrationNodes
        new_nodes = OrchestrationNodes(
            registry=self.registry,
            workflow_repo=self.repo,
            approval_repo=AsyncMock(),
            event_bus=self.mock_event_bus,
            max_retries=1,
            max_replan_attempts=2,
        )

        # Recovery state has NO execution_plan and replan_count=0
        restart_state: GraphWorkflowState = {
            "workflow_id": wf_id,
            "tenant_id": tenant_id,
            "task_id": "task-restart",
            "fencing_epoch": epoch,
            "lock_token": token,
            "current_revision": rev,
            "user_context": {"roles": ["analyst"]},
        }

        # Run node_init_and_recover
        recovered = await new_nodes.node_init_and_recover(restart_state)

        # Assert revised plan and replan counter were restored from DB
        self.assertEqual(recovered.get("plan_hash"), h1)
        self.assertEqual(recovered.get("replan_count"), 1)
        self.assertIsNotNone(recovered.get("execution_plan"))
        self.assertEqual(len(recovered.get("steps", [])), 2)
        self.assertIn(0, recovered.get("completed_steps", {}))
        self.assertEqual(recovered["completed_steps"][0], {"res": "recovered data"})

    # -------------------------------------------------------------------------
    # 6. Exhaustion of replan budget terminates safely
    # -------------------------------------------------------------------------
    async def test_replan_budget_exhaustion_terminates_workflow(self):
        """Verifies that exceeding max_replan_attempts terminates with MAX_REPLANS_EXHAUSTED."""
        wf_id = "wf-budget-01"
        tenant_id = "tenant-alpha"

        plan = self._build_multi_step_plan("task-budget", [
            {"agent_id": AgentID.RESEARCH_RAG, "description": "Step 0"},
        ])
        h = compute_plan_hash(plan)

        await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-01",
            task_id="task-budget",
            task_description="Budget test",
            execution_plan=plan.model_dump(),
            plan_hash=h,
        )

        ok, epoch, rev, token = await self.repo.acquire_execution_lease(wf_id, tenant_id)

        # State with replan_count already at max_replan_attempts (2)
        state: GraphWorkflowState = {
            "workflow_id": wf_id,
            "tenant_id": tenant_id,
            "task_id": "task-budget",
            "fencing_epoch": epoch,
            "lock_token": token,
            "current_revision": rev,
            "step_cursor": 0,
            "current_step_index": 0,
            "replan_count": 2,  # Max is 2
            "steps": [s.model_dump() for s in plan.steps],
            "error_info": {"code": "TRANSIENT_FAILURE", "message": "Failed again"},
        }

        route_res = await self.nodes.node_handle_step_failure(state)
        self.assertEqual(route_res["status"], WorkflowStatus.FAILED.value)
        self.assertEqual(route_res["error_info"]["code"], "MAX_REPLANS_EXHAUSTED")
        self.assertEqual(route_res["next_route"], "fail_workflow")

    # -------------------------------------------------------------------------
    # 7. Concurrent update conflict handling
    # -------------------------------------------------------------------------
    async def test_concurrent_plan_update_conflict(self):
        """Verifies that concurrent updates with the same expected_revision result in exactly one winner."""
        wf_id = "wf-concurrent-01"
        tenant_id = "tenant-test"

        plan = self._build_multi_step_plan("task-conc", [{"agent_id": AgentID.RESEARCH_RAG}])
        await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-01",
            task_id="task-conc",
            task_description="Concurrent test",
            execution_plan=plan.model_dump(),
            plan_hash=compute_plan_hash(plan),
        )

        ok, epoch, rev, token = await self.repo.acquire_execution_lease(wf_id, tenant_id)
        self.assertTrue(ok)

        # Worker 1 succeeds
        new_rev = await self.repo.update_workflow_plan(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            execution_plan=plan.model_dump(),
            plan_hash="hash-worker-1",
            replan_count=1,
            replan_metadata={"worker": 1},
            current_step_index=0,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )
        self.assertEqual(new_rev, rev + 1)

        # Worker 2 attempts with stale expected_revision and must fail
        with self.assertRaises(ConcurrencyConflictError):
            await self.repo.update_workflow_plan(
                workflow_id=wf_id,
                tenant_id=tenant_id,
                execution_plan=plan.model_dump(),
                plan_hash="hash-worker-2",
                replan_count=1,
                replan_metadata={"worker": 2},
                current_step_index=0,
                fencing_epoch=epoch,
                lock_token=token,
                expected_revision=rev,  # Stale revision
            )

    # -------------------------------------------------------------------------
    # 8. Tenant Isolation
    # -------------------------------------------------------------------------
    async def test_update_workflow_plan_tenant_isolation(self):
        """Verifies that attempting to update a workflow plan under a different tenant fails closed."""
        wf_id = "wf-tenant-iso-01"
        tenant_a = "tenant-a"
        tenant_b = "tenant-b"

        plan = self._build_multi_step_plan("task-iso", [{"agent_id": AgentID.RESEARCH_RAG}])
        await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_a,
            user_id="usr-01",
            task_id="task-iso",
            task_description="Tenant isolation test",
            execution_plan=plan.model_dump(),
            plan_hash=compute_plan_hash(plan),
        )

        ok, epoch, rev, token = await self.repo.acquire_execution_lease(wf_id, tenant_a)
        self.assertTrue(ok)

        # Cross-tenant access must fail
        with self.assertRaises(FencingLeaseLostError):
            await self.repo.update_workflow_plan(
                workflow_id=wf_id,
                tenant_id=tenant_b,  # Wrong tenant
                execution_plan=plan.model_dump(),
                plan_hash="new-hash",
                replan_count=1,
                replan_metadata={},
                current_step_index=0,
                fencing_epoch=epoch,
                lock_token=token,
                expected_revision=rev,
            )

    # -------------------------------------------------------------------------
    # 9. Postgres Repository Contract Conformance
    # -------------------------------------------------------------------------
    def test_postgres_repo_contract_conformance(self):
        """Verifies PostgresWorkflowRecoveryRepository implements update_workflow_plan conforming to IWorkflowRecoveryRepository."""
        import inspect
        from eaoc.state.workflow_repository import (
            IWorkflowRecoveryRepository,
            PostgresWorkflowRecoveryRepository,
        )

        self.assertTrue(issubclass(PostgresWorkflowRecoveryRepository, IWorkflowRecoveryRepository))
        self.assertTrue(hasattr(PostgresWorkflowRecoveryRepository, "update_workflow_plan"))

        sig_abstract = inspect.signature(IWorkflowRecoveryRepository.update_workflow_plan)
        sig_postgres = inspect.signature(PostgresWorkflowRecoveryRepository.update_workflow_plan)
        sig_in_memory = inspect.signature(InMemoryWorkflowRecoveryRepository.update_workflow_plan)

        self.assertEqual(list(sig_abstract.parameters.keys()), list(sig_postgres.parameters.keys()))
        self.assertEqual(list(sig_abstract.parameters.keys()), list(sig_in_memory.parameters.keys()))

    # -------------------------------------------------------------------------
    # 10. Multi-replan History Chaining
    # -------------------------------------------------------------------------
    async def test_multiple_sequential_replans_preserve_history(self):
        """Verifies that multiple sequential replans chain previous_plan_hashes and replan_history."""
        wf_id = "wf-chain-01"
        tenant_id = "tenant-test"

        plan_0 = self._build_multi_step_plan("task-chain", [{"agent_id": AgentID.RESEARCH_RAG}])
        h0 = compute_plan_hash(plan_0)

        await self.repo.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            user_id="usr-01",
            task_id="task-chain",
            task_description="Chain test",
            execution_plan=plan_0.model_dump(),
            plan_hash=h0,
        )

        ok, epoch, rev, token = await self.repo.acquire_execution_lease(wf_id, tenant_id)

        # Replan 1
        plan_1 = self._build_multi_step_plan("task-chain", [{"agent_id": AgentID.DOCUMENT}])
        h1 = compute_plan_hash(plan_1)
        rev = await self.repo.update_workflow_plan(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            execution_plan=plan_1.model_dump(),
            plan_hash=h1,
            replan_count=1,
            replan_metadata={"replan": 1},
            current_step_index=0,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        # Replan 2
        plan_2 = self._build_multi_step_plan("task-chain", [{"agent_id": AgentID.ANALYTICS}])
        h2 = compute_plan_hash(plan_2)
        rev = await self.repo.update_workflow_plan(
            workflow_id=wf_id,
            tenant_id=tenant_id,
            execution_plan=plan_2.model_dump(),
            plan_hash=h2,
            replan_count=2,
            replan_metadata={"replan": 2},
            current_step_index=0,
            fencing_epoch=epoch,
            lock_token=token,
            expected_revision=rev,
        )

        wf = await self.repo.get_workflow(wf_id, tenant_id)
        exec_meta = wf.execution_metadata
        self.assertEqual(exec_meta["replan_count"], 2)
        self.assertEqual(exec_meta["previous_plan_hashes"], [h0, h1])
        self.assertEqual(len(exec_meta["replan_history"]), 2)
        self.assertEqual(exec_meta["replan_history"][0]["previous_plan_hash"], h0)
        self.assertEqual(exec_meta["replan_history"][0]["new_plan_hash"], h1)
        self.assertEqual(exec_meta["replan_history"][1]["previous_plan_hash"], h1)
        self.assertEqual(exec_meta["replan_history"][1]["new_plan_hash"], h2)


if __name__ == "__main__":
    unittest.main()
