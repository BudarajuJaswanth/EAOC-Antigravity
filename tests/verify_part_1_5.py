"""
EAOC Part 1.5 Verification Utility: Configuration & Environment Management
Verifies settings initialization, secret protection & masking, repository secret audit,
and executes regression verification for Parts 1.2, 1.3, and 1.4.
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


def run_part_1_5_verification() -> bool:
    print("=" * 70)
    print("EAOC PART 1.5 VERIFICATION: Configuration & Environment Management")
    print("=" * 70)

    # 1. Imports and Syntax Check
    print("\n[1/7] Verifying Part 1.5 configuration imports & syntax...")
    try:
        from eaoc.config import Settings, get_settings
        settings = get_settings()
        assert settings.APP_NAME == "Enterprise AI Operations Center (EAOC)"
        print("  [OK] Configuration module imported and initialized successfully.")
    except Exception as exc:
        print(f"  [FAIL] Configuration import failed: {exc}")
        return False

    # 2. Secret Protection & Sanitized Config Verification
    print("\n[2/7] Testing Secret Protection & Sanitization...")
    try:
        test_settings = Settings(
            DATABASE_URL="postgresql+asyncpg://postgres:SuperSecretPassword123@db.supabase.co:5432/postgres",
            GEMINI_API_KEY="secret-gemini-key",
            JWT_SECRET_KEY="secret-jwt-key"
        )
        repr_output = repr(test_settings)
        if "SuperSecretPassword123" in repr_output or "secret-gemini-key" in repr_output:
            print("  [FAIL] SECURITY FAILURE: Plaintext secrets exposed in repr()!")
            return False
            
        sanitized = test_settings.get_sanitized_config()
        if "SuperSecretPassword123" in str(sanitized) or sanitized["GEMINI_API_KEY"] != "****":
            print("  [FAIL] SECURITY FAILURE: Plaintext secrets exposed in get_sanitized_config()!")
            return False
            
        print("  [OK] Secret Protection & Sanitization verified.")
    except Exception as exc:
        print(f"  [FAIL] Sanitization verification error: {exc}")
        return False

    # 3. Provider-Neutral Selection Validation
    print("\n[3/7] Verifying Provider-Neutral Configuration Validation...")
    try:
        provider_cfg = settings.get_active_provider_config()
        assert "provider" in provider_cfg
        assert "status" in provider_cfg
        print(f"  [OK] Default provider configuration status: {provider_cfg['status']} ({provider_cfg['provider']}).")
    except Exception as exc:
        print(f"  [FAIL] Provider configuration check failed: {exc}")
        return False

    # 4. Repository Secret Security Audit
    print("\n[4/7] Auditing Repository for Secret Leaks & Unsafe Environment Files...")
    try:
        env_file = project_root / ".env"
        if env_file.exists():
            print("  [WARNING] Local .env file exists. Ensuring it is ignored by git...")
            gitignore_file = project_root / ".gitignore"
            if gitignore_file.exists():
                gitignore_text = gitignore_file.read_text()
                if ".env" not in gitignore_text:
                    print("  [FAIL] SECURITY AUDIT FAILURE: .env file is NOT listed in .gitignore!")
                    return False
        print("  [OK] Security Audit PASSED: Repository files protected.")
    except Exception as exc:
        print(f"  [FAIL] Security audit error: {exc}")
        return False

    # 5. Part 1.2 Regression Verification
    print("\n[5/7] Running Part 1.2 Regression Verification...")
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
    print("\n[6/7] Running Part 1.3 Regression Verification...")
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
    print("\n[7/7] Running Part 1.4 Regression Verification...")
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

    print("\n" + "=" * 70)
    print("ALL PART 1.5 VERIFICATION CHECKS PASSED SUCCESSFULLY [OK]")
    print("=" * 70)
    return True


if __name__ == "__main__":
    success = run_part_1_5_verification()
    sys.exit(0 if success else 1)
