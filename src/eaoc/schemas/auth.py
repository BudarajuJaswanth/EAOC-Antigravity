"""
EAOC Authentication Schemas
Defines structured principal and identity models for authenticated sessions.
"""

from typing import Any, Dict, List
from pydantic import BaseModel, Field, field_validator


class AuthenticatedUser(BaseModel):
    """
    Cryptographically verified principal representation.
    Populated solely from validated JWT claims by get_trusted_identity.
    """
    user_id: str = Field(..., min_length=1, description="Verified subject/user identifier from JWT sub claim")
    tenant_id: str = Field(..., min_length=1, description="Verified tenant organization identifier from JWT tenant_id claim")
    roles: List[str] = Field(default_factory=list, description="Verified security and application roles from JWT roles claim")
    token_claims: Dict[str, Any] = Field(default_factory=dict, description="Raw validated token claims")

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("user_id must be a non-empty string.")
        return v.strip()

    @field_validator("tenant_id")
    @classmethod
    def validate_tenant_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("tenant_id must be a non-empty string.")
        return v.strip()
