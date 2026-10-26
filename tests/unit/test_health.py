"""
EAOC Part 1.4 Unit Test Suite: Health & Infrastructure Services
Uses standard Python unittest framework to verify application factory, canonical health endpoints,
system information endpoint, database health check handling, and secret protection.
"""

import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from eaoc.api.factory import create_app
from eaoc.schemas.health import DatabaseHealth, HealthStatus, RedisHealth


class TestHealthServices(unittest.TestCase):

    def setUp(self):
        self.app = create_app()
        self.client = TestClient(self.app)

    def test_app_creation(self):
        """Verify FastAPI application factory creates a valid app instance."""
        self.assertEqual(self.app.title, "Enterprise AI Operations Center (EAOC)")
        self.assertEqual(self.app.version, "1.0.0")

    def test_liveness_endpoint(self):
        """Verify GET /health/live returns HTTP 200 HEALTHY."""
        res = self.client.get("/health/live")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "HEALTHY")
        self.assertIn("process_uptime_seconds", data)
        self.assertGreaterEqual(data["process_uptime_seconds"], 0)

    def test_system_info_endpoint(self):
        """Verify GET /api/v1/system/info returns correct metadata without exposing secrets."""
        res = self.client.get("/api/v1/system/info")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["app_name"], "Enterprise AI Operations Center (EAOC)")
        self.assertEqual(data["version"], "1.0.0")
        self.assertEqual(data["api_version"], "v1")
        self.assertIn("environment", data)

        # Verify no secret keywords in payload
        json_str = str(data).lower()
        for forbidden in ["password", "secret", "anon_key", "service_role"]:
            self.assertNotIn(forbidden, json_str)

    def test_overall_health_unconfigured_db(self):
        """Verify GET /health when DATABASE_URL is not set or unconfigured."""
        with patch("eaoc.config.settings.Settings.is_db_configured", return_value=False):
            res = self.client.get("/health")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertIn("status", data)
            self.assertIn("components", data)
            db_comp = data["components"]["database"]
            self.assertEqual(db_comp["status"], "NOT_CONFIGURED")
            self.assertEqual(db_comp["pgvector_status"], "BLOCKED")

    def test_overall_health_healthy_db(self):
        """Verify GET /health with a mocked successful database connection."""
        mock_db_health = DatabaseHealth(
            status=HealthStatus.HEALTHY,
            details="Connected successfully to Supabase PostgreSQL.",
            pgvector_status="ENABLED",
            latency_ms=12.5,
        )
        mock_redis_health = RedisHealth(
            status=HealthStatus.HEALTHY,
            details="Connected successfully to Redis.",
        )
        with patch(
            "eaoc.services.health.HealthService.check_database_health",
            new_callable=AsyncMock,
            return_value=mock_db_health,
        ), patch(
            "eaoc.services.health.HealthService.check_redis_health",
            new_callable=AsyncMock,
            return_value=mock_redis_health,
        ):
            res = self.client.get("/health")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["status"], "HEALTHY")
            self.assertEqual(data["components"]["database"]["status"], "HEALTHY")
            self.assertEqual(data["components"]["database"]["pgvector_status"], "ENABLED")

    def test_readiness_endpoint_failing_db(self):
        """Verify GET /health/ready returns HTTP 503 when a configured DB connection fails."""
        mock_failing_db = DatabaseHealth(
            status=HealthStatus.UNHEALTHY,
            details="Supabase PostgreSQL connection failed: connection refused",
            pgvector_status="BLOCKED",
            latency_ms=None,
        )
        with patch(
            "eaoc.services.health.HealthService.check_database_health",
            new_callable=AsyncMock,
            return_value=mock_failing_db,
        ):
            res = self.client.get("/health/ready")
            self.assertEqual(res.status_code, 503)
            data = res.json()
            self.assertEqual(data["status"], "UNHEALTHY")
            self.assertEqual(data["components"]["database"], "UNHEALTHY")

    def test_model_provider_neutral_health(self):
        """Verify model provider health returns provider-neutral status without LLM clients."""
        mock_unconfigured = {
            "provider": "gemini",
            "category": "CLOUD",
            "model": "gemini-1.5-pro",
            "is_configured": False,
            "status": "NOT_CONFIGURED",
            "details": "Active provider 'gemini': GEMINI_API_KEY is not set.",
        }
        with patch(
            "eaoc.config.settings.Settings.get_active_provider_config",
            return_value=mock_unconfigured,
        ):
            res = self.client.get("/health")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            model_comp = data["components"]["model_providers"]
            self.assertEqual(model_comp["status"], "NOT_CONFIGURED")
            providers = model_comp["registered_providers"]
            provider_ids = [p["provider_id"] for p in providers]
            self.assertIn("lm_studio", provider_ids)
            self.assertIn("ollama", provider_ids)
            self.assertIn("gemini", provider_ids)


if __name__ == "__main__":
    unittest.main()
