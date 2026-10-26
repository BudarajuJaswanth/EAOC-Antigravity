"""
EAOC Isolated Test Database Session Helper
Provides test-only SQLAlchemy async engine and sessionmaker factories
targeting TEST_DATABASE_URL exclusively.

Invariants:
- Never falls back to DATABASE_URL or production credentials.
- Validates candidate URL via settings.validate_test_database_url() before engine instantiation.
- Side-effect-free at import time: no engine or connection is created on module load.
- Provides explicit lifecycle management and async disposal.
- Secret-safe: never prints or includes credentials/passwords in error messages.
"""

from typing import Any, Optional, Tuple
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from eaoc.config import get_settings


class TestDatabaseConfigurationError(ValueError):
    """
    Raised when TEST_DATABASE_URL is missing, unconfigured, or fails
    structural or collision safety checks.
    """
    pass


def create_test_async_engine(
    candidate_url: Optional[str] = None,
    **engine_kwargs: Any,
) -> AsyncEngine:
    """
    Creates and returns a dedicated SQLAlchemy AsyncEngine targeting TEST_DATABASE_URL.

    Safety behavior:
    - Never falls back to DATABASE_URL.
    - Fails closed if candidate_url (or settings.TEST_DATABASE_URL) fails safety validation.
    - Normalizes driver prefix to postgresql+asyncpg://.
    - Never logs passwords or query parameters.
    - Does not connect over the network at creation time (lazy connection pool).
    """
    settings = get_settings()

    target_url = candidate_url if candidate_url is not None else settings.TEST_DATABASE_URL

    is_valid, diag_msg = settings.validate_test_database_url(candidate_url=target_url)
    if not is_valid:
        raise TestDatabaseConfigurationError(
            f"Test database engine creation rejected: {diag_msg}"
        )

    url_str = str(target_url).strip()
    if url_str.startswith("postgresql://"):
        url_str = url_str.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif url_str.startswith("postgres://"):
        url_str = url_str.replace("postgres://", "postgresql+asyncpg://", 1)

    default_kwargs: dict[str, Any] = {
        "pool_pre_ping": True,
        "echo": False,
    }
    default_kwargs.update(engine_kwargs)

    return create_async_engine(url_str, **default_kwargs)


def create_test_session_factory(
    engine: Optional[AsyncEngine] = None,
    candidate_url: Optional[str] = None,
    **engine_kwargs: Any,
) -> Tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
    """
    Creates an async_sessionmaker[AsyncSession] bound to an isolated test AsyncEngine.
    Returns (session_factory, engine) so the caller can inject the session factory
    into repository constructors and later dispose the engine.
    """
    test_engine = engine or create_test_async_engine(candidate_url=candidate_url, **engine_kwargs)
    factory = async_sessionmaker(
        test_engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    return factory, test_engine


async def dispose_test_engine(engine: Optional[AsyncEngine]) -> None:
    """
    Disposes the provided test AsyncEngine and closes all pooled connections.
    Safe to call with None.
    """
    if engine is not None:
        await engine.dispose()


class TestSessionManager:
    """
    Manages the lifecycle of a dedicated test database engine and sessionmaker.
    Guarantees caller-controlled lazy engine instantiation and clean async teardown.
    """

    def __init__(
        self,
        candidate_url: Optional[str] = None,
        **engine_kwargs: Any,
    ):
        self._candidate_url = candidate_url
        self._engine_kwargs = engine_kwargs
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker[AsyncSession]] = None

    def get_engine(self) -> AsyncEngine:
        """Lazily creates and returns the test AsyncEngine."""
        if self._engine is None:
            self._engine = create_test_async_engine(
                candidate_url=self._candidate_url,
                **self._engine_kwargs,
            )
        return self._engine

    def get_session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Lazily creates and returns the test async_sessionmaker."""
        if self._session_factory is None:
            engine = self.get_engine()
            self._session_factory = async_sessionmaker(
                engine,
                expire_on_commit=False,
                class_=AsyncSession,
            )
        return self._session_factory

    async def dispose(self) -> None:
        """Asynchronously disposes the managed engine and resets state."""
        if self._engine is not None:
            await dispose_test_engine(self._engine)
            self._engine = None
            self._session_factory = None
