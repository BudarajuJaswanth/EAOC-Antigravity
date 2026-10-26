"""
EAOC Authentication & RBAC Dependencies
Provides cryptographic JWT verification and role-based access control (RBAC) enforcement
for FastAPI route handlers. Fails closed on invalid credentials, absent configuration,
or unauthorized roles. Never trusts caller-controlled spoofing headers.
"""

import logging
from typing import Any, Dict, List, Optional, Set
import jwt
from fastapi import Depends, Header, HTTPException, status

from eaoc.config import Settings, get_settings
from eaoc.schemas.auth import AuthenticatedUser

logger = logging.getLogger("eaoc.api.dependencies.auth")

SUPPORTED_ALGORITHMS: Set[str] = {"HS256", "HS384", "HS512"}


def validate_access_token(
    token: str,
    secret_key: str,
    algorithm: str,
    expected_issuer: Optional[str] = None,
    expected_audience: Optional[str] = None,
) -> AuthenticatedUser:
    """
    Cryptographically decodes and semantically validates a JWT access token.

    Enforces:
    - Signature verification against secret_key
    - Explicit algorithm allowlist (rejects 'none' and unexpected algorithms)
    - Expiration verification (exp)
    - Issuer verification if expected_issuer is provided
    - Audience verification if expected_audience is provided
    - Required claims presence ('sub', 'tenant_id', 'roles', 'exp')
    - Non-empty string types for 'sub' and 'tenant_id'
    - List of string types for 'roles'

    Raises HTTPException(401) on any verification or semantic validation failure.
    """
    if algorithm not in SUPPORTED_ALGORITHMS:
        logger.error(f"Configured JWT algorithm '{algorithm}' is not in allowlist {SUPPORTED_ALGORITHMS}.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication subsystem is improperly configured.",
        )

    options: Dict[str, Any] = {
        "verify_signature": True,
        "verify_exp": True,
        "require": ["exp", "sub", "tenant_id", "roles"],
    }

    if expected_issuer is not None:
        options["verify_iss"] = True
    else:
        options["verify_iss"] = False

    if expected_audience is not None:
        options["verify_aud"] = True
    else:
        options["verify_aud"] = False

    try:
        payload = jwt.decode(
            token,
            key=secret_key,
            algorithms=[algorithm],
            issuer=expected_issuer,
            audience=expected_audience,
            options=options,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has expired.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="The token has expired"'},
        )
    except jwt.InvalidSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token signature.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="Signature verification failed"'},
        )
    except jwt.InvalidIssuerError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token issuer.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="Invalid issuer"'},
        )
    except jwt.InvalidAudienceError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token audience.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="Invalid audience"'},
        )
    except jwt.InvalidAlgorithmError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token algorithm.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="Algorithm not allowed"'},
        )
    except jwt.MissingRequiredClaimError as claim_err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing required token claims.",
            headers={"WWW-Authenticate": f'Bearer error="invalid_token", error_description="Missing claim {claim_err}"'},
        )
    except jwt.DecodeError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed authentication token.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="Malformed token"'},
        )
    except jwt.PyJWTError as jwt_err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )

    # Semantic inspection of decoded claims
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token: subject is missing or empty.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )

    tenant_id = payload.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token: tenant_id is missing or empty.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )

    roles = payload.get("roles")
    if not isinstance(roles, list) or not all(isinstance(r, str) and r.strip() for r in roles):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token: roles must be a list of non-empty strings.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )

    return AuthenticatedUser(
        user_id=sub.strip(),
        tenant_id=tenant_id.strip(),
        roles=[r.strip() for r in roles],
        token_claims=payload,
    )


async def get_trusted_identity(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    """
    FastAPI dependency extracting and cryptographically verifying the bearer JWT token.

    Fails closed:
    - HTTP 500 if server configuration (JWT_SECRET_KEY, JWT_ALGORITHM) is missing or invalid.
    - HTTP 401 if Authorization header is absent, malformed, expired, tampered, or invalid.
    - Never uses caller-supplied identity headers (X-User-Roles, X-User-ID, X-Tenant-ID).
    """
    if not settings.JWT_SECRET_KEY or not settings.JWT_SECRET_KEY.strip():
        logger.error("Authentication failed closed: JWT_SECRET_KEY is not configured.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication subsystem is improperly configured.",
        )

    if settings.JWT_ALGORITHM not in SUPPORTED_ALGORITHMS:
        logger.error(f"Authentication failed closed: Unsupported JWT_ALGORITHM '{settings.JWT_ALGORITHM}'.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication subsystem is improperly configured.",
        )

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header scheme. Expected 'Bearer <token>'.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )

    raw_token = parts[1].strip()

    return validate_access_token(
        token=raw_token,
        secret_key=settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
        expected_issuer=settings.JWT_ISSUER,
        expected_audience=settings.JWT_AUDIENCE,
    )


def require_roles(*allowed_roles: str):
    """
    FastAPI dependency factory requiring the authenticated principal
    to possess at least one of the specified allowed_roles.

    Raises HTTP 403 Forbidden if the authenticated user lacks all allowed roles.
    """
    async def _role_verifier(
        user: AuthenticatedUser = Depends(get_trusted_identity),
    ) -> AuthenticatedUser:
        if allowed_roles and not any(role in user.roles for role in allowed_roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access forbidden: Principal lacks required role(s).",
            )
        return user

    return _role_verifier
