"""
EAOC Part 1.8 - Redis Integration Test Suite

Gated integration tests against a live Redis instance.
Skipped automatically when REDIS_ENABLED=false or Redis is unreachable.
"""

import asyncio
import unittest
import uuid
from datetime import datetime, timezone

from eaoc.config import get_settings
from eaoc.schemas.domain import (
    WorkflowState,
    WorkflowStatus,
    RequestContext,
    ExecutionMetadata,
    Task,
    AgentID,
    TaskStatus,
)
from eaoc.state.redis_store import RedisWorkflowStateStore, close_redis_client


def is_redis_available() -> bool:
    """Check if Redis is configured and reachable for integration tests."""
    settings = get_settings()
    if not settings.is_redis_configured() or not settings.REDIS_URL:
        return False
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(settings.REDIS_URL.get_secret_value())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            res = loop.run_until_complete(client.ping())
            loop.run_until_complete(client.aclose())
            return res is True
        finally:
            loop.close()
    except Exception:
        return False


class TestRedisIntegration(unittest.TestCase):
    """Integration test suite for RedisWorkflowStateStore against a real Redis instance."""

    @classmethod
    def setUpClass(cls):
        if not is_redis_available():
            raise unittest.SkipTest("Redis is not enabled or not reachable. Skipping live integration tests.")
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)
        cls.store = RedisWorkflowStateStore()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'loop') and not cls.loop.is_closed():
            cls.loop.run_until_complete(close_redis_client())
            cls.loop.close()

    def _create_sample_state(self, workflow_id: str) -> WorkflowState:
        now = datetime.now(timezone.utc).isoformat()
        task = Task(
            task_id="task-1",
            workflow_id=workflow_id,
            name="Plan Generation",
            assigned_agent_id=AgentID.PLANNER,
            status=TaskStatus.COMPLETED,
            input_data={"prompt": "test"},
            output_data={"plan": "ok"},
        )
        return WorkflowState(
            workflow_id=workflow_id,
            request_context=RequestContext(request_id="req-1", correlation_id="corr-1"),
            status=WorkflowStatus.RUNNING,
            current_task_id="task-1",
            tasks={"task-1": task},
            execution_metadata=ExecutionMetadata(),
            created_at=now,
            updated_at=now,
        )

    def test_live_crud_lifecycle(self):
        """Test full Create, Read, Update, Exists, Delete lifecycle against real Redis."""
        wf_id = f"wf-integration-{uuid.uuid4().hex[:8]}"
        state = self._create_sample_state(wf_id)

        # 1. Initially does not exist
        exists_before = self.loop.run_until_complete(self.store.exists(wf_id))
        self.assertFalse(exists_before)

        # 2. Create state
        created = self.loop.run_until_complete(self.store.create_state(state))
        self.assertEqual(created.workflow_id, wf_id)
        self.assertEqual(created.status, WorkflowStatus.RUNNING)

        # 3. Exists should now be True
        exists_after = self.loop.run_until_complete(self.store.exists(wf_id))
        self.assertTrue(exists_after)

        # 4. Duplicate create should raise ValueError
        with self.assertRaises(ValueError):
            self.loop.run_until_complete(self.store.create_state(state))

        # 5. Get state
        retrieved = self.loop.run_until_complete(self.store.get_state(wf_id))
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.workflow_id, wf_id)
        self.assertIn("task-1", retrieved.tasks)

        # 6. Update state
        retrieved.status = WorkflowStatus.COMPLETED
        updated = self.loop.run_until_complete(self.store.update_state(retrieved))
        self.assertEqual(updated.status, WorkflowStatus.COMPLETED)

        # 7. Delete state
        deleted = self.loop.run_until_complete(self.store.delete_state(wf_id))
        self.assertTrue(deleted)

        # 8. Delete again should return False
        deleted_again = self.loop.run_until_complete(self.store.delete_state(wf_id))
        self.assertFalse(deleted_again)

        # 9. Get missing state should return None
        missing = self.loop.run_until_complete(self.store.get_state(wf_id))
        self.assertIsNone(missing)


if __name__ == "__main__":
    unittest.main()
