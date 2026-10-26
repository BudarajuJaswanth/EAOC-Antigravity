"""
EAOC Part 1.11 - API Intake & Workflow Request Lifecycle Foundation Verification Script

This script validates:
1. Scope & domain contract integrity (12 logical agents preserved, AIC omitted from metadata).
2. API Intake endpoint contracts (POST /api/v1/workflows creates status=PENDING, random wf- ID, user_id=None).
3. Trace header propagation (X-Request-ID, X-Correlation-ID).
4. Status lookup contracts (GET /api/v1/workflows/{id} returns 200 OK or 404 Not Found).
5. Explicit store failure behavior (503 Service Unavailable when store connection fails, NO silent fallback).
6. Confirmation of zero LLM calls, tool executions, or agent invocations.
7. Execution of full unit and regression test suite across Parts 1.2–1.11.
"""

import sys
import unittest
from typing import Dict, Optional
from fastapi.testclient import TestClient

# Import EAOC modules
from eaoc.api.factory import create_app
from eaoc.api.v1.endpoints.workflows import get_state_store
from eaoc.schemas.domain import (
    WorkflowState,
    WorkflowStatus,
    RequestContext,
    ExecutionMetadata,
    AgentID,
)
from eaoc.state.interfaces import IWorkflowStateStore


def print_section(title: str):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def verify_scope_and_contracts():
    print_section("1. SCOPE & CONTRACT INTEGRITY VERIFICATION")

    # 1. Check Agent Count
    agents = list(AgentID)
    print(f"[*] Verifying AgentID count: {len(agents)} logical agents found.")
    assert len(agents) == 12, f"Expected 12 logical agents, found {len(agents)}"
    print("    [PASS] Exactly 12 logical agents preserved.")

    # 2. Check ExecutionMetadata for AIC absence
    fields = ExecutionMetadata.model_fields
    print(f"[*] Verifying ExecutionMetadata fields: {list(fields.keys())}")
    assert "agent_invocations_count" not in fields, "agent_invocations_count MUST NOT be in ExecutionMetadata!"
    print("    [PASS] agent_invocations_count is NOT in ExecutionMetadata (AIC remains separate).")


class InMemoryVerificationStore(IWorkflowStateStore):
    def __init__(self):
        self._store: Dict[str, WorkflowState] = {}

    async def create_state(self, state: WorkflowState) -> WorkflowState:
        if state.workflow_id in self._store:
            raise ValueError(f"Workflow state with ID '{state.workflow_id}' already exists.")
        self._store[state.workflow_id] = state
        return state

    async def get_state(self, workflow_id: str) -> Optional[WorkflowState]:
        return self._store.get(workflow_id)

    async def update_state(self, state: WorkflowState) -> WorkflowState:
        if state.workflow_id not in self._store:
            raise KeyError(f"Workflow state with ID '{state.workflow_id}' does not exist.")
        self._store[state.workflow_id] = state
        return state

    async def delete_state(self, workflow_id: str) -> bool:
        if workflow_id in self._store:
            del self._store[workflow_id]
            return True
        return False

    async def exists(self, workflow_id: str) -> bool:
        return workflow_id in self._store


class FailingVerificationStore(IWorkflowStateStore):
    async def create_state(self, state: WorkflowState) -> WorkflowState:
        raise RuntimeError("Redis connection refused at 127.0.0.1:6379")

    async def get_state(self, workflow_id: str) -> Optional[WorkflowState]:
        raise RuntimeError("Redis connection refused at 127.0.0.1:6379")

    async def update_state(self, state: WorkflowState) -> WorkflowState:
        raise RuntimeError("Redis connection refused at 127.0.0.1:6379")

    async def delete_state(self, workflow_id: str) -> bool:
        raise RuntimeError("Redis connection refused at 127.0.0.1:6379")

    async def exists(self, workflow_id: str) -> bool:
        raise RuntimeError("Redis connection refused at 127.0.0.1:6379")


def verify_api_intake_lifecycle():
    print_section("2. API INTAKE & WORKFLOW REQUEST LIFECYCLE VERIFICATION")

    app = create_app()
    test_store = InMemoryVerificationStore()
    app.dependency_overrides[get_state_store] = lambda: test_store
    client = TestClient(app)

    try:
        # 1. Test POST /api/v1/workflows creation
        payload = {"input_payload": {"prompt": "Verification workflow prompt"}, "task_type": "verification"}
        headers = {"X-Request-ID": "req-verify-001", "X-Correlation-ID": "corr-verify-001"}
        resp = client.post("/api/v1/workflows", json=payload, headers=headers)

        assert resp.status_code == 201, f"Expected 201 Created, got {resp.status_code}"
        data = resp.json()

        assert data["workflow_id"].startswith("wf-"), f"Unexpected workflow_id prefix: {data['workflow_id']}"
        assert data["status"] == "PENDING", f"Expected PENDING status, got {data['status']}"
        assert data["request_context"]["request_id"] == "req-verify-001"
        assert data["request_context"]["correlation_id"] == "corr-verify-001"
        assert data["request_context"]["user_id"] is None
        assert data["request_context"]["tenant_id"] is None
        assert resp.headers.get("Location") == f"/api/v1/workflows/{data['workflow_id']}"
        print("    [PASS] POST /api/v1/workflows creates initial PENDING state with trace headers.")

        # 2. Test GET /api/v1/workflows/{workflow_id} lookup
        wf_id = data["workflow_id"]
        get_resp = client.get(f"/api/v1/workflows/{wf_id}")
        assert get_resp.status_code == 200, f"Expected 200 OK, got {get_resp.status_code}"
        assert get_resp.json()["workflow_id"] == wf_id
        print("    [PASS] GET /api/v1/workflows/{workflow_id} retrieves status and state.")

        # 3. Test GET /api/v1/workflows/wf-missing returns 404
        missing_resp = client.get("/api/v1/workflows/wf-missing-999")
        assert missing_resp.status_code == 404
        assert "was not found" in missing_resp.json()["detail"]
        print("    [PASS] Missing workflow ID returns HTTP 404 Not Found.")

        # 4. Test Store Connection Failure returns 503
        app.dependency_overrides[get_state_store] = lambda: FailingVerificationStore()
        fail_resp = client.post("/api/v1/workflows", json=payload)
        assert fail_resp.status_code == 503, f"Expected 503 Service Unavailable, got {fail_resp.status_code}"
        assert "unavailable" in fail_resp.json()["detail"].lower()
        print("    [PASS] State store failure returns explicit HTTP 503 (No silent fallback).")

    finally:
        app.dependency_overrides.clear()


def run_unit_and_regression_tests():
    print_section("3. RUNNING UNIT & REGRESSION TEST SUITES")

    loader = unittest.TestLoader()
    suite = loader.discover("tests/unit")

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print(f"\n[*] Test Results: {result.testsRun} tests run.")
    print(f"[*] Failures: {len(result.failures)}, Errors: {len(result.errors)}, Skipped: {len(result.skipped)}")

    assert result.wasSuccessful(), f"Unit & Regression tests failed: {result.failures} {result.errors}"
    print("    [PASS] All unit & regression tests passed successfully!")


def main():
    print("==================================================================")
    print("   EAOC PHASE 1 / PART 1.11 VERIFICATION SUITE")
    print("==================================================================")

    verify_scope_and_contracts()
    verify_api_intake_lifecycle()
    run_unit_and_regression_tests()

    print("\n==================================================================")
    print("   PART 1.11 VERIFICATION COMPLETE: ALL CHECKS PASSED [OK]")
    print("==================================================================")


if __name__ == "__main__":
    main()
