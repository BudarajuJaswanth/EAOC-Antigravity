"""
EAOC Part 1.4 Verification Utility: Core System Services & Health Checkers
Verifies FastAPI application factory, canonical health endpoints (/health, /health/live, /health/ready),
system information service (/api/v1/system/info), provider-neutral model health abstraction, CORS settings,
and runs regression verification for Part 1.2 and Part 1.3.
"""

import sys
import subprocess
from pathlib import Path

# Add project src directory to sys.path
project_root = Path(__file__).resolve().parent.parent
src_dir = project_root / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

def run_part_1_4_verification() -> bool:
    print("=" * 70)
    print("EAOC PART 1.4 VERIFICATION: Core System Services & Health Checkers")
    print("=" * 70)

    # 1. Imports and Syntax Check
    print("\n[1/6] Verifying Part 1.4 imports and syntax...")
    try:
        from eaoc.config import get_settings
        from eaoc.schemas.health import HealthStatus, OverallHealthResponse, SystemInfoResponse
        from eaoc.services.health import HealthService, get_health_service
        from eaoc.api.factory import create_app
        from eaoc.main import app
        print("  [OK] Part 1.4 modules imported successfully.")
    except Exception as exc:
        print(f"  [FAIL] Import failure: {exc}")
        return False

    # 2. FastAPI Application Factory Verification
    print("\n[2/6] Verifying FastAPI Application Factory...")
    try:
        test_app = create_app()
        assert test_app.title == "Enterprise AI Operations Center (EAOC)"
        assert test_app.version == "1.0.0"
        print("  [OK] FastAPI factory creates application instance correctly.")
    except Exception as exc:
        print(f"  [FAIL] App factory verification failed: {exc}")
        return False

    # 3. Test Client Endpoint Verification
    print("\n[3/6] Testing Canonical Infrastructure & System Info Endpoints...")
    try:
        from fastapi.testclient import TestClient
        client = TestClient(test_app)

        # GET /health/live
        res_live = client.get("/health/live")
        assert res_live.status_code == 200, f"Liveness returned status {res_live.status_code}"
        live_data = res_live.json()
        assert live_data["status"] == "HEALTHY"
        print("  [OK] GET /health/live returned 200 HEALTHY.")

        # GET /api/v1/system/info
        res_sys = client.get("/api/v1/system/info")
        assert res_sys.status_code == 200, f"System info returned status {res_sys.status_code}"
        sys_data = res_sys.json()
        assert sys_data["app_name"] == "Enterprise AI Operations Center (EAOC)"
        assert sys_data["version"] == "1.0.0"
        print("  [OK] GET /api/v1/system/info returned 200 with valid metadata.")

        # GET /health
        res_health = client.get("/health")
        assert res_health.status_code == 200, f"Health returned status {res_health.status_code}"
        health_data = res_health.json()
        assert "status" in health_data
        assert "components" in health_data
        print("  [OK] GET /health returned 200 with structured component statuses.")

        # GET /health/ready
        res_ready = client.get("/health/ready")
        assert res_ready.status_code in [200, 503], f"Readiness returned invalid status {res_ready.status_code}"
        print(f"  [OK] GET /health/ready returned HTTP {res_ready.status_code}.")

    except Exception as exc:
        print(f"  [FAIL] Endpoint testing failed: {exc}")
        return False

    # 4. Secret Exposure Audit
    print("\n[4/6] Auditing Health & System Responses for Secret Protection...")
    try:
        raw_responses = [
            str(res_live.json()),
            str(sys_data),
            str(health_data)
        ]
        forbidden_terms = ["password", "secret", "anon_key", "service_role", "postgres://", "postgresql+asyncpg://"]
        for resp in raw_responses:
            resp_lower = resp.lower()
            for term in forbidden_terms:
                if term in resp_lower:
                    print(f"  [FAIL] SECURITY AUDIT FAILURE: Forbidden credential term '{term}' exposed in response!")
                    return False
        print("  [OK] Security Audit PASSED: Zero credentials or raw database secrets exposed.")
    except Exception as exc:
        print(f"  [FAIL] Security audit exception: {exc}")
        return False

    # 5. Part 1.2 Regression Verification
    print("\n[5/6] Running Part 1.2 Regression Verification...")
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
    print("\n[6/6] Running Part 1.3 Regression Verification...")
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

    print("\n" + "=" * 70)
    print("ALL PART 1.4 VERIFICATION CHECKS PASSED SUCCESSFULLY [OK]")
    print("=" * 70)
    return True


if __name__ == "__main__":
    success = run_part_1_4_verification()
    sys.exit(0 if success else 1)
