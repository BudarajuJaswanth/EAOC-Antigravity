"""
EAOC Part 1.7 Unit Test Suite: Shared Domain Models & Core State Interfaces
Uses standard Python unittest framework to test domain models, 12-agent registry,
strongly-typed approval decisions, optional identity fields, explicit AIC separation,
JSON serialization/deserialization, and storage-agnostic IWorkflowStateStore contract semantics.
"""

import asyncio
import json
import unittest
from typing import Dict, Optional
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ErrorInfo,
    ExecutionMetadata,
    RequestContext,
    RiskLevel,
    Task,
    TaskStatus,
    ToolExecutionContext,
    ToolExecutionResult,
    ValidationResult,
    WorkflowState,
    WorkflowStatus,
)
from eaoc.state.interfaces import IWorkflowStateStore


class InMemoryWorkflowStateStore(IWorkflowStateStore):
    """
    Test-only in-memory implementation of IWorkflowStateStore for contract verification.
    """

    def __init__(self):
        self._store: Dict[str, WorkflowState] = {}

    async def create_state(self, state: WorkflowState) -> WorkflowState:
        if state.workflow_id in self._store:
            raise ValueError(f"Workflow state with ID '{state.workflow_id}' already exists.")
        self._store[state.workflow_id] = state.model_copy(deep=True)
        return self._store[state.workflow_id]

    async def get_state(self, workflow_id: str) -> Optional[WorkflowState]:
        state = self._store.get(workflow_id)
        return state.model_copy(deep=True) if state else None

    async def update_state(self, state: WorkflowState) -> WorkflowState:
        if state.workflow_id not in self._store:
            raise KeyError(f"Workflow state with ID '{state.workflow_id}' does not exist.")
        self._store[state.workflow_id] = state.model_copy(deep=True)
        return self._store[state.workflow_id]

    async def delete_state(self, workflow_id: str) -> bool:
        if workflow_id in self._store:
            del self._store[workflow_id]
            return True
        return False

    async def exists(self, workflow_id: str) -> bool:
        return workflow_id in self._store


class TestDomainModelsAndInterfaces(unittest.TestCase):

    def test_exact_12_agent_registry(self):
        """Verify AgentID contains exactly the 12 approved logical agents."""
        approved_agents = {
            "research_rag",
            "sql_data",
            "document",
            "analytics",
            "finance",
            "hr",
            "it_support",
            "developer",
            "reviewer_validation",
            "security_policy",
            "orchestrator",
            "planner",
        }
        all_agents = {agent.value for agent in AgentID}
        self.assertEqual(len(all_agents), 12)
        self.assertEqual(all_agents, approved_agents)

    def test_request_context_optional_identity(self):
        """Verify RequestContext requires request/correlation IDs, while user/tenant IDs are optional."""
        ctx = RequestContext(request_id="req_123", correlation_id="corr_456")
        self.assertEqual(ctx.request_id, "req_123")
        self.assertEqual(ctx.correlation_id, "corr_456")
        self.assertIsNone(ctx.user_id)
        self.assertIsNone(ctx.tenant_id)

    def test_execution_metadata_aic_omission(self):
        """Verify ExecutionMetadata contains operational timing fields but no AIC attribute."""
        meta = ExecutionMetadata(
            start_time="2026-10-05T20:00:00Z",
            end_time="2026-10-05T20:00:05Z",
            total_duration_ms=5000.0,
            tokens_used=150,
        )
        self.assertEqual(meta.total_duration_ms, 5000.0)
        self.assertFalse(hasattr(meta, "agent_invocations_count"))
        self.assertFalse(hasattr(meta, "aic"))

    def test_approval_decision_strongly_typed(self):
        """Verify ApprovalDecision requires controlled ApprovalStatus enum values."""
        decision = ApprovalDecision(
            approval_id="appr_001",
            workflow_id="wf_100",
            decision=ApprovalStatus.APPROVED,
            approver_id="user_mgr",
            decided_at="2026-10-05T20:01:00Z",
        )
        self.assertEqual(decision.decision, ApprovalStatus.APPROVED)

    def test_validation_result_score_semantics(self):
        """Verify ValidationResult supports optional raw float score without scale enforcement."""
        v1 = ValidationResult(is_valid=True, score=0.95, findings=["All checks passed."])
        self.assertTrue(v1.is_valid)
        self.assertEqual(v1.score, 0.95)

        v2 = ValidationResult(is_valid=False, reason="Rule violation")
        self.assertFalse(v2.is_valid)
        self.assertIsNone(v2.score)

    def test_workflow_state_serialization(self):
        """Verify WorkflowState serializes cleanly to JSON and deserializes back."""
        req_ctx = RequestContext(request_id="req_ser", correlation_id="corr_ser")
        task = Task(
            task_id="task_1",
            workflow_id="wf_ser",
            name="Execute RAG Search",
            assigned_agent_id=AgentID.RESEARCH_RAG,
        )
        wf_state = WorkflowState(
            workflow_id="wf_ser",
            request_context=req_ctx,
            status=WorkflowStatus.RUNNING,
            current_task_id="task_1",
            tasks={"task_1": task},
            created_at="2026-10-05T20:00:00Z",
            updated_at="2026-10-05T20:00:01Z",
        )

        json_data = wf_state.model_dump_json()
        deserialized = WorkflowState.model_validate_json(json_data)

        self.assertEqual(deserialized.workflow_id, "wf_ser")
        self.assertEqual(deserialized.status, WorkflowStatus.RUNNING)
        self.assertIn("task_1", deserialized.tasks)
        self.assertEqual(deserialized.tasks["task_1"].assigned_agent_id, AgentID.RESEARCH_RAG)

    def test_in_memory_state_store_contract(self):
        """Verify IWorkflowStateStore contract method semantics using test-only in-memory implementation."""
        store = InMemoryWorkflowStateStore()

        async def run_store_tests():
            req_ctx = RequestContext(request_id="req_st", correlation_id="corr_st")
            state = WorkflowState(
                workflow_id="wf_test_store",
                request_context=req_ctx,
                created_at="2026-10-05T20:00:00Z",
                updated_at="2026-10-05T20:00:00Z",
            )

            # 1. exists before create
            self.assertFalse(await store.exists("wf_test_store"))
            self.assertIsNone(await store.get_state("wf_test_store"))

            # 2. create_state
            created = await store.create_state(state)
            self.assertEqual(created.workflow_id, "wf_test_store")
            self.assertTrue(await store.exists("wf_test_store"))

            # 3. create_state duplicate -> raises ValueError
            with self.assertRaises(ValueError):
                await store.create_state(state)

            # 4. get_state
            retrieved = await store.get_state("wf_test_store")
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved.workflow_id, "wf_test_store")

            # 5. update_state
            state.status = WorkflowStatus.COMPLETED
            updated = await store.update_state(state)
            self.assertEqual(updated.status, WorkflowStatus.COMPLETED)

            # 6. update_state missing -> raises KeyError
            missing_state = WorkflowState(
                workflow_id="wf_missing",
                request_context=req_ctx,
                created_at="2026-10-05T20:00:00Z",
                updated_at="2026-10-05T20:00:00Z",
            )
            with self.assertRaises(KeyError):
                await store.update_state(missing_state)

            # 7. delete_state
            self.assertTrue(await store.delete_state("wf_test_store"))
            self.assertFalse(await store.exists("wf_test_store"))
            self.assertFalse(await store.delete_state("wf_test_store"))

        asyncio.run(run_store_tests())


if __name__ == "__main__":
    unittest.main()
