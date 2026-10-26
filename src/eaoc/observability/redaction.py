"""
EAOC Observability Data Redaction & Sanitization Engine
Redacts passwords, tokens, API keys, database connection credentials, and sensitive headers.
"""

import re
from typing import Any, Dict, List, Set

# Key names to redact (case-insensitive substring match)
SENSITIVE_KEYS: Set[str] = {
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "auth",
    "jwt",
    "cookie",
    "anon_key",
    "service_role",
    "database_url",
    "db_url",
}

# Regex pattern for matching inline connection strings (e.g. postgresql+asyncpg://user:password@host:port/db, redis://:secret@host:port/db)
DB_CREDENTIAL_PATTERN = re.compile(
    r"((?:postgresql(?:\+asyncpg)?|redis|rediss)://[^:]*:)([^@]+)(@[^\s'\"]+)", re.IGNORECASE
)
KEY_VALUE_SECRET_PATTERN = re.compile(
    r"((?:api[_-]?key|password|secret|token)\s*[=:]\s*['\"]?)([^'\";\s]+)(['\"]?)",
    re.IGNORECASE,
)


def mask_string(val: str) -> str:
    """Masks secret string values, leaving first/last character if long enough."""
    if not val:
        return "****"
    if len(val) <= 4:
        return "****"
    return f"{val[0]}****{val[-1]}"


def redact_sensitive_data(obj: Any) -> Any:
    """
    Recursively redacts sensitive keys and patterns from dicts, lists, strings, and exception objects.
    """
    if isinstance(obj, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in obj.items():
            key_lower = str(k).lower()
            if any(sens in key_lower for sens in SENSITIVE_KEYS):
                if isinstance(v, str) and ("postgresql://" in v.lower() or "postgresql+asyncpg://" in v.lower()):
                    cleaned[k] = DB_CREDENTIAL_PATTERN.sub(r"\1****\3", v)
                else:
                    cleaned[k] = "****"
            else:
                cleaned[k] = redact_sensitive_data(v)
        return cleaned

    elif isinstance(obj, list):
        return [redact_sensitive_data(item) for item in obj]

    elif isinstance(obj, str):
        # Sanitize database URLs in text
        redacted = DB_CREDENTIAL_PATTERN.sub(r"\1****\3", obj)
        # Sanitize inline key-value secret assignments
        redacted = KEY_VALUE_SECRET_PATTERN.sub(r"\1****\3", redacted)
        return redacted

    elif isinstance(obj, Exception):
        err_msg = str(obj)
        return redact_sensitive_data(err_msg)

    return obj
