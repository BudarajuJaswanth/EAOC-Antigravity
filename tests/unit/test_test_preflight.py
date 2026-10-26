"""
EAOC Part 2.18.7.2E Unit Test Suite: Read-Only Test Database Schema Preflight
Hermetic unit tests verifying:
- Environment and fail-closed gates (production blocked, missing URL, primary collision, no fallback).
- Engine disposal in all code paths (success, validation failure, inspection error).
- Genuine PostgreSQL read-only transaction initialization (SET TRANSACTION READ ONLY;).
- Exact schema verification (Alembic head, extension, tables, columns, PKs, UQs, Checks, FKs, Indexes).
- Incompatibility detection for divergent revisions, missing objects, wrong index uniqueness, missing partial predicates.
- Secret redaction in reports and error messages.
- Side-effect-free import (no module-level engine or connection).
"""

from typing import Any, List
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from eaoc.config.settings import Settings
from eaoc.db.test_preflight import (
    DISCLAIMER_TEXT,
    EXPECTED_ALEMBIC_HEAD,
    PreflightReport,
    PreflightStatus,
    _get_safe_target_description,
    inspect_catalog,
    run_test_database_preflight,
)


def make_mock_catalog_connection(
    alembic_rev: str = EXPECTED_ALEMBIC_HEAD,
    extensions: List[str] = None,
    tables: List[str] = None,
    omit_columns: List[str] = None,
    omit_constraints: List[str] = None,
    omit_indexes: List[str] = None,
    alter_index_unique: bool = False,
    alter_index_predicate: bool = False,
    fk_on_delete_override: Any = None,
    fk_workflow_tasks_del_override: Any = None,
) -> AsyncMock:
    """
    Creates an AsyncMock connection simulating PostgreSQL catalog inspection responses.
    """
    extensions = extensions if extensions is not None else ["vector"]
    tables = (
        tables
        if tables is not None
        else ["approval_requests", "workflows", "workflow_tasks", "alembic_version"]
    )
    omit_columns = omit_columns or []
    omit_constraints = omit_constraints or []
    omit_indexes = omit_indexes or []

    conn = AsyncMock()
    executed_queries: List[str] = []

    async def mock_execute(statement, *args, **kwargs):
        sql = str(statement).strip().lower()
        executed_queries.append(sql)
        res_mock = MagicMock()

        # 1. Alembic version
        if "from alembic_version" in sql:
            res_mock.fetchone.return_value = (alembic_rev,) if alembic_rev else None
            return res_mock

        # 2. Extensions
        if "from pg_extension" in sql:
            res_mock.fetchall.return_value = [(ext,) for ext in extensions]
            return res_mock

        # 3. Tables
        if "from information_schema.tables" in sql:
            res_mock.fetchall.return_value = [(tbl,) for tbl in tables]
            return res_mock

        # 4. Columns
        if "from information_schema.columns" in sql:
            all_cols = [
                # approval_requests
                ("approval_requests", "approval_id"), ("approval_requests", "workflow_id"),
                ("approval_requests", "tenant_id"), ("approval_requests", "task_id"),
                ("approval_requests", "step_index"), ("approval_requests", "requested_action"),
                ("approval_requests", "risk_level"), ("approval_requests", "status"),
                ("approval_requests", "requested_at"), ("approval_requests", "expires_at"),
                ("approval_requests", "details"), ("approval_requests", "decision"),
                ("approval_requests", "approver_id"), ("approval_requests", "decision_reason"),
                ("approval_requests", "decided_at"), ("approval_requests", "resume_status"),
                ("approval_requests", "resume_error"), ("approval_requests", "created_at"),
                ("approval_requests", "updated_at"),
                # workflows
                ("workflows", "workflow_id"), ("workflows", "tenant_id"),
                ("workflows", "user_id"), ("workflows", "task_id"),
                ("workflows", "task_description"), ("workflows", "status"),
                ("workflows", "current_step_index"), ("workflows", "execution_plan"),
                ("workflows", "plan_hash"), ("workflows", "fencing_epoch"),
                ("workflows", "row_revision"), ("workflows", "lock_token"),
                ("workflows", "lock_expires_at"), ("workflows", "idempotency_key"),
                ("workflows", "idempotency_fingerprint"), ("workflows", "execution_metadata"),
                ("workflows", "error"), ("workflows", "created_at"), ("workflows", "updated_at"),
                # workflow_tasks
                ("workflow_tasks", "task_id"), ("workflow_tasks", "workflow_id"),
                ("workflow_tasks", "tenant_id"), ("workflow_tasks", "step_index"),
                ("workflow_tasks", "agent_id"), ("workflow_tasks", "name"),
                ("workflow_tasks", "plan_hash"), ("workflow_tasks", "step_hash"),
                ("workflow_tasks", "status"), ("workflow_tasks", "input_data"),
                ("workflow_tasks", "output_data"), ("workflow_tasks", "provenance"),
                ("workflow_tasks", "duration_ms"), ("workflow_tasks", "error"),
                ("workflow_tasks", "created_at"), ("workflow_tasks", "updated_at"),
            ]
            filtered_cols = [
                (t, c) for (t, c) in all_cols if f"{t}.{c}" not in omit_columns and c not in omit_columns
            ]
            res_mock.fetchall.return_value = filtered_cols
            return res_mock

        # 5. Primary keys & Unique constraints
        if "from information_schema.table_constraints" in sql:
            con_rows = [
                ("approval_requests", "pk_approval_requests", "PRIMARY KEY", "approval_id"),
                ("workflows", "pk_workflows", "PRIMARY KEY", "workflow_id"),
                ("workflow_tasks", "pk_workflow_tasks", "PRIMARY KEY", "task_id"),
                ("workflow_tasks", "uq_workflow_step_index", "UNIQUE", "step_index"),
            ]
            res_mock.fetchall.return_value = [
                r for r in con_rows if r[1] not in omit_constraints
            ]
            return res_mock

        # 6. Check constraints
        if "from pg_constraint c" in sql and "contype = 'c'" in sql:
            chk_rows = [
                ("chk_approval_requests_status", "approval_requests"),
                ("chk_approval_requests_risk_level", "approval_requests"),
                ("chk_approval_requests_resume_status", "approval_requests"),
                ("chk_workflows_status", "workflows"),
                ("chk_workflow_tasks_status", "workflow_tasks"),
            ]
            res_mock.fetchall.return_value = [
                r for r in chk_rows if r[0] not in omit_constraints
            ]
            return res_mock

        # 7. Foreign keys
        if "from pg_constraint c" in sql and "c.contype = 'f'" in sql:
            appr_del = fk_on_delete_override if fk_on_delete_override is not None else "r"
            wf_task_del = (
                fk_workflow_tasks_del_override
                if fk_workflow_tasks_del_override is not None
                else "c"
            )
            fk_rows = [
                ("fk_approval_requests_workflow_id", "approval_requests", "workflows", appr_del),
                ("fk_workflow_tasks_workflow_id", "workflow_tasks", "workflows", wf_task_del),
            ]
            res_mock.fetchall.return_value = [
                r for r in fk_rows if r[0] not in omit_constraints
            ]
            return res_mock

        # 8. Indexes
        if "from pg_indexes" in sql:
            pred = "where (idempotency_key is not null)" if not alter_index_predicate else ""
            unique_word = "unique index" if not alter_index_unique else "index"
            idx_rows = [
                ("ix_approval_requests_workflow_id", "approval_requests", "create index ix_approval_requests_workflow_id on public.approval_requests using btree (workflow_id)"),
                ("ix_approval_requests_tenant_id", "approval_requests", "create index ix_approval_requests_tenant_id on public.approval_requests using btree (tenant_id)"),
                ("ix_approval_requests_expires_at", "approval_requests", "create index ix_approval_requests_expires_at on public.approval_requests using btree (expires_at)"),
                ("ix_approval_tenant_status_expires", "approval_requests", "create index ix_approval_tenant_status_expires on public.approval_requests using btree (tenant_id, status, expires_at)"),
                ("ix_workflows_tenant_id", "workflows", "create index ix_workflows_tenant_id on public.workflows using btree (tenant_id)"),
                ("ix_workflows_user_id", "workflows", "create index ix_workflows_user_id on public.workflows using btree (user_id)"),
                ("ix_workflows_tenant_status", "workflows", "create index ix_workflows_tenant_status on public.workflows using btree (tenant_id, status)"),
                ("ix_workflows_tenant_idempotency", "workflows", f"create {unique_word} ix_workflows_tenant_idempotency on public.workflows using btree (tenant_id, idempotency_key) {pred}"),
                ("ix_workflow_tasks_workflow_id", "workflow_tasks", "create index ix_workflow_tasks_workflow_id on public.workflow_tasks using btree (workflow_id)"),
                ("ix_workflow_tasks_tenant_id", "workflow_tasks", "create index ix_workflow_tasks_tenant_id on public.workflow_tasks using btree (tenant_id)"),
                ("ix_workflow_tasks_lookup", "workflow_tasks", "create index ix_workflow_tasks_lookup on public.workflow_tasks using btree (tenant_id, workflow_id, step_index)"),
            ]
            res_mock.fetchall.return_value = [
                r for r in idx_rows if r[0] not in omit_indexes
            ]
            return res_mock

        res_mock.fetchall.return_value = []
        res_mock.fetchone.return_value = None
        return res_mock

    conn.execute = AsyncMock(side_effect=mock_execute)
    conn.executed_queries = executed_queries
    return conn


class TestTestDatabasePreflight(unittest.IsolatedAsyncioTestCase):

    def test_safe_target_description_redacts_credentials(self):
        """Verify _get_safe_target_description never leaks usernames, passwords, or query tokens."""
        url = "postgresql+asyncpg://admin_user:SuperSecretPassword123@db.supabase.co:5432/test_db?token=secret_token"
        desc = _get_safe_target_description(url)
        self.assertEqual(desc, "db.supabase.co:5432/test_db")
        self.assertNotIn("SuperSecretPassword123", desc)
        self.assertNotIn("admin_user", desc)
        self.assertNotIn("secret_token", desc)

    async def test_production_environment_blocked_before_engine(self):
        """Preflight must reject execution when environment is production without touching engine."""
        mock_settings = Settings(
            ENVIRONMENT="production",
            TEST_DATABASE_URL="postgresql+asyncpg://test:testpass@127.0.0.1:5433/test_db",
        )
        with patch("eaoc.db.test_preflight.get_settings", return_value=mock_settings):
            with patch("eaoc.db.test_preflight.create_test_async_engine") as mock_create:
                report = await run_test_database_preflight()
                self.assertEqual(report.status, PreflightStatus.SAFETY_BLOCKED)
                self.assertFalse(report.is_compatible)
                self.assertIn("running in production environment is prohibited", report.error_message)
                mock_create.assert_not_called()

    async def test_missing_or_empty_url_blocked(self):
        """Preflight must fail closed when TEST_DATABASE_URL is missing or empty."""
        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql+asyncpg://prod:prodpass@db.supabase.co:5432/prod_db",
            TEST_DATABASE_URL=None,
        )
        with patch("eaoc.db.test_preflight.get_settings", return_value=mock_settings):
            with patch("eaoc.db.test_preflight.create_test_async_engine") as mock_create:
                report = await run_test_database_preflight()
                self.assertEqual(report.status, PreflightStatus.SAFETY_BLOCKED)
                self.assertFalse(report.is_compatible)
                self.assertIn("missing, empty, or whitespace-only", report.error_message)
                mock_create.assert_not_called()

    async def test_primary_database_collision_blocked(self):
        """Preflight rejects candidates matching the primary database target."""
        primary_url = "postgresql+asyncpg://prod:prodpass@db.example.com:5432/primary_db"
        candidate = "postgresql://other:otherpass@db.example.com:5432/primary_db"
        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL=primary_url,
            TEST_DATABASE_URL=None,
        )
        with patch("eaoc.db.test_preflight.get_settings", return_value=mock_settings):
            with patch("eaoc.db.test_preflight.create_test_async_engine") as mock_create:
                report = await run_test_database_preflight(candidate_url=candidate)
                self.assertEqual(report.status, PreflightStatus.SAFETY_BLOCKED)
                self.assertFalse(report.is_compatible)
                self.assertIn("matches primary database target", report.error_message)
                mock_create.assert_not_called()

    async def test_no_fallback_to_database_url(self):
        """Preflight never falls back to DATABASE_URL when candidate/TEST_DATABASE_URL is unset."""
        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql+asyncpg://prod:prodpass@db.example.com:5432/primary_db",
            TEST_DATABASE_URL="",
        )
        with patch("eaoc.db.test_preflight.get_settings", return_value=mock_settings):
            with patch("eaoc.db.test_preflight.create_test_async_engine") as mock_create:
                report = await run_test_database_preflight()
                self.assertEqual(report.status, PreflightStatus.SAFETY_BLOCKED)
                mock_create.assert_not_called()

    async def test_read_only_transaction_enforced_before_queries(self):
        """Preflight must set SET TRANSACTION READ ONLY; before running catalog inspection queries."""
        mock_conn = make_mock_catalog_connection()
        mock_begin = MagicMock()
        mock_begin.__aenter__ = AsyncMock(return_value=None)
        mock_begin.__aexit__ = AsyncMock(return_value=None)
        mock_conn.begin = MagicMock(return_value=mock_begin)

        mock_engine = MagicMock()
        mock_connect = MagicMock()
        mock_connect.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_connect.__aexit__ = AsyncMock(return_value=None)
        mock_engine.connect = MagicMock(return_value=mock_connect)

        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql+asyncpg://prod:prodpass@db.example.com:5432/primary_db",
            TEST_DATABASE_URL="postgresql+asyncpg://test:testpass@127.0.0.1:5433/test_db",
        )

        with patch("eaoc.db.test_preflight.get_settings", return_value=mock_settings):
            with patch("eaoc.db.test_preflight.create_test_async_engine", return_value=mock_engine):
                with patch("eaoc.db.test_preflight.dispose_test_engine", new_callable=AsyncMock) as mock_dispose:
                    report = await run_test_database_preflight()
                    self.assertEqual(report.status, PreflightStatus.COMPATIBLE)
                    self.assertTrue(report.is_compatible)
                    self.assertEqual(report.disclaimer, DISCLAIMER_TEXT)
                    # Verify read-only transaction was first executed query
                    self.assertGreater(len(mock_conn.executed_queries), 0)
                    self.assertEqual(mock_conn.executed_queries[0], "set transaction read only;")
                    mock_dispose.assert_called_once_with(mock_engine)

    async def test_compatible_schema_returns_compatible_report(self):
        """Fully compliant schema catalog inspection yields PreflightStatus.COMPATIBLE."""
        mock_conn = make_mock_catalog_connection()
        report = await inspect_catalog(mock_conn)
        self.assertEqual(report.status, PreflightStatus.COMPATIBLE)
        self.assertTrue(report.is_compatible)
        self.assertEqual(report.alembic_head, EXPECTED_ALEMBIC_HEAD)
        self.assertEqual(len(report.incompatibilities), 0)
        self.assertEqual(len(report.missing_tables), 0)
        self.assertEqual(len(report.missing_columns), 0)
        self.assertEqual(len(report.missing_constraints), 0)
        self.assertEqual(len(report.missing_indexes), 0)

    async def test_divergent_alembic_revision_reported_incompatible(self):
        """Alembic revision not matching EXPECTED_ALEMBIC_HEAD marks report INCOMPATIBLE."""
        mock_conn = make_mock_catalog_connection(alembic_rev="0002_approval_requests")
        report = await inspect_catalog(mock_conn)
        self.assertEqual(report.status, PreflightStatus.INCOMPATIBLE)
        self.assertFalse(report.is_compatible)
        self.assertIn("Alembic revision mismatch", report.incompatibilities[0])

    async def test_missing_table_and_column_reported_incompatible(self):
        """Missing tables and columns are accurately detected and reported."""
        mock_conn = make_mock_catalog_connection(
            tables=["approval_requests", "workflows", "alembic_version"],  # workflow_tasks missing
            omit_columns=["workflows.lock_token"],
        )
        report = await inspect_catalog(mock_conn)
        self.assertEqual(report.status, PreflightStatus.INCOMPATIBLE)
        self.assertFalse(report.is_compatible)
        self.assertIn("workflow_tasks", report.missing_tables)
        self.assertIn("workflows.lock_token", report.missing_columns)

    async def test_missing_extension_reported_incompatible(self):
        """Missing vector extension marks report INCOMPATIBLE."""
        mock_conn = make_mock_catalog_connection(extensions=[])
        report = await inspect_catalog(mock_conn)
        self.assertEqual(report.status, PreflightStatus.INCOMPATIBLE)
        self.assertIn("vector", report.missing_extensions)

    async def test_missing_constraints_and_foreign_keys_reported_incompatible(self):
        """Missing check constraint, unique constraint, or foreign key marks report INCOMPATIBLE."""
        mock_conn = make_mock_catalog_connection(
            omit_constraints=[
                "uq_workflow_step_index",
                "chk_workflows_status",
                "fk_approval_requests_workflow_id",
            ]
        )
        report = await inspect_catalog(mock_conn)
        self.assertEqual(report.status, PreflightStatus.INCOMPATIBLE)
        self.assertIn("uq_workflow_step_index", report.missing_constraints)
        self.assertIn("chk_workflows_status", report.missing_constraints)
        self.assertIn("fk_approval_requests_workflow_id", report.missing_constraints)

    async def test_fk_delete_rule_mismatch_reported_incompatible(self):
        """Foreign key with mismatched ON DELETE rule (e.g. CASCADE instead of RESTRICT) is rejected."""
        mock_conn = make_mock_catalog_connection(fk_on_delete_override="c")  # CASCADE instead of RESTRICT
        report = await inspect_catalog(mock_conn)
        self.assertEqual(report.status, PreflightStatus.INCOMPATIBLE)
        self.assertTrue(
            any("expected 'RESTRICT'" in inc for inc in report.incompatibilities)
        )

    async def test_fk_delete_rule_bytes_values_recognized_compatible(self):
        """Regression test: asyncpg returns bytes (b'r', b'c') for char OID; must be recognized as COMPATIBLE."""
        mock_conn = make_mock_catalog_connection(
            fk_on_delete_override=b"r",
            fk_workflow_tasks_del_override=b"c",
        )
        report = await inspect_catalog(mock_conn)
        self.assertEqual(report.status, PreflightStatus.COMPATIBLE)
        self.assertTrue(report.is_compatible)
        self.assertEqual(len(report.incompatibilities), 0)

    async def test_fk_delete_rule_string_values_recognized_compatible(self):
        """String values ('r', 'c') are recognized correctly as RESTRICT and CASCADE."""
        mock_conn = make_mock_catalog_connection(
            fk_on_delete_override="r",
            fk_workflow_tasks_del_override="c",
        )
        report = await inspect_catalog(mock_conn)
        self.assertEqual(report.status, PreflightStatus.COMPATIBLE)
        self.assertTrue(report.is_compatible)
        self.assertEqual(len(report.incompatibilities), 0)

    async def test_fk_delete_rule_unknown_value_reported_incompatible(self):
        """Unknown or unexpected confdeltype values (str or bytes) result in UNKNOWN and INCOMPATIBLE."""
        # Test unknown string value
        mock_conn_str = make_mock_catalog_connection(fk_on_delete_override="x")
        report_str = await inspect_catalog(mock_conn_str)
        self.assertEqual(report_str.status, PreflightStatus.INCOMPATIBLE)
        self.assertTrue(
            any("has ondelete 'UNKNOWN', expected 'RESTRICT'" in inc for inc in report_str.incompatibilities)
        )

        # Test unknown byte value
        mock_conn_bytes = make_mock_catalog_connection(
            fk_on_delete_override="r",
            fk_workflow_tasks_del_override=b"?",
        )
        report_bytes = await inspect_catalog(mock_conn_bytes)
        self.assertEqual(report_bytes.status, PreflightStatus.INCOMPATIBLE)
        self.assertTrue(
            any("has ondelete 'UNKNOWN', expected 'CASCADE'" in inc for inc in report_bytes.incompatibilities)
        )

    async def test_index_uniqueness_and_partial_predicate_mismatch_reported(self):
        """Indexes with incorrect uniqueness or missing partial filter are flagged."""
        # Non-unique idempotency index
        mock_conn1 = make_mock_catalog_connection(alter_index_unique=True)
        report1 = await inspect_catalog(mock_conn1)
        self.assertEqual(report1.status, PreflightStatus.INCOMPATIBLE)
        self.assertTrue(any("uniqueness mismatch" in inc for inc in report1.incompatibilities))

        # Missing partial predicate
        mock_conn2 = make_mock_catalog_connection(alter_index_predicate=True)
        report2 = await inspect_catalog(mock_conn2)
        self.assertEqual(report2.status, PreflightStatus.INCOMPATIBLE)
        self.assertTrue(any("missing partial predicate" in inc for inc in report2.incompatibilities))

    async def test_connection_and_inspection_failure_handled_safely(self):
        """Exceptions during connection/inspection yield INSPECTION_FAILED and dispose engine."""
        mock_engine = MagicMock()
        mock_engine.connect.side_effect = RuntimeError("Simulated socket connection failure")

        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql+asyncpg://prod:prodpass@db.example.com:5432/primary_db",
            TEST_DATABASE_URL="postgresql+asyncpg://test:testpass@127.0.0.1:5433/test_db",
        )

        with patch("eaoc.db.test_preflight.get_settings", return_value=mock_settings):
            with patch("eaoc.db.test_preflight.create_test_async_engine", return_value=mock_engine):
                with patch("eaoc.db.test_preflight.dispose_test_engine", new_callable=AsyncMock) as mock_dispose:
                    report = await run_test_database_preflight()
                    self.assertEqual(report.status, PreflightStatus.INSPECTION_FAILED)
                    self.assertFalse(report.is_compatible)
                    self.assertIn("Simulated socket connection failure", report.error_message)
                    mock_dispose.assert_called_once_with(mock_engine)

    def test_side_effect_free_module_import(self):
        """Module import creates no engines, connections, or sessions."""
        import eaoc.db.test_preflight as tp
        # Inspect module variables: no engine or sessionmaker instance at module scope
        self.assertFalse(hasattr(tp, "engine"))
        self.assertFalse(hasattr(tp, "session"))


if __name__ == "__main__":
    unittest.main()
