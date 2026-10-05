"""
EAOC Phase 1 / Part 1.2 — Development Environment & Dependency Verification Utility
Note: This is a test/verification script only, NOT production code.
"""

import sys
import os
import importlib

# Ensure UTF-8 stdout encoding on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

def verify_environment():
    print("==================================================")
    print("EAOC PART 1.2 ENVIRONMENT & DEPENDENCY VERIFICATION")
    print("==================================================")
    
    # 1. Python Environment Check
    py_ver = sys.version.split()[0]
    py_exec = sys.executable
    print(f"[1] Python Executable: {py_exec}")
    print(f"[1] Python Version   : {py_ver}")
    if sys.version_info >= (3, 11):
        print("    -> Python Version Compatibility: PASSED (>= 3.11)")
    else:
        print("    -> Python Version Compatibility: FAILED (< 3.11)")

    # 2. Virtual Environment Check
    in_venv = sys.prefix != sys.base_prefix
    print(f"[2] Virtual Environment Active: {in_venv}")
    print(f"[2] Venv Path                : {sys.prefix}")
    
    # 3. Core Dependencies Import Verification
    core_packages = [
        ("fastapi", "FastAPI"),
        ("pydantic", "Pydantic"),
        ("langgraph", "LangGraph"),
        ("langchain_core", "LangChain Core"),
        ("sqlalchemy", "SQLAlchemy"),
        ("asyncpg", "asyncpg"),
        ("redis", "Redis Client"),
        ("pypdf", "PyPDF"),
        ("docx", "python-docx"),
        ("pandas", "Pandas"),
        ("httpx", "HTTPX"),
        ("uvicorn", "Uvicorn"),
    ]
    
    print("\n[3] Core Dependency Compatibility Table:")
    print("| Package | Import Name | Version | Import Status |")
    print("|---|---|---|---|")
    
    all_packages_ok = True
    for mod_name, label in core_packages:
        try:
            mod = importlib.import_module(mod_name)
            ver = getattr(mod, "__version__", "Available")
            print(f"| {label} | `{mod_name}` | {ver} | SUCCESS [OK] |")
        except Exception as e:
            all_packages_ok = False
            print(f"| {label} | `{mod_name}` | N/A | FAILED [FAIL] ({e}) |")
            
    # 4. EAOC Package Import Verification
    print("\n[4] EAOC Package Discovery & Structural Subpackage Import Test:")
    eaoc_subpackages = [
        "eaoc",
        "eaoc.api",
        "eaoc.api.v1",
        "eaoc.api.middlewares",
        "eaoc.config",
        "eaoc.db",
        "eaoc.evaluation",
        "eaoc.knowledge",
        "eaoc.orchestration",
        "eaoc.security",
        "eaoc.state",
        "eaoc.tools",
        "eaoc.agents",
        # 12 approved logical agents
        "eaoc.agents.research_rag",
        "eaoc.agents.sql_data",
        "eaoc.agents.document",
        "eaoc.agents.analytics",
        "eaoc.agents.finance",
        "eaoc.agents.hr",
        "eaoc.agents.it_support",
        "eaoc.agents.developer",
        "eaoc.agents.reviewer_validation",
        "eaoc.agents.security_policy",
        "eaoc.agents.orchestrator",
        "eaoc.agents.planner",
    ]
    
    all_eaoc_ok = True
    for subpkg in eaoc_subpackages:
        try:
            importlib.import_module(subpkg)
            print(f"  - `{subpkg}`: IMPORTED [OK]")
        except Exception as e:
            all_eaoc_ok = False
            print(f"  - `{subpkg}`: FAILED [FAIL] ({e})")
            
    # 5. Configuration & Secret Audit
    print("\n[5] Security & Configuration Check:")
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    gitignore_path = os.path.join(root_dir, ".gitignore")
    env_example_path = os.path.join(root_dir, ".env.example")
    
    # Check .gitignore
    gitignore_ok = False
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8") as f:
            content = f.read()
            if ".env" in content and ".venv" in content:
                gitignore_ok = True
    print(f"  - `.gitignore` protects `.env` & `.venv`: {'PASSED [OK]' if gitignore_ok else 'FAILED [FAIL]'}")
    
    # Check .env.example
    env_example_ok = False
    if os.path.exists(env_example_path):
        with open(env_example_path, "r", encoding="utf-8") as f:
            content = f.read()
            if "change_this" in content or "placeholder" in content.lower():
                env_example_ok = True
    print(f"  - `.env.example` contains placeholders (no real secrets): {'PASSED [OK]' if env_example_ok else 'FAILED [FAIL]'}")

    print("\n==================================================")
    if all_packages_ok and all_eaoc_ok and gitignore_ok and env_example_ok:
        print("PART 1.2 VERIFICATION RESULT: PASSED [OK]")
    else:
        print("PART 1.2 VERIFICATION RESULT: FAILED [FAIL]")
    print("==================================================")

if __name__ == "__main__":
    verify_environment()
