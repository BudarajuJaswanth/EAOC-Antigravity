"""
EAOC Canonical Infrastructure Health Endpoints
Exposes root infrastructure health endpoints:
GET /health
GET /health/live
GET /health/ready
"""

from fastapi import APIRouter, Response, status
from eaoc.schemas.health import (
    HealthStatus,
    LivenessResponse,
    OverallHealthResponse,
    ReadinessResponse,
)
from eaoc.services.health import get_health_service

router = APIRouter(prefix="/health", tags=["Infrastructure Health"])


@router.get("", response_model=OverallHealthResponse, summary="Overall health status")
async def get_overall_health() -> OverallHealthResponse:
    """Returns overall application and dependency health status."""
    service = get_health_service()
    return await service.get_overall_health()


@router.get("/live", response_model=LivenessResponse, summary="Liveness probe")
async def get_liveness() -> LivenessResponse:
    """
    Liveness probe for process execution state.
    Returns HTTP 200 HEALTHY as long as the application process is running.
    """
    service = get_health_service()
    return service.get_liveness()


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness probe")
async def get_readiness(response: Response) -> ReadinessResponse:
    """
    Readiness probe for dependency responsiveness.
    Returns HTTP 200 when essential configured dependencies are responsive.
    Returns HTTP 503 Service Unavailable if an essential configured dependency fails.
    """
    service = get_health_service()
    readiness = await service.get_readiness()

    if readiness.status == HealthStatus.UNHEALTHY:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return readiness
