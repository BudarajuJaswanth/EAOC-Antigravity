"""
EAOC Unit Test Suite - Approval REST Endpoints (Part 2.16-B)

Tests:
1. List pending approvals with pagination.
2. Tenant isolation for reads (list and detail).
3. Retrieve approval detail.
4. Record approval decision (APPROVED) with verified approver attribution.
5. Record rejection decision (REJECTED).
6. Missing or invalid authentication returns 401.
7. Insufficient roles return 403.
8. Duplicate decisions return 409 Conflict.
9. Expired approvals return 410 Gone and are excluded from pending list.
10. Malformed decision payloads return 422.
11. Confirmation that zero orchestrator execution or resume occurs.
12. Spoofed headers have zero effect on authenticated principal.
13. Database availability failure returns clean 503.
"""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import jwt

from eaoc.api.factory import create_app
from eaoc.api.v1.endpoints.approvals import get_approval_repository
from eaoc.config import Settings, get_settings
from eaoc.schemas.domain import (
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
)
from eaoc.state.approval_repository import (
    IApprovalRepository,
    InMemoryApprovalRepository,
)

TEST_SECRET = "super-secret-test-signing-key-for-eaoc-tests-32bytes-min"
TEST_ISSUER = "eaoc-test-issuer"
TEST_AUDIENCE = "eaoc-test-audience"


def make_token(
    sub: str = "usr-approver-1",
    tenant_id: str = "tenant-alpha",
    roles=None,
    exp_offset_seconds: int = 3600,
) -> str:
    """Helper creating signed test JWT for approval endpoint tests."""
    if roles is None:
        roles = ["approver", "finance_user"]
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "tenant_id": tenant_id,
        "roles": roles,
        "iss": TEST_ISSUER,
        "aud": TEST_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=exp_offset_seconds)).timestamp()),
    }
    return jwt.encode(payload, TEST_SECRET, algorithm="HS256")


def make_domain_request(
    approval_id: str = "appr-1234567890ab",
    workflow_id: str = "wf-abcdef123456",
    requested_action: str = "Execute critical action",
    risk_level: RiskLevel = RiskLevel.HIGH,
    status: ApprovalStatus = ApprovalStatus.PENDING,
) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=approval_id,
        workflow_id=workflow_id,
        requested_action=requested_action,
        risk_level=risk_level,
        status=status,
        requested_at=datetime.now(timezone.utc).isoformat(),
        details={"step_index": 1, "task_id": "task-01"},
    )


class FailingApprovalRepository(InMemoryApprovalRepository):
    """Repository double that simulates Supabase connection failure."""

    async def list_pending_models(self, tenant_id: str, limit: int = 50, offset: int = 0):
        raise RuntimeError("Connection refused to Supabase PostgreSQL at db.supabase.co:5432")

    async def get_model_by_id(self, approval_id: str, tenant_id: str):
        raise RuntimeError("Connection refused to Supabase PostgreSQL at db.supabase.co:5432")

    async def record_conditional_decision(self, approval_id, tenant_id, decision, approver_id, reason=None):
        raise RuntimeError("Connection refused to Supabase PostgreSQL at db.supabase.co:5432")


class TestApiApprovals(unittest.IsolatedAsyncioTestCase):
    """Test suite for Part 2.16-B Approval REST Endpoints."""

    async def asyncSetUp(self):
        self.app = create_app()
        self.test_repo = InMemoryApprovalRepository()
        self.tenant_a = "tenant-alpha"
        self.tenant_b = "tenant-beta"

        self.test_settings = Settings(
            ENVIRONMENT="testing",
            JWT_SECRET_KEY=TEST_SECRET,
            JWT_ALGORITHM="HS256",
            JWT_ISSUER=TEST_ISSUER,
            JWT_AUDIENCE=TEST_AUDIENCE,
        )

        self.app.dependency_overrides[get_approval_repository] = lambda: self.test_repo
        self.app.dependency_overrides[get_settings] = lambda: self.test_settings
        self.client = TestClient(self.app)

    async def asyncTearDown(self):
        self.app.dependency_overrides.clear()

    async def test_1_list_pending_approvals_success(self):
        """1. Verify GET /api/v1/approvals returns paginated list of pending approvals for tenant."""
        now = datetime.now(timezone.utc)
        for i in range(3):
            req = make_domain_request(approval_id=f"appr-list-{i}")
            await self.test_repo.create_request(req, self.tenant_a, now + timedelta(hours=24))

        token = make_token(tenant_id=self.tenant_a)
        response = self.client.get(
            "/api/v1/approvals?limit=2&offset=0",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["limit"], 2)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["items"][0]["approval_id"], "appr-list-0")
        self.assertEqual(data["items"][1]["approval_id"], "appr-list-1")

    async def test_2_tenant_isolation_reads(self):
        """2. Verify approvals belonging to Tenant A cannot be seen or retrieved by Tenant B."""
        now = datetime.now(timezone.utc)
        req_a = make_domain_request(approval_id="appr-tenant-a-only")
        await self.test_repo.create_request(req_a, self.tenant_a, now + timedelta(hours=24))

        # Tenant B lists approvals -> returns 0 items
        token_b = make_token(tenant_id=self.tenant_b)
        res_list = self.client.get(
            "/api/v1/approvals",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        self.assertEqual(res_list.status_code, 200)
        self.assertEqual(res_list.json()["total"], 0)
        self.assertEqual(len(res_list.json()["items"]), 0)

        # Tenant B retrieves Tenant A approval directly -> 404 Not Found
        res_detail = self.client.get(
            "/api/v1/approvals/appr-tenant-a-only",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        self.assertEqual(res_detail.status_code, 404)
        self.assertIn("not found", res_detail.json()["detail"].lower())

    async def test_3_get_approval_detail_success(self):
        """3. Verify GET /api/v1/approvals/{approval_id} retrieves full approval representation."""
        now = datetime.now(timezone.utc)
        req = make_domain_request(approval_id="appr-detail-test")
        await self.test_repo.create_request(req, self.tenant_a, now + timedelta(hours=12))

        token = make_token(tenant_id=self.tenant_a)
        response = self.client.get(
            "/api/v1/approvals/appr-detail-test",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["approval_id"], "appr-detail-test")
        self.assertEqual(data["status"], "PENDING")
        self.assertEqual(data["tenant_id"], self.tenant_a)
        self.assertEqual(data["risk_level"], "HIGH")
        self.assertEqual(data["resume_status"], "none")

    async def test_4_record_approval_decision_success(self):
        """4. Verify POST /api/v1/approvals/{approval_id}/decision commits decision with approver attribution."""
        now = datetime.now(timezone.utc)
        req = make_domain_request(approval_id="appr-decide-ok")
        await self.test_repo.create_request(req, self.tenant_a, now + timedelta(hours=12))

        token = make_token(sub="usr-lead-architect", tenant_id=self.tenant_a, roles=["approver"])
        payload = {
            "decision": "APPROVED",
            "reason": "Authorized after architecture review"
        }
        response = self.client.post(
            "/api/v1/approvals/appr-decide-ok/decision",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["approval_id"], "appr-decide-ok")
        self.assertEqual(data["decision"], "APPROVED")
        self.assertEqual(data["approver_id"], "usr-lead-architect")
        self.assertEqual(data["status"], "APPROVED")
        self.assertEqual(data["resume_status"], "pending")
        self.assertIn("pending", data["message"].lower())

        # Verify detail endpoint shows updated decision
        detail_resp = self.client.get(
            "/api/v1/approvals/appr-decide-ok",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(detail_resp.status_code, 200)
        detail_data = detail_resp.json()
        self.assertEqual(detail_data["status"], "APPROVED")
        self.assertEqual(detail_data["decision"]["decision"], "APPROVED")
        self.assertEqual(detail_data["decision"]["approver_id"], "usr-lead-architect")

    async def test_5_record_rejection_decision_success(self):
        """5. Verify POST /decision with REJECTED records rejection correctly."""
        now = datetime.now(timezone.utc)
        req = make_domain_request(approval_id="appr-reject-test")
        await self.test_repo.create_request(req, self.tenant_a, now + timedelta(hours=12))

        token = make_token(sub="usr-sec-ops", tenant_id=self.tenant_a, roles=["admin"])
        payload = {
            "decision": "REJECTED",
            "reason": "Security violation detected in requested operation"
        }
        response = self.client.post(
            "/api/v1/approvals/appr-reject-test/decision",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["decision"], "REJECTED")
        self.assertEqual(data["status"], "REJECTED")
        self.assertEqual(data["approver_id"], "usr-sec-ops")

    async def test_6_unauthorized_missing_token(self):
        """6. Verify all endpoints return HTTP 401 when Authorization header is absent or invalid."""
        # a. List
        r1 = self.client.get("/api/v1/approvals")
        self.assertEqual(r1.status_code, 401)

        # b. Detail
        r2 = self.client.get("/api/v1/approvals/appr-any")
        self.assertEqual(r2.status_code, 401)

        # c. Decision
        r3 = self.client.post("/api/v1/approvals/appr-any/decision", json={"decision": "APPROVED"})
        self.assertEqual(r3.status_code, 401)

    async def test_7_forbidden_insufficient_role(self):
        """7. Verify decision endpoint returns HTTP 403 when user lacks approver/admin role."""
        now = datetime.now(timezone.utc)
        req = make_domain_request(approval_id="appr-role-test")
        await self.test_repo.create_request(req, self.tenant_a, now + timedelta(hours=12))

        # Caller has 'viewer' and 'developer' roles only (no approver/admin)
        token_viewer = make_token(tenant_id=self.tenant_a, roles=["viewer", "developer"])
        response = self.client.post(
            "/api/v1/approvals/appr-role-test/decision",
            json={"decision": "APPROVED"},
            headers={"Authorization": f"Bearer {token_viewer}"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("lacks required role", response.json()["detail"].lower())

    async def test_8_duplicate_decision_conflict(self):
        """8. Verify submitting a second decision on already decided approval returns HTTP 409 Conflict."""
        now = datetime.now(timezone.utc)
        req = make_domain_request(approval_id="appr-dup-test")
        await self.test_repo.create_request(req, self.tenant_a, now + timedelta(hours=12))

        token = make_token(tenant_id=self.tenant_a, roles=["approver"])
        # First decision succeeds
        r1 = self.client.post(
            "/api/v1/approvals/appr-dup-test/decision",
            json={"decision": "APPROVED"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r1.status_code, 200)

        # Second decision fails with 409 Conflict
        r2 = self.client.post(
            "/api/v1/approvals/appr-dup-test/decision",
            json={"decision": "REJECTED", "reason": "Attempting override"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r2.status_code, 409)
        self.assertIn("already been decided", r2.json()["detail"].lower())

    async def test_9_expired_approval_rejection_and_list_exclusion(self):
        """9. Verify expired approval cannot be decided (410 Gone) and is excluded from pending list."""
        now = datetime.now(timezone.utc)
        # Expired 30 minutes ago
        req = make_domain_request(approval_id="appr-exp-test")
        await self.test_repo.create_request(req, self.tenant_a, now - timedelta(minutes=30))

        token = make_token(tenant_id=self.tenant_a, roles=["approver"])

        # Excluded from pending list
        res_list = self.client.get(
            "/api/v1/approvals",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(res_list.status_code, 200)
        self.assertEqual(res_list.json()["total"], 0)

        # Attempting decision returns 410 Gone
        res_decision = self.client.post(
            "/api/v1/approvals/appr-exp-test/decision",
            json={"decision": "APPROVED"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(res_decision.status_code, 410)
        self.assertIn("expired", res_decision.json()["detail"].lower())

    async def test_10_malformed_decision_body(self):
        """10. Verify malformed decision payloads return HTTP 422 Unprocessable Content."""
        now = datetime.now(timezone.utc)
        req = make_domain_request(approval_id="appr-bad-payload")
        await self.test_repo.create_request(req, self.tenant_a, now + timedelta(hours=12))

        token = make_token(tenant_id=self.tenant_a, roles=["approver"])

        # a. Attempting to submit PENDING as decision
        r1 = self.client.post(
            "/api/v1/approvals/appr-bad-payload/decision",
            json={"decision": "PENDING"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r1.status_code, 422)

        # b. Unknown enum value
        r2 = self.client.post(
            "/api/v1/approvals/appr-bad-payload/decision",
            json={"decision": "MAYBE"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r2.status_code, 422)

        # c. Reason exceeding 1000 characters
        r3 = self.client.post(
            "/api/v1/approvals/appr-bad-payload/decision",
            json={"decision": "APPROVED", "reason": "X" * 1001},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r3.status_code, 422)

    async def test_11_zero_orchestrator_execution(self):
        """11. Verify that deciding an approval commits to DB with ZERO orchestrator execution or resume."""
        now = datetime.now(timezone.utc)
        req = make_domain_request(approval_id="appr-zero-exec")
        await self.test_repo.create_request(req, self.tenant_a, now + timedelta(hours=12))

        token = make_token(tenant_id=self.tenant_a, roles=["approver"])

        with patch("eaoc.agents.orchestrator.engine.OrchestratorEngine.execute_workflow") as mock_orch:
            response = self.client.post(
                "/api/v1/approvals/appr-zero-exec/decision",
                json={"decision": "APPROVED"},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(response.status_code, 200)
            # Orchestrator execute was NEVER called
            mock_orch.assert_not_called()

    async def test_12_spoofed_headers_ignored(self):
        """12. Verify caller-supplied spoofing headers cannot override verified token attributes."""
        now = datetime.now(timezone.utc)
        req = make_domain_request(approval_id="appr-spoof-test")
        await self.test_repo.create_request(req, self.tenant_a, now + timedelta(hours=12))

        # Real token says: user_id="real-approver", tenant_id="tenant-alpha", roles=["approver"]
        token = make_token(sub="real-approver", tenant_id=self.tenant_a, roles=["approver"])

        # Attacker injects spoofing headers attempting to attribute decision to another user/tenant
        spoofed_headers = {
            "Authorization": f"Bearer {token}",
            "X-User-ID": "fake-impostor",
            "X-Tenant-ID": "fake-tenant-root",
            "X-User-Roles": "superadmin,root",
        }

        response = self.client.post(
            "/api/v1/approvals/appr-spoof-test/decision",
            json={"decision": "APPROVED"},
            headers=spoofed_headers,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        # Decision must be attributed exclusively to verified token principal
        self.assertEqual(data["approver_id"], "real-approver")

    async def test_13_database_unavailable_503(self):
        """13. Verify database failures return HTTP 503 without leaking stack traces or credentials."""
        self.app.dependency_overrides[get_approval_repository] = lambda: FailingApprovalRepository()
        token = make_token(tenant_id=self.tenant_a, roles=["approver"])

        # a. List
        r1 = self.client.get("/api/v1/approvals", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r1.status_code, 503)
        self.assertIn("unavailable", r1.json()["detail"].lower())
        self.assertNotIn("db.supabase.co", r1.text)

        # b. Detail
        r2 = self.client.get("/api/v1/approvals/appr-any", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r2.status_code, 503)
        self.assertIn("unavailable", r2.json()["detail"].lower())
        self.assertNotIn("db.supabase.co", r2.text)

        # c. Decision
        r3 = self.client.post(
            "/api/v1/approvals/appr-any/decision",
            json={"decision": "APPROVED"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r3.status_code, 503)
        self.assertIn("unavailable", r3.json()["detail"].lower())
        self.assertNotIn("db.supabase.co", r3.text)


if __name__ == "__main__":
    unittest.main()
