"""
EAOC IT Infrastructure & Secret Redaction Engine
Provides deep, defensive masking of infrastructure-sensitive details:
- IPv4 and IPv6 addresses
- Internal hostnames and private FQDNs (*.internal, *.corp, *.local, *.lan, *.priv)
- Passwords, tokens, API keys, and connection credentials (always redacted for all roles, including admin)

Preserves auditability while preventing sensitive infrastructure topology exposure to unauthorized roles.
"""

import copy
import re
from typing import Any, Dict, List, Set, Tuple

from eaoc.observability.redaction import redact_sensitive_data

# Comprehensive IPv4 regex (matching valid octets 0-255)
IPV4_PATTERN = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\b"
)

# Standard IPv6 pattern (full and compressed representations)
IPV6_PATTERN = re.compile(
    r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b|"
    r"\b(?:[0-9a-fA-F]{1,4}:){1,7}:|"
    r"\b:(?::[0-9a-fA-F]{1,4}){1,7}\b|"
    r"\b(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}\b",
    re.IGNORECASE,
)

# Internal private domain / hostname suffix pattern
INTERNAL_HOSTNAME_PATTERN = re.compile(
    r"\b[a-zA-Z0-9_\-\.]+\.(?:internal|corp|local|lan|priv|intra|private)\b",
    re.IGNORECASE,
)

# Additional credentials / secret key patterns
SECRET_FIELD_KEYS: Set[str] = {
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "auth_token",
    "bearer",
    "private_key",
    "client_secret",
    "credential",
    "credentials",
}


class ItInfraRedactor:
    """
    Defensive redaction engine for IT/Support data.
    Ensures:
    1. Passwords, tokens, credentials, and API keys are ALWAYS redacted for all roles (including admin).
    2. Infrastructure details (IP addresses, internal hostnames) are redacted for non-authorized callers.
    3. Accurately tracks if any infrastructure fields were masked via `was_infra_redacted`.
    """

    IP_REPLACEMENT = "[REDACTED_IP]"
    HOST_REPLACEMENT = "[REDACTED_HOST]"
    SECRET_REPLACEMENT = "****"

    @classmethod
    def redact_text_secrets(cls, text: str) -> str:
        """Sanitizes credentials, Bearer tokens, and connection strings from text."""
        if not isinstance(text, str):
            return text
        # Redact via system-wide secret sanitization first
        sanitized = redact_sensitive_data(text)
        # Redact Bearer tokens: Bearer <token>
        sanitized = re.sub(
            r"(Bearer\s+)[A-Za-z0-9_\-\.]+",
            r"\1" + cls.SECRET_REPLACEMENT,
            sanitized,
            flags=re.IGNORECASE,
        )
        return sanitized

    @classmethod
    def redact_text_infra(cls, text: str) -> Tuple[str, bool]:
        """
        Masks IP addresses and internal hostnames within freeform text.
        Returns the sanitized text and a boolean indicating if changes were made.
        """
        if not isinstance(text, str):
            return text, False

        modified = False
        new_text = text

        # Check and replace IPv4
        if IPV4_PATTERN.search(new_text):
            new_text = IPV4_PATTERN.sub(cls.IP_REPLACEMENT, new_text)
            modified = True

        # Check and replace IPv6
        if IPV6_PATTERN.search(new_text):
            new_text = IPV6_PATTERN.sub(cls.IP_REPLACEMENT, new_text)
            modified = True

        # Check and replace internal hostnames
        if INTERNAL_HOSTNAME_PATTERN.search(new_text):
            new_text = INTERNAL_HOSTNAME_PATTERN.sub(cls.HOST_REPLACEMENT, new_text)
            modified = True

        return new_text, modified

    @classmethod
    def redact_record(
        cls,
        data: Any,
        is_admin_or_infra_authorized: bool = False,
    ) -> Tuple[Any, bool]:
        """
        Deeply inspects and sanitizes a record (dict, list, string, or primitive).
        - Secrets/passwords: ALWAYS redacted unconditionally.
        - Infrastructure topology (IP addresses, hostnames): Redacted if is_admin_or_infra_authorized is False.
        Returns (sanitized_data, was_infra_redacted).
        """
        was_infra_redacted = False

        if isinstance(data, dict):
            cleaned_dict = {}
            for k, v in data.items():
                key_lower = str(k).lower()

                # Rule 1: Secret fields are ALWAYS redacted for all roles
                if any(sec in key_lower for sec in SECRET_FIELD_KEYS):
                    cleaned_dict[k] = cls.SECRET_REPLACEMENT
                    continue

                # Rule 2: Dedicated infrastructure fields
                if not is_admin_or_infra_authorized:
                    if key_lower in ("ip_address", "ip", "source_ip", "dest_ip", "client_ip", "server_ip"):
                        cleaned_dict[k] = cls.IP_REPLACEMENT
                        was_infra_redacted = True
                        continue
                    if key_lower in ("hostname", "host", "fqdn", "internal_host", "node_name"):
                        cleaned_dict[k] = cls.HOST_REPLACEMENT
                        was_infra_redacted = True
                        continue

                # Rule 3: Recursive processing for nested structures
                sub_res, sub_infra_modified = cls.redact_record(
                    v, is_admin_or_infra_authorized=is_admin_or_infra_authorized
                )
                if sub_infra_modified:
                    was_infra_redacted = True
                cleaned_dict[k] = sub_res
            return cleaned_dict, was_infra_redacted

        elif isinstance(data, list):
            cleaned_list = []
            for item in data:
                sub_res, sub_infra_modified = cls.redact_record(
                    item, is_admin_or_infra_authorized=is_admin_or_infra_authorized
                )
                if sub_infra_modified:
                    was_infra_redacted = True
                cleaned_list.append(sub_res)
            return cleaned_list, was_infra_redacted

        elif isinstance(data, str):
            # First, unconditionally redact secrets in the string
            sanitized_str = cls.redact_text_secrets(data)

            # Second, if not authorized for infra topology, mask IPs and hostnames in the string
            if not is_admin_or_infra_authorized:
                sanitized_str, infra_changed = cls.redact_text_infra(sanitized_str)
                if infra_changed:
                    was_infra_redacted = True

            return sanitized_str, was_infra_redacted

        return data, was_infra_redacted
