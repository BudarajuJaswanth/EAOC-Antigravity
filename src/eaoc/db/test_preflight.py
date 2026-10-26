"""
EAOC Read-Only Test Database Schema Preflight Utility
Performs non-destructive, strictly read-only catalog inspections against
TEST_DATABASE_URL to verify schema compatibility before live integration testing.

Invariants:
- Never falls back to DATABASE_URL or production configuration.
- Rejects production environments before engine creation.
- Executes all catalog queries inside a PostgreSQL read-only transaction (SET TRANSACTION READ ONLY).
- Never executes SQL, DDL, DML, or migrations.
- Reports schema compatibility without altering database state.
- Redacts all credentials, tokens, and full connection URLs.
- Disposes all engines and connections in finally blocks.
"""

from dataclasses import dataclass, field
from enum import Enum
import logging
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from eaoc.config import get_settings
from eaoc.db.test_session import (
    TestDatabaseConfigurationError,
    create_test_async_engine,
    dispose_test_engine,
)

logger = logging.getLogger("eaoc.db.test_preflight")

DISCLAIMER_TEXT = (
    "Passing preflight verifies schema compatibility only. "
    "It does not prove physical instance isolation or database disposability."
)

EXPECTED_ALEMBIC_HEAD = "0003_workflow_recovery_state"
REQUIRED_EXTENSIONS = {"vector"}

EXPECTED_TABLES = {"approval_requests", "workflows", "workflow_tasks", "alembic_version"}

EXPECTED_COLUMNS: Dict[str, Set[str]] = {
    "approval_requests": {
        "approval_id", "workflow_id", "tenant_id", "task_id", "step_index",
        "requested_action", "risk_level", "status", "requested_at", "expires_at",
        "details", "decision", "approver_id", "decision_reason", "decided_at",
        "resume_status", "resume_error", "created_at", "updated_at",
    },
    "workflows": {
        "workflow_id", "tenant_id", "user_id", "task_id", "task_description",
        "status", "current_step_index", "execution_plan", "plan_hash",
        "fencing_epoch", "row_revision", "lock_token", "lock_expires_at",
        "idempotency_key", "idempotency_fingerprint", "execution_metadata",
        "error", "created_at", "updated_at",
    },
    "workflow_tasks": {
        "task_id", "workflow_id", "tenant_id", "step_index", "agent_id",
        "name", "plan_hash", "step_hash", "status", "input_data", "output_data",
        "provenance", "duration_ms", "error", "created_at", "updated_at",
    },
}

EXPECTED_PRIMARY_KEYS: Dict[str, str] = {
    "approval_requests": "approval_id",
    "workflows": "workflow_id",
    "workflow_tasks": "task_id",
}

EXPECTED_UNIQUE_CONSTRAINTS: Dict[str, Set[str]] = {
    "workflow_tasks": {"uq_workflow_step_index"},
}

EXPECTED_CHECK_CONSTRAINTS: Dict[str, Set[str]] = {
    "approval_requests": {
        "chk_approval_requests_status",
        "chk_approval_requests_risk_level",
        "chk_approval_requests_resume_status",
    },
    "workflows": {
        "chk_workflows_status",
    },
    "workflow_tasks": {
        "chk_workflow_tasks_status",
    },
}

EXPECTED_FOREIGN_KEYS: Dict[str, List[Dict[str, str]]] = {
    "approval_requests": [
        {
            "name": "fk_approval_requests_workflow_id",
            "target_table": "workflows",
            "on_delete": "RESTRICT",
        }
    ],
    "workflow_tasks": [
        {
            "name": "fk_workflow_tasks_workflow_id",
            "target_table": "workflows",
            "on_delete": "CASCADE",
        }
    ],
}

EXPECTED_INDEXES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "approval_requests": {
        "ix_approval_requests_workflow_id": {"unique": False},
        "ix_approval_requests_tenant_id": {"unique": False},
        "ix_approval_requests_expires_at": {"unique": False},
        "ix_approval_tenant_status_expires": {"unique": False},
    },
    "workflows": {
        "ix_workflows_tenant_id": {"unique": False},
        "ix_workflows_user_id": {"unique": False},
        "ix_workflows_tenant_status": {"unique": False},
        "ix_workflows_tenant_idempotency": {
            "unique": True,
            "partial_predicate": "idempotency_key is not null",
        },
    },
    "workflow_tasks": {
        "ix_workflow_tasks_workflow_id": {"unique": False},
        "ix_workflow_tasks_tenant_id": {"unique": False},
        "ix_workflow_tasks_lookup": {"unique": False},
    },
}


class PreflightStatus(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"
    INSPECTION_FAILED = "INSPECTION_FAILED"


@dataclass
class PreflightReport:
    status: PreflightStatus
    target_description: str
    is_compatible: bool = False
    alembic_head: Optional[str] = None
    missing_extensions: List[str] = field(default_factory=list)
    missing_tables: List[str] = field(default_factory=list)
    missing_columns: List[str] = field(default_factory=list)
    missing_constraints: List[str] = field(default_factory=list)
    missing_indexes: List[str] = field(default_factory=list)
    incompatibilities: List[str] = field(default_factory=list)
    disclaimer: str = DISCLAIMER_TEXT
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "target_description": self.target_description,
            "is_compatible": self.is_compatible,
            "alembic_head": self.alembic_head,
            "missing_extensions": self.missing_extensions,
            "missing_tables": self.missing_tables,
            "missing_columns": self.missing_columns,
            "missing_constraints": self.missing_constraints,
            "missing_indexes": self.missing_indexes,
            "incompatibilities": self.incompatibilities,
            "disclaimer": self.disclaimer,
            "error_message": self.error_message,
        }


def _get_safe_target_description(raw_url: Optional[str]) -> str:
    """Extracts a secret-free host:port/db description without credentials."""
    if not raw_url or not str(raw_url).strip():
        return "UNKNOWN_TARGET"
    try:
        parsed = urlparse(str(raw_url).strip())
        host = parsed.hostname or "unknown_host"
        port = parsed.port or 5432
        db = parsed.path.lstrip("/").split("/")[0] if parsed.path else "unknown_db"
        return f"{host}:{port}/{db}"
    except Exception:
        return "UNPARSEABLE_TARGET"


async def inspect_catalog(conn: AsyncConnection) -> PreflightReport:
    """
    Performs read-only PostgreSQL catalog queries and compares findings
    with expected schema definitions from migrations 0001-0003.
    """
    incompatibilities: List[str] = []
    missing_extensions: List[str] = []
    missing_tables: List[str] = []
    missing_columns: List[str] = []
    missing_constraints: List[str] = []
    missing_indexes: List[str] = []
    actual_alembic_head: Optional[str] = None

    # 1. Alembic Revision Check
    try:
        res = await conn.execute(text("SELECT version_num FROM alembic_version;"))
        row = res.fetchone()
        if row and row[0]:
            actual_alembic_head = str(row[0]).strip()
            if actual_alembic_head != EXPECTED_ALEMBIC_HEAD:
                incompatibilities.append(
                    f"Alembic revision mismatch: found '{actual_alembic_head}', expected '{EXPECTED_ALEMBIC_HEAD}'."
                )
        else:
            incompatibilities.append("Alembic version table 'alembic_version' is empty.")
    except Exception as exc:
        incompatibilities.append(f"Failed to inspect alembic_version: {exc}")

    # 2. Extensions Check
    try:
        res = await conn.execute(text("SELECT extname FROM pg_extension;"))
        installed_exts = {row[0].lower() for row in res.fetchall() if row and row[0]}
        for req_ext in REQUIRED_EXTENSIONS:
            if req_ext.lower() not in installed_exts:
                missing_extensions.append(req_ext)
                incompatibilities.append(f"Missing required PostgreSQL extension: '{req_ext}'.")
    except Exception as exc:
        incompatibilities.append(f"Failed to inspect extensions: {exc}")

    # 3. Tables Check
    try:
        res = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';"
            )
        )
        existing_tables = {row[0].lower() for row in res.fetchall() if row and row[0]}
        for exp_tbl in EXPECTED_TABLES:
            if exp_tbl.lower() not in existing_tables:
                missing_tables.append(exp_tbl)
                incompatibilities.append(f"Missing required table: '{exp_tbl}'.")
    except Exception as exc:
        incompatibilities.append(f"Failed to inspect tables: {exc}")
        existing_tables = set()

    # 4. Columns Check
    try:
        res = await conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public';"
            )
        )
        existing_columns: Dict[str, Set[str]] = {}
        for row in res.fetchall():
            if row and len(row) >= 2:
                tbl, col = row[0].lower(), row[1].lower()
                existing_columns.setdefault(tbl, set()).add(col)

        for tbl, exp_cols in EXPECTED_COLUMNS.items():
            if tbl in existing_tables:
                actual_cols = existing_columns.get(tbl, set())
                for col in exp_cols:
                    if col.lower() not in actual_cols:
                        missing_columns.append(f"{tbl}.{col}")
                        incompatibilities.append(f"Missing required column: '{tbl}.{col}'.")
    except Exception as exc:
        incompatibilities.append(f"Failed to inspect columns: {exc}")

    # 5. Primary Keys & Unique Constraints
    try:
        res = await conn.execute(
            text(
                """
                SELECT tc.table_name, tc.constraint_name, tc.constraint_type, kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
                WHERE tc.table_schema = 'public' AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE');
                """
            )
        )
        pk_cols: Dict[str, str] = {}
        unique_cons: Dict[str, Set[str]] = {}
        for row in res.fetchall():
            if row and len(row) >= 4:
                tbl, con_name, con_type, col_name = row[0].lower(), row[1].lower(), row[2], row[3].lower()
                if con_type == "PRIMARY KEY":
                    pk_cols[tbl] = col_name
                elif con_type == "UNIQUE":
                    unique_cons.setdefault(tbl, set()).add(con_name)

        for tbl, exp_pk in EXPECTED_PRIMARY_KEYS.items():
            if tbl in existing_tables:
                if pk_cols.get(tbl) != exp_pk.lower():
                    missing_constraints.append(f"pk_{tbl}")
                    incompatibilities.append(
                        f"Missing or mismatched primary key on '{tbl}': expected '{exp_pk}', found '{pk_cols.get(tbl)}'."
                    )

        for tbl, exp_uniques in EXPECTED_UNIQUE_CONSTRAINTS.items():
            if tbl in existing_tables:
                actual_uniques = unique_cons.get(tbl, set())
                for uq in exp_uniques:
                    if uq.lower() not in actual_uniques:
                        missing_constraints.append(uq)
                        incompatibilities.append(f"Missing required unique constraint: '{uq}' on '{tbl}'.")
    except Exception as exc:
        incompatibilities.append(f"Failed to inspect constraints: {exc}")

    # 6. Check Constraints
    try:
        res = await conn.execute(
            text(
                """
                SELECT c.conname, t.relname
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                WHERE c.contype = 'c' AND n.nspname = 'public';
                """
            )
        )
        existing_checks: Dict[str, Set[str]] = {}
        for row in res.fetchall():
            if row and len(row) >= 2:
                con_name, tbl = row[0].lower(), row[1].lower()
                existing_checks.setdefault(tbl, set()).add(con_name)

        for tbl, exp_checks in EXPECTED_CHECK_CONSTRAINTS.items():
            if tbl in existing_tables:
                actual_checks = existing_checks.get(tbl, set())
                for chk in exp_checks:
                    if chk.lower() not in actual_checks:
                        missing_constraints.append(chk)
                        incompatibilities.append(f"Missing required check constraint: '{chk}' on '{tbl}'.")
    except Exception as exc:
        incompatibilities.append(f"Failed to inspect check constraints: {exc}")

    # 7. Foreign Keys
    try:
        res = await conn.execute(
            text(
                """
                SELECT c.conname, t.relname AS source_table, rt.relname AS target_table, c.confdeltype::text AS confdeltype
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN pg_class rt ON c.confrelid = rt.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                WHERE c.contype = 'f' AND n.nspname = 'public';
                """
            )
        )
        existing_fks: Dict[str, Dict[str, Dict[str, str]]] = {}
        # In PostgreSQL pg_constraint.confdeltype: 'c' = CASCADE, 'r' = RESTRICT, 'a' = NO ACTION
        del_type_map = {"c": "CASCADE", "r": "RESTRICT", "a": "NO ACTION"}

        for row in res.fetchall():
            if row and len(row) >= 4:
                con_name, src_tbl, target_tbl, confdel = (
                    row[0].lower(),
                    row[1].lower(),
                    row[2].lower(),
                    row[3],
                )
                if isinstance(confdel, (bytes, bytearray)):
                    confdel_key = confdel.decode("ascii", errors="replace").strip().lower()
                elif confdel is not None:
                    confdel_key = str(confdel).strip().lower()
                else:
                    confdel_key = ""

                existing_fks.setdefault(src_tbl, {})[con_name] = {
                    "target_table": target_tbl,
                    "on_delete": del_type_map.get(confdel_key, "UNKNOWN"),
                }

        for tbl, exp_fk_list in EXPECTED_FOREIGN_KEYS.items():
            if tbl in existing_tables:
                tbl_fks = existing_fks.get(tbl, {})
                for exp_fk in exp_fk_list:
                    fk_name = exp_fk["name"].lower()
                    if fk_name not in tbl_fks:
                        missing_constraints.append(exp_fk["name"])
                        incompatibilities.append(f"Missing required foreign key: '{exp_fk['name']}' on '{tbl}'.")
                    else:
                        actual_fk = tbl_fks[fk_name]
                        if actual_fk["target_table"] != exp_fk["target_table"].lower():
                            incompatibilities.append(
                                f"Foreign key '{exp_fk['name']}' targets '{actual_fk['target_table']}', expected '{exp_fk['target_table']}'."
                            )
                        if actual_fk["on_delete"] != exp_fk["on_delete"]:
                            incompatibilities.append(
                                f"Foreign key '{exp_fk['name']}' has ondelete '{actual_fk['on_delete']}', expected '{exp_fk['on_delete']}'."
                            )
    except Exception as exc:
        incompatibilities.append(f"Failed to inspect foreign keys: {exc}")

    # 8. Indexes (uniqueness and partial predicates)
    try:
        res = await conn.execute(
            text(
                """
                SELECT indexname, tablename, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public';
                """
            )
        )
        existing_indexes: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for row in res.fetchall():
            if row and len(row) >= 3:
                idx_name, tbl, idx_def = row[0].lower(), row[1].lower(), str(row[2]).lower()
                is_unique = "unique index" in idx_def
                existing_indexes.setdefault(tbl, {})[idx_name] = {
                    "unique": is_unique,
                    "indexdef": idx_def,
                }

        for tbl, exp_index_map in EXPECTED_INDEXES.items():
            if tbl in existing_tables:
                tbl_indexes = existing_indexes.get(tbl, {})
                for idx_name, idx_props in exp_index_map.items():
                    name_lower = idx_name.lower()
                    if name_lower not in tbl_indexes:
                        missing_indexes.append(idx_name)
                        incompatibilities.append(f"Missing required index: '{idx_name}' on '{tbl}'.")
                    else:
                        actual_idx = tbl_indexes[name_lower]
                        if actual_idx["unique"] != idx_props.get("unique", False):
                            incompatibilities.append(
                                f"Index '{idx_name}' uniqueness mismatch: found {actual_idx['unique']}, expected {idx_props['unique']}."
                            )
                        if "partial_predicate" in idx_props:
                            pred = idx_props["partial_predicate"].lower()
                            if pred not in actual_idx["indexdef"]:
                                incompatibilities.append(
                                    f"Index '{idx_name}' missing partial predicate '{idx_props['partial_predicate']}'."
                                )
    except Exception as exc:
        incompatibilities.append(f"Failed to inspect indexes: {exc}")

    is_compatible = len(incompatibilities) == 0
    status = PreflightStatus.COMPATIBLE if is_compatible else PreflightStatus.INCOMPATIBLE

    return PreflightReport(
        status=status,
        target_description="",
        is_compatible=is_compatible,
        alembic_head=actual_alembic_head,
        missing_extensions=missing_extensions,
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        missing_constraints=missing_constraints,
        missing_indexes=missing_indexes,
        incompatibilities=incompatibilities,
    )


async def run_test_database_preflight(
    candidate_url: Optional[str] = None,
) -> PreflightReport:
    """
    Main programmatic entry point for safe, read-only test database schema preflight.

    Safety lifecycle:
    1. Rejects execution if ENVIRONMENT == 'production' or settings.is_production().
    2. Validates test URL via settings.validate_test_database_url().
    3. Fails closed without falling back to DATABASE_URL.
    4. Creates an isolated test engine lazily via eaoc.db.test_session.
    5. Begins a PostgreSQL transaction and explicitly runs 'SET TRANSACTION READ ONLY;'.
    6. Executes non-destructive catalog queries and checks against migrations 0001-0003.
    7. Disposes engine in a guaranteed finally block.
    8. Returns structured PreflightReport with credentials redacted.
    """
    settings = get_settings()

    raw_target = candidate_url if candidate_url is not None else settings.TEST_DATABASE_URL
    target_desc = _get_safe_target_description(raw_target)

    # 1. Environment Gate: strictly refuse to run against production
    if settings.is_production() or (settings.ENVIRONMENT and settings.ENVIRONMENT.lower() == "production"):
        logger.error("Preflight blocked: Refusing to run in production environment.")
        return PreflightReport(
            status=PreflightStatus.SAFETY_BLOCKED,
            target_description=target_desc,
            is_compatible=False,
            error_message="Preflight execution blocked: running in production environment is prohibited.",
        )

    # 2. Safety Validation Gate: check test URL validity, loopback aliases, driver normalization, primary collision
    is_safe, diag_msg = settings.validate_test_database_url(candidate_url=raw_target)
    if not is_safe:
        logger.warning(f"Preflight safety validation failed: {diag_msg}")
        return PreflightReport(
            status=PreflightStatus.SAFETY_BLOCKED,
            target_description=target_desc,
            is_compatible=False,
            error_message=f"Preflight target rejected by safety validation: {diag_msg}",
        )

    # 3. Connection and Inspection Scope
    test_engine: Optional[AsyncEngine] = None
    try:
        test_engine = create_test_async_engine(candidate_url=raw_target)

        async with test_engine.connect() as conn:
            # Enforce genuine PostgreSQL read-only transaction mode
            async with conn.begin():
                await conn.execute(text("SET TRANSACTION READ ONLY;"))
                report = await inspect_catalog(conn)
                report.target_description = target_desc
                return report

    except TestDatabaseConfigurationError as exc:
        return PreflightReport(
            status=PreflightStatus.SAFETY_BLOCKED,
            target_description=target_desc,
            is_compatible=False,
            error_message=str(exc),
        )
    except Exception as exc:
        logger.error(f"Test database schema preflight failed during connection/inspection: {exc}")
        return PreflightReport(
            status=PreflightStatus.INSPECTION_FAILED,
            target_description=target_desc,
            is_compatible=False,
            error_message=f"Inspection failed: {type(exc).__name__}: {exc}",
        )
    finally:
        if test_engine is not None:
            await dispose_test_engine(test_engine)


if __name__ == "__main__":
    import asyncio
    import json
    import sys

    async def main() -> int:
        rep = await run_test_database_preflight()
        print(json.dumps(rep.to_dict(), indent=2))
        return 0 if rep.is_compatible else 1

    sys.exit(asyncio.run(main()))
