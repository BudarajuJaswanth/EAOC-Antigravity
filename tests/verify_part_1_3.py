"""
EAOC Phase 1 / Part 1.3 — Database Foundation & Supabase Connectivity Verification Utility
Note: This is a test/verification script only, NOT production code.
"""

import sys
import os
import asyncio
import importlib
from sqlalchemy import text

# Ensure root directory is in sys.path
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

# Ensure UTF-8 stdout encoding on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


async def verify_part_1_3():
    print("==================================================")
    print("EAOC PART 1.3 SUPABASE DATABASE FOUNDATION VERIFICATION")
    print("==================================================")

    # 1. Configuration & Password Masking Verification
    print("\n[1] Configuration & Settings Audit:")
    from eaoc.config import get_settings
    settings = get_settings()
    
    db_configured = settings.is_db_configured()
    sanitized_url = settings.get_sanitized_db_url()
    
    print(f"  - pydantic-settings Loaded: YES [OK]")
    print(f"  - Database Configured State: {db_configured}")
    print(f"  - Sanitized DATABASE_URL   : {sanitized_url}")
    
    # Secret exposure check
    secret_leaked = False
    if db_configured and settings.DATABASE_URL:
        from urllib.parse import urlparse
        parsed = urlparse(settings.DATABASE_URL)
        if parsed.password and parsed.password in sanitized_url:
            secret_leaked = True
            
    print(f"  - Password Masking Security: {'PASSED [OK]' if not secret_leaked else 'FAILED [FAIL]'}")

    # 2. Database Connection Layer Architecture
    print("\n[2] Database Connection Layer Architecture:")
    from eaoc.db import Base, engine, AsyncSessionFactory, get_async_db_session
    print(f"  - DeclarativeBase Root     : `{Base.__name__}` [OK]")
    print(f"  - AsyncEngine Instantiated : {'YES [OK]' if engine is not None else 'NO (Unconfigured)'}")
    print(f"  - AsyncSessionFactory      : {'YES [OK]' if AsyncSessionFactory is not None else 'NO (Unconfigured)'}")

    # 3. Supabase Connectivity & Minimal Query Test
    print("\n[3] Supabase PostgreSQL Connectivity Test:")
    db_conn_status = "BLOCKED"
    pgver_str = "N/A"
    pgvector_status = "BLOCKED"
    
    if not db_configured or engine is None:
        print("  - Connection Status        : BLOCKED (Credentials Not Configured in .env)")
        print("  - Minimal Query (SELECT 1) : BLOCKED")
        print("    --> Action Required      : Add real Supabase DATABASE_URL to local .env file")
        pgvector_status = "BLOCKED"
    else:
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT 1;"))
                val = result.scalar()
                
                ver_res = await conn.execute(text("SELECT version();"))
                pgver_str = ver_res.scalar()
                
                if val == 1:
                    db_conn_status = "PASS"
                    print(f"  - Connection Status        : PASS [OK]")
                    print(f"  - Minimal Query (SELECT 1) : PASS [OK]")
                    print(f"  - PostgreSQL Version       : {pgver_str}")
                    
                    # Test pgvector extension state
                    try:
                        ext_res = await conn.execute(
                            text("SELECT extname FROM pg_extension WHERE extname = 'vector';")
                        )
                        ext_name = ext_res.scalar()
                        if ext_name == 'vector':
                            pgvector_status = "PASS"
                        else:
                            try:
                                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
                                await conn.commit()
                                pgvector_status = "PASS"
                            except Exception:
                                pgvector_status = "MANUAL ACTION REQUIRED"
                    except Exception:
                        pgvector_status = "MANUAL ACTION REQUIRED"
        except Exception as e:
            db_conn_status = "FAILED"
            print(f"  - Connection Status        : FAILED [FAIL]")
            print(f"  - Connection Error         : {e}")
            pgvector_status = "BLOCKED"

    # 4. pgvector Readiness Classification
    print("\n[4] pgvector Extension Readiness Status:")
    if pgvector_status == "PASS":
        print("  - pgvector Classification   : PASS [OK] (vector extension active)")
    elif pgvector_status == "BLOCKED":
        print("  - pgvector Classification   : BLOCKED (Database connection unconfigured/unavailable)")
    else:
        print("  - pgvector Classification   : MANUAL ACTION REQUIRED")
        print("    --> Action Required       : Enable 'vector' extension via Supabase Dashboard -> Database -> Extensions")

    # 5. Alembic Migration Structure Verification
    print("\n[5] Migration Foundation (Alembic Setup):")
    alembic_ini_path = os.path.join(root_dir, "alembic.ini")
    env_py_path = os.path.join(root_dir, "src", "eaoc", "db", "migrations", "env.py")
    migration_version_path = os.path.join(root_dir, "src", "eaoc", "db", "migrations", "versions", "0001_initial_foundation.py")
    
    alembic_ok = os.path.exists(alembic_ini_path) and os.path.exists(env_py_path) and os.path.exists(migration_version_path)
    print(f"  - `alembic.ini` Exists    : {'YES [OK]' if os.path.exists(alembic_ini_path) else 'NO [FAIL]'}")
    print(f"  - Async `env.py` Exists   : {'YES [OK]' if os.path.exists(env_py_path) else 'NO [FAIL]'}")
    print(f"  - Initial Migration 0001  : {'YES [OK]' if os.path.exists(migration_version_path) else 'NO [FAIL]'}")

    # 6. Part 1.2 Regression Verification Check
    print("\n[6] Part 1.2 Regression Verification Check:")
    try:
        import fastapi, pydantic, langgraph, sqlalchemy, asyncpg, redis, pypdf, docx, pandas, httpx, uvicorn
        import eaoc, eaoc.api, eaoc.agents, eaoc.config, eaoc.db
        print("  - Part 1.2 Core Dependencies & Package Discovery: PASSED [OK]")
        
        from tests.verify_part_1_2 import verify_environment
        print("  - Part 1.2 Regression Test Suite Execution:")
        verify_environment()
        part_1_2_ok = True
    except Exception as reg_err:
        print(f"  - Part 1.2 Regression Check: FAILED [FAIL] ({reg_err})")
        part_1_2_ok = False

    print("\n==================================================")
    print("PART 1.3 VERIFICATION SUMMARY:")
    print(f"  - Database Configured     : {db_configured}")
    print(f"  - Connectivity Status     : {db_conn_status}")
    print(f"  - pgvector Status         : {pgvector_status}")
    print(f"  - Migration Setup         : {'PASS [OK]' if alembic_ok else 'FAIL [FAIL]'}")
    print(f"  - Part 1.2 Regression Check: {'PASS [OK]' if part_1_2_ok else 'FAIL [FAIL]'}")
    print("==================================================")


if __name__ == "__main__":
    asyncio.run(verify_part_1_3())
