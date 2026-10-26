"""
EAOC API Middlewares Package
"""

from eaoc.api.middlewares.exception_middleware import register_exception_handlers
from eaoc.api.middlewares.logging_middleware import ObservabilityMiddleware

__all__ = ["ObservabilityMiddleware", "register_exception_handlers"]
