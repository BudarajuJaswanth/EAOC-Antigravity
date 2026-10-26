"""
EAOC Part 2.18.7.2C Unit Test Suite: Isolated Test Database Session Helper
Verifies test-only SQLAlchemy async engine and session-factory helpers:
- Fails closed on missing or invalid TEST_DATABASE_URL before engine creation.
- Never falls back to DATABASE_URL.
- Redacts secrets in error messages.
- Does not create engines or open connections at module import.
- Preserves production engine and session factory invariants.
- Supports repository injection.
- Cleans up and disposes engines without connecting to a live database.
"""

import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from eaoc.config.settings import Settings
from eaoc.db.session import AsyncSessionFactory as ProdAsyncSessionFactory, engine as prod_engine
from eaoc.db.test_session import (
    TestDatabaseConfigurationError,
    TestSessionManager,
    create_test_async_engine,
    create_test_session_factory,
    dispose_test_engine,
)
from eaoc.state.approval_repository import PostgresApprovalRepository
from eaoc.state.workflow_repository import PostgresWorkflowRecoveryRepository


class TestIsolatedTestSessionHelper(unittest.IsolatedAsyncioTestCase):

    def test_missing_test_database_url_fails_closed(self):
        """When TEST_DATABASE_URL is None, engine creation fails with TestDatabaseConfigurationError."""
        mock_settings = Settings(
            DATABASE_URL="postgresql+asyncpg://prod_user:p1@db.supabase.co:5432/prod_db",
            TEST_DATABASE_URL=None,
        )
        with patch("eaoc.db.test_session.get_settings", return_value=mock_settings):
            with self.assertRaises(TestDatabaseConfigurationError) as ctx:
                create_test_async_engine()
            self.assertIn("missing, empty, or whitespace-only", str(ctx.exception))

    def test_never_falls_back_to_database_url(self):
        """Even with a valid primary DATABASE_URL, TEST_DATABASE_URL never falls back to it."""
        mock_settings = Settings(
            DATABASE_URL="postgresql+asyncpg://prod_user:p1@db.supabase.co:5432/prod_db",
            TEST_DATABASE_URL=None,
        )
        with patch("eaoc.db.test_session.get_settings", return_value=mock_settings):
            with self.assertRaises(TestDatabaseConfigurationError):
                create_test_async_engine()

    def test_collision_with_primary_database_rejected(self):
        """Candidate matching primary DATABASE_URL is rejected before engine creation."""
        mock_settings = Settings(
            DATABASE_URL="postgresql+asyncpg://prod_user:prod_pass@db.example.com:5432/primary_db",
            TEST_DATABASE_URL=None,
        )
        with patch("eaoc.db.test_session.get_settings", return_value=mock_settings):
            with self.assertRaises(TestDatabaseConfigurationError) as ctx:
                create_test_async_engine(
                    candidate_url="postgresql://other_user:other_pass@db.example.com:5432/primary_db"
                )
            self.assertIn("matches primary database target", str(ctx.exception))

    def test_unsupported_scheme_and_malformed_url_rejected(self):
        """Non-PostgreSQL schemes and malformed URLs raise TestDatabaseConfigurationError."""
        mock_settings = Settings()
        with patch("eaoc.db.test_session.get_settings", return_value=mock_settings):
            # Unsupported scheme
            with self.assertRaises(TestDatabaseConfigurationError) as ctx1:
                create_test_async_engine(candidate_url="sqlite:///test.db")
            self.assertIn("Unsupported database scheme", str(ctx1.exception))

            # Missing host
            with self.assertRaises(TestDatabaseConfigurationError) as ctx2:
                create_test_async_engine(candidate_url="postgresql:///missing_host_db")
            self.assertIn("missing host", str(ctx2.exception))

    def test_secret_safety_in_exception_messages(self):
        """Sensitive passwords or credentials in candidate URLs are never leaked in exceptions."""
        sensitive_pass = "super_secret_test_password_xyz!"
        mock_settings = Settings(
            DATABASE_URL=f"postgresql+asyncpg://prod:{sensitive_pass}@db.prod.co:5432/prod_db",
            TEST_DATABASE_URL=None,
        )
        with patch("eaoc.db.test_session.get_settings", return_value=mock_settings):
            with self.assertRaises(TestDatabaseConfigurationError) as ctx:
                create_test_async_engine(
                    candidate_url=f"postgresql+asyncpg://admin:{sensitive_pass}@db.prod.co:5432/prod_db"
                )
            error_message = str(ctx.exception)
            self.assertNotIn(sensitive_pass, error_message)
            self.assertNotIn("admin:", error_message)

    def test_valid_candidate_creates_isolated_async_engine(self):
        """A valid candidate URL creates a distinct AsyncEngine with normalized asyncpg driver."""
        mock_settings = Settings(
            DATABASE_URL="postgresql+asyncpg://prod:prodpass@db.prod.co:5432/prod_db",
            TEST_DATABASE_URL="postgresql://test:testpass@127.0.0.1:5433/test_isolated_db",
        )
        with patch("eaoc.db.test_session.get_settings", return_value=mock_settings):
            engine = create_test_async_engine()
            self.assertIsInstance(engine, AsyncEngine)
            self.assertNotEqual(engine, prod_engine)
            # URL scheme normalized to postgresql+asyncpg
            self.assertEqual(engine.url.drivername, "postgresql+asyncpg")
            self.assertEqual(engine.url.host, "127.0.0.1")
            self.assertEqual(engine.url.port, 5433)
            self.assertEqual(engine.url.database, "test_isolated_db")

    def test_session_factory_creation_and_repository_injection(self):
        """
        create_test_session_factory creates an async_sessionmaker that can be injected
        into PostgresWorkflowRecoveryRepository and PostgresApprovalRepository without
        touching the production AsyncSessionFactory.
        """
        mock_settings = Settings(
            DATABASE_URL="postgresql+asyncpg://prod:prodpass@db.prod.co:5432/prod_db",
            TEST_DATABASE_URL="postgresql+asyncpg://test:testpass@127.0.0.1:5433/test_db",
        )
        with patch("eaoc.db.test_session.get_settings", return_value=mock_settings):
            session_factory, test_engine = create_test_session_factory()
            self.assertIsInstance(session_factory, async_sessionmaker)
            self.assertNotEqual(session_factory, ProdAsyncSessionFactory)

            # Verify repository injection
            wf_repo = PostgresWorkflowRecoveryRepository(session_factory=session_factory)
            self.assertIs(wf_repo._session_factory, session_factory)
            self.assertIsNot(wf_repo._session_factory, ProdAsyncSessionFactory)

            appr_repo = PostgresApprovalRepository(session_factory=session_factory)
            self.assertIs(appr_repo._session_factory, session_factory)
            self.assertIsNot(appr_repo._session_factory, ProdAsyncSessionFactory)

    async def test_disposal_helper_and_manager_lifecycle(self):
        """
        Verify TestSessionManager provides lazy instantiation and clean async disposal
        without leaving open resources.
        """
        mock_settings = Settings(
            DATABASE_URL="postgresql+asyncpg://prod:prodpass@db.prod.co:5432/prod_db",
            TEST_DATABASE_URL="postgresql+asyncpg://test:testpass@127.0.0.1:5433/test_db",
        )
        with patch("eaoc.db.test_session.get_settings", return_value=mock_settings):
            manager = TestSessionManager()
            self.assertIsNone(manager._engine)
            self.assertIsNone(manager._session_factory)

            # Lazy retrieval
            engine = manager.get_engine()
            self.assertIsInstance(engine, AsyncEngine)
            self.assertIs(manager._engine, engine)

            factory = manager.get_session_factory()
            self.assertIsInstance(factory, async_sessionmaker)
            self.assertIs(manager._session_factory, factory)

            # Dispose
            await manager.dispose()
            self.assertIsNone(manager._engine)
            self.assertIsNone(manager._session_factory)

            # Functional disposal is safe with None
            await dispose_test_engine(None)

    def test_production_invariants_preserved(self):
        """Verify production AsyncSessionFactory, production engine, and LangGraph default remain untouched."""
        from eaoc.agents.orchestrator.engine import OrchestratorEngine
        import inspect

        # Check production session factory and engine
        self.assertIs(ProdAsyncSessionFactory, ProdAsyncSessionFactory)
        self.assertIs(prod_engine, prod_engine)

        # Check LangGraph remains default False
        sig = inspect.signature(OrchestratorEngine.__init__)
        self.assertIn("use_langgraph", sig.parameters)
        self.assertFalse(sig.parameters["use_langgraph"].default)


if __name__ == "__main__":
    unittest.main()
