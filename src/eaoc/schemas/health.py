"""
EAOC Health & System Information Pydantic Schemas
Defines structured responses for infrastructure health, liveness, readiness,
and system information endpoints.
"""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    BLOCKED = "BLOCKED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class ProviderCategory(str, Enum):
    LOCAL = "LOCAL"
    CLOUD = "CLOUD"


class DatabaseHealth(BaseModel):
    status: HealthStatus
    details: str
    pgvector_status: Optional[str] = None
    latency_ms: Optional[float] = None


class RedisHealth(BaseModel):
    status: HealthStatus
    details: str


class ModelProviderInfo(BaseModel):
    provider_id: str
    provider_name: str
    category: ProviderCategory
    status: HealthStatus
    details: str


class ModelProviderHealth(BaseModel):
    status: HealthStatus
    registered_providers: List[ModelProviderInfo]
    details: str


class SystemInfoResponse(BaseModel):
    app_name: str = Field(..., example="Enterprise AI Operations Center (EAOC)")
    version: str = Field(..., example="1.0.0")
    environment: str = Field(..., example="development")
    api_version: str = Field(..., example="v1")


class LivenessResponse(BaseModel):
    status: HealthStatus = HealthStatus.HEALTHY
    process_uptime_seconds: float


class ReadinessResponse(BaseModel):
    status: HealthStatus
    components: Dict[str, HealthStatus]


class OverallHealthResponse(BaseModel):
    status: HealthStatus
    timestamp: str
    version: str = "1.0.0"
    components: Dict[str, Any]
