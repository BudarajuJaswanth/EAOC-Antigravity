"""
EAOC FastAPI Observability Middleware
Extracts or generates tracing IDs (request_id, correlation_id), propagates context,
measures HTTP request durations, logs structured request events, and injects response headers.
"""

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from eaoc.observability.context import (
    clear_tracing_context,
    generate_id,
    set_correlation_id,
    set_request_id,
)
from eaoc.observability.events import EVENT_REQUEST_COMPLETED, EVENT_REQUEST_STARTED
from eaoc.observability.logger import get_logger
from eaoc.observability.timing import Timer

logger = get_logger("eaoc.api.http")


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """
    FastAPI HTTP Middleware for request observability, context propagation, and timing.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        clear_tracing_context()

        # Extract or generate tracing IDs
        req_id = request.headers.get("X-Request-ID") or generate_id("req")
        corr_id = request.headers.get("X-Correlation-ID") or generate_id("corr")

        set_request_id(req_id)
        set_correlation_id(corr_id)

        method = request.method
        path = request.url.path

        # Log request started event
        logger.info(
            f"HTTP {method} {path} - Started",
            extra={
                "event_name": EVENT_REQUEST_STARTED,
                "http_method": method,
                "http_path": path,
            },
        )

        timer = Timer().start()
        try:
            response = await call_next(request)
            dur_ms = timer.stop()

            # Inject tracing headers into response
            response.headers["X-Request-ID"] = req_id
            response.headers["X-Correlation-ID"] = corr_id

            logger.info(
                f"HTTP {method} {path} - Completed {response.status_code} in {dur_ms}ms",
                extra={
                    "event_name": EVENT_REQUEST_COMPLETED,
                    "http_method": method,
                    "http_path": path,
                    "status_code": response.status_code,
                    "duration_ms": dur_ms,
                },
            )
            return response
        except Exception as exc:
            dur_ms = timer.stop()
            logger.error(
                f"HTTP {method} {path} - Exception encountered after {dur_ms}ms: {exc}",
                extra={
                    "event_name": "request_exception",
                    "http_method": method,
                    "http_path": path,
                    "duration_ms": dur_ms,
                    "error_type": exc.__class__.__name__,
                },
                exc_info=True,
            )
            raise
        finally:
            pass
