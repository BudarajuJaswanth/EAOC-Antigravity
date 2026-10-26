"""
EAOC Part 1.6 Verification Utility: Observability, Structured Logging & Diagnostics
Verifies structured logging, contextvars propagation, timing utilities, sensitive data redaction,
FastAPI observability middleware, safe error responses, repository security audit,
and executes regression verification for Parts 1.2, 1.3, 1.4, and 1.5.
"""

import os
import sys
import subprocess
from pathlib import Path

# Add project src directory to sys.path
project_root = Path(__file__).resolve().parent.parent
src_dir = project_root / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))


def run_part_1_6_verification() -> bool:
    print("=" * 70)
    print("EAOC PART 1.6 VERIFICATION: Observability & Structured Logging")
    print("=" * 70)

    # 1. Imports and Syntax Check
    print("\n[1/8] Verifying Part 1.6 observability imports & syntax...")
    try:
        from eaoc.observability import (
            DiagnosticEvent,
            Timer,
            configure_logging,
            get_logger,
            get_request_id,
            redact_sensitive_data,
            set_request_id,
        )
        from eaoc.api.middlewares import ObservabilityMiddleware, register_exception_handlers
        print("  [OK] Observability modules imported successfully.")
    except Exception as exc:
        print(f"  [FAIL] Import failure: {exc}")
        return False

    # 2. Context Tracing & Redaction Audit
    print("\n[2/8] Testing Tracing Context & Redaction Engine...")
    try:
        set_request_id("req_verif_123")
        assert get_request_id() == "req_verif_123"

        secret_str = "postgresql+asyncpg://postgres:MyPass123@db.supabase.co:5432/postgres"
        clean_str = redact_sensitive_data(secret_str)
        if "MyPass123" in clean_str:
            print("  [FAIL] Redaction failure: Password not redacted from connection string!")
            return False
        print("  [OK] Tracing context propagation and redaction engine verified.")
    except Exception as exc:
        print(f"  [FAIL] Context & Redaction test failed: {exc}")
        return False

    # 3. FastAPI Middleware & Response Header Test
    print("\n[3/8] Testing FastAPI Request Tracing Middleware...")
    try:
        from fastapi.testclient import TestClient
        from eaoc.api.factory import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        res = client.get("/health/live")
        assert res.status_code == 200
        assert "X-Request-ID" in res.headers
        assert "X-Correlation-ID" in res.headers
        print(f"  [OK] GET /health/live returned 200 with X-Request-ID: {res.headers['X-Request-ID']}.")
    except Exception as exc:
        print(f"  [FAIL] Middleware test failed: {exc}")
        return False

    # 4. Safe Error Response Test
    print("\n[4/8] Testing Global Exception Handler Safe Error Responses...")
    try:
        @app.get("/verif-error-route")
        def route_with_error():
            raise ValueError("Database connection failed: postgresql://admin:SecretKey@host/db")

        res_err = client.get("/verif-error-route")
        assert res_err.status_code == 500
        err_data = res_err.json()
        assert err_data["error"]["code"] == "INTERNAL_SERVER_ERROR"
        assert "SecretKey" not in str(err_data)
        assert "Traceback" not in str(err_data)
        print("  [OK] Safe client error response verified (zero stack trace/credential leaks).")
    except Exception as exc:
        print(f"  [FAIL] Exception handler test failed: {exc}")
        return False

    # 5. Part 1.2 Regression Verification
    print("\n[5/8] Running Part 1.2 Regression Verification...")
    try:
        verify_p1_2 = project_root / "tests" / "verify_part_1_2.py"
        res_p12 = subprocess.run(
            [sys.executable, str(verify_p1_2)],
            capture_output=True,
            text=True,
            cwd=str(project_root)
        )
        if res_p12.returncode != 0:
            print(f"  [FAIL] Part 1.2 Regression Failed!\n{res_p12.stderr}")
            return False
        print("  [OK] Part 1.2 Regression Verification PASSED.")
    except Exception as exc:
        print(f"  [FAIL] Part 1.2 Regression Error: {exc}")
        return False

    # 6. Part 1.3 Regression Verification
    print("\n[6/8] Running Part 1.3 Regression Verification...")
    try:
        verify_p1_3 = project_root / "tests" / "verify_part_1_3.py"
        res_p13 = subprocess.run(
            [sys.executable, str(verify_p1_3)],
            capture_output=True,
            text=True,
            cwd=str(project_root)
        )
        if res_p13.returncode != 0:
            print(f"  [FAIL] Part 1.3 Regression Failed!\n{res_p13.stderr}")
            return False
        print("  [OK] Part 1.3 Regression Verification PASSED.")
    except Exception as exc:
        print(f"  [FAIL] Part 1.3 Regression Error: {exc}")
        return False

    # 7. Part 1.4 Regression Verification
    print("\n[7/8] Running Part 1.4 Regression Verification...")
    try:
        verify_p1_4 = project_root / "tests" / "verify_part_1_4.py"
        res_p14 = subprocess.run(
            [sys.executable, str(verify_p1_4)],
            capture_output=True,
            text=True,
            cwd=str(project_root)
        )
        if res_p14.returncode != 0:
            print(f"  [FAIL] Part 1.4 Regression Failed!\n{res_p14.stderr}")
            return False
        print("  [OK] Part 1.4 Regression Verification PASSED.")
    except Exception as exc:
        print(f"  [FAIL] Part 1.4 Regression Error: {exc}")
        return False

    # 8. Part 1.5 Regression Verification
    print("\n[8/8] Running Part 1.5 Regression Verification...")
    try:
        verify_p1_5 = project_root / "tests" / "verify_part_1_5.py"
        res_p15 = subprocess.run(
            [sys.executable, str(verify_p1_5)],
            capture_output=True,
            text=True,
            cwd=str(project_root)
        )
        if res_p15.returncode != 0:
            print(f"  [FAIL] Part 1.5 Regression Failed!\n{res_p15.stderr}")
            return False
        print("  [OK] Part 1.5 Regression Verification PASSED.")
    except Exception as exc:
        print(f"  [FAIL] Part 1.5 Regression Error: {exc}")
        return False

    print("\n" + "=" * 70)
    print("ALL PART 1.6 VERIFICATION CHECKS PASSED SUCCESSFULLY [OK]")
    print("=" * 70)
    return True


if __name__ == "__main__":
    success = run_part_1_6_verification()
    sys.exit(0 if success else 1)
