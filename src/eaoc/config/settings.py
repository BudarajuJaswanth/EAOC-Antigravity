"""
EAOC Centralized Configuration & Environment Management Module
Uses pydantic-settings to load application, Supabase PostgreSQL database,
Redis, model-agnostic LLM providers, security placeholders, and CORS parameters.

Enforces strict secret protection: secrets are never hard-coded, committed,
logged, or exposed through string representations.
"""

from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse, urlunparse
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # 1. Application Settings
    APP_NAME: str = "Enterprise AI Operations Center (EAOC)"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = Field(
        default="development",
        description="Application environment: development, testing, or production"
    )
    DEBUG: bool = False
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_PREFIX: str = "/api/v1"
    
    # Configuration-Driven CORS Settings
    CORS_ORIGINS: List[str] = Field(
        default=["http://localhost:3000", "http://localhost:8000"],
        description="Allowed CORS origins"
    )

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            if not v.strip():
                return []
            return [i.strip() for i in v.split(",") if i.strip()]
        return v

    # 2. Observability Settings
    LOG_LEVEL: str = "INFO"

    # 3. Primary Database Settings: Supabase PostgreSQL
    DATABASE_URL: Optional[str] = Field(
        default=None,
        repr=False,
        description="Supabase PostgreSQL connection URL (postgresql+asyncpg://...)"
    )
    TEST_DATABASE_URL: Optional[str] = Field(
        default=None,
        repr=False,
        description="Dedicated test database URL for live integration tests only."
    )
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_RECYCLE: int = 1800

    # Optional Supabase metadata
    SUPABASE_URL: Optional[str] = None
    SUPABASE_ANON_KEY: Optional[str] = Field(default=None, repr=False)

    # 4. Redis Settings (Runtime State Persistence Layer)
    REDIS_URL: Optional[str] = Field(
        default=None,
        repr=False,
        description="Redis connection URL (e.g. redis://localhost:6379/0)"
    )
    REDIS_ENABLED: bool = False
    REDIS_TTL_SECONDS: int = Field(
        default=86400,
        description="Default TTL for stored workflow state in seconds (24 hours)"
    )

    # 5. Model Provider Settings (Model-Agnostic Abstraction)
    DEFAULT_MODEL_PROVIDER: str = Field(
        default="gemini",
        description="Selected model provider: gemini, lm_studio, or ollama"
    )
    
    # Local Model Providers
    LM_STUDIO_BASE_URL: Optional[str] = "http://localhost:1234/v1"
    LM_STUDIO_MODEL: Optional[str] = "qwen2.5-7b-instruct"
    
    OLLAMA_BASE_URL: Optional[str] = "http://localhost:11434"
    OLLAMA_MODEL: Optional[str] = "llama3.2:3b"
    
    # Cloud Model Provider
    GEMINI_API_KEY: Optional[str] = Field(
        default=None,
        repr=False,
        description="Google Gemini API Key"
    )
    GEMINI_MODEL: Optional[str] = "gemini-1.5-pro"

    # 6. Security Placeholders (Future Authentication & Token Signing)
    JWT_SECRET_KEY: Optional[str] = Field(
        default=None,
        repr=False,
        description="Secret key for JWT token signing"
    )
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: Optional[str] = Field(
        default=None,
        description="Expected JWT issuer (iss) claim"
    )
    JWT_AUDIENCE: Optional[str] = Field(
        default=None,
        description="Expected JWT audience (aud) claim"
    )
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # Helpers
    def is_development(self) -> bool:
        return self.ENVIRONMENT.lower() == "development"

    def is_testing(self) -> bool:
        return self.ENVIRONMENT.lower() == "testing"

    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() == "production"

    def is_db_configured(self) -> bool:
        """
        Returns True if DATABASE_URL is explicitly set in environment
        and does not contain placeholder strings.
        """
        if not self.DATABASE_URL or not self.DATABASE_URL.strip():
            return False
        
        placeholders = [
            "change_this",
            "YOUR-PASSWORD",
            "YOUR-SUPABASE-REF",
            "[YOUR-PASSWORD]",
            "[YOUR-SUPABASE-REF]",
            "localhost",
            "127.0.0.1"
        ]
        db_url_str = self.DATABASE_URL.lower()
        return not any(p.lower() in db_url_str for p in placeholders)

    def get_sanitized_db_url(self) -> str:
        """
        Returns DATABASE_URL with user password masked for safe logging/auditing.
        """
        if not self.is_db_configured() or not self.DATABASE_URL:
            return "NOT CONFIGURED (Supabase Credentials Required)"
        
        try:
            parsed = urlparse(self.DATABASE_URL)
            if parsed.password:
                netloc = parsed.netloc.replace(f":{parsed.password}@", ":****@")
                sanitized_tuple = (
                    parsed.scheme,
                    netloc,
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment
                )
                return urlunparse(sanitized_tuple)
            return self.DATABASE_URL
        except Exception:
            return "CONFIGURED (Password Masked)"

    def is_test_db_configured(self) -> bool:
        """
        Returns True if TEST_DATABASE_URL is explicitly configured, non-empty,
        and passes structural safety validation.
        """
        if not self.TEST_DATABASE_URL or not self.TEST_DATABASE_URL.strip():
            return False
        is_safe, _ = self.validate_test_database_url()
        return is_safe

    def get_sanitized_test_db_url(self) -> str:
        """
        Returns TEST_DATABASE_URL with user password masked for safe logging/auditing.
        Never exposes credentials or full URLs if malformed.
        """
        if not self.TEST_DATABASE_URL or not self.TEST_DATABASE_URL.strip():
            return "NOT CONFIGURED (TEST_DATABASE_URL is None or empty)"
        try:
            parsed = urlparse(self.TEST_DATABASE_URL.strip())
            if parsed.password:
                netloc = parsed.netloc.replace(f":{parsed.password}@", ":****@")
                sanitized_tuple = (
                    parsed.scheme,
                    netloc,
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
                return urlunparse(sanitized_tuple)
            return self.TEST_DATABASE_URL.strip()
        except Exception:
            return "CONFIGURED (Password Masked)"

    def validate_test_database_url(
        self, candidate_url: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Side-effect-free safety validation of TEST_DATABASE_URL.
        Does NOT open a connection, execute queries, create sessions, or import engines.

        Validates:
        - Non-empty and non-whitespace.
        - Supported PostgreSQL driver scheme (postgresql, postgresql+asyncpg, postgres).
        - Valid URL structure (host, database name).
        - Normalized driver prefixes before comparing with DATABASE_URL.
        - Collision prevention against primary DATABASE_URL (normalized host/port/db).
        - Ambiguous comparisons fail closed.
        - Returns secret-safe diagnostic message (never exposes passwords or full tokens).
        - Leaves live endpoint and instance-identity verification explicitly unresolved.
        """
        raw_url = candidate_url if candidate_url is not None else self.TEST_DATABASE_URL
        if not raw_url or not str(raw_url).strip():
            return False, "TEST_DATABASE_URL is missing, empty, or whitespace-only."

        url_str = str(raw_url).strip()

        # Parse test URL
        try:
            parsed_test = urlparse(url_str)
        except Exception:
            return False, "Failed to parse TEST_DATABASE_URL: malformed URL."

        scheme = (parsed_test.scheme or "").lower().strip()
        valid_pg_schemes = {"postgresql", "postgresql+asyncpg", "postgres"}
        if scheme not in valid_pg_schemes:
            return (
                False,
                f"Unsupported database scheme '{scheme}'. Only PostgreSQL drivers (postgresql, postgresql+asyncpg) are supported.",
            )

        test_host = (parsed_test.hostname or "").lower().strip()
        if not test_host:
            return False, "Malformed TEST_DATABASE_URL: missing host."

        test_port = parsed_test.port or 5432
        test_path = parsed_test.path.strip()
        test_db = test_path.lstrip("/").split("/")[0].lower().strip() if test_path else ""
        if not test_db:
            return False, "Malformed TEST_DATABASE_URL: missing database name."

        # Normalized driver comparison with primary DATABASE_URL
        if self.DATABASE_URL and self.DATABASE_URL.strip():
            try:
                parsed_prod = urlparse(self.DATABASE_URL.strip())
                prod_scheme = (parsed_prod.scheme or "").lower().strip()
                prod_host = (parsed_prod.hostname or "").lower().strip()
                prod_port = parsed_prod.port or 5432
                prod_path = parsed_prod.path.strip()
                prod_db = prod_path.lstrip("/").split("/")[0].lower().strip() if prod_path else ""
            except Exception:
                # Ambiguous comparison: fail closed
                return False, "Ambiguous target comparison: failed to parse primary DATABASE_URL."

            if not prod_host or not prod_db:
                return False, "Ambiguous target comparison: primary DATABASE_URL host or database name is undefined."

            # Normalize schemes to canonical 'postgresql' for equivalent comparison
            norm_test_scheme = "postgresql" if scheme in valid_pg_schemes else scheme
            norm_prod_scheme = "postgresql" if prod_scheme in valid_pg_schemes else prod_scheme

            # Check loopback host aliases
            loopback_hosts = {"localhost", "127.0.0.1", "::1"}
            is_same_host = (test_host == prod_host) or (
                test_host in loopback_hosts and prod_host in loopback_hosts
            )
            is_same_port = test_port == prod_port
            is_same_db = test_db == prod_db

            if is_same_host and is_same_port and is_same_db and (norm_test_scheme == norm_prod_scheme):
                return (
                    False,
                    f"Rejected: TEST_DATABASE_URL matches primary database target on '{test_host}:{test_port}/{test_db}'.",
                )

        return (
            True,
            f"Valid test database target on '{test_host}:{test_port}/{test_db}'. Note: URL comparison does not verify physical instance identity.",
        )

    def is_redis_configured(self) -> bool:
        """
        Returns True if REDIS_ENABLED is True and REDIS_URL is explicitly configured.
        """
        if not self.REDIS_ENABLED or not self.REDIS_URL or not self.REDIS_URL.strip():
            return False
        placeholders = ["change_this", "placeholder"]
        return not any(p.lower() in self.REDIS_URL.lower() for p in placeholders)

    def get_sanitized_redis_url(self) -> str:
        """
        Returns REDIS_URL with user password masked for safe logging/auditing.
        """
        if not self.is_redis_configured() or not self.REDIS_URL:
            return "NOT CONFIGURED (Redis Disabled or URL Missing)"
        try:
            parsed = urlparse(self.REDIS_URL)
            if parsed.password:
                netloc = parsed.netloc.replace(f":{parsed.password}@", ":****@")
                sanitized_tuple = (
                    parsed.scheme,
                    netloc,
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
                return urlunparse(sanitized_tuple)
            return self.REDIS_URL
        except Exception:
            return "CONFIGURED (Password Masked)"

    def get_active_provider_config(self) -> Dict[str, Any]:
        """
        Returns configuration status for the default selected model provider.
        Does NOT instantiate LLM clients or make network requests.
        """
        provider = self.DEFAULT_MODEL_PROVIDER.lower()
        if provider == "gemini":
            is_configured = bool(self.GEMINI_API_KEY and self.GEMINI_API_KEY.strip())
            return {
                "provider": "gemini",
                "category": "CLOUD",
                "model": self.GEMINI_MODEL,
                "is_configured": is_configured,
                "status": "CONFIGURED" if is_configured else "NOT_CONFIGURED",
                "details": "Gemini API key is configured." if is_configured else "GEMINI_API_KEY is not set."
            }
        elif provider == "lm_studio":
            is_configured = bool(self.LM_STUDIO_BASE_URL and self.LM_STUDIO_BASE_URL.strip())
            return {
                "provider": "lm_studio",
                "category": "LOCAL",
                "base_url": self.LM_STUDIO_BASE_URL,
                "model": self.LM_STUDIO_MODEL,
                "is_configured": is_configured,
                "status": "CONFIGURED" if is_configured else "NOT_CONFIGURED",
                "details": f"LM Studio base URL: {self.LM_STUDIO_BASE_URL}"
            }
        elif provider == "ollama":
            is_configured = bool(self.OLLAMA_BASE_URL and self.OLLAMA_BASE_URL.strip())
            return {
                "provider": "ollama",
                "category": "LOCAL",
                "base_url": self.OLLAMA_BASE_URL,
                "model": self.OLLAMA_MODEL,
                "is_configured": is_configured,
                "status": "CONFIGURED" if is_configured else "NOT_CONFIGURED",
                "details": f"Ollama base URL: {self.OLLAMA_BASE_URL}"
            }
        else:
            return {
                "provider": provider,
                "category": "UNKNOWN",
                "is_configured": False,
                "status": "INVALID_PROVIDER",
                "details": f"Unsupported provider '{provider}'. Approved choices: gemini, lm_studio, ollama."
            }

    def get_sanitized_config(self) -> Dict[str, Any]:
        """
        Exports settings dictionary with all secrets (passwords, API keys, JWT secret) masked.
        """
        data = self.model_dump()
        
        # Mask sensitive secrets
        data["DATABASE_URL"] = self.get_sanitized_db_url()
        data["TEST_DATABASE_URL"] = self.get_sanitized_test_db_url()
        data["REDIS_URL"] = self.get_sanitized_redis_url()
        
        if data.get("SUPABASE_ANON_KEY"):
            data["SUPABASE_ANON_KEY"] = "****"
            
        if data.get("GEMINI_API_KEY"):
            data["GEMINI_API_KEY"] = "****"
            
        if data.get("JWT_SECRET_KEY"):
            data["JWT_SECRET_KEY"] = "****"
            
        return data


_settings_instance: Optional[Settings] = None


def get_settings() -> Settings:
    """Singleton getter for application settings."""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
    return _settings_instance
