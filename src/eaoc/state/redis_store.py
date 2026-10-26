"""
EAOC Redis Workflow State Store
Concrete implementation of IWorkflowStateStore using async Redis.
Key pattern: eaoc:workflow:{workflow_id}
Enforces atomic creation (SET NX EX), exact Part 1.7 contract semantics, and secret-safe error handling.
"""

import logging
from typing import Optional
import redis.asyncio as aioredis
from redis.asyncio import Redis
from eaoc.config import get_settings
from eaoc.observability import redact_sensitive_data
from eaoc.schemas.domain import WorkflowState
from eaoc.state.interfaces import IWorkflowStateStore

logger = logging.getLogger("eaoc.state.redis")

_redis_client_instance: Optional[Redis] = None


def get_redis_client() -> Optional[Redis]:
    """
    Returns global async Redis client instance.
    Returns None if Redis is not configured or disabled in Settings.
    """
    global _redis_client_instance
    settings = get_settings()

    if not settings.is_redis_configured() or not settings.REDIS_URL:
        return None

    if _redis_client_instance is None:
        _redis_client_instance = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
        )
    return _redis_client_instance


async def close_redis_client() -> None:
    """Cleanly closes global async Redis client connection pool."""
    global _redis_client_instance
    if _redis_client_instance is not None:
        try:
            await _redis_client_instance.aclose()
            logger.info("Async Redis client connection pool closed cleanly.")
        except Exception as exc:
            logger.warning(f"Error closing Redis client: {redact_sensitive_data(exc)}")
        finally:
            _redis_client_instance = None


class RedisWorkflowStateStore(IWorkflowStateStore):
    """
    Async Redis-backed workflow state store implementing IWorkflowStateStore.
    """

    def __init__(self, client: Optional[Redis] = None, ttl_seconds: Optional[int] = None):
        self._custom_client = client
        self._custom_ttl = ttl_seconds

    def _get_client(self) -> Redis:
        """Resolves active Redis client or raises RuntimeError if unconfigured."""
        client = self._custom_client or get_redis_client()
        if client is None:
            raise RuntimeError(
                "Redis state store is unconfigured or disabled. "
                "Enable REDIS_ENABLED=true and set a valid REDIS_URL."
            )
        return client

    def _get_ttl(self) -> int:
        """Resolves TTL in seconds."""
        if self._custom_ttl is not None:
            return self._custom_ttl
        return get_settings().REDIS_TTL_SECONDS

    def _get_key(self, workflow_id: str) -> str:
        """Returns deterministic Redis key for workflow state."""
        return f"eaoc:workflow:{workflow_id}"

    async def create_state(self, state: WorkflowState) -> WorkflowState:
        """
        Creates a new workflow state atomically using SET key val NX EX ttl.
        Raises ValueError if workflow_id already exists.
        """
        client = self._get_client()
        key = self._get_key(state.workflow_id)
        ttl = self._get_ttl()
        json_payload = state.model_dump_json()

        try:
            # Atomic set-if-not-exists with expiration
            success = await client.set(key, json_payload, nx=True, ex=ttl)
            if not success:
                raise ValueError(
                    f"Workflow state with ID '{state.workflow_id}' already exists."
                )
            return state
        except ValueError:
            raise
        except Exception as exc:
            safe_err = redact_sensitive_data(exc)
            logger.error(f"Redis create_state failed for '{state.workflow_id}': {safe_err}")
            raise RuntimeError(f"Redis state store operation failed: {safe_err}") from exc

    async def get_state(self, workflow_id: str) -> Optional[WorkflowState]:
        """
        Retrieves workflow state by workflow_id.
        Returns WorkflowState if present; None if absent.
        """
        client = self._get_client()
        key = self._get_key(workflow_id)

        try:
            raw_json = await client.get(key)
            if raw_json is None:
                return None
            return WorkflowState.model_validate_json(raw_json)
        except Exception as exc:
            if "model_validate" in str(exc) or "validation" in str(exc).lower():
                safe_err = redact_sensitive_data(exc)
                logger.error(f"Malformed WorkflowState JSON for '{workflow_id}': {safe_err}")
                raise ValueError(f"Stored workflow state for ID '{workflow_id}' is malformed.") from exc
            safe_err = redact_sensitive_data(exc)
            logger.error(f"Redis get_state failed for '{workflow_id}': {safe_err}")
            raise RuntimeError(f"Redis state store operation failed: {safe_err}") from exc

    async def update_state(self, state: WorkflowState) -> WorkflowState:
        """
        Updates an existing workflow state.
        Raises KeyError if workflow_id does not exist in store.
        """
        client = self._get_client()
        key = self._get_key(state.workflow_id)
        ttl = self._get_ttl()
        json_payload = state.model_dump_json()

        try:
            exists = await client.exists(key)
            if not exists:
                raise KeyError(
                    f"Workflow state with ID '{state.workflow_id}' does not exist."
                )
            await client.set(key, json_payload, ex=ttl)
            return state
        except KeyError:
            raise
        except Exception as exc:
            safe_err = redact_sensitive_data(exc)
            logger.error(f"Redis update_state failed for '{state.workflow_id}': {safe_err}")
            raise RuntimeError(f"Redis state store operation failed: {safe_err}") from exc

    async def delete_state(self, workflow_id: str) -> bool:
        """
        Removes workflow state by workflow_id.
        Returns True if found and deleted; False if absent.
        """
        client = self._get_client()
        key = self._get_key(workflow_id)

        try:
            res = await client.delete(key)
            return bool(res > 0)
        except Exception as exc:
            safe_err = redact_sensitive_data(exc)
            logger.error(f"Redis delete_state failed for '{workflow_id}': {safe_err}")
            raise RuntimeError(f"Redis state store operation failed: {safe_err}") from exc

    async def exists(self, workflow_id: str) -> bool:
        """
        Checks whether a workflow state with workflow_id exists in store.
        Returns True if present; False otherwise.
        """
        client = self._get_client()
        key = self._get_key(workflow_id)

        try:
            res = await client.exists(key)
            return bool(res > 0)
        except Exception as exc:
            safe_err = redact_sensitive_data(exc)
            logger.error(f"Redis exists check failed for '{workflow_id}': {safe_err}")
            raise RuntimeError(f"Redis state store operation failed: {safe_err}") from exc
