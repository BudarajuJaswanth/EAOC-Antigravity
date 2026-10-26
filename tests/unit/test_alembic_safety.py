"""
EAOC Part 2.18.7.4B Unit Test Suite: Alembic Migration Target Safety
Hermetic unit tests verifying:
1. Default target selection uses DATABASE_URL when ALEMBIC_TARGET_TEST_DB is absent.
2. Default target selection uses DATABASE_URL when ALEMBIC_TARGET_TEST_DB is empty.
3. Exact opt-in (ALEMBIC_TARGET_TEST_DB=1) selects validated TEST_DATABASE_URL.
4. Invalid non-empty flag values (e.g. 'true', '2', 'yes') fail closed without selecting a target.
5. Missing or blank TEST_DATABASE_URL fails closed without falling back to DATABASE_URL.
6. Production environment blocks test-target mode before engine creation.
7. Test URL colliding with primary DATABASE_URL is rejected.
8. Unsupported driver schemes or malformed test URLs are rejected.
9. Asyncpg driver normalization correctly normalizes postgresql:// and postgres:// prefixes.
10. Passwords and secret query tokens are never leaked in error messages or logs.
11. Pre-connection validation: all checks execute inside get_url() before any engine or connection.
"""

import os
import unittest
from unittest.mock import patch

from eaoc.config.settings import Settings
from eaoc.db.migrations.env import get_url


class TestAlembicTargetSafety(unittest.TestCase):

    def test_default_selection_when_flag_absent(self):
        """When ALEMBIC_TARGET_TEST_DB is absent, get_url() uses DATABASE_URL."""
        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql+asyncpg://prod_user:prod_pass@db.supabase.co:5432/postgres",
            TEST_DATABASE_URL="postgresql+asyncpg://test_user:test_pass@127.0.0.1:5433/eaoc_test",
        )
        with patch.dict(os.environ, {}, clear=False):
            if "ALEMBIC_TARGET_TEST_DB" in os.environ:
                del os.environ["ALEMBIC_TARGET_TEST_DB"]
            with patch("eaoc.db.migrations.env.get_settings", return_value=mock_settings):
                url = get_url()
                self.assertEqual(
                    url,
                    "postgresql+asyncpg://prod_user:prod_pass@db.supabase.co:5432/postgres",
                )

    def test_default_selection_when_flag_empty(self):
        """When ALEMBIC_TARGET_TEST_DB is empty or whitespace, get_url() uses DATABASE_URL."""
        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql+asyncpg://prod_user:prod_pass@db.supabase.co:5432/postgres",
            TEST_DATABASE_URL="postgresql+asyncpg://test_user:test_pass@127.0.0.1:5433/eaoc_test",
        )
        for empty_val in ["", "   ", "\t"]:
            with patch.dict(os.environ, {"ALEMBIC_TARGET_TEST_DB": empty_val}):
                with patch("eaoc.db.migrations.env.get_settings", return_value=mock_settings):
                    url = get_url()
                    self.assertEqual(
                        url,
                        "postgresql+asyncpg://prod_user:prod_pass@db.supabase.co:5432/postgres",
                    )

    def test_exact_opt_in_selects_test_database_url(self):
        """When ALEMBIC_TARGET_TEST_DB is exactly '1', get_url() uses TEST_DATABASE_URL."""
        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql+asyncpg://prod_user:prod_pass@db.supabase.co:5432/postgres",
            TEST_DATABASE_URL="postgresql+asyncpg://test_user:test_pass@127.0.0.1:5433/eaoc_test",
        )
        with patch.dict(os.environ, {"ALEMBIC_TARGET_TEST_DB": "1"}):
            with patch("eaoc.db.migrations.env.get_settings", return_value=mock_settings):
                url = get_url()
                self.assertEqual(
                    url,
                    "postgresql+asyncpg://test_user:test_pass@127.0.0.1:5433/eaoc_test",
                )

    def test_invalid_nonempty_flag_values_fail_closed(self):
        """Any nonempty value other than '1' must fail closed with a descriptive RuntimeError."""
        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql+asyncpg://prod_user:prod_pass@db.supabase.co:5432/postgres",
            TEST_DATABASE_URL="postgresql+asyncpg://test_user:test_pass@127.0.0.1:5433/eaoc_test",
        )
        invalid_values = ["true", "TRUE", "yes", "2", "0", "false", "on", "test"]
        for inv_val in invalid_values:
            with patch.dict(os.environ, {"ALEMBIC_TARGET_TEST_DB": inv_val}):
                with patch("eaoc.db.migrations.env.get_settings", return_value=mock_settings):
                    with self.assertRaises(RuntimeError) as ctx:
                        get_url()
                    self.assertIn("Invalid value", str(ctx.exception))
                    self.assertIn("Only exactly '1' is accepted", str(ctx.exception))

    def test_missing_or_blank_test_url_fails_without_fallback(self):
        """When ALEMBIC_TARGET_TEST_DB=1 but TEST_DATABASE_URL is missing or blank, fails without fallback."""
        for blank_url in [None, "", "   "]:
            mock_settings = Settings(
                ENVIRONMENT="development",
                DATABASE_URL="postgresql+asyncpg://prod_user:prod_pass@db.supabase.co:5432/postgres",
                TEST_DATABASE_URL=blank_url,
            )
            with patch.dict(os.environ, {"ALEMBIC_TARGET_TEST_DB": "1"}):
                with patch("eaoc.db.migrations.env.get_settings", return_value=mock_settings):
                    with self.assertRaises(RuntimeError) as ctx:
                        get_url()
                    self.assertIn("TEST_DATABASE_URL is missing or empty", str(ctx.exception))
                    self.assertIn("will not fall back to DATABASE_URL", str(ctx.exception))

    def test_production_environment_blocks_test_target_mode(self):
        """When ENVIRONMENT is production, test-target migration mode is strictly prohibited."""
        mock_settings = Settings(
            ENVIRONMENT="production",
            DATABASE_URL="postgresql+asyncpg://prod_user:prod_pass@db.supabase.co:5432/postgres",
            TEST_DATABASE_URL="postgresql+asyncpg://test_user:test_pass@127.0.0.1:5433/eaoc_test",
        )
        with patch.dict(os.environ, {"ALEMBIC_TARGET_TEST_DB": "1"}):
            with patch("eaoc.db.migrations.env.get_settings", return_value=mock_settings):
                with self.assertRaises(RuntimeError) as ctx:
                    get_url()
                self.assertIn("strictly prohibited in a production environment", str(ctx.exception))

    def test_colliding_test_url_rejected(self):
        """When TEST_DATABASE_URL matches primary DATABASE_URL, safety validation rejects it."""
        primary_url = "postgresql+asyncpg://prod_user:prod_pass@db.example.com:5432/primary_db"
        colliding_test_url = "postgresql://test_user:test_pass@db.example.com:5432/primary_db"
        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL=primary_url,
            TEST_DATABASE_URL=colliding_test_url,
        )
        with patch.dict(os.environ, {"ALEMBIC_TARGET_TEST_DB": "1"}):
            with patch("eaoc.db.migrations.env.get_settings", return_value=mock_settings):
                with self.assertRaises(RuntimeError) as ctx:
                    get_url()
                self.assertIn("rejected by safety validation", str(ctx.exception))
                self.assertIn("matches primary database target", str(ctx.exception))

    def test_unsupported_or_malformed_test_url_rejected(self):
        """Unsupported drivers or malformed URLs in TEST_DATABASE_URL are rejected."""
        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql+asyncpg://prod_user:prod_pass@db.supabase.co:5432/postgres",
            TEST_DATABASE_URL="sqlite:///test.db",
        )
        with patch.dict(os.environ, {"ALEMBIC_TARGET_TEST_DB": "1"}):
            with patch("eaoc.db.migrations.env.get_settings", return_value=mock_settings):
                with self.assertRaises(RuntimeError) as ctx:
                    get_url()
                self.assertIn("rejected by safety validation", str(ctx.exception))
                self.assertIn("Unsupported database scheme", str(ctx.exception))

    def test_driver_prefix_normalization(self):
        """PostgreSQL URL driver prefixes are safely normalized to postgresql+asyncpg://."""
        # 1. postgresql:// prefix
        mock_settings_1 = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql://prod_user:p1@db.supabase.co:5432/postgres",
            TEST_DATABASE_URL="postgresql://test_user:p2@127.0.0.1:5433/eaoc_test",
        )
        with patch.dict(os.environ, {"ALEMBIC_TARGET_TEST_DB": "1"}):
            with patch("eaoc.db.migrations.env.get_settings", return_value=mock_settings_1):
                url1 = get_url()
                self.assertTrue(url1.startswith("postgresql+asyncpg://"))

        # 2. postgres:// legacy prefix
        mock_settings_2 = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgres://prod_user:p1@db.supabase.co:5432/postgres",
            TEST_DATABASE_URL="postgres://test_user:p2@127.0.0.1:5433/eaoc_test",
        )
        with patch.dict(os.environ, {"ALEMBIC_TARGET_TEST_DB": "1"}):
            with patch("eaoc.db.migrations.env.get_settings", return_value=mock_settings_2):
                url2 = get_url()
                self.assertTrue(url2.startswith("postgresql+asyncpg://"))

    def test_secret_safety_in_rejection_messages(self):
        """Password sentinels and query tokens are never included in rejection errors."""
        sensitive_pass = "super_secret_test_pw_999!"
        mock_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql+asyncpg://prod_user:prod_pass@db.example.com:5432/app_db",
            TEST_DATABASE_URL=f"postgresql+asyncpg://admin:{sensitive_pass}@db.example.com:5432/app_db?token=secret_tok",
        )
        with patch.dict(os.environ, {"ALEMBIC_TARGET_TEST_DB": "1"}):
            with patch("eaoc.db.migrations.env.get_settings", return_value=mock_settings):
                with self.assertRaises(RuntimeError) as ctx:
                    get_url()
                err_msg = str(ctx.exception)
                self.assertNotIn(sensitive_pass, err_msg)
                self.assertNotIn("secret_tok", err_msg)
                self.assertNotIn("admin:", err_msg)

    def test_validation_occurs_before_engine_or_connection(self):
        """Confirm get_url() performs all safety checks purely via URL/string parsing without engine creation."""
        mock_settings = Settings(
            ENVIRONMENT="production",
            DATABASE_URL="postgresql+asyncpg://prod:p1@db.supabase.co:5432/prod_db",
            TEST_DATABASE_URL="postgresql+asyncpg://test:p2@127.0.0.1:5433/test_db",
        )
        # In production, get_url() must raise RuntimeError before returning a string or creating an engine
        with patch.dict(os.environ, {"ALEMBIC_TARGET_TEST_DB": "1"}):
            with patch("eaoc.db.migrations.env.get_settings", return_value=mock_settings):
                with patch("sqlalchemy.ext.asyncio.create_async_engine") as mock_engine:
                    with self.assertRaises(RuntimeError):
                        get_url()
                    mock_engine.assert_not_called()


if __name__ == "__main__":
    unittest.main()
