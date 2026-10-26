"""
EAOC Part 1.8 Unit Test Suite: Redis Workflow State Store
Uses standard Python unittest framework and AsyncMock to test RedisWorkflowStateStore
behavior against the IWorkflowStateStore contract without requiring a live Redis server.
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from eaoc.schemas.domain import RequestContext, WorkflowState, WorkflowStatus
from eaoc.state.redis_store import RedisWorkflowStateStore


class TestRedisWorkflowStateStore(unittest.TestCase):

    def setUp(self):
        self.mock_client = AsyncMock()
        self.store = RedisWorkflowStateStore(client=self.mock_client, ttl_seconds=3600)
        self.req_ctx = RequestContext(request_id="req_test_r", correlation_id="corr_test_r")
        self.sample_state = WorkflowState(
            workflow_id="wf_redis_100",
            request_context=self.req_ctx,
            status=WorkflowStatus.RUNNING,
            created_at="2026-10-05T20:30:00Z",
            updated_at="2026-10-05T20:30:00Z",
        )

    def test_create_state_success(self):
        """Verify create_state uses SET NX EX and returns WorkflowState."""
        self.mock_client.set.return_value = True

        async def run_test():
            res = await self.store.create_state(self.sample_state)
            self.assertEqual(res.workflow_id, "wf_redis_100")
            self.mock_client.set.assert_called_once()
            args, kwargs = self.mock_client.set.call_args
            self.assertEqual(args[0], "eaoc:workflow:wf_redis_100")
            self.assertEqual(kwargs.get("nx"), True)
            self.assertEqual(kwargs.get("ex"), 3600)

        asyncio.run(run_test())

    def test_create_state_duplicate_raises_value_error(self):
        """Verify create_state raises ValueError when workflow_id already exists."""
        self.mock_client.set.return_value = None  # SET NX returned False/None

        async def run_test():
            with self.assertRaises(ValueError) as cm:
                await self.store.create_state(self.sample_state)
            self.assertIn("already exists", str(cm.exception))

        asyncio.run(run_test())

    def test_get_state_existing(self):
        """Verify get_state retrieves and deserializes stored WorkflowState JSON."""
        json_payload = self.sample_state.model_dump_json()
        self.mock_client.get.return_value = json_payload

        async def run_test():
            res = await self.store.get_state("wf_redis_100")
            self.assertIsNotNone(res)
            self.assertEqual(res.workflow_id, "wf_redis_100")
            self.assertEqual(res.status, WorkflowStatus.RUNNING)
            self.mock_client.get.assert_called_once_with("eaoc:workflow:wf_redis_100")

        asyncio.run(run_test())

    def test_get_state_missing(self):
        """Verify get_state returns None when workflow_id is absent in Redis."""
        self.mock_client.get.return_value = None

        async def run_test():
            res = await self.store.get_state("wf_missing")
            self.assertIsNone(res)

        asyncio.run(run_test())

    def test_update_state_existing(self):
        """Verify update_state verifies existence and updates key with TTL."""
        self.mock_client.exists.return_value = 1
        self.mock_client.set.return_value = True

        async def run_test():
            self.sample_state.status = WorkflowStatus.COMPLETED
            res = await self.store.update_state(self.sample_state)
            self.assertEqual(res.status, WorkflowStatus.COMPLETED)
            self.mock_client.exists.assert_called_once_with("eaoc:workflow:wf_redis_100")
            self.mock_client.set.assert_called_once()

        asyncio.run(run_test())

    def test_update_state_missing_raises_key_error(self):
        """Verify update_state raises KeyError when workflow_id does not exist."""
        self.mock_client.exists.return_value = 0

        async def run_test():
            with self.assertRaises(KeyError) as cm:
                await self.store.update_state(self.sample_state)
            self.assertIn("does not exist", str(cm.exception))

        asyncio.run(run_test())

    def test_delete_state_existing_and_missing(self):
        """Verify delete_state returns True if deleted and False if missing."""
        async def run_test():
            self.mock_client.delete.return_value = 1
            self.assertTrue(await self.store.delete_state("wf_redis_100"))

            self.mock_client.delete.return_value = 0
            self.assertFalse(await self.store.delete_state("wf_missing"))

        asyncio.run(run_test())

    def test_exists_true_and_false(self):
        """Verify exists returns True if key present, False otherwise."""
        async def run_test():
            self.mock_client.exists.return_value = 1
            self.assertTrue(await self.store.exists("wf_redis_100"))

            self.mock_client.exists.return_value = 0
            self.assertFalse(await self.store.exists("wf_missing"))

        asyncio.run(run_test())

    def test_malformed_json_handling(self):
        """Verify get_state raises ValueError when stored payload is malformed."""
        self.mock_client.get.return_value = '{"invalid_json": true}'

        async def run_test():
            with self.assertRaises(ValueError) as cm:
                await self.store.get_state("wf_malformed")
            self.assertIn("malformed", str(cm.exception))

        asyncio.run(run_test())

    def test_redis_connection_error_sanitization(self):
        """Verify Redis connection errors are sanitized and wrapped into RuntimeError."""
        self.mock_client.set.side_effect = Exception("Connection refused to redis://:SecretPass@127.0.0.1:6379/0")

        async def run_test():
            with self.assertRaises(RuntimeError) as cm:
                await self.store.create_state(self.sample_state)
            err_msg = str(cm.exception)
            self.assertNotIn("SecretPass", err_msg)
            self.assertIn("failed", err_msg)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
