"""
EAOC PostgreSQL Live Integration Test Suite (PG-01 through PG-07)
Exercises durable workflow recovery, atomic CAS lease acquisition, fencing epochs,
OCC row revisions, partial unique index idempotency, and foreign key rules against
an explicitly configured and preflight-verified TEST_DATABASE_URL.

Guards:
- Automatically skipped when RUN_POSTGRES_INTEGRATION=1 is not set.
- Automatically skipped when TEST_DATABASE_URL is not configured.
- Fails closed on production environment, collision with DATABASE_URL, or preflight incompatibility.
- Dual independent engines (Worker A and Worker B) ensure genuine concurrent database contention.
- Exact primary-key tracking ensures zero cross-test interference or blanket table truncation.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest

from sqlalchemy import and_, func, select, text, update
from sqlalchemy.exc import IntegrityError

from eaoc.db.models.approval import ApprovalRequestModel
from eaoc.db.models.workflow import WorkflowModel, WorkflowTaskModel
from eaoc.schemas.domain import (
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
    TaskStatus,
    WorkflowStatus,
)
from eaoc.state.workflow_repository import (
    ConcurrencyConflictError,
    FencingLeaseLostError,
)
from tests.integration.fixtures import PostgresIntegrationTestCase


class TestPostgresIntegration(PostgresIntegrationTestCase):
    """
    Live PostgreSQL integration test suite covering scenarios PG-01 through PG-08.
    """

    async def test_pg_01_concurrent_cas_lease_acquisition_collision(self):
        """
        PG-01: Two competing workers simultaneously attempt to acquire an execution lease
        on the same workflow. PostgreSQL row-level locks and atomic CAS updates guarantee
        that exactly one worker wins and the other is rejected without error.
        """
        wf_id = f"wf-{self.run_id}-01"
        self.tracked_workflow_ids.append(wf_id)

        # Intake workflow via Worker A
        wf, is_new = await self.repo_a.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=self.tenant_id,
            user_id="user-pg-01",
            task_id="task-01",
            task_description="PG-01 concurrent lease acquisition",
            execution_plan={"steps": [{"step_index": 0, "name": "step-0"}]},
            plan_hash="hash-01",
        )
        self.assertTrue(is_new)

        # Concurrently attempt lease acquisition via Worker A and Worker B
        res_a, res_b = await asyncio.gather(
            self.repo_a.acquire_execution_lease(workflow_id=wf_id, tenant_id=self.tenant_id),
            self.repo_b.acquire_execution_lease(workflow_id=wf_id, tenant_id=self.tenant_id),
        )

        success_a, epoch_a, rev_a, token_a = res_a
        success_b, epoch_b, rev_b, token_b = res_b

        # Exactly one worker must win
        self.assertTrue(success_a ^ success_b, "Exactly one worker must acquire the execution lease.")

        winner_epoch = epoch_a if success_a else epoch_b
        winner_rev = rev_a if success_a else rev_b
        self.assertEqual(winner_epoch, 1)
        self.assertEqual(winner_rev, 2)

    async def test_pg_02_stale_fencing_epoch_rejection(self):
        """
        PG-02: A worker attempting to complete a step with a stale fencing epoch
        is rejected by the database and raises FencingLeaseLostError.
        """
        wf_id = f"wf-{self.run_id}-02"
        self.tracked_workflow_ids.append(wf_id)
        self.tracked_task_ids.append(f"{wf_id}:step-0")

        await self.repo_a.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=self.tenant_id,
            user_id="user-pg-02",
            task_id="task-02",
            task_description="PG-02 stale fencing epoch",
            execution_plan={"steps": [{"step_index": 0, "name": "step-0"}]},
            plan_hash="hash-02",
        )

        # Worker A acquires lease (epoch 1, rev 2)
        succ_a, epoch_a, rev_a, tok_a = await self.repo_a.acquire_execution_lease(
            workflow_id=wf_id, tenant_id=self.tenant_id
        )
        self.assertTrue(succ_a)
        self.assertEqual(epoch_a, 1)

        # Worker A prepares step 0, transitioning task to RUNNING status (bumping revision to rev_a + 1)
        is_comp, out, prov, prep_rev_a = await self.repo_a.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=self.tenant_id,
            step_index=0,
            agent_id="research_rag",
            name="step-0",
            plan_hash="hash-02",
            step_hash="step-hash-02",
            inputs={"q": "research"},
            fencing_epoch=epoch_a,
            lock_token=tok_a,
            expected_revision=rev_a,
        )
        self.assertFalse(is_comp)
        self.assertEqual(prep_rev_a, rev_a + 1)

        # Worker B acquires lease (simulating recovery takeover, epoch 2, rev prep_rev_a + 1)
        succ_b, epoch_b, rev_b, tok_b = await self.repo_b.acquire_execution_lease(
            workflow_id=wf_id, tenant_id=self.tenant_id, current_token=tok_a
        )
        self.assertTrue(succ_b)
        self.assertEqual(epoch_b, 2)

        # Worker A tries to record step completion using stale epoch 1.
        # Task is in RUNNING status, so it passes task status check and fails closed at the
        # workflow lease fencing epoch check with FencingLeaseLostError.
        with self.assertRaises(FencingLeaseLostError):
            await self.repo_a.complete_task_step(
                workflow_id=wf_id,
                tenant_id=self.tenant_id,
                step_index=0,
                outputs={"result": "stale"},
                provenance={"agent": "research_rag"},
                fencing_epoch=epoch_a,  # Stale epoch 1
                lock_token=tok_a,
                expected_revision=prep_rev_a,
            )

    async def test_pg_03_optimistic_concurrency_control_revision_check(self):
        """
        PG-03: OCC row_revision collision check ensures that an update with an
        outdated revision number fails closed with FencingLeaseLostError.
        """
        wf_id = f"wf-{self.run_id}-03"
        self.tracked_workflow_ids.append(wf_id)
        self.tracked_task_ids.append(f"{wf_id}:step-0")

        await self.repo_a.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=self.tenant_id,
            user_id="user-pg-03",
            task_id="task-03",
            task_description="PG-03 OCC revision check",
            execution_plan={"steps": [{"step_index": 0, "name": "step-0"}]},
            plan_hash="hash-03",
        )

        succ, epoch, rev, tok = await self.repo_a.acquire_execution_lease(
            workflow_id=wf_id, tenant_id=self.tenant_id
        )
        self.assertTrue(succ)

        # Worker A prepares step 0, transitioning task to RUNNING status (bumping revision to rev + 1)
        is_comp, out, prov, prep_rev = await self.repo_a.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=self.tenant_id,
            step_index=0,
            agent_id="developer",
            name="step-0",
            plan_hash="hash-03",
            step_hash="step-hash-03",
            inputs={"code": "test"},
            fencing_epoch=epoch,
            lock_token=tok,
            expected_revision=rev,
        )
        self.assertFalse(is_comp)
        self.assertEqual(prep_rev, rev + 1)

        # 1. Attempt step completion with mismatched expected_revision (prep_rev + 99).
        # Task is RUNNING, but workflow combined CAS check fails closed with FencingLeaseLostError.
        with self.assertRaises(FencingLeaseLostError):
            await self.repo_a.complete_task_step(
                workflow_id=wf_id,
                tenant_id=self.tenant_id,
                step_index=0,
                outputs={"code": "done"},
                provenance={"agent": "developer"},
                fencing_epoch=epoch,
                lock_token=tok,
                expected_revision=prep_rev + 99,  # Invalid revision
            )

        # 2. Attempt replan update with valid lease credentials but stale expected_revision.
        # update_workflow_plan specifically distinguishes revision conflict from lease loss,
        # raising ConcurrencyConflictError.
        stale_plan = {
            "steps": [
                {"step_index": 0, "name": "step-0-mutated"},
            ]
        }
        with self.assertRaises(ConcurrencyConflictError):
            await self.repo_a.update_workflow_plan(
                workflow_id=wf_id,
                tenant_id=self.tenant_id,
                execution_plan=stale_plan,
                plan_hash="hash-03-mutated",
                replan_count=1,
                replan_metadata={"reason": "stale replan attempt"},
                current_step_index=0,
                fencing_epoch=epoch,
                lock_token=tok,
                expected_revision=prep_rev + 99,  # Stale revision
            )

        # Verify that after the rejected replan, persisted revision and plan state remain completely unchanged
        wf_unchanged = await self.repo_a.get_workflow(workflow_id=wf_id, tenant_id=self.tenant_id)
        self.assertIsNotNone(wf_unchanged)
        self.assertEqual(wf_unchanged.row_revision, prep_rev)
        self.assertEqual(wf_unchanged.plan_hash, "hash-03")
        self.assertEqual(wf_unchanged.execution_plan, {"steps": [{"step_index": 0, "name": "step-0"}]})

    async def test_pg_04_partial_unique_index_idempotency(self):
        """
        PG-04: PostgreSQL partial unique index ix_workflows_tenant_idempotency
        (tenant_id, idempotency_key WHERE idempotency_key IS NOT NULL) blocks duplicate
        intake and transparently resolves concurrent intake races.
        """
        idemp_key = f"idemp-{self.run_id}"
        fingerprint = "fp-unique-04"
        wf_id_1 = f"wf-{self.run_id}-04-a"
        wf_id_2 = f"wf-{self.run_id}-04-b"
        self.tracked_workflow_ids.extend([wf_id_1, wf_id_2])

        # First intake succeeds
        wf1, is_new1 = await self.repo_a.create_workflow_intake(
            workflow_id=wf_id_1,
            tenant_id=self.tenant_id,
            user_id="user-pg-04",
            task_id="task-04",
            task_description="PG-04 idempotency test",
            idempotency_key=idemp_key,
            idempotency_fingerprint=fingerprint,
        )
        self.assertTrue(is_new1)
        self.assertEqual(wf1.workflow_id, wf_id_1)

        # Replaying identical intake returns existing workflow without creating duplicate
        wf2, is_new2 = await self.repo_b.create_workflow_intake(
            workflow_id=wf_id_2,
            tenant_id=self.tenant_id,
            user_id="user-pg-04",
            task_id="task-04",
            task_description="PG-04 idempotency test",
            idempotency_key=idemp_key,
            idempotency_fingerprint=fingerprint,
        )
        self.assertFalse(is_new2)
        self.assertEqual(wf2.workflow_id, wf_id_1)

        # Replaying same key with altered fingerprint raises ValueError
        with self.assertRaises(ValueError):
            await self.repo_b.create_workflow_intake(
                workflow_id=wf_id_2,
                tenant_id=self.tenant_id,
                user_id="user-pg-04",
                task_id="task-04",
                task_description="PG-04 idempotency test",
                idempotency_key=idemp_key,
                idempotency_fingerprint="fp-altered-payload",
            )

    async def test_pg_05_heartbeat_lease_renewal(self):
        """
        PG-05: Heartbeat lease renewal uses database-side func.now() + interval '30 seconds'
        to extend lock_expires_at without incrementing row_revision or fencing_epoch.
        """
        wf_id = f"wf-{self.run_id}-05"
        self.tracked_workflow_ids.append(wf_id)

        await self.repo_a.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=self.tenant_id,
            user_id="user-pg-05",
            task_id="task-05",
            task_description="PG-05 lease renewal",
        )

        succ, epoch, rev, tok = await self.repo_a.acquire_execution_lease(
            workflow_id=wf_id, tenant_id=self.tenant_id
        )
        self.assertTrue(succ)

        # Renew lease
        renewed = await self.repo_a.renew_execution_lease(
            workflow_id=wf_id,
            tenant_id=self.tenant_id,
            fencing_epoch=epoch,
            lock_token=tok,
        )
        self.assertTrue(renewed)

        # Verify row_revision and fencing_epoch remain unchanged after renewal
        wf = await self.repo_a.get_workflow(workflow_id=wf_id, tenant_id=self.tenant_id)
        self.assertIsNotNone(wf)
        self.assertEqual(wf.fencing_epoch, epoch)
        self.assertEqual(wf.row_revision, rev)
        self.assertIsNotNone(wf.lock_expires_at)

    async def test_pg_06_foreign_key_cascades_and_restrictions(self):
        """
        PG-06: Verifies foreign key constraints in live PostgreSQL:
        - fk_approval_requests_workflow_id (ON DELETE RESTRICT) prevents deleting a workflow with active approvals.
        - fk_workflow_tasks_workflow_id (ON DELETE CASCADE) deletes task children when workflow is deleted.
        """
        # 1. Test ON DELETE RESTRICT on workflows referenced by approval_requests
        wf_id_restrict = f"wf-{self.run_id}-06-restrict"
        appr_id = f"appr-{self.run_id}-06"
        self.tracked_workflow_ids.append(wf_id_restrict)
        self.tracked_approval_ids.append(appr_id)

        await self.repo_a.create_workflow_intake(
            workflow_id=wf_id_restrict,
            tenant_id=self.tenant_id,
            user_id="user-pg-06",
            task_id="task-06-restrict",
            task_description="PG-06 RESTRICT foreign key test",
        )

        now_utc = datetime.now(timezone.utc)
        approval_req = ApprovalRequest(
            approval_id=appr_id,
            workflow_id=wf_id_restrict,
            requested_action="Deploy critical infrastructure",
            risk_level=RiskLevel.HIGH,
            status=ApprovalStatus.PENDING,
            requested_at=now_utc.isoformat(),
            details={"task_id": "task-06-restrict", "step_index": 0},
        )
        await self.approval_repo_a.create_request(
            request=approval_req,
            tenant_id=self.tenant_id,
            expires_at=now_utc + timedelta(hours=1),
        )

        # Attempting to delete workflow while approval exists must fail due to ON DELETE RESTRICT
        async with self.session_factory_a() as session:
            with self.assertRaises(IntegrityError):
                async with session.begin():
                    await session.execute(
                        text("DELETE FROM workflows WHERE workflow_id = :wid"),
                        {"wid": wf_id_restrict},
                    )

        # 2. Test ON DELETE CASCADE on workflows referenced by workflow_tasks
        wf_id_cascade = f"wf-{self.run_id}-06-cascade"
        task_id_cascade = f"{wf_id_cascade}:step-0"
        self.tracked_workflow_ids.append(wf_id_cascade)
        self.tracked_task_ids.append(task_id_cascade)

        await self.repo_a.create_workflow_intake(
            workflow_id=wf_id_cascade,
            tenant_id=self.tenant_id,
            user_id="user-pg-06",
            task_id="task-06-cascade",
            task_description="PG-06 CASCADE foreign key test",
            execution_plan={"steps": [{"step_index": 0, "name": "step-0"}]},
            plan_hash="hash-06-cascade",
        )

        succ, epoch, rev, tok = await self.repo_a.acquire_execution_lease(
            workflow_id=wf_id_cascade, tenant_id=self.tenant_id
        )
        self.assertTrue(succ)

        # Prepare task step 0 to insert row in workflow_tasks
        await self.repo_a.prepare_task_step(
            workflow_id=wf_id_cascade,
            tenant_id=self.tenant_id,
            step_index=0,
            agent_id="developer",
            name="step-0",
            plan_hash="hash-06-cascade",
            step_hash="step-hash-06",
            inputs={"test": "cascade"},
            fencing_epoch=epoch,
            lock_token=tok,
            expected_revision=rev,
        )

        # Verify child task exists
        tasks_before = await self.repo_a.get_tasks_for_workflow(
            workflow_id=wf_id_cascade, tenant_id=self.tenant_id
        )
        self.assertEqual(len(tasks_before), 1)

        # Delete parent workflow directly; CASCADE constraint must automatically remove child tasks
        async with self.session_factory_a() as session:
            async with session.begin():
                del_res = await session.execute(
                    text("DELETE FROM workflows WHERE workflow_id = :wid"),
                    {"wid": wf_id_cascade},
                )
                self.assertEqual(del_res.rowcount, 1)

        # Query workflow_tasks directly: child task must have been cascade-deleted
        async with self.session_factory_a() as session:
            res_tasks = await session.execute(
                select(WorkflowTaskModel).where(
                    WorkflowTaskModel.workflow_id == wf_id_cascade
                )
            )
            self.assertEqual(len(res_tasks.scalars().all()), 0)

    async def test_pg_07_atomic_plan_update_and_superseded_cleanup(self):
        """
        PG-07: update_workflow_plan atomically replaces execution plan, updates canonical
        plan_hash and current_step_index, and purges superseded non-completed tasks within
        the same transaction, while preserving completed task records and authentic provenance.
        """
        wf_id = f"wf-{self.run_id}-07"
        self.tracked_workflow_ids.append(wf_id)
        self.tracked_task_ids.append(f"{wf_id}:step-0")
        self.tracked_task_ids.append(f"{wf_id}:step-1")

        initial_plan = {
            "steps": [
                {"step_index": 0, "name": "step-0-completed"},
                {"step_index": 1, "name": "step-1-superseded"},
            ]
        }
        await self.repo_a.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=self.tenant_id,
            user_id="user-pg-07",
            task_id="task-07",
            task_description="PG-07 atomic replan cleanup",
            execution_plan=initial_plan,
            plan_hash="initial-plan-hash",
        )

        succ, epoch, rev, tok = await self.repo_a.acquire_execution_lease(
            workflow_id=wf_id, tenant_id=self.tenant_id
        )
        self.assertTrue(succ)

        # 1. Prepare and complete step 0
        is_comp0, out0, prov0, prep_rev0 = await self.repo_a.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=self.tenant_id,
            step_index=0,
            agent_id="research_rag",
            name="step-0-completed",
            plan_hash="initial-plan-hash",
            step_hash="step-0-hash",
            inputs={"q": "initial"},
            fencing_epoch=epoch,
            lock_token=tok,
            expected_revision=rev,
        )
        self.assertFalse(is_comp0)

        comp_rev0 = await self.repo_a.complete_task_step(
            workflow_id=wf_id,
            tenant_id=self.tenant_id,
            step_index=0,
            outputs={"findings": "complete"},
            provenance={"agent": "research_rag", "source": "archive"},
            duration_ms=45.0,
            fencing_epoch=epoch,
            lock_token=tok,
            expected_revision=prep_rev0,
        )
        self.assertGreater(comp_rev0, prep_rev0)

        # 2. Prepare step 1 (putting it in RUNNING status as superseded candidate)
        is_comp1, out1, prov1, prep_rev1 = await self.repo_a.prepare_task_step(
            workflow_id=wf_id,
            tenant_id=self.tenant_id,
            step_index=1,
            agent_id="developer",
            name="step-1-superseded",
            plan_hash="initial-plan-hash",
            step_hash="step-1-hash",
            inputs={"cmd": "run"},
            fencing_epoch=epoch,
            lock_token=tok,
            expected_revision=comp_rev0,
        )
        self.assertFalse(is_comp1)

        # Verify step 0 and step 1 exist before replan
        tasks_before = await self.repo_a.get_tasks_for_workflow(
            workflow_id=wf_id, tenant_id=self.tenant_id
        )
        self.assertEqual(len(tasks_before), 2)

        # 3. Now atomically replan from step 1 with updated execution plan
        updated_plan = {
            "steps": [
                {"step_index": 0, "name": "step-0-completed"},
                {"step_index": 1, "name": "step-1-new-plan"},
                {"step_index": 2, "name": "step-2-new-plan"},
            ]
        }
        replan_rev = await self.repo_a.update_workflow_plan(
            workflow_id=wf_id,
            tenant_id=self.tenant_id,
            execution_plan=updated_plan,
            plan_hash="updated-plan-hash",
            replan_count=1,
            replan_metadata={"reason": "Replanned after step 0"},
            current_step_index=1,
            fencing_epoch=epoch,
            lock_token=tok,
            expected_revision=prep_rev1,
        )
        self.assertGreater(replan_rev, prep_rev1)

        # 4. Verify completed task 0 was preserved with authentic provenance, while step 1 was purged
        tasks_after = await self.repo_a.get_tasks_for_workflow(
            workflow_id=wf_id, tenant_id=self.tenant_id
        )
        self.assertEqual(len(tasks_after), 1)
        self.assertEqual(tasks_after[0].step_index, 0)
        self.assertEqual(tasks_after[0].status, TaskStatus.COMPLETED.value)
        self.assertEqual(tasks_after[0].name, "step-0-completed")
        self.assertEqual(tasks_after[0].provenance, {"agent": "research_rag", "source": "archive"})
        self.assertEqual(tasks_after[0].output_data, {"findings": "complete"})

    async def _setup_workflow_and_approved_request(
        self,
        suffix: str,
        step_index: int = 0,
        risk_level: str = "HIGH",
        operation: str = "deploy_service",
        action_desc: str = "Deploy service to staging environment",
        agent_id: str = "developer",
    ):
        wf_id = f"wf-{self.run_id}-{suffix}"
        appr_id = f"appr-{self.run_id}-{suffix}"
        task_id = f"{wf_id}:step-{step_index}"
        plan_hash = f"hash-{suffix}"
        plan_id = f"plan-{suffix}"

        self.tracked_workflow_ids.append(wf_id)
        self.tracked_task_ids.append(task_id)
        self.tracked_approval_ids.append(appr_id)

        plan = {
            "plan_id": plan_id,
            "steps": [
                {
                    "step_index": step_index,
                    "agent_id": agent_id,
                    "risk_level": risk_level,
                    "operation": operation,
                    "description": action_desc,
                }
            ],
        }

        # 1. Intake workflow (initial status PENDING)
        await self.repo_a.create_workflow_intake(
            workflow_id=wf_id,
            tenant_id=self.tenant_id,
            user_id=f"user-{suffix}",
            task_id=task_id,
            task_description=f"PG-08 test {suffix}",
            execution_plan=plan,
            plan_hash=plan_hash,
        )

        # 2. Acquire initial execution lease (epoch 1, rev 2)
        succ, epoch, rev, tok = await self.repo_a.acquire_execution_lease(
            workflow_id=wf_id, tenant_id=self.tenant_id
        )
        assert succ, "Failed to acquire execution lease during setup"

        # 3. Pause workflow for approval (transitions to WAITING_APPROVAL, creates approval with resume_status='none')
        now_utc = datetime.now(timezone.utc)
        pause_rev = await self.repo_a.pause_workflow_for_approval(
            workflow_id=wf_id,
            tenant_id=self.tenant_id,
            step_index=step_index,
            approval_id=appr_id,
            requested_action=action_desc,
            risk_level=risk_level,
            details={
                "plan_hash": plan_hash,
                "plan_id": plan_id,
                "agent_id": agent_id,
                "operation": operation,
            },
            expires_at=now_utc + timedelta(hours=1),
            fencing_epoch=epoch,
            lock_token=tok,
            expected_revision=rev,
        )

        # 4. Human decides APPROVED (sets status='approved', decision='approved', resume_status='pending')
        dec_succ, _, dec_reason = await self.approval_repo_a.record_conditional_decision(
            approval_id=appr_id,
            tenant_id=self.tenant_id,
            decision=ApprovalStatus.APPROVED,
            approver_id="security-lead",
            reason="Authorized deployment",
        )
        assert dec_succ, f"Failed to record approval decision during setup: {dec_reason}"

        return wf_id, appr_id, epoch, pause_rev, plan_hash, plan_id

    async def test_pg_08a_first_time_approval_claim_success(self):
        """
        PG-08A: First-time approval claim when workflow is WAITING_APPROVAL and has no active lease.
        Verifies:
        - Successful claim with token issued, status transition to RUNNING.
        - Fencing epoch incremented (+1), row revision incremented (+1).
        - Database persistence: workflow execution metadata updated, approval resume_status set to 'resumed',
          claim_count set to 1, is_first_claim set to True.
        """
        wf_id, appr_id, epoch, pause_rev, plan_hash, plan_id = (
            await self._setup_workflow_and_approved_request("08a")
        )

        # Worker A claims approval resume
        claimed, new_epoch, new_rev, token, wf_model, reason = (
            await self.repo_a.claim_approval_resume(
                approval_id=appr_id, workflow_id=wf_id, tenant_id=self.tenant_id
            )
        )

        self.assertTrue(claimed)
        self.assertEqual(new_epoch, epoch + 1)
        self.assertEqual(new_rev, pause_rev + 1)
        self.assertIsNotNone(token)
        self.assertTrue(token.startswith("tok-"))
        self.assertEqual(reason, "success")
        self.assertEqual(wf_model.status, WorkflowStatus.RUNNING.value)

        # Verify persisted state via independent session
        async with self.session_factory_b() as session:
            wf_db = await session.get(WorkflowModel, wf_id)
            self.assertIsNotNone(wf_db)
            self.assertEqual(wf_db.status, WorkflowStatus.RUNNING.value)
            self.assertEqual(wf_db.fencing_epoch, new_epoch)
            self.assertEqual(wf_db.row_revision, new_rev)
            self.assertEqual(wf_db.lock_token, token)
            self.assertIsNotNone(wf_db.lock_expires_at)
            self.assertTrue(wf_db.execution_metadata.get("is_first_resume_claim"))
            self.assertEqual(wf_db.execution_metadata.get("last_resumed_approval_id"), appr_id)

            appr_db = await session.get(ApprovalRequestModel, appr_id)
            self.assertIsNotNone(appr_db)
            self.assertEqual(appr_db.resume_status, "resumed")
            self.assertEqual(appr_db.details.get("claim_count"), 1)
            self.assertTrue(appr_db.details.get("is_first_claim"))
            self.assertFalse(appr_db.details.get("is_recovery_claim"))

    async def test_pg_08b_active_lease_rejection(self):
        """
        PG-08B: Active-lease rejection. A workflow holding an unexpired active lease in database time
        cannot be claimed for approval resume.
        Verifies:
        - Claim is rejected with 'workflow_lease_active_or_ineligible'.
        - Fencing epoch and row revision are not incremented.
        - Persisted approval resume_status remains 'pending' without corrupting human approval.
        """
        wf_id, appr_id, epoch, pause_rev, plan_hash, plan_id = (
            await self._setup_workflow_and_approved_request("08b")
        )

        # Set an active unexpired lease directly in database
        async with self.session_factory_a() as session:
            async with session.begin():
                await session.execute(
                    update(WorkflowModel)
                    .where(WorkflowModel.workflow_id == wf_id)
                    .values(
                        lock_token="tok-held-active",
                        lock_expires_at=func.now() + text("interval '60 seconds'"),
                    )
                )

        # Competing Worker B attempts claim_approval_resume
        claimed, new_epoch, new_rev, token, wf_model, reason = (
            await self.repo_b.claim_approval_resume(
                approval_id=appr_id, workflow_id=wf_id, tenant_id=self.tenant_id
            )
        )

        self.assertFalse(claimed)
        self.assertIsNone(new_epoch)
        self.assertIsNone(new_rev)
        self.assertIsNone(token)
        self.assertEqual(reason, "workflow_lease_active_or_ineligible")

        # Verify database state was not mutated by rejected claim
        async with self.session_factory_a() as session:
            wf_db = await session.get(WorkflowModel, wf_id)
            self.assertEqual(wf_db.fencing_epoch, epoch)
            self.assertEqual(wf_db.row_revision, pause_rev)
            self.assertEqual(wf_db.lock_token, "tok-held-active")

            appr_db = await session.get(ApprovalRequestModel, appr_id)
            self.assertEqual(appr_db.resume_status, "pending")
            self.assertIsNone(appr_db.details.get("claim_count"))

    async def test_pg_08c_expired_lease_recovery(self):
        """
        PG-08C: Expired-lease recovery for an abandoned execution.
        Verifies:
        - When a workflow is already 'resumed' and 'RUNNING' but its lease has expired in database time,
          a recovery worker can successfully reclaim it.
        - Fencing epoch increments, fresh lock token is issued, claim_count increments to 2,
          and is_recovery_claim is recorded as True.
        """
        wf_id, appr_id, epoch, pause_rev, plan_hash, plan_id = (
            await self._setup_workflow_and_approved_request("08c")
        )

        # Worker A performs initial claim
        claimed_a, epoch_a, rev_a, tok_a, _, reason_a = (
            await self.repo_a.claim_approval_resume(
                approval_id=appr_id, workflow_id=wf_id, tenant_id=self.tenant_id
            )
        )
        self.assertTrue(claimed_a)
        self.assertEqual(reason_a, "success")

        # Simulate Worker A abandonment: lease expires in database time
        async with self.session_factory_a() as session:
            async with session.begin():
                await session.execute(
                    update(WorkflowModel)
                    .where(WorkflowModel.workflow_id == wf_id)
                    .values(
                        lock_expires_at=func.now() - text("interval '10 seconds'"),
                    )
                )

        # Worker B (recovery worker) recovers the abandoned execution
        claimed_b, epoch_b, rev_b, tok_b, _, reason_b = (
            await self.repo_b.claim_approval_resume(
                approval_id=appr_id, workflow_id=wf_id, tenant_id=self.tenant_id
            )
        )

        self.assertTrue(claimed_b)
        self.assertEqual(epoch_b, epoch_a + 1)
        self.assertEqual(rev_b, rev_a + 1)
        self.assertNotEqual(tok_b, tok_a)
        self.assertEqual(reason_b, "success")

        # Verify persisted recovery metadata
        async with self.session_factory_b() as session:
            wf_db = await session.get(WorkflowModel, wf_id)
            self.assertEqual(wf_db.status, WorkflowStatus.RUNNING.value)
            self.assertEqual(wf_db.fencing_epoch, epoch_b)
            self.assertEqual(wf_db.row_revision, rev_b)

            appr_db = await session.get(ApprovalRequestModel, appr_id)
            self.assertEqual(appr_db.resume_status, "resumed")
            self.assertEqual(appr_db.details.get("claim_count"), 2)
            self.assertTrue(appr_db.details.get("is_recovery_claim"))
            self.assertFalse(appr_db.details.get("is_first_claim"))

    async def test_pg_08d_tenant_and_approval_binding_rejections(self):
        """
        PG-08D: Tenant, approval ID, step binding, and plan hash mismatch rejections.
        Verifies fail-closed behavior:
        - Wrong tenant fails with 'approval_not_found'.
        - Wrong approval ID fails with 'approval_not_found'.
        - Mismatched step index fails with 'step_index_mismatch'.
        - Mismatched plan hash fails with 'plan_hash_mismatch'.
        - None of the rejected attempts mutate persisted workflow or approval state.
        """
        wf_id, appr_id, epoch, pause_rev, plan_hash, plan_id = (
            await self._setup_workflow_and_approved_request("08d")
        )

        # 1. Foreign tenant rejection
        c1, _, _, _, _, r1 = await self.repo_a.claim_approval_resume(
            approval_id=appr_id, workflow_id=wf_id, tenant_id="tenant-foreign-unauthorized"
        )
        self.assertFalse(c1)
        self.assertEqual(r1, "approval_not_found")

        # 2. Non-existent approval ID rejection
        c2, _, _, _, _, r2 = await self.repo_a.claim_approval_resume(
            approval_id="appr-nonexistent-id", workflow_id=wf_id, tenant_id=self.tenant_id
        )
        self.assertFalse(c2)
        self.assertEqual(r2, "approval_not_found")

        # 3. Step index mismatch rejection
        async with self.session_factory_a() as session:
            async with session.begin():
                await session.execute(
                    update(ApprovalRequestModel)
                    .where(ApprovalRequestModel.approval_id == appr_id)
                    .values(step_index=99)
                )

        c3, _, _, _, _, r3 = await self.repo_a.claim_approval_resume(
            approval_id=appr_id, workflow_id=wf_id, tenant_id=self.tenant_id
        )
        self.assertFalse(c3)
        self.assertEqual(r3, "step_index_mismatch")

        # Restore step index
        async with self.session_factory_a() as session:
            async with session.begin():
                await session.execute(
                    update(ApprovalRequestModel)
                    .where(ApprovalRequestModel.approval_id == appr_id)
                    .values(step_index=0)
                )

        # 4. Plan hash mismatch rejection
        async with self.session_factory_a() as session:
            async with session.begin():
                await session.execute(
                    update(ApprovalRequestModel)
                    .where(ApprovalRequestModel.approval_id == appr_id)
                    .values(
                        details={
                            "plan_hash": "altered-hash-tampered",
                            "plan_id": plan_id,
                            "agent_id": "developer",
                            "operation": "deploy_service",
                        }
                    )
                )

        c4, _, _, _, _, r4 = await self.repo_a.claim_approval_resume(
            approval_id=appr_id, workflow_id=wf_id, tenant_id=self.tenant_id
        )
        self.assertFalse(c4)
        self.assertEqual(r4, "plan_hash_mismatch")

        # 5. Verify database state remained unchanged throughout rejected claims
        async with self.session_factory_a() as session:
            wf_db = await session.get(WorkflowModel, wf_id)
            self.assertEqual(wf_db.status, WorkflowStatus.WAITING_APPROVAL.value)
            self.assertEqual(wf_db.fencing_epoch, epoch)
            self.assertEqual(wf_db.row_revision, pause_rev)

    async def test_pg_08e_concurrent_approval_claim_safety(self):
        """
        PG-08E: Two competing workers simultaneously attempt claim_approval_resume on the same approval.
        PostgreSQL row-level locks and atomic CAS updates guarantee that exactly one worker wins
        and the other is rejected without corruption.
        """
        wf_id, appr_id, epoch, pause_rev, plan_hash, plan_id = (
            await self._setup_workflow_and_approved_request("08e")
        )

        # Worker A and Worker B concurrently contend for the approval resume claim
        res_a, res_b = await asyncio.gather(
            self.repo_a.claim_approval_resume(
                approval_id=appr_id, workflow_id=wf_id, tenant_id=self.tenant_id
            ),
            self.repo_b.claim_approval_resume(
                approval_id=appr_id, workflow_id=wf_id, tenant_id=self.tenant_id
            ),
        )

        succ_a, epoch_a, rev_a, tok_a, _, reason_a = res_a
        succ_b, epoch_b, rev_b, tok_b, _, reason_b = res_b

        # Exactly one worker must win
        self.assertTrue(succ_a ^ succ_b, "Exactly one worker must successfully claim approval resume.")

        winner_epoch = epoch_a if succ_a else epoch_b
        winner_rev = rev_a if succ_a else rev_b
        loser_reason = reason_b if succ_a else reason_a

        self.assertEqual(winner_epoch, epoch + 1)
        self.assertEqual(winner_rev, pause_rev + 1)
        self.assertIn(loser_reason, ("workflow_lease_active_or_ineligible", "invalid_resume_state"))

        # Verify persisted state reflects exactly one claim
        async with self.session_factory_a() as session:
            wf_db = await session.get(WorkflowModel, wf_id)
            self.assertEqual(wf_db.status, WorkflowStatus.RUNNING.value)
            self.assertEqual(wf_db.fencing_epoch, epoch + 1)
            self.assertEqual(wf_db.row_revision, pause_rev + 1)

            appr_db = await session.get(ApprovalRequestModel, appr_id)
            self.assertEqual(appr_db.resume_status, "resumed")
            self.assertEqual(appr_db.details.get("claim_count"), 1)


if __name__ == "__main__":
    unittest.main()
