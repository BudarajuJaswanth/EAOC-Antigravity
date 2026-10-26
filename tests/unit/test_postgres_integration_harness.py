"""
EAOC Part 2.18.7.3B Unit Test Suite: PostgreSQL Integration Harness Unit Tests
Hermetic unit tests verifying the safety gates, lifecycle management,
and cleanup behavior of the integration test harness without live database connections:
1. Opt-in gate: RUN_POSTGRES_INTEGRATION=1 required before any connection.
2. Production environment blocks execution before engine creation.
3. Missing, malformed, or colliding URLs fail closed.
4. Incompatible schema preflight prevents integration test execution.
5. Dual engines are constructed independently with separate session factories.
6. Engine disposal occurs reliably on success and failure.
7. Tracked entity cleanup strictly deletes by exact ID in foreign-key dependency order.
8. Default unit-test discovery does not run the live integration suite.
"""

import os
from typing import List
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from eaoc.config.settings import Settings
from eaoc.db.models.approval import ApprovalRequestModel
from eaoc.db.models.workflow import WorkflowModel, WorkflowTaskModel
from eaoc.db.test_preflight import PreflightReport, PreflightStatus
from eaoc.db.test_session import TestDatabaseConfigurationError
from tests.integration.fixtures import (
    PostgresIntegrationTestCase,
    check_postgres_integration_safety_gate,
    cleanup_tracked_entities,
)


class TestPostgresIntegrationHarness(unittest.IsolatedAsyncioTestCase):

    def test_opt_in_gate_skips_when_env_var_unset(self):
        """When RUN_POSTGRES_INTEGRATION is not '1', safety gate skips immediately without engine creation."""
        with patch.dict(os.environ, {"RUN_POSTGRES_INTEGRATION": ""}):
            with self.assertRaises(unittest.SkipTest) as ctx:
                check_postgres_integration_safety_gate()
            self.assertIn("RUN_POSTGRES_INTEGRATION=1 not set", str(ctx.exception))

    def test_production_environment_blocks_execution(self):
        """When ENVIRONMENT is production, safety gate raises RuntimeError before engine creation."""
        mock_settings = Settings(
            ENVIRONMENT="production",
            TEST_DATABASE_URL="postgresql+asyncpg://test:testpass@127.0.0.1:5433/test_db",
        )
        with patch.dict(os.environ, {"RUN_POSTGRES_INTEGRATION": "1"}):
            with patch("tests.integration.fixtures.get_settings", return_value=mock_settings):
                with self.assertRaises(RuntimeError) as ctx:
                    check_postgres_integration_safety_gate()
                self.assertIn("cannot run in a production environment", str(ctx.exception))

    def test_missing_test_database_url_skips_safely(self):
        """When TEST_DATABASE_URL is missing or empty, safety gate skips cleanly."""
        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql+asyncpg://prod:prodpass@db.example.com:5432/primary_db",
            TEST_DATABASE_URL=None,
        )
        with patch.dict(os.environ, {"RUN_POSTGRES_INTEGRATION": "1"}):
            with patch("tests.integration.fixtures.get_settings", return_value=mock_settings):
                with self.assertRaises(unittest.SkipTest) as ctx:
                    check_postgres_integration_safety_gate()
                self.assertIn("TEST_DATABASE_URL is not configured", str(ctx.exception))

    def test_primary_database_collision_fails_closed(self):
        """When TEST_DATABASE_URL matches primary DATABASE_URL, safety gate raises TestDatabaseConfigurationError."""
        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql+asyncpg://prod:prodpass@db.example.com:5432/app_db",
            TEST_DATABASE_URL="postgresql://test:testpass@db.example.com:5432/app_db",
        )
        with patch.dict(os.environ, {"RUN_POSTGRES_INTEGRATION": "1"}):
            with patch("tests.integration.fixtures.get_settings", return_value=mock_settings):
                with self.assertRaises(TestDatabaseConfigurationError) as ctx:
                    check_postgres_integration_safety_gate()
                self.assertIn("failed safety validation", str(ctx.exception))

    def test_incompatible_preflight_blocks_execution(self):
        """When preflight reports INCOMPATIBLE, safety gate raises RuntimeError."""
        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql+asyncpg://prod:prodpass@db.example.com:5432/primary_db",
            TEST_DATABASE_URL="postgresql+asyncpg://test:testpass@127.0.0.1:5433/test_db",
        )
        mock_preflight = PreflightReport(
            status=PreflightStatus.INCOMPATIBLE,
            target_description="127.0.0.1:5433/test_db",
            is_compatible=False,
            incompatibilities=["Alembic revision mismatch"],
        )
        with patch.dict(os.environ, {"RUN_POSTGRES_INTEGRATION": "1"}):
            with patch("tests.integration.fixtures.get_settings", return_value=mock_settings):
                with patch(
                    "tests.integration.fixtures.run_test_database_preflight",
                    new_callable=AsyncMock,
                    return_value=mock_preflight,
                ):
                    with self.assertRaises(RuntimeError) as ctx:
                        check_postgres_integration_safety_gate()
                    self.assertIn("schema preflight failed", str(ctx.exception))
                    self.assertIn("Alembic revision mismatch", str(ctx.exception))

    def test_dual_engine_construction_and_disposal_lifecycle(self):
        """PostgresIntegrationTestCase constructs two distinct engines and session factories, and disposes both."""
        mock_engine_a = MagicMock(spec=AsyncEngine)
        mock_engine_b = MagicMock(spec=AsyncEngine)
        mock_factory_a = MagicMock(spec=async_sessionmaker)
        mock_factory_b = MagicMock(spec=async_sessionmaker)

        side_effects = [
            (mock_factory_a, mock_engine_a),
            (mock_factory_b, mock_engine_b),
        ]

        with patch("tests.integration.fixtures.check_postgres_integration_safety_gate") as mock_gate:
            with patch("tests.integration.fixtures.create_test_session_factory", side_effect=side_effects):
                with patch("tests.integration.fixtures.dispose_test_engine", new_callable=AsyncMock) as mock_dispose:
                    # Run setUpClass
                    PostgresIntegrationTestCase.setUpClass()
                    mock_gate.assert_called_once()
                    self.assertIs(PostgresIntegrationTestCase.engine_a, mock_engine_a)
                    self.assertIs(PostgresIntegrationTestCase.engine_b, mock_engine_b)
                    self.assertIsNot(PostgresIntegrationTestCase.engine_a, PostgresIntegrationTestCase.engine_b)
                    self.assertIs(PostgresIntegrationTestCase.session_factory_a, mock_factory_a)
                    self.assertIs(PostgresIntegrationTestCase.session_factory_b, mock_factory_b)

                    # Repositories bound to separate factories
                    self.assertIs(PostgresIntegrationTestCase.repo_a._session_factory, mock_factory_a)
                    self.assertIs(PostgresIntegrationTestCase.repo_b._session_factory, mock_factory_b)

                    # Run tearDownClass
                    PostgresIntegrationTestCase.tearDownClass()
                    self.assertEqual(mock_dispose.call_count, 2)
                    mock_dispose.assert_any_call(mock_engine_a)
                    mock_dispose.assert_any_call(mock_engine_b)
                    self.assertIsNone(PostgresIntegrationTestCase.engine_a)
                    self.assertIsNone(PostgresIntegrationTestCase.engine_b)

    async def test_cleanup_tracked_entities_dependency_order_and_scoping(self):
        """
        cleanup_tracked_entities deletes strictly by tracked IDs in exact dependency order:
        1. approval_requests
        2. workflow_tasks
        3. workflows
        Never executes TRUNCATE or blanket deletes.
        """
        mock_session = AsyncMock(spec=AsyncSession)
        mock_begin = MagicMock()
        mock_begin.__aenter__ = AsyncMock(return_value=None)
        mock_begin.__aexit__ = AsyncMock(return_value=None)
        mock_session.begin = MagicMock(return_value=mock_begin)

        mock_factory = MagicMock()
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_conn_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_factory.return_value = mock_conn_ctx

        executed_statements: List[Any] = []

        async def capture_execute(stmt, *args, **kwargs):
            executed_statements.append(stmt)
            return MagicMock()

        mock_session.execute = AsyncMock(side_effect=capture_execute)

        wf_ids = ["wf-101", "wf-102"]
        task_ids = ["task-201"]
        appr_ids = ["appr-301"]

        await cleanup_tracked_entities(
            session_factory=mock_factory,
            workflow_ids=wf_ids,
            task_ids=task_ids,
            approval_ids=appr_ids,
        )

        self.assertEqual(len(executed_statements), 3)

        # 1. First statement targets approval_requests
        stmt_1_str = str(executed_statements[0]).lower()
        self.assertIn("delete from approval_requests", stmt_1_str)
        self.assertIn("where", stmt_1_str)

        # 2. Second statement targets workflow_tasks
        stmt_2_str = str(executed_statements[1]).lower()
        self.assertIn("delete from workflow_tasks", stmt_2_str)
        self.assertIn("where", stmt_2_str)

        # 3. Third statement targets workflows
        stmt_3_str = str(executed_statements[2]).lower()
        self.assertIn("delete from workflows", stmt_3_str)
        self.assertIn("where", stmt_3_str)

    def test_default_unit_discovery_ignores_integration_suite(self):
        """Integration tests reside in tests/integration/ and are not discovered by tests/unit."""
        import unittest
        loader = unittest.TestLoader()
        unit_suite = loader.discover(start_dir="tests/unit")
        
        # Collect all discovered test names
        def extract_test_names(suite):
            names = []
            for item in suite:
                if isinstance(item, unittest.TestCase):
                    names.append(item.id())
                else:
                    names.extend(extract_test_names(item))
            return names

        discovered = extract_test_names(unit_suite)
        # Verify no test from tests.integration is discovered in tests/unit
        integration_tests_in_unit = [
            t for t in discovered if "tests.integration" in t or t.startswith("test_postgres_integration.")
        ]
        self.assertEqual(
            integration_tests_in_unit,
            [],
            "Live PostgreSQL integration tests must not be discovered by default unit-test runner.",
        )


if __name__ == "__main__":
    unittest.main()
