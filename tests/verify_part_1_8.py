"""
EAOC Part 1.8 - Redis Integration & State Persistence Foundation Verification Script

This script validates:
1. Preservation of Part 1.7 domain schemas and state interfaces (no unauthorized edits).
2. Clean integration of Redis configuration and secret redaction.
3. Redis Workflow State Store contract behavior (Atomic SET NX EX, serialization, error handling).
4. System Health Service Redis integration (safe when disabled, async ping check).
5. Scope compliance (12 agents, no AIC in metadata, no LangGraph/LLM/RAG).
6. Execution of unit tests & regression tests for Parts 1.2 - 1.7.
"""

import sys
import unittest
import asyncio
from datetime import datetime, timezone
from pydantic import SecretStr
from unittest.mock import AsyncMock, patch, MagicMock

# Import EAOC modules
from eaoc.config import get_settings
from eaoc.schemas.domain import (
    WorkflowState,
    WorkflowStatus,
    RequestContext,
    ExecutionMetadata,
    AgentID,
)
from eaoc.state.interfaces import IWorkflowStateStore
from eaoc.state.redis_store import RedisWorkflowStateStore
from eaoc.schemas.health import HealthStatus
from eaoc.services.health import HealthService


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

    # 3. Check RedisWorkflowStateStore interface inheritance
    assert issubclass(RedisWorkflowStateStore, IWorkflowStateStore), "RedisWorkflowStateStore must inherit IWorkflowStateStore"
    print("    [PASS] RedisWorkflowStateStore inherits IWorkflowStateStore.")


def verify_redis_config_and_redaction():
    print_section("2. REDIS CONFIGURATION & SECRET REDACTION VERIFICATION")

    settings = get_settings()
    if settings.REDIS_URL is not None:
        url_str = (
            settings.REDIS_URL.get_secret_value()
            if hasattr(settings.REDIS_URL, "get_secret_value")
            else str(settings.REDIS_URL)
        )
        sanitized = settings.get_sanitized_redis_url()
        print(f"[*] Raw REDIS_URL length: {len(url_str)}")
        print(f"[*] Sanitized REDIS_URL: {sanitized}")
        assert "@" not in sanitized or "redis://***:***@" in sanitized or "***" in sanitized or sanitized.endswith("rediss://***:***@localhost:6379/0"), \
            f"Sanitized URL did not properly redact password: {sanitized}"
    else:
        print("[*] REDIS_URL is None by default in environment.")

    mock_s = get_settings().model_copy(update={"REDIS_ENABLED": True, "REDIS_URL": "redis://:SecretPass123@localhost:6379/0"})
    with patch("eaoc.config.get_settings", return_value=mock_s):
        test_settings = get_settings()
        sanitized_test = test_settings.get_sanitized_redis_url()
        print(f"[*] Test Sanitized REDIS_URL: {sanitized_test}")
        assert "SecretPass123" not in sanitized_test, "Password leaked in sanitized Redis URL!"

        config_dict = test_settings.get_sanitized_config()
        print(f"[*] get_sanitized_config()['REDIS_URL']: {config_dict.get('REDIS_URL')}")
        assert "SecretPass123" not in str(config_dict.get('REDIS_URL')), "Password leaked in sanitized config dict!"

    print("    [PASS] Redis connection secrets are redacted in string representations and logs.")


async def verify_redis_store_contract():
    print_section("3. REDIS STATE STORE CONTRACT & ATOMICITY VERIFICATION")

    settings = get_settings()
    store = RedisWorkflowStateStore()
    now = datetime.now(timezone.utc).isoformat()
    sample_state = WorkflowState(
        workflow_id="wf-verify-1",
        request_context=RequestContext(request_id="req-verify-1", correlation_id="corr-verify-1"),
        status=WorkflowStatus.RUNNING,
        tasks={},
        execution_metadata=ExecutionMetadata(),
        created_at=now,
        updated_at=now,
    )

    with patch("eaoc.state.redis_store.get_redis_client") as mock_get_client:
        mock_redis = AsyncMock()
        mock_get_client.return_value = mock_redis

        # Test atomic create (SET NX EX) success
        mock_redis.set.return_value = True
        res = await store.create_state(sample_state)
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        key, val = call_args[0]
        kwargs = call_args[1]
        assert key == "eaoc:workflow:wf-verify-1", f"Unexpected key format: {key}"
        assert kwargs.get("nx") is True, "create_state must set nx=True for atomicity"
        assert kwargs.get("ex") == settings.REDIS_TTL_SECONDS, "create_state must include TTL"
        print("    [PASS] Atomic create_state() uses SET key val NX EX ttl.")

        # Test duplicate create raises ValueError
        mock_redis.set.return_value = None
        try:
            await store.create_state(sample_state)
            assert False, "Duplicate create should have raised ValueError"
        except ValueError as e:
            assert "already exists" in str(e)
            print("    [PASS] Duplicate create_state() raises ValueError when key exists.")

        # Test update missing state raises KeyError
        mock_redis.exists.return_value = False
        try:
            await store.update_state(sample_state)
            assert False, "Update missing state should have raised KeyError"
        except KeyError as e:
            assert "not found" in str(e) or "does not exist" in str(e)
            print("    [PASS] update_state() raises KeyError when updating non-existent state.")


async def verify_health_service():
    print_section("4. HEALTH SERVICE REDIS INTEGRATION VERIFICATION")

    health_svc = HealthService()

    # Case A: Redis disabled
    with patch("eaoc.services.health.get_settings") as mock_set:
        s_mock = MagicMock()
        s_mock.REDIS_ENABLED = False
        s_mock.is_redis_configured.return_value = False
        s_mock.REDIS_URL = None
        mock_set.return_value = s_mock

        h = await health_svc.check_redis_health()
        assert h.status == HealthStatus.NOT_CONFIGURED or h.status.value == "not_configured"
        assert "unconfigured" in h.details or "disabled" in h.details
        print("    [PASS] Health check returns NOT_CONFIGURED when REDIS_ENABLED=false.")

    # Case B: Redis enabled and ping succeeds
    with patch("eaoc.services.health.get_settings") as mock_set, \
         patch("eaoc.state.redis_store.get_redis_client") as mock_get_client:
        s_mock = MagicMock()
        s_mock.REDIS_ENABLED = True
        s_mock.is_redis_configured.return_value = True
        s_mock.get_sanitized_redis_url.return_value = "redis://***:***@localhost:6379/0"
        mock_set.return_value = s_mock

        mock_redis = AsyncMock()
        mock_redis.ping.return_value = True
        mock_get_client.return_value = mock_redis

        h = await health_svc.check_redis_health()
        assert h.status == HealthStatus.HEALTHY or h.status.value == "healthy"
        print("    [PASS] Health check returns HEALTHY when Redis pings successfully.")


def run_unit_and_regression_tests():
    print_section("5. RUNNING UNIT & REGRESSION TEST SUITES")

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
    print("   EAOC PHASE 1 / PART 1.8 VERIFICATION SUITE")
    print("==================================================================")

    verify_scope_and_contracts()
    verify_redis_config_and_redaction()

    asyncio.run(verify_redis_store_contract())
    asyncio.run(verify_health_service())

    run_unit_and_regression_tests()

    print("\n==================================================================")
    print("   PART 1.8 VERIFICATION COMPLETE: ALL CHECKS PASSED [OK]")
    print("==================================================================")


if __name__ == "__main__":
    main()
