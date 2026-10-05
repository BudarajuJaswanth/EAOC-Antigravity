"""
EAOC Centralized Database Connection Layer
SQLAlchemy 2.0 AsyncEngine + asyncpg session management for Supabase PostgreSQL.
Preserves caller-controlled transaction boundaries (auto-rollback on exception, NO auto-commit).
"""

from typing import AsyncGenerator, Optional
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from eaoc.config import get_settings


def create_eaoc_async_engine() -> Optional[AsyncEngine]:
    """
    Creates and returns a SQLAlchemy AsyncEngine connected to Supabase PostgreSQL.
    Returns None if database credentials are not configured.
    """
    settings = get_settings()
    if not settings.is_db_configured() or not settings.DATABASE_URL:
        return None

    # Ensure postgresql+asyncpg driver prefix is present
    db_url = settings.DATABASE_URL
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    return create_async_engine(
        db_url,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_recycle=settings.DB_POOL_RECYCLE,
        pool_pre_ping=True,
        echo=False,  # Never log SQL parameters containing credentials
    )


# Module-level engine and session factory instances
engine: Optional[AsyncEngine] = create_eaoc_async_engine()

AsyncSessionFactory: Optional[async_sessionmaker[AsyncSession]] = (
    async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    if engine
    else None
)


async def get_async_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Reusable async database session dependency/context-manager.
    Yields an AsyncSession with caller-controlled transaction boundaries.
    
    Behavior:
    - Automatically performs rollback if an exception occurs during the block.
    - Does NOT automatically commit on exit; the calling service explicitly calls `await session.commit()`.
    """
    if AsyncSessionFactory is None:
        raise RuntimeError(
            "Supabase database connection is not configured. "
            "Please set a valid DATABASE_URL in your .env file."
        )

    async with AsyncSessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
