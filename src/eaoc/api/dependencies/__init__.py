"""
EAOC API Dependencies Package
Exposes reusable FastAPI route dependencies for authentication, state stores, and context.
"""

from eaoc.api.dependencies.auth import get_trusted_identity, require_roles

__all__ = [
    "get_trusted_identity",
    "require_roles",
]
