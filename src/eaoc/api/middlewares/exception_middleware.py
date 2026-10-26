"""
EAOC Global Exception Handler & Diagnostic Error Middleware
Provides safe API error responses while recording internal diagnostic logs with sanitized details.
"""

from datetime import datetime, timezone
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from eaoc.observability.context import get_request_id
from eaoc.observability.logger import get_logger
from eaoc.observability.redaction import redact_sensitive_data

logger = get_logger("eaoc.api.exceptions")


def register_exception_handlers(app: FastAPI) -> None:
    """
    Registers global exception handlers on the FastAPI application instance.
    """

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        req_id = get_request_id() or "unknown"
        safe_err_msg = redact_sensitive_data(str(exc))
        err_type = exc.__class__.__name__

        # Internal diagnostic logging with sanitized exception trace
        logger.error(
            f"Unhandled Exception [{err_type}]: {safe_err_msg}",
            extra={
                "event_name": "unhandled_exception",
                "error_type": err_type,
                "error_code": "INTERNAL_SERVER_ERROR",
                "request_id": req_id,
            },
            exc_info=True,
        )

        # Safe client response (NO stack traces, NO credentials exposed)
        now_utc = datetime.now(timezone.utc).isoformat()
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "An internal system error occurred. Please contact support.",
                    "request_id": req_id,
                    "timestamp": now_utc,
                }
            },
            headers={"X-Request-ID": req_id},
        )
