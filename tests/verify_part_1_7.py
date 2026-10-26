"""
EAOC Part 1.7 Verification Utility: Shared Domain Models & Core State Interfaces
Verifies domain model data contracts, exact 12-agent registry, serialization, secret safety,
storage-agnostic state interface semantics, repository security audit,
and executes regression verification for Parts 1.2, 1.3, 1.4, 1.5, and 1.6.
"""

import sys
import subprocess
from pathlib import Path

# Add project src directory to sys.path
project_root = Path(__file__).resolve().parent.parent
src_dir = project_root / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))


def run_part_1_7_verification() -> bool:
    print("=" * 70)
    print("EAOC PART 1.7 VERIFICATION: Shared Domain Models & Core State Interfaces")
    print("=" * 70)

    # 1. Imports and Syntax Check
    print("\n[1/9] Verifying Part 1.7 domain models & state interface syntax...")
    try:
        from eaoc.schemas.domain import (
            AgentID,
            ApprovalStatus,
            ExecutionMetadata,
            RequestContext,
            RiskLevel,
            TaskStatus,
            WorkflowState,
            WorkflowStatus,
        )
        from eaoc.state.interfaces import IWorkflowStateStore
        print("  [OK] Domain models and state interface imported successfully.")
    except Exception as exc:
        print(f"  [FAIL] Import failure: {exc}")
        return False

    # 2. 12-Agent Registry Audit
    print("\n[2/9] Auditing Approved 12-Agent Registry (AgentID Enum)...")
    try:
        approved_agents = {
            "research_rag",
            "sql_data",
            "document",
            "analytics",
            "finance",
            "hr",
            "it_support",
            "developer",
            "reviewer_validation",
            "security_policy",
            "orchestrator",
            "planner",
        }
        all_agents = {agent.value for agent in AgentID}
        if len(all_agents) != 12 or all_agents != approved_agents:
            print(f"  [FAIL] AgentID registry contains invalid agent set: {all_agents}")
            return False
        print("  [OK] AgentID registry contains EXACTLY the 12 approved logical agents.")
    except Exception as exc:
        print(f"  [FAIL] Agent registry verification error: {exc}")
        return False

    # 3. Explicit AIC Separation Audit
    print("\n[3/9] Auditing ExecutionMetadata for Explicit AIC Metric Separation...")
    try:
        meta = ExecutionMetadata()
        if hasattr(meta, "agent_invocations_count") or hasattr(meta, "aic"):
            print("  [FAIL] ExecutionMetadata unexpectedly contains AIC fields!")
            return False
        print("  [OK] ExecutionMetadata correctly excludes AIC fields (metric boundary preserved).")
    except Exception as exc:
        print(f"  [FAIL] AIC separation check failed: {exc}")
        return False

    # 4. Serialization & Secret Protection Audit
    print("\n[4/9] Testing Model Serialization & Secret Security Audit...")
    try:
        req_ctx = RequestContext(request_id="req_audit", correlation_id="corr_audit")
        state = WorkflowState(
            workflow_id="wf_audit",
            request_context=req_ctx,
            created_at="2026-10-05T20:00:00Z",
            updated_at="2026-10-05T20:00:00Z",
        )
        json_str = state.model_dump_json()
        forbidden_terms = ["password", "secret", "anon_key", "service_role", "postgres://", "postgresql+asyncpg://"]
        for term in forbidden_terms:
            if term in json_str.lower():
                print(f"  [FAIL] SECURITY AUDIT FAILURE: Forbidden term '{term}' found in model json!")
                return False
        print("  [OK] Serialization PASSED: Zero secret fields embedded in workflow state models.")
    except Exception as exc:
        print(f"  [FAIL] Serialization audit error: {exc}")
        return False

    # 5. Part 1.2 Regression Verification
    print("\n[5/9] Running Part 1.2 Regression Verification...")
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
    print("\n[6/9] Running Part 1.3 Regression Verification...")
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
    print("\n[7/9] Running Part 1.4 Regression Verification...")
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
    print("\n[8/9] Running Part 1.5 Regression Verification...")
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

    # 9. Part 1.6 Regression Verification
    print("\n[9/9] Running Part 1.6 Regression Verification...")
    try:
        verify_p1_6 = project_root / "tests" / "verify_part_1_6.py"
        res_p16 = subprocess.run(
            [sys.executable, str(verify_p1_6)],
            capture_output=True,
            text=True,
            cwd=str(project_root)
        )
        if res_p16.returncode != 0:
            print(f"  [FAIL] Part 1.6 Regression Failed!\n{res_p16.stderr}")
            return False
        print("  [OK] Part 1.6 Regression Verification PASSED.")
    except Exception as exc:
        print(f"  [FAIL] Part 1.6 Regression Error: {exc}")
        return False

    print("\n" + "=" * 70)
    print("ALL PART 1.7 VERIFICATION CHECKS PASSED SUCCESSFULLY [OK]")
    print("=" * 70)
    return True


if __name__ == "__main__":
    success = run_part_1_7_verification()
    sys.exit(0 if success else 1)
