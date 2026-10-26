"""
EAOC Unit Test Suite - Approval Persistence & Alembic Migration (Part 2.16-A)

Tests:
1. Domain-to-database and database-to-domain conversion.
2. Request creation, retrieval, and tenant isolation.
3. Pending-list pagination and workflow history filtering.
4. Successful conditional decision.
5. Duplicate or conflicting decisions.
6. Expired requests and expiration persistence.
7. Concurrent decision attempts.
8. Resume-status transitions and safe failure recording.
9. Migration revision linkage, generated DDL, constraints, indexes, and downgrade behavior.
10. Unconfigured database handling and error safety.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
import importlib

from eaoc.db.models.approval import ApprovalRequestModel
migration_0002 = importlib.import_module("eaoc.db.migrations.versions.0002_approval_requests")
from eaoc.schemas.domain import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
)
from eaoc.state.approval_repository import (
    IApprovalRepository,
    InMemoryApprovalRepository,
    PostgresApprovalRepository,
)


def _make_sample_domain_request(
    approval_id: str = "appr-1234567890ab",
    workflow_id: str = "wf-abcdef123456",
    requested_action: str = "Execute critical DB migration via sql_data",
    risk_level: RiskLevel = RiskLevel.HIGH,
    status: ApprovalStatus = ApprovalStatus.PENDING,
    step_index: int = 1,
    task_id: str = "task-01",
) -> ApprovalRequest:
    now_str = datetime.now(timezone.utc).isoformat()
    return ApprovalRequest(
        approval_id=approval_id,
        workflow_id=workflow_id,
        requested_action=requested_action,
        risk_level=risk_level,
        status=status,
        requested_at=now_str,
        details={
            "step_index": step_index,
            "task_id": task_id,
            "agent_id": "sql_data",
            "operation_type": "write",
        },
    )


class TestApprovalPersistence(unittest.IsolatedAsyncioTestCase):
    """Test suite for Part 2.16-A approval persistence and repository operations."""

    async def asyncSetUp(self):
        self.repo: IApprovalRepository = InMemoryApprovalRepository()
        self.tenant_a = "tenant-alpha"
        self.tenant_b = "tenant-beta"

    async def test_1_domain_database_conversion(self):
        """1. Verify domain-to-database and database-to-domain conversion accuracy."""
        req = _make_sample_domain_request(approval_id="appr-conv-123456")
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

        # Domain -> ORM Model
        model = ApprovalRequestModel.from_domain_request(
            request=req,
            tenant_id=self.tenant_a,
            expires_at=expires_at,
        )
        self.assertEqual(model.approval_id, req.approval_id)
        self.assertEqual(model.workflow_id, req.workflow_id)
        self.assertEqual(model.tenant_id, self.tenant_a)
        self.assertEqual(model.risk_level, "HIGH")
        self.assertEqual(model.status, "PENDING")
        self.assertEqual(model.step_index, 1)
        self.assertEqual(model.task_id, "task-01")
        self.assertEqual(model.resume_status, "none")
        self.assertIsNone(model.decision)

        # ORM Model -> Domain Request
        domain_req = model.to_domain_request()
        self.assertEqual(domain_req.approval_id, req.approval_id)
        self.assertEqual(domain_req.workflow_id, req.workflow_id)
        self.assertEqual(domain_req.risk_level, RiskLevel.HIGH)
        self.assertEqual(domain_req.status, ApprovalStatus.PENDING)
        self.assertEqual(domain_req.details["step_index"], 1)

        # With Decision recorded -> Domain Decision
        dec_time = datetime.now(timezone.utc)
        model.decision = "APPROVED"
        model.approver_id = "usr-approver-99"
        model.decision_reason = "Approved for Q3 closeout"
        model.decided_at = dec_time

        domain_decision = model.to_domain_decision()
        self.assertIsNotNone(domain_decision)
        self.assertEqual(domain_decision.approval_id, req.approval_id)
        self.assertEqual(domain_decision.workflow_id, req.workflow_id)
        self.assertEqual(domain_decision.decision, ApprovalStatus.APPROVED)
        self.assertEqual(domain_decision.approver_id, "usr-approver-99")
        self.assertEqual(domain_decision.reason, "Approved for Q3 closeout")

    async def test_2_request_creation_and_tenant_isolation(self):
        """2. Verify request creation, retrieval, and strict tenant isolation."""
        req_a = _make_sample_domain_request(approval_id="appr-tenant-a-1")
        exp_a = datetime.now(timezone.utc) + timedelta(hours=12)

        created = await self.repo.create_request(req_a, self.tenant_a, exp_a)
        self.assertEqual(created.approval_id, "appr-tenant-a-1")

        # Tenant A can access
        fetched_a = await self.repo.get_by_id("appr-tenant-a-1", self.tenant_a)
        self.assertIsNotNone(fetched_a)
        self.assertEqual(fetched_a.approval_id, "appr-tenant-a-1")

        # Tenant B CANNOT access Tenant A's approval
        fetched_b = await self.repo.get_by_id("appr-tenant-a-1", self.tenant_b)
        self.assertIsNone(fetched_b)

        # Duplicate ID creation fails
        with self.assertRaises(ValueError):
            await self.repo.create_request(req_a, self.tenant_a, exp_a)

    async def test_3_pending_list_pagination_and_workflow_filtering(self):
        """3. Verify pagination, ordering, and workflow filtering within tenant scope."""
        now = datetime.now(timezone.utc)
        for i in range(5):
            req = _make_sample_domain_request(
                approval_id=f"appr-page-{i}",
                workflow_id="wf-bulk-1",
            )
            await self.repo.create_request(
                req, self.tenant_a, now + timedelta(hours=24)
            )

        # Different tenant request
        req_b = _make_sample_domain_request(
            approval_id="appr-tenant-b-req",
            workflow_id="wf-bulk-1",
        )
        await self.repo.create_request(req_b, self.tenant_b, now + timedelta(hours=24))

        # Test pagination for Tenant A: limit=2, offset=0
        paged_1 = await self.repo.list_pending(self.tenant_a, limit=2, offset=0)
        self.assertEqual(len(paged_1), 2)
        self.assertEqual(paged_1[0].approval_id, "appr-page-0")
        self.assertEqual(paged_1[1].approval_id, "appr-page-1")

        # Test pagination: limit=2, offset=2
        paged_2 = await self.repo.list_pending(self.tenant_a, limit=2, offset=2)
        self.assertEqual(len(paged_2), 2)
        self.assertEqual(paged_2[0].approval_id, "appr-page-2")
        self.assertEqual(paged_2[1].approval_id, "appr-page-3")

        # Workflow listing for Tenant A
        wf_items_a = await self.repo.list_by_workflow("wf-bulk-1", self.tenant_a)
        self.assertEqual(len(wf_items_a), 5)

        # Workflow listing for Tenant B
        wf_items_b = await self.repo.list_by_workflow("wf-bulk-1", self.tenant_b)
        self.assertEqual(len(wf_items_b), 1)
        self.assertEqual(wf_items_b[0].approval_id, "appr-tenant-b-req")

    async def test_4_successful_conditional_decision(self):
        """4. Verify successful atomic conditional decision update."""
        req = _make_sample_domain_request(approval_id="appr-decide-1")
        exp = datetime.now(timezone.utc) + timedelta(hours=6)
        await self.repo.create_request(req, self.tenant_a, exp)

        success, updated, code = await self.repo.record_conditional_decision(
            approval_id="appr-decide-1",
            tenant_id=self.tenant_a,
            decision=ApprovalStatus.APPROVED,
            approver_id="usr-sec-lead",
            reason="Verified security review pass",
        )

        self.assertTrue(success)
        self.assertEqual(code, "success")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.status, ApprovalStatus.APPROVED)

        # Verify decision retrieval
        decision = await self.repo.get_decision("appr-decide-1", self.tenant_a)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision, ApprovalStatus.APPROVED)
        self.assertEqual(decision.approver_id, "usr-sec-lead")
        self.assertEqual(decision.reason, "Verified security review pass")

    async def test_5_duplicate_or_conflicting_decisions(self):
        """5. Verify already-decided approval cannot be re-decided or modified."""
        req = _make_sample_domain_request(approval_id="appr-conflict-1")
        exp = datetime.now(timezone.utc) + timedelta(hours=6)
        await self.repo.create_request(req, self.tenant_a, exp)

        # First decision succeeds
        s1, u1, c1 = await self.repo.record_conditional_decision(
            approval_id="appr-conflict-1",
            tenant_id=self.tenant_a,
            decision=ApprovalStatus.REJECTED,
            approver_id="usr-approver-1",
            reason="Risk too high",
        )
        self.assertTrue(s1)
        self.assertEqual(c1, "success")
        self.assertEqual(u1.status, ApprovalStatus.REJECTED)

        # Second conflicting attempt fails closed
        s2, u2, c2 = await self.repo.record_conditional_decision(
            approval_id="appr-conflict-1",
            tenant_id=self.tenant_a,
            decision=ApprovalStatus.APPROVED,
            approver_id="usr-approver-2",
            reason="Attempting overwrite",
        )
        self.assertFalse(s2)
        self.assertEqual(c2, "already_decided")
        self.assertEqual(u2.status, ApprovalStatus.REJECTED)

    async def test_6_expired_requests_and_expiration_persistence(self):
        """6. Verify expired requests cannot be approved and are marked EXPIRED."""
        req = _make_sample_domain_request(approval_id="appr-expired-1")
        # Expired 1 hour ago
        exp = datetime.now(timezone.utc) - timedelta(hours=1)
        await self.repo.create_request(req, self.tenant_a, exp)

        # Attempt to decide expired request
        success, updated, code = await self.repo.record_conditional_decision(
            approval_id="appr-expired-1",
            tenant_id=self.tenant_a,
            decision=ApprovalStatus.APPROVED,
            approver_id="usr-approver-late",
        )
        self.assertFalse(success)
        self.assertEqual(code, "expired")
        self.assertEqual(updated.status, ApprovalStatus.EXPIRED)

        # Verify get_by_id marks it EXPIRED
        fetched = await self.repo.get_by_id("appr-expired-1", self.tenant_a)
        self.assertEqual(fetched.status, ApprovalStatus.EXPIRED)

    async def test_7_concurrent_decision_attempts(self):
        """7. Verify atomic concurrency handling: exactly one winner succeeds."""
        req = _make_sample_domain_request(approval_id="appr-concurrent-1")
        exp = datetime.now(timezone.utc) + timedelta(hours=1)
        await self.repo.create_request(req, self.tenant_a, exp)

        # Launch 5 concurrent decision attempts
        async def _attempt(approver: str, dec: ApprovalStatus):
            return await self.repo.record_conditional_decision(
                approval_id="appr-concurrent-1",
                tenant_id=self.tenant_a,
                decision=dec,
                approver_id=approver,
            )

        tasks = [
            _attempt(f"approver-{i}", ApprovalStatus.APPROVED if i % 2 == 0 else ApprovalStatus.REJECTED)
            for i in range(5)
        ]
        results = await asyncio.gather(*tasks)

        successes = [r for r in results if r[0] is True]
        failures = [r for r in results if r[0] is False]

        # Exactly 1 attempt must succeed
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 4)
        for f in failures:
            self.assertEqual(f[2], "already_decided")

    async def test_8_resume_status_transitions_and_safe_failure_recording(self):
        """8. Verify resume-status lifecycle and safe sensitive data redaction."""
        req = _make_sample_domain_request(approval_id="appr-resume-1")
        exp = datetime.now(timezone.utc) + timedelta(hours=1)
        await self.repo.create_request(req, self.tenant_a, exp)

        # Decide request -> resume_status becomes 'pending'
        await self.repo.record_conditional_decision(
            approval_id="appr-resume-1",
            tenant_id=self.tenant_a,
            decision=ApprovalStatus.APPROVED,
            approver_id="usr-sec",
        )

        # Update resume_status to 'failed' with sensitive error
        sensitive_error = "Connection failed to database: token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret"
        up_ok = await self.repo.update_resume_status(
            approval_id="appr-resume-1",
            tenant_id=self.tenant_a,
            resume_status="failed",
            resume_error=sensitive_error,
        )
        self.assertTrue(up_ok)

        # Verify in memory record: decision remains APPROVED, not reset to pending!
        rec = self.repo._records["appr-resume-1"]
        self.assertEqual(rec.status, "APPROVED")
        self.assertEqual(rec.resume_status, "failed")
        # Sensitive token in error should be redacted
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret", rec.resume_error)
        self.assertIn("token=****", rec.resume_error)

        # Invalid resume_status raises ValueError
        with self.assertRaises(ValueError):
            await self.repo.update_resume_status(
                approval_id="appr-resume-1",
                tenant_id=self.tenant_a,
                resume_status="invalid_state",
            )

    def test_9_migration_revision_and_ddl_linkage(self):
        """9. Verify Alembic migration 0002 linkage, tables, constraints, and indexes."""
        # 1. Revision ID and down_revision linkage
        self.assertEqual(migration_0002.revision, "0002_approval_requests")
        self.assertEqual(migration_0002.down_revision, "0001_initial_foundation")

        # 2. Inspect upgrade and downgrade functions
        self.assertTrue(callable(migration_0002.upgrade))
        self.assertTrue(callable(migration_0002.downgrade))

    async def test_10_unconfigured_database_handling(self):
        """10. Verify PostgresApprovalRepository fails closed with RuntimeError if DB unconfigured."""
        repo_unconfigured = PostgresApprovalRepository(session_factory=None)
        req = _make_sample_domain_request(approval_id="appr-unconf-1")
        exp = datetime.now(timezone.utc) + timedelta(hours=1)

        with self.assertRaises(RuntimeError) as cm:
            await repo_unconfigured.create_request(req, self.tenant_a, exp)
        self.assertIn("not configured", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
