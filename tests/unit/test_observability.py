"""
EAOC Part 1.6 Unit Test Suite: Observability, Structured Logging & Diagnostics
Tests context propagation, redaction engine, timing utilities, JSON formatting,
FastAPI observability middleware, global exception handler, and backward compatibility.
"""

import json
import logging
import unittest
from io import StringIO
from fastapi.testclient import TestClient
from eaoc.api.factory import create_app
from eaoc.observability.context import (
    clear_tracing_context,
    get_current_context,
    set_correlation_id,
    set_request_id,
)
from eaoc.observability.formatter import StructuredJsonFormatter
from eaoc.observability.redaction import redact_sensitive_data
from eaoc.observability.timing import Timer


class TestObservabilityModule(unittest.TestCase):

    def setUp(self):
        clear_tracing_context()
        self.app = create_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self):
        clear_tracing_context()

    def test_context_propagation(self):
        """Verify request_id and correlation_id contextvars storage and clearing."""
        set_request_id("req_test123")
        set_correlation_id("corr_test456")

        ctx = get_current_context()
        self.assertEqual(ctx["request_id"], "req_test123")
        self.assertEqual(ctx["correlation_id"], "corr_test456")

        clear_tracing_context()
        self.assertEqual(get_current_context(), {})

    def test_redaction_engine(self):
        """Verify sensitive credentials, keys, and connection strings are redacted."""
        raw_db_url = "postgresql+asyncpg://postgres:SuperSecret123@db.supabase.co:5432/postgres"
        redacted_url = redact_sensitive_data(raw_db_url)
        self.assertNotIn("SuperSecret123", redacted_url)
        self.assertIn("****@db.supabase.co", redacted_url)

        sensitive_dict = {
            "user": "admin",
            "password": "my_password",
            "api_key": "secret-gemini-key",
            "safe_field": "public_val",
        }
        clean_dict = redact_sensitive_data(sensitive_dict)
        self.assertEqual(clean_dict["password"], "****")
        self.assertEqual(clean_dict["api_key"], "****")
        self.assertEqual(clean_dict["safe_field"], "public_val")

    def test_timer_utility(self):
        """Verify monotonic Timer measures duration in milliseconds."""
        with Timer() as timer:
            import time
            time.sleep(0.01)  # 10ms
        self.assertGreaterEqual(timer.elapsed_ms, 8.0)

    def test_structured_json_formatter(self):
        """Verify StructuredJsonFormatter outputs valid JSON with expected fields."""
        formatter = StructuredJsonFormatter()
        set_request_id("req_json_test")

        log_record = logging.LogRecord(
            name="eaoc.test",
            level=logging.INFO,
            pathname="test_path.py",
            lineno=10,
            msg="Structured event message",
            args=(),
            exc_info=None,
        )
        setattr(log_record, "event_name", "unit_test_event")
        setattr(log_record, "duration_ms", 12.5)

        formatted_json_str = formatter.format(log_record)
        data = json.loads(formatted_json_str)

        self.assertEqual(data["logger"], "eaoc.test")
        self.assertEqual(data["log_level"], "INFO")
        self.assertEqual(data["message"], "Structured event message")
        self.assertEqual(data["request_id"], "req_json_test")
        self.assertEqual(data["event_name"], "unit_test_event")
        self.assertEqual(data["duration_ms"], 12.5)
        self.assertIn("timestamp", data)
        self.assertEqual(data["service"], "eaoc-core")

    def test_observability_middleware_headers(self):
        """Verify ObservabilityMiddleware injects X-Request-ID and X-Correlation-ID into responses."""
        res = self.client.get("/health/live", headers={"X-Correlation-ID": "corr_custom_999"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("X-Request-ID", res.headers)
        self.assertEqual(res.headers["X-Correlation-ID"], "corr_custom_999")

    def test_global_exception_handler_safe_response(self):
        """Verify global exception handler returns safe HTTP 500 JSON without exposing stack trace."""

        @self.app.get("/test-error-endpoint")
        def error_route():
            raise RuntimeError("Database connection string postgresql://user:SecretPass123@host/db failed")

        res = self.client.get("/test-error-endpoint")
        self.assertEqual(res.status_code, 500)
        data = res.json()
        self.assertIn("error", data)
        err = data["error"]
        self.assertEqual(err["code"], "INTERNAL_SERVER_ERROR")
        self.assertIn("request_id", err)
        self.assertNotIn("SecretPass123", str(data))
        self.assertNotIn("Traceback", str(data))

    def test_health_endpoints_backward_compatibility(self):
        """Verify all Part 1.4 infrastructure endpoints remain fully functional."""
        res_live = self.client.get("/health/live")
        self.assertEqual(res_live.status_code, 200)

        res_sys = self.client.get("/api/v1/system/info")
        self.assertEqual(res_sys.status_code, 200)

        res_health = self.client.get("/health")
        self.assertEqual(res_health.status_code, 200)


if __name__ == "__main__":
    unittest.main()
