"""
EAOC Developer Agent Credential Scanning Engine
Scans generated source code and proposed fixes for likely hardcoded credentials,
API keys, database passwords, private keys, and authentication tokens before returning
them to callers.

LIMITATION NOTICE:
This scanner is a defensive heuristic pattern-matcher designed to prevent inadvertent
exposure of obvious secrets and credentials. It does NOT guarantee detection of every
possible secret, obfuscated token, or custom encryption key.
"""

import re
from typing import List, Tuple

from eaoc.observability import get_logger

logger = get_logger("eaoc.agents.developer.scanner")

# Heuristic patterns for likely hardcoded secrets and credentials
SECRET_PATTERNS = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
            re.IGNORECASE,
        ),
    ),
    (
        "aws_access_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "inline_database_credential",
        re.compile(
            r"(?:postgresql(?:\+asyncpg)?|mysql|redis|mongodb)://[^:]+:[^@\s'\"]+@[^\s'\"]+",
            re.IGNORECASE,
        ),
    ),
    (
        "hardcoded_password_assignment",
        re.compile(
            r"""(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*["'][^"'\s]{8,}["']""",
            re.IGNORECASE,
        ),
    ),
    (
        "jwt_or_bearer_token",
        re.compile(
            r"\b(?:Bearer\s+)?ey[A-Za-z0-9-_]+\.ey[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+\b"
        ),
    ),
]


class DevCredentialScanner:
    """
    Defensive credential scanner for AI-generated code.
    Blocks output containing likely hardcoded secrets per Phase 0 Section O requirements.
    """

    @classmethod
    def scan(cls, code: str) -> Tuple[bool, List[str]]:
        """
        Scans code string for likely hardcoded credentials.
        Returns:
            (has_secret: bool, detected_categories: List[str])
        """
        if not code or not isinstance(code, str):
            return False, []

        detected = []
        for category, pattern in SECRET_PATTERNS:
            if pattern.search(code):
                detected.append(category)

        return len(detected) > 0, detected

    @classmethod
    def check_and_block(cls, code: str) -> None:
        """
        Validates code safety.
        Raises ValueError and blocks execution if a likely hardcoded credential is detected.
        """
        has_secret, detected_categories = cls.scan(code)
        if has_secret:
            msg = (
                f"Hardcoded credential detected in generated output; output blocked. "
                f"Categories: {detected_categories}. "
                f"Generated code must not contain hardcoded secrets."
            )
            logger.warning(
                msg,
                extra={
                    "event_name": "dev_credential_detected_in_output",
                    "detected_categories": detected_categories,
                },
            )
            raise ValueError(msg)
