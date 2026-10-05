"""
EAOC Configuration & Settings Module
Uses pydantic-settings to load application & Supabase database parameters.
No hard-coded secrets, no localhost default fallback for database.
"""

from typing import Optional
from urllib.parse import urlparse, urlunparse
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    # Primary Database Settings: Supabase PostgreSQL
    # Default is None to prevent silent localhost connection attempts
    DATABASE_URL: Optional[str] = Field(
        default=None,
        description="Supabase PostgreSQL connection URL (postgresql+asyncpg://...)"
    )
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_RECYCLE: int = 1800

    # Optional Supabase metadata
    SUPABASE_URL: Optional[str] = None
    SUPABASE_ANON_KEY: Optional[str] = None

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


_settings_instance: Optional[Settings] = None


def get_settings() -> Settings:
    """Singleton getter for application settings."""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
    return _settings_instance
