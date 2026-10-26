"""
EAOC FastAPI Application Factory
Provides a structured app factory with async lifespan management, configuration-driven CORS,
observability middleware, and global error handling.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from eaoc.api.endpoints import health as health_endpoints
from eaoc.api.middlewares import ObservabilityMiddleware, register_exception_handlers
from eaoc.api.v1.router import api_v1_router
from eaoc.config import get_settings
from eaoc.db.session import engine
from eaoc.observability import configure_logging, get_logger

logger = get_logger("eaoc.api.factory")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI async lifespan context manager.
    Handles startup logging & resource initialization, followed by clean shutdown.
    """
    settings = get_settings()
    logger.info(
        f"Starting EAOC Core Application in [{settings.ENVIRONMENT}] mode...",
        extra={"event_name": "app_startup", "environment": settings.ENVIRONMENT},
    )
    yield
    logger.info(
        "Shutting down EAOC Core Application...",
        extra={"event_name": "app_shutdown"},
    )
    if engine:
        await engine.dispose()
        logger.info(
            "Supabase database engine disposed cleanly.",
            extra={"event_name": "db_engine_disposed"},
        )
    try:
        from eaoc.state.redis_store import close_redis_client
        await close_redis_client()
    except Exception as exc:
        logger.warning(f"Redis client shutdown warning: {exc}")


def create_app() -> FastAPI:
    """
    FastAPI Application Factory.
    Creates and configures the EAOC application instance.
    """
    # Initialize application logging configuration
    configure_logging()
    settings = get_settings()

    app = FastAPI(
        title="Enterprise AI Operations Center (EAOC)",
        description="Core System Services & Infrastructure Health Foundation",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Observability & Request Tracing Middleware
    app.add_middleware(ObservabilityMiddleware)

    # Configuration-driven CORS middleware setup
    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    # Register global exception handlers for safe diagnostic responses
    register_exception_handlers(app)

    # Mount canonical infrastructure health endpoints (/health, /health/live, /health/ready)
    app.include_router(health_endpoints.router)

    # Mount versioned API router (/api/v1/system/info)
    app.include_router(api_v1_router)

    return app
