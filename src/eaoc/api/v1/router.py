"""
EAOC API v1 Root Router
Aggregates versioned v1 API endpoints.
"""

from fastapi import APIRouter
from eaoc.api.v1.endpoints import approvals, system, workflows

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(system.router)
api_v1_router.include_router(workflows.router)
api_v1_router.include_router(approvals.router)
