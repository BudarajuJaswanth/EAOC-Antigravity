"""
EAOC PostgreSQL Integration Test Fixtures & Safety Lifecycle Manager
Provides fail-closed safety gates, dual-engine concurrent isolation,
and strict tracked-record cleanup for PostgreSQL integration tests.

Invariants:
- Never connects unless RUN_POSTGRES_INTEGRATION=1 is set.
- Never runs in a production environment.
- Validates TEST_DATABASE_URL and verifies schema preflight before creating engines.
- Creates two independent AsyncEngines with separate connection pools for concurrency testing.
- Tracks exact primary keys and purges only test-created records in strict dependency order.
- Disposes all test engines in tearDownClass and on errors.
"""

import asyncio
import logging
import os
from typing import Any, List, Optional
import unittest
import uuid

from sqlalchemy import delete, or_
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from eaoc.config import get_settings
from eaoc.db.models.approval import ApprovalRequestModel
from eaoc.db.models.workflow import WorkflowModel, WorkflowTaskModel
from eaoc.db.test_preflight import PreflightStatus, run_test_database_preflight
from eaoc.db.test_session import (
    TestDatabaseConfigurationError,
    create_test_session_factory,
    dispose_test_engine,
)
from eaoc.state.approval_repository import PostgresApprovalRepository
from eaoc.state.workflow_repository import PostgresWorkflowRecoveryRepository

logger = logging.getLogger("eaoc.tests.integration.fixtures")


def check_postgres_integration_safety_gate() -> None:
    """
    Validates all safety prerequisites before any engine creation or database connection:
    1. RUN_POSTGRES_INTEGRATION=1 must be explicitly set.
    2. Environment must not be production.
    3. TEST_DATABASE_URL must be configured and valid.
    4. Schema preflight must report COMPATIBLE.
    """
    opt_in = os.environ.get("RUN_POSTGRES_INTEGRATION", "").strip()
    if opt_in != "1":
        raise unittest.SkipTest(
            "RUN_POSTGRES_INTEGRATION=1 not set. Skipping live PostgreSQL integration tests."
        )

    settings = get_settings()

    # Reject production environments
    if settings.is_production() or (settings.ENVIRONMENT and settings.ENVIRONMENT.lower() == "production"):
        raise RuntimeError("PostgreSQL integration tests cannot run in a production environment.")

    # Validate test database URL
    if not settings.TEST_DATABASE_URL or not settings.TEST_DATABASE_URL.strip():
        raise unittest.SkipTest(
            "TEST_DATABASE_URL is not configured. Skipping live PostgreSQL integration tests."
        )

    is_safe, diag_msg = settings.validate_test_database_url()
    if not is_safe:
        raise TestDatabaseConfigurationError(
            f"TEST_DATABASE_URL failed safety validation: {diag_msg}"
        )

    # Verify read-only schema preflight
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        preflight_report = loop.run_until_complete(run_test_database_preflight())
        if preflight_report.status != PreflightStatus.COMPATIBLE:
            raise RuntimeError(
                f"Test database schema preflight failed ({preflight_report.status.value}): "
                f"{preflight_report.incompatibilities or preflight_report.error_message}"
            )
    finally:
        loop.close()


async def cleanup_tracked_entities(
    session_factory: async_sessionmaker[AsyncSession],
    workflow_ids: List[str],
    task_ids: List[str],
    approval_ids: List[str],
) -> None:
    """
    Deletes strictly tracked primary keys in foreign-key dependency order:
    1. approval_requests (referencing workflows)
    2. workflow_tasks (referencing workflows)
    3. workflows (parent table)
    Never executes TRUNCATE, blanket deletes, or wildcard tenant deletes.
    """
    if not workflow_ids and not task_ids and not approval_ids:
        return

    async with session_factory() as session:
        async with session.begin():
            # 1. Purge approval requests
            if approval_ids or workflow_ids:
                appr_conds = []
                if approval_ids:
                    appr_conds.append(ApprovalRequestModel.approval_id.in_(approval_ids))
                if workflow_ids:
                    appr_conds.append(ApprovalRequestModel.workflow_id.in_(workflow_ids))
                await session.execute(delete(ApprovalRequestModel).where(or_(*appr_conds)))

            # 2. Purge workflow tasks
            if task_ids or workflow_ids:
                task_conds = []
                if task_ids:
                    task_conds.append(WorkflowTaskModel.task_id.in_(task_ids))
                if workflow_ids:
                    task_conds.append(WorkflowTaskModel.workflow_id.in_(workflow_ids))
                await session.execute(delete(WorkflowTaskModel).where(or_(*task_conds)))

            # 3. Purge parent workflows
            if workflow_ids:
                await session.execute(
                    delete(WorkflowModel).where(WorkflowModel.workflow_id.in_(workflow_ids))
                )


class PostgresIntegrationTestCase(unittest.IsolatedAsyncioTestCase):
    """
    Base test case for PostgreSQL integration tests.
    Provides dual-engine setup (Worker A and Worker B) and exact record cleanup.
    """

    engine_a: Optional[AsyncEngine] = None
    engine_b: Optional[AsyncEngine] = None
    session_factory_a: Optional[async_sessionmaker[AsyncSession]] = None
    session_factory_b: Optional[async_sessionmaker[AsyncSession]] = None

    repo_a: Optional[PostgresWorkflowRecoveryRepository] = None
    repo_b: Optional[PostgresWorkflowRecoveryRepository] = None
    approval_repo_a: Optional[PostgresApprovalRepository] = None
    approval_repo_b: Optional[PostgresApprovalRepository] = None

    @classmethod
    def setUpClass(cls):
        """Validates safety gates and creates two independent connection pools."""
        check_postgres_integration_safety_gate()

        # Engine A (Worker 1)
        cls.session_factory_a, cls.engine_a = create_test_session_factory(
            poolclass=NullPool
        )
        # Engine B (Worker 2) - completely separate connection pool
        cls.session_factory_b, cls.engine_b = create_test_session_factory(
            poolclass=NullPool
        )

        cls.repo_a = PostgresWorkflowRecoveryRepository(session_factory=cls.session_factory_a)
        cls.repo_b = PostgresWorkflowRecoveryRepository(session_factory=cls.session_factory_b)
        cls.approval_repo_a = PostgresApprovalRepository(session_factory=cls.session_factory_a)
        cls.approval_repo_b = PostgresApprovalRepository(session_factory=cls.session_factory_b)

    async def asyncSetUp(self):
        """Initializes tracking lists and unique run nonce for this test instance."""
        self.tracked_workflow_ids: List[str] = []
        self.tracked_task_ids: List[str] = []
        self.tracked_approval_ids: List[str] = []
        self.run_id: str = f"testrun-{uuid.uuid4().hex[:12]}"
        self.tenant_id: str = f"tenant-{self.run_id}"

    async def asyncTearDown(self):
        """Purges only explicitly tracked primary keys created by this test."""
        if self.session_factory_a:
            try:
                await cleanup_tracked_entities(
                    self.session_factory_a,
                    self.tracked_workflow_ids,
                    self.tracked_task_ids,
                    self.tracked_approval_ids,
                )
            except Exception as exc:
                logger.error(f"Error during tracked entity cleanup: {exc}")

    @classmethod
    def tearDownClass(cls):
        """Closes and disposes both test engines."""
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            if cls.engine_a is not None:
                loop.run_until_complete(dispose_test_engine(cls.engine_a))
                cls.engine_a = None
            if cls.engine_b is not None:
                loop.run_until_complete(dispose_test_engine(cls.engine_b))
                cls.engine_b = None
        finally:
            loop.close()
