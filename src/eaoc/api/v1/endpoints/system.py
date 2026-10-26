"""
EAOC API v1 System Information Endpoint
Returns safe system metadata for application versioning and runtime configuration.
"""

from fastapi import APIRouter
from eaoc.schemas.health import SystemInfoResponse
from eaoc.services.health import get_health_service

router = APIRouter(prefix="/system", tags=["System Information"])


@router.get("/info", response_model=SystemInfoResponse, summary="Get system metadata")
async def get_system_info() -> SystemInfoResponse:
    """Returns safe application system information without exposing secrets or credentials."""
    service = get_health_service()
    return service.get_system_info()
