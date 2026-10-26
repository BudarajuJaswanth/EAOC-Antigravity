"""
EAOC Unit Test Suite - API Intake & Workflow Request Lifecycle Foundation

Tests required Part 1.11 behavior:
1. POST /api/v1/workflows creation success (201 Created, PENDING status, Location header).
2. Trace headers propagation (X-Request-ID, X-Correlation-ID).
3. Unauthenticated boundary validation (user_id=None, tenant_id=None).
4. GET /api/v1/workflows/{workflow_id} status lookup success (200 OK).
5. Missing workflow lookup (404 Not Found with ErrorInfo).
6. Invalid request body handling (422 Unprocessable Entity).
7. State store connection failure handling (503 Service Unavailable).
8. Dependency injection override of get_state_store using in-memory test store.
9. Verification that zero LLM calls, tool executions, or agent invocations occur.
"""

import unittest
from typing import Dict, Optional, Any
from fastapi.testclient import TestClient

from eaoc.api.factory import create_app
from eaoc.api.v1.endpoints.workflows import get_state_store
from eaoc.schemas.domain import WorkflowState, WorkflowStatus
from eaoc.state.interfaces import IWorkflowStateStore


class InMemoryTestStateStore(IWorkflowStateStore):
    """In-memory implementation of IWorkflowStateStore for isolated unit testing."""

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


class FailingTestStateStore(IWorkflowStateStore):
    """Test state store that simulates Redis/store connection failure."""

    async def create_state(self, state: WorkflowState) -> WorkflowState:
        raise RuntimeError("Connection refused to Redis server at 127.0.0.1:6379")

    async def get_state(self, workflow_id: str) -> Optional[WorkflowState]:
        raise RuntimeError("Connection refused to Redis server at 127.0.0.1:6379")

    async def update_state(self, state: WorkflowState) -> WorkflowState:
        raise RuntimeError("Connection refused to Redis server at 127.0.0.1:6379")

    async def delete_state(self, workflow_id: str) -> bool:
        raise RuntimeError("Connection refused to Redis server at 127.0.0.1:6379")

    async def exists(self, workflow_id: str) -> bool:
        raise RuntimeError("Connection refused to Redis server at 127.0.0.1:6379")


class TestApiWorkflows(unittest.TestCase):
    """Unit test suite for Part 1.11 API intake and workflow request lifecycle."""

    def setUp(self):
        self.app = create_app()
        self.test_store = InMemoryTestStateStore()
        self.app.dependency_overrides[get_state_store] = lambda: self.test_store
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def test_1_create_workflow_success(self):
        """Verify POST /api/v1/workflows creates initial PENDING WorkflowState and returns HTTP 201."""
        payload = {
            "input_payload": {"prompt": "Analyze Q3 financial records"},
            "task_type": "finance_analysis",
            "metadata": {"client": "test_suite"}
        }
        response = self.client.post("/api/v1/workflows", json=payload)
        self.assertEqual(response.status_code, 201)

        data = response.json()
        self.assertTrue(data["workflow_id"].startswith("wf-"))
        self.assertEqual(data["status"], "PENDING")
        self.assertEqual(data["request_context"]["user_id"], None)
        self.assertEqual(data["request_context"]["tenant_id"], None)
        self.assertIn("Location", response.headers)
        self.assertEqual(response.headers["Location"], f"/api/v1/workflows/{data['workflow_id']}")

    def test_2_trace_headers_propagation(self):
        """Verify X-Request-ID and X-Correlation-ID trace headers are returned in response."""
        headers = {
            "X-Request-ID": "req-custom-12345",
            "X-Correlation-ID": "corr-custom-67890"
        }
        payload = {"input_payload": {"prompt": "test"}}
        response = self.client.post("/api/v1/workflows", json=payload, headers=headers)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers.get("X-Request-ID"), "req-custom-12345")
        self.assertEqual(response.headers.get("X-Correlation-ID"), "corr-custom-67890")

    def test_3_get_workflow_state_success(self):
        """Verify GET /api/v1/workflows/{workflow_id} retrieves status and state."""
        payload = {"input_payload": {"prompt": "Query active IT tickets"}}
        create_resp = self.client.post("/api/v1/workflows", json=payload)
        self.assertEqual(create_resp.status_code, 201)
        wf_id = create_resp.json()["workflow_id"]

        get_resp = self.client.get(f"/api/v1/workflows/{wf_id}")
        self.assertEqual(get_resp.status_code, 200)
        data = get_resp.json()
        self.assertEqual(data["workflow_id"], wf_id)
        self.assertEqual(data["status"], "PENDING")

    def test_4_get_workflow_state_missing_404(self):
        """Verify GET /api/v1/workflows/wf-missing returns HTTP 404 Not Found."""
        response = self.client.get("/api/v1/workflows/wf-missing-999")
        self.assertEqual(response.status_code, 404)
        data = response.json()
        self.assertIn("detail", data)
        self.assertIn("was not found", data["detail"])

    def test_5_invalid_request_body_422(self):
        """Verify POST /api/v1/workflows with missing required input_payload returns HTTP 422."""
        invalid_payload = {"task_type": "missing_payload"}
        response = self.client.post("/api/v1/workflows", json=invalid_payload)
        self.assertEqual(response.status_code, 422)

    def test_6_state_store_failure_503(self):
        """Verify state store connection failure returns HTTP 503 Service Unavailable."""
        self.app.dependency_overrides[get_state_store] = lambda: FailingTestStateStore()
        payload = {"input_payload": {"prompt": "test"}}

        create_resp = self.client.post("/api/v1/workflows", json=payload)
        self.assertEqual(create_resp.status_code, 503)
        self.assertIn("unavailable", create_resp.json()["detail"].lower())

        get_resp = self.client.get("/api/v1/workflows/wf-test")
        self.assertEqual(get_resp.status_code, 503)

    def test_7_no_external_execution_occurs(self):
        """Verify submitting a workflow initializes state in PENDING without triggering LLM/tools/agents."""
        payload = {"input_payload": {"prompt": "Do heavy analysis"}}
        response = self.client.post("/api/v1/workflows", json=payload)
        self.assertEqual(response.status_code, 201)

        data = response.json()
        # Verify status remains PENDING and no tasks have executed
        self.assertEqual(data["status"], "PENDING")
        self.assertEqual(len(data["tasks"]), 0)


if __name__ == "__main__":
    unittest.main()
