"""
EAOC Async Alembic Environment Configuration
Loads DATABASE_URL dynamically from eaoc.config.get_settings()
and metadata from eaoc.db.base.Base.metadata.
"""

import asyncio
import os
from logging.config import fileConfig
from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from eaoc.config import get_settings
from eaoc.db.base import Base
from eaoc.db.models.approval import ApprovalRequestModel  # Registers model in Base.metadata
from eaoc.db.models.workflow import WorkflowModel, WorkflowTaskModel  # Registers recovery models

# Alembic Config object
config = getattr(context, "config", None)

if config and config.config_file_name:
    fileConfig(config.config_file_name)

# Set target metadata for 'autogenerate' support
target_metadata = Base.metadata


def get_url() -> str:
    """
    Retrieves database URL for Alembic migrations.

    Target Selection:
    - If ALEMBIC_TARGET_TEST_DB is absent or empty:
      Default production behavior: selects settings.DATABASE_URL.
    - If ALEMBIC_TARGET_TEST_DB is exactly "1":
      Explicit test-target opt-in: validates and selects settings.TEST_DATABASE_URL.
      Fails closed if in production, missing TEST_DATABASE_URL, or if validation fails.
      Never falls back to DATABASE_URL.
    - If ALEMBIC_TARGET_TEST_DB is present with any other non-empty value:
      Fails closed without choosing a target.
    """
    settings = get_settings()
    flag_val = os.environ.get("ALEMBIC_TARGET_TEST_DB", "").strip()

    if flag_val:
        if flag_val != "1":
            raise RuntimeError(
                f"Invalid value '{flag_val}' for ALEMBIC_TARGET_TEST_DB. "
                "Only exactly '1' is accepted for test database migration opt-in."
            )

        # Explicit test-target mode (ALEMBIC_TARGET_TEST_DB=1)
        if settings.is_production() or (settings.ENVIRONMENT and settings.ENVIRONMENT.lower() == "production"):
            raise RuntimeError(
                "Alembic test target migration mode (ALEMBIC_TARGET_TEST_DB=1) "
                "is strictly prohibited in a production environment."
            )

        if not settings.TEST_DATABASE_URL or not settings.TEST_DATABASE_URL.strip():
            raise RuntimeError(
                "ALEMBIC_TARGET_TEST_DB=1 is set, but TEST_DATABASE_URL is missing or empty. "
                "Migrations will not fall back to DATABASE_URL."
            )

        is_safe, diag_msg = settings.validate_test_database_url()
        if not is_safe:
            raise RuntimeError(
                f"Alembic test target rejected by safety validation: {diag_msg}"
            )

        raw_url = settings.TEST_DATABASE_URL.strip()
    else:
        # Default production path (existing behavior)
        if not settings.is_db_configured() or not settings.DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not configured in .env file.")
        raw_url = settings.DATABASE_URL.strip()

    # Asyncpg driver normalization for PostgreSQL URLs
    if raw_url.startswith("postgresql://"):
        raw_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif raw_url.startswith("postgres://"):
        raw_url = raw_url.replace("postgres://", "postgresql+asyncpg://", 1)

    return raw_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode using AsyncEngine."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run online migrations using asyncio event loop."""
    asyncio.run(run_async_migrations())


if config is not None:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
