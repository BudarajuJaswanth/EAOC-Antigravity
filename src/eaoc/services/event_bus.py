"""
EAOC Workflow Event Bus Subsystem (Part 2.17)
Provides storage-agnostic, tenant-isolated real-time event publishing and subscription
over Redis Pub/Sub, with in-memory implementations for hermetic unit testing.
Enforces automatic data sanitization and strict non-blocking guarantees.
"""

from abc import ABC, abstractmethod
import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, Optional, Set
from redis.asyncio import Redis

from eaoc.config import get_settings
from eaoc.observability import redact_sensitive_data
from eaoc.schemas.events import WorkflowStreamEvent
from eaoc.state.redis_store import get_redis_client

logger = logging.getLogger("eaoc.services.event_bus")


def get_channel_name(tenant_id: str, workflow_id: str) -> str:
    """Computes deterministic, tenant-isolated Redis channel name."""
    clean_tenant = tenant_id.strip() if tenant_id else "default"
    clean_wf = workflow_id.strip()
    return f"eaoc:events:tenant:{clean_tenant}:wf:{clean_wf}"


class IWorkflowEventBus(ABC):
    """Abstract interface for workflow event publishing and subscription."""

    @abstractmethod
    async def publish(self, event: WorkflowStreamEvent) -> None:
        """
        Publishes a stream event to the tenant-isolated channel.
        Must be non-blocking and fail-safe (never raise unhandled exceptions to callers).
        """
        pass

    @abstractmethod
    async def subscribe(
        self, tenant_id: str, workflow_id: str
    ) -> AsyncIterator[WorkflowStreamEvent]:
        """
        Subscribes to live events for a given workflow and tenant.
        Yields WorkflowStreamEvent instances until closed or cancelled.
        """
        pass


class RedisWorkflowEventBus(IWorkflowEventBus):
    """
    Concrete event bus leveraging Redis Pub/Sub for ephemeral live event distribution.
    """

    def __init__(self, redis_client: Optional[Redis] = None):
        self._custom_client = redis_client

    def _get_client(self) -> Optional[Redis]:
        return self._custom_client or get_redis_client()

    async def publish(self, event: WorkflowStreamEvent) -> None:
        """
        Publishes event to Redis Pub/Sub with automatic payload sanitization.
        Logs any errors without propagating exceptions to prevent execution disruption.
        """
        client = self._get_client()
        if client is None:
            logger.debug("Redis client unconfigured; skipping live event publication.")
            return

        channel = get_channel_name(event.tenant_id, event.workflow_id)
        try:
            # Enforce data redaction prior to wire serialization
            event_dict = event.model_dump()
            event_dict["payload"] = redact_sensitive_data(event_dict.get("payload", {}))
            wire_json = json.dumps(event_dict, separators=(",", ":"))
            await client.publish(channel, wire_json)
        except Exception as exc:
            safe_err = redact_sensitive_data(str(exc))
            logger.warning(
                f"Failed to publish event '{event.event_id}' to channel '{channel}': {safe_err}"
            )

    async def subscribe(
        self, tenant_id: str, workflow_id: str
    ) -> AsyncIterator[WorkflowStreamEvent]:
        """
        Subscribes to tenant-scoped Redis Pub/Sub channel and yields parsed stream events.
        Guarantees clean unsubscribe and pubsub closure on exit.
        """
        client = self._get_client()
        if client is None:
            raise RuntimeError("Redis client is not configured or disabled.")

        channel = get_channel_name(tenant_id, workflow_id)
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)
        logger.debug(f"Subscribed to Redis channel: {channel}")

        try:
            async for message in pubsub.listen():
                if message is None:
                    continue
                msg_type = message.get("type")
                if msg_type != "message":
                    continue
                raw_data = message.get("data")
                if not raw_data:
                    continue
                try:
                    if isinstance(raw_data, bytes):
                        raw_data = raw_data.decode("utf-8")
                    parsed = json.loads(raw_data)
                    event = WorkflowStreamEvent.model_validate(parsed)
                    yield event
                except Exception as parse_exc:
                    logger.warning(
                        f"Malformed message on channel '{channel}': {redact_sensitive_data(str(parse_exc))}"
                    )
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
                logger.debug(f"Unsubscribed and closed Redis channel: {channel}")
            except Exception as close_exc:
                logger.warning(f"Error during pubsub teardown for '{channel}': {close_exc}")


class InMemoryWorkflowEventBus(IWorkflowEventBus):
    """
    Hermetic in-memory event bus supporting concurrent fan-out for unit testing.
    """

    def __init__(self):
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        self.published_events: list[WorkflowStreamEvent] = []

    async def publish(self, event: WorkflowStreamEvent) -> None:
        """Fans out published event to all active in-memory subscriber queues."""
        channel = get_channel_name(event.tenant_id, event.workflow_id)
        # Apply redaction
        clean_event = WorkflowStreamEvent.model_validate(event.model_dump())
        clean_event.payload = redact_sensitive_data(clean_event.payload)
        self.published_events.append(clean_event)

        async with self._lock:
            queues = list(self._subscribers.get(channel, set()))

        for q in queues:
            try:
                q.put_nowait(clean_event)
            except asyncio.QueueFull:
                pass

    async def subscribe(
        self, tenant_id: str, workflow_id: str
    ) -> AsyncIterator[WorkflowStreamEvent]:
        """Registers a queue subscriber and yields events until generator cancelled."""
        channel = get_channel_name(tenant_id, workflow_id)
        queue: asyncio.Queue[WorkflowStreamEvent] = asyncio.Queue(maxsize=512)

        async with self._lock:
            if channel not in self._subscribers:
                self._subscribers[channel] = set()
            self._subscribers[channel].add(queue)

        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            async with self._lock:
                if channel in self._subscribers:
                    self._subscribers[channel].discard(queue)
                    if not self._subscribers[channel]:
                        del self._subscribers[channel]
