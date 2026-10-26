"""
EAOC Centralized Health Check Service
Implements infrastructure health, liveness, readiness, and system information logic.
Integrates with Supabase PostgreSQL (Part 1.3), Redis placeholder, and model-agnostic
provider health abstraction.
"""

import time
from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy import text
from eaoc.config import get_settings
from eaoc.db.session import create_eaoc_async_engine
from eaoc.schemas.health import (
    DatabaseHealth,
    HealthStatus,
    LivenessResponse,
    ModelProviderHealth,
    ModelProviderInfo,
    OverallHealthResponse,
    ProviderCategory,
    ReadinessResponse,
    RedisHealth,
    SystemInfoResponse,
)

_PROCESS_START_TIME = time.time()


class ModelProviderRegistry:
    """
    Provider-neutral registry holding metadata for approved model providers.
    Supports future dynamic provider registration without modifying HealthService.
    """

    def __init__(self):
        self._providers: List[ModelProviderInfo] = [
            ModelProviderInfo(
                provider_id="lm_studio",
                provider_name="LM Studio",
                category=ProviderCategory.LOCAL,
                status=HealthStatus.NOT_CONFIGURED,
                details="Local model provider abstraction. Integration scheduled for future part.",
            ),
            ModelProviderInfo(
                provider_id="ollama",
                provider_name="Ollama",
                category=ProviderCategory.LOCAL,
                status=HealthStatus.NOT_CONFIGURED,
                details="Local model provider abstraction. Integration scheduled for future part.",
            ),
            ModelProviderInfo(
                provider_id="gemini",
                provider_name="Gemini",
                category=ProviderCategory.CLOUD,
                status=HealthStatus.NOT_CONFIGURED,
                details="Cloud model provider abstraction. Integration scheduled for future part.",
            ),
        ]

    def register_provider(self, provider_info: ModelProviderInfo) -> None:
        """Register a new approved model provider cleanly."""
        self._providers = [
            p for p in self._providers if p.provider_id != provider_info.provider_id
        ]
        self._providers.append(provider_info)

    def get_all_providers(self) -> List[ModelProviderInfo]:
        """Return all registered model providers."""
        return self._providers


class HealthService:
    """Centralized service for checking application and dependency health."""

    def __init__(self):
        self.provider_registry = ModelProviderRegistry()

    async def check_database_health(self) -> DatabaseHealth:
        """
        Determines Supabase PostgreSQL health.
        If DATABASE_URL is missing, returns NOT_CONFIGURED.
        If configured, executes a minimal 'SELECT 1' query and verifies pgvector state.
        Never exposes credentials in logs or responses.
        """
        settings = get_settings()
        if not settings.is_db_configured() or not settings.DATABASE_URL:
            return DatabaseHealth(
                status=HealthStatus.NOT_CONFIGURED,
                details="Supabase DATABASE_URL is not configured.",
                pgvector_status="BLOCKED",
                latency_ms=None,
            )

        engine = create_eaoc_async_engine()
        if engine is None:
            return DatabaseHealth(
                status=HealthStatus.NOT_CONFIGURED,
                details="Failed to create engine (invalid or unconfigured DATABASE_URL).",
                pgvector_status="BLOCKED",
                latency_ms=None,
            )

        start_t = time.perf_counter()
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                elapsed_ms = round((time.perf_counter() - start_t) * 1000, 2)

                # Check pgvector extension state
                ext_res = await conn.execute(
                    text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
                )
                ext_row = ext_res.fetchone()
                pgv_state = (
                    "ENABLED" if ext_row else "AVAILABLE_IN_SUPABASE"
                )

            await engine.dispose()
            return DatabaseHealth(
                status=HealthStatus.HEALTHY,
                details="Connected successfully to Supabase PostgreSQL.",
                pgvector_status=pgv_state,
                latency_ms=elapsed_ms,
            )
        except Exception as exc:
            await engine.dispose()
            # Mask any credentials from exception detail
            raw_err = str(exc)
            safe_err = raw_err.split("@")[-1] if "@" in raw_err else raw_err
            return DatabaseHealth(
                status=HealthStatus.UNHEALTHY,
                details=f"Supabase PostgreSQL connection failed: {safe_err[:120]}",
                pgvector_status="BLOCKED",
                latency_ms=None,
            )

    async def check_redis_health(self) -> RedisHealth:
        """
        Determines Redis state store health.
        If Redis is disabled or unconfigured, returns NOT_CONFIGURED.
        If configured, attempts a live async PING.
        Sanitizes all error messages to protect credentials.
        """
        settings = get_settings()
        if not settings.is_redis_configured() or not settings.REDIS_URL:
            return RedisHealth(
                status=HealthStatus.NOT_CONFIGURED,
                details="Redis state store is unconfigured or disabled.",
            )

        try:
            from eaoc.state.redis_store import get_redis_client
            client = get_redis_client()
            if client is None:
                return RedisHealth(
                    status=HealthStatus.NOT_CONFIGURED,
                    details="Redis client initialization failed.",
                )
            pong = await client.ping()
            if pong:
                return RedisHealth(
                    status=HealthStatus.HEALTHY,
                    details="Connected successfully to Redis.",
                )
            return RedisHealth(
                status=HealthStatus.UNHEALTHY,
                details="Redis ping did not return expected response.",
            )
        except Exception as exc:
            from eaoc.observability import redact_sensitive_data
            safe_err = redact_sensitive_data(exc)
            return RedisHealth(
                status=HealthStatus.UNHEALTHY,
                details=f"Redis ping failed: {str(safe_err)[:120]}",
            )

    def check_model_provider_health(self) -> ModelProviderHealth:
        """
        Returns provider-neutral model health abstraction.
        Inspects static configuration state without making network or LLM calls.
        """
        providers = self.provider_registry.get_all_providers()
        settings = get_settings()
        active_cfg = settings.get_active_provider_config()
        
        status = HealthStatus.HEALTHY if active_cfg.get("is_configured") else HealthStatus.NOT_CONFIGURED

        return ModelProviderHealth(
            status=status,
            registered_providers=providers,
            details=f"Active provider '{active_cfg.get('provider')}': {active_cfg.get('details')}",
        )

    def get_system_info(self) -> SystemInfoResponse:
        """Returns safe application system information without exposing secrets."""
        settings = get_settings()
        return SystemInfoResponse(
            app_name="Enterprise AI Operations Center (EAOC)",
            version="1.0.0",
            environment=settings.ENVIRONMENT,
            api_version="v1",
        )

    def get_liveness(self) -> LivenessResponse:
        """Returns application process liveness probe response."""
        uptime = round(time.time() - _PROCESS_START_TIME, 2)
        return LivenessResponse(
            status=HealthStatus.HEALTHY, process_uptime_seconds=uptime
        )

    async def get_readiness(self) -> ReadinessResponse:
        """
        Evaluates application readiness.
        Application is ready if app process is alive and essential configured dependencies respond.
        Unconfigured future dependencies do not block development readiness.
        """
        db_health = await self.check_database_health()
        redis_health = await self.check_redis_health()
        
        components = {
            "application": HealthStatus.HEALTHY,
            "database": db_health.status,
            "redis": redis_health.status,
            "model_providers": HealthStatus.NOT_CONFIGURED,
        }

        # If DB or configured Redis connection fails, report overall UNHEALTHY/DEGRADED
        if db_health.status == HealthStatus.UNHEALTHY or redis_health.status == HealthStatus.UNHEALTHY:
            overall_status = HealthStatus.UNHEALTHY
        elif db_health.status == HealthStatus.DEGRADED or redis_health.status == HealthStatus.DEGRADED:
            overall_status = HealthStatus.DEGRADED
        else:
            overall_status = HealthStatus.HEALTHY

        return ReadinessResponse(status=overall_status, components=components)

    async def get_overall_health(self) -> OverallHealthResponse:
        """Aggregates overall application and dependency health status."""
        db_health = await self.check_database_health()
        redis_health = await self.check_redis_health()
        model_health = self.check_model_provider_health()

        components = {
            "database": db_health.model_dump(),
            "redis": redis_health.model_dump(),
            "model_providers": model_health.model_dump(),
        }

        if db_health.status == HealthStatus.UNHEALTHY or redis_health.status == HealthStatus.UNHEALTHY:
            overall_status = HealthStatus.UNHEALTHY
        elif db_health.status == HealthStatus.DEGRADED or redis_health.status == HealthStatus.DEGRADED:
            overall_status = HealthStatus.DEGRADED
        else:
            overall_status = HealthStatus.HEALTHY

        now_utc = datetime.now(timezone.utc).isoformat()
        return OverallHealthResponse(
            status=overall_status,
            timestamp=now_utc,
            version="1.0.0",
            components=components,
        )


_health_service_instance: Optional[HealthService] = None


def get_health_service() -> HealthService:
    """Singleton getter for HealthService."""
    global _health_service_instance
    if _health_service_instance is None:
        _health_service_instance = HealthService()
    return _health_service_instance
