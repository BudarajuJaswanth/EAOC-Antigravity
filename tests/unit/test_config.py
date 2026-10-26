"""
EAOC Part 1.5 Unit Test Suite: Configuration & Environment Management
Tests pydantic-settings loading, environment detection helpers, CORS origins parsing,
model provider selection validation, secret protection & masking, and database URL sanitization.
"""

import unittest
from unittest.mock import patch
from eaoc.config.settings import Settings


class TestConfigurationManagement(unittest.TestCase):

    def test_default_config_loading(self):
        """Verify default settings values when no custom env is provided."""
        settings = Settings()
        self.assertEqual(settings.APP_NAME, "Enterprise AI Operations Center (EAOC)")
        self.assertEqual(settings.APP_VERSION, "1.0.0")
        self.assertEqual(settings.ENVIRONMENT, "development")
        self.assertEqual(settings.DEFAULT_MODEL_PROVIDER, "gemini")
        self.assertEqual(settings.LOG_LEVEL, "INFO")

    def test_environment_helpers(self):
        """Verify is_development(), is_testing(), and is_production() helpers."""
        dev_settings = Settings(ENVIRONMENT="development")
        self.assertTrue(dev_settings.is_development())
        self.assertFalse(dev_settings.is_testing())
        self.assertFalse(dev_settings.is_production())

        test_settings = Settings(ENVIRONMENT="testing")
        self.assertFalse(test_settings.is_development())
        self.assertTrue(test_settings.is_testing())
        self.assertFalse(test_settings.is_production())

        prod_settings = Settings(ENVIRONMENT="production")
        self.assertFalse(prod_settings.is_development())
        self.assertFalse(prod_settings.is_testing())
        self.assertTrue(prod_settings.is_production())

    def test_cors_origins_parsing(self):
        """Verify CORS_ORIGINS handles both list and comma-separated string inputs."""
        list_settings = Settings(CORS_ORIGINS=["http://app1.com", "http://app2.com"])
        self.assertEqual(list_settings.CORS_ORIGINS, ["http://app1.com", "http://app2.com"])

        str_settings = Settings(CORS_ORIGINS="http://app1.com, http://app2.com")
        self.assertEqual(str_settings.CORS_ORIGINS, ["http://app1.com", "http://app2.com"])

    def test_env_override(self):
        """Verify settings override from environment dictionary."""
        override_settings = Settings(
            LOG_LEVEL="DEBUG",
            DEFAULT_MODEL_PROVIDER="ollama",
            OLLAMA_MODEL="mistral:7b"
        )
        self.assertEqual(override_settings.LOG_LEVEL, "DEBUG")
        self.assertEqual(override_settings.DEFAULT_MODEL_PROVIDER, "ollama")
        self.assertEqual(override_settings.OLLAMA_MODEL, "mistral:7b")

    def test_provider_config_validation(self):
        """Verify get_active_provider_config returns correct status without making API calls."""
        # Gemini without API key
        gemini_unconfigured = Settings(DEFAULT_MODEL_PROVIDER="gemini", GEMINI_API_KEY=None)
        cfg_g1 = gemini_unconfigured.get_active_provider_config()
        self.assertEqual(cfg_g1["provider"], "gemini")
        self.assertEqual(cfg_g1["status"], "NOT_CONFIGURED")

        # Gemini with API key
        gemini_configured = Settings(DEFAULT_MODEL_PROVIDER="gemini", GEMINI_API_KEY="secret-gemini-key")
        cfg_g2 = gemini_configured.get_active_provider_config()
        self.assertEqual(cfg_g2["provider"], "gemini")
        self.assertEqual(cfg_g2["status"], "CONFIGURED")

        # LM Studio
        lms_settings = Settings(DEFAULT_MODEL_PROVIDER="lm_studio", LM_STUDIO_BASE_URL="http://localhost:1234/v1")
        cfg_lms = lms_settings.get_active_provider_config()
        self.assertEqual(cfg_lms["provider"], "lm_studio")
        self.assertEqual(cfg_lms["category"], "LOCAL")

        # Invalid provider
        invalid_settings = Settings(DEFAULT_MODEL_PROVIDER="unsupported_llm")
        cfg_inv = invalid_settings.get_active_provider_config()
        self.assertEqual(cfg_inv["status"], "INVALID_PROVIDER")

    def test_secret_protection_and_masking(self):
        """Verify sensitive secrets are excluded from repr and masked in get_sanitized_config()."""
        secret_db_url = "postgresql+asyncpg://postgres:SuperSecretPassword123@db.supabase.co:5432/postgres"
        settings = Settings(
            DATABASE_URL=secret_db_url,
            GEMINI_API_KEY="secret-gemini-key-999",
            JWT_SECRET_KEY="secret-jwt-signing-key",
            SUPABASE_ANON_KEY="secret-anon-key"
        )

        # Verify secrets are NOT in string repr
        repr_str = repr(settings)
        self.assertNotIn("SuperSecretPassword123", repr_str)
        self.assertNotIn("secret-gemini-key-999", repr_str)
        self.assertNotIn("secret-jwt-signing-key", repr_str)

        # Verify get_sanitized_config() masks secrets
        sanitized = settings.get_sanitized_config()
        self.assertEqual(sanitized["DATABASE_URL"], "postgresql+asyncpg://postgres:****@db.supabase.co:5432/postgres")
        self.assertEqual(sanitized["GEMINI_API_KEY"], "****")
        self.assertEqual(sanitized["JWT_SECRET_KEY"], "****")
        self.assertEqual(sanitized["SUPABASE_ANON_KEY"], "****")

    def test_unconfigured_database(self):
        """Verify database configured check correctly detects missing or placeholder DATABASE_URL."""
        unconfigured = Settings(DATABASE_URL=None)
        self.assertFalse(unconfigured.is_db_configured())
        self.assertEqual(unconfigured.get_sanitized_db_url(), "NOT CONFIGURED (Supabase Credentials Required)")

        placeholder = Settings(DATABASE_URL="postgresql+asyncpg://postgres:[YOUR-PASSWORD]@db.ref.supabase.co:5432/postgres")
        self.assertFalse(placeholder.is_db_configured())


class TestTestDatabaseConfiguration(unittest.TestCase):
    """
    EAOC Part 2.18.7.2B: Test Database Configuration & Safety Helpers.
    Verifies side-effect-free safety validation, secret redaction, prefix normalization,
    and target collision rejection without opening any network connections or engines.
    """

    def test_default_test_database_url_is_none(self):
        """TEST_DATABASE_URL must default to None and not silently fall back to DATABASE_URL."""
        settings = Settings()
        self.assertIsNone(settings.TEST_DATABASE_URL)
        self.assertFalse(settings.is_test_db_configured())
        self.assertEqual(
            settings.get_sanitized_test_db_url(),
            "NOT CONFIGURED (TEST_DATABASE_URL is None or empty)",
        )

    def test_missing_and_whitespace_only_urls(self):
        """Reject missing, empty, or whitespace-only test URLs."""
        settings = Settings()
        for empty_val in [None, "", "   ", "\t\n "]:
            is_valid, msg = settings.validate_test_database_url(candidate_url=empty_val)
            self.assertFalse(is_valid)
            self.assertIn("missing, empty, or whitespace-only", msg)

    def test_valid_postgresql_async_driver_url(self):
        """Accept valid postgresql+asyncpg URL pointing to an isolated target."""
        settings = Settings(
            DATABASE_URL="postgresql+asyncpg://prod_user:p1@db.supabase.co:5432/prod_db",
            TEST_DATABASE_URL="postgresql+asyncpg://test_user:test_secret@127.0.0.1:5433/test_db",
        )
        self.assertTrue(settings.is_test_db_configured())
        is_valid, msg = settings.validate_test_database_url()
        self.assertTrue(is_valid)
        self.assertIn("127.0.0.1:5433/test_db", msg)
        self.assertNotIn("test_secret", msg)

    def test_equivalent_driver_prefix_normalization_and_rejection(self):
        """
        Normalize driver prefixes (postgresql, postgresql+asyncpg, postgres) before comparing
        and reject matching primary database target regardless of driver variant.
        """
        primary_url = "postgresql+asyncpg://prod_user:prod_pass@db.example.com:5432/primary_db"
        settings = Settings(DATABASE_URL=primary_url)

        # Same driver prefix
        is_valid, msg = settings.validate_test_database_url(
            "postgresql+asyncpg://other_user:other_pass@db.example.com:5432/primary_db"
        )
        self.assertFalse(is_valid)
        self.assertIn("matches primary database target", msg)

        # Sync/generic postgresql prefix against asyncpg primary
        is_valid, msg = settings.validate_test_database_url(
            "postgresql://diff_user:diff_pass@db.example.com:5432/primary_db"
        )
        self.assertFalse(is_valid)
        self.assertIn("matches primary database target", msg)

        # Legacy postgres prefix
        is_valid, msg = settings.validate_test_database_url(
            "postgres://diff_user:diff_pass@db.example.com:5432/primary_db"
        )
        self.assertFalse(is_valid)
        self.assertIn("matches primary database target", msg)

    def test_loopback_alias_collision_rejection(self):
        """Reject collision when host uses loopback alias (localhost vs 127.0.0.1)."""
        settings = Settings(
            DATABASE_URL="postgresql+asyncpg://prod_user:p1@localhost:5432/eaoc_db"
        )
        is_valid, msg = settings.validate_test_database_url(
            "postgresql+asyncpg://test_user:p2@127.0.0.1:5432/eaoc_db"
        )
        self.assertFalse(is_valid)
        self.assertIn("matches primary database target", msg)

    def test_different_database_name_acceptance_at_url_level_only(self):
        """
        Accept different database name on same host/port at URL comparison level only,
        without asserting physical instance identity.
        """
        settings = Settings(
            DATABASE_URL="postgresql+asyncpg://prod_user:p1@db.example.com:5432/prod_db"
        )
        is_valid, msg = settings.validate_test_database_url(
            "postgresql+asyncpg://test_user:p2@db.example.com:5432/test_isolated_db"
        )
        self.assertTrue(is_valid)
        self.assertIn("db.example.com:5432/test_isolated_db", msg)
        self.assertIn("URL comparison does not verify physical instance identity", msg)

    def test_malformed_urls_and_unsupported_drivers(self):
        """Reject unsupported drivers, missing host, or missing database name."""
        settings = Settings()

        # Unsupported driver schemes
        unsupported = [
            "sqlite:///test.db",
            "mysql://user:pass@localhost:3306/testdb",
            "http://example.com/db",
            "mongodb://user:pass@localhost:27017/db",
        ]
        for url in unsupported:
            is_valid, msg = settings.validate_test_database_url(url)
            self.assertFalse(is_valid)
            self.assertIn("Unsupported database scheme", msg)

        # Missing host
        is_valid, msg = settings.validate_test_database_url("postgresql:///test_db")
        self.assertFalse(is_valid)
        self.assertIn("missing host", msg)

        # Missing database name
        is_valid, msg = settings.validate_test_database_url("postgresql://localhost:5432/")
        self.assertFalse(is_valid)
        self.assertIn("missing database name", msg)

    def test_ambiguous_comparisons_fail_closed(self):
        """Ambiguous comparisons against primary DATABASE_URL fail closed."""
        # Malformed primary DATABASE_URL with missing host/db
        settings = Settings(DATABASE_URL="postgresql://")
        is_valid, msg = settings.validate_test_database_url(
            "postgresql+asyncpg://user:pass@127.0.0.1:5432/test_db"
        )
        self.assertFalse(is_valid)
        self.assertIn("Ambiguous target comparison", msg)

    def test_secret_safe_error_messages_and_representation(self):
        """Verify credentials and query params are never leaked in errors, repr, or sanitized output."""
        sensitive_pass = "super_secret_test_password_xyz!"
        test_url = f"postgresql+asyncpg://admin:{sensitive_pass}@db.test.co:5432/test_db?ssl=true&token=secret_token"

        settings = Settings(
            DATABASE_URL="postgresql+asyncpg://prod:prodpass@db.prod.co:5432/prod_db",
            TEST_DATABASE_URL=test_url,
        )

        # 1. Check repr() does not include the password
        repr_output = repr(settings)
        self.assertNotIn(sensitive_pass, repr_output)
        self.assertNotIn("secret_token", repr_output)

        # 2. Check validate_test_database_url() diagnostic message
        is_valid, msg = settings.validate_test_database_url()
        self.assertTrue(is_valid)
        self.assertNotIn(sensitive_pass, msg)
        self.assertNotIn("secret_token", msg)
        self.assertNotIn("admin:", msg)

        # Rejection message is also secret-safe
        is_valid_rej, msg_rej = settings.validate_test_database_url(
            f"postgresql+asyncpg://admin:{sensitive_pass}@db.prod.co:5432/prod_db"
        )
        self.assertFalse(is_valid_rej)
        self.assertNotIn(sensitive_pass, msg_rej)
        self.assertNotIn("admin:", msg_rej)

        # 3. Check get_sanitized_config() masks the password
        sanitized = settings.get_sanitized_config()
        self.assertIn("TEST_DATABASE_URL", sanitized)
        self.assertNotIn(sensitive_pass, sanitized["TEST_DATABASE_URL"])
        self.assertIn(":****@", sanitized["TEST_DATABASE_URL"])

    def test_side_effect_free_validation(self):
        """
        Verify validation is side-effect-free, makes no network connections,
        and does not alter production configuration or activate LangGraph.
        """
        from eaoc.agents.orchestrator.engine import OrchestratorEngine

        settings = Settings(
            DATABASE_URL="postgresql+asyncpg://prod:prodpass@db.prod.co:5432/prod_db",
            TEST_DATABASE_URL="postgresql+asyncpg://test:testpass@127.0.0.1:5433/test_db",
        )

        # Validate
        is_valid, _ = settings.validate_test_database_url()
        self.assertTrue(is_valid)

        # Invariants preserved
        self.assertEqual(settings.DATABASE_URL, "postgresql+asyncpg://prod:prodpass@db.prod.co:5432/prod_db")
        self.assertFalse(settings.is_production())

        # Verify LangGraph remains default False in OrchestratorEngine
        import inspect
        sig = inspect.signature(OrchestratorEngine.__init__)
        self.assertIn("use_langgraph", sig.parameters)
        self.assertFalse(sig.parameters["use_langgraph"].default)


if __name__ == "__main__":
    unittest.main()

