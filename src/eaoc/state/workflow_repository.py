"""
EAOC Durable Workflow Recovery Repository
Provides storage abstractions and concrete implementations for PostgreSQL-backed
workflow recovery, atomic lease acquisition, fencing epochs, task immutability,
and authentic provenance tracking.
"""

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eaoc.db.models.approval import ApprovalRequestModel
from eaoc.db.models.workflow import WorkflowModel, WorkflowTaskModel
from eaoc.db.session import AsyncSessionFactory
from eaoc.observability import redact_sensitive_data
from eaoc.schemas.domain import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
    TaskStatus,
    WorkflowStatus,
)

logger = logging.getLogger("eaoc.state.workflow_repository")


class FencingLeaseLostError(Exception):
    """Raised when an executor loses its execution lease or encounters an epoch mismatch."""
    pass


class ConcurrencyConflictError(Exception):
    """Raised when a concurrent update conflict or OCC row_revision collision occurs."""
    pass


class PlanIntegrityError(Exception):
    """Raised when plan hashes, step hashes, or task identities conflict with stored records."""
    pass


class IWorkflowRecoveryRepository(ABC):
    """
    Storage-agnostic workflow recovery repository interface.
    Enforces tenant scoping, atomic lease acquisition (fencing_epoch),
    optimistic concurrency control (row_revision), task identity validation,
    and atomic approval resumption.
    """

    @abstractmethod
    async def create_workflow_intake(
        self,
        workflow_id: str,
        tenant_id: str,
        user_id: str,
        task_id: str,
        task_description: str,
        execution_plan: Optional[Dict[str, Any]] = None,
        plan_hash: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        idempotency_fingerprint: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[WorkflowModel, bool]:
        """
        Creates or idempotently retrieves a workflow record.
        Returns: (WorkflowModel, is_new: bool)
        Raises: ValueError if Idempotency-Key is reused with an altered fingerprint.
        """
        pass

    @abstractmethod
    async def get_workflow(
        self,
        workflow_id: str,
        tenant_id: str,
    ) -> Optional[WorkflowModel]:
        """Retrieves a workflow within tenant scope."""
        pass

    @abstractmethod
    async def acquire_execution_lease(
        self,
        workflow_id: str,
        tenant_id: str,
        current_token: Optional[str] = None,
    ) -> Tuple[bool, Optional[int], Optional[int], Optional[str]]:
        """
        Atomically acquires execution ownership for unowned or expired workflows.
        Returns: (success: bool, fencing_epoch, row_revision, lock_token)
        """
        pass

    @abstractmethod
    async def renew_execution_lease(
        self,
        workflow_id: str,
        tenant_id: str,
        fencing_epoch: int,
        lock_token: str,
    ) -> bool:
        """
        Renews an existing execution lease without modifying row_revision.
        Returns True if renewed; False if ownership was lost.
        """
        pass

    @abstractmethod
    async def release_execution_lease(
        self,
        workflow_id: str,
        tenant_id: str,
        fencing_epoch: int,
        lock_token: str,
        final_status: Optional[str] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Releases execution ownership upon pause, completion, or failure."""
        pass

    @abstractmethod
    async def prepare_task_step(
        self,
        workflow_id: str,
        tenant_id: str,
        step_index: int,
        agent_id: str,
        name: str,
        plan_hash: str,
        step_hash: str,
        inputs: Dict[str, Any],
        fencing_epoch: int,
        lock_token: str,
        expected_revision: int,
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]], int]:
        """
        Guards task execution: checks existing task identity, preserves COMPLETED tasks byte-for-byte,
        and advances workflow state to RUNNING.
        Returns: (is_completed: bool, output_data, provenance, new_revision: int)
        Raises: FencingLeaseLostError, PlanIntegrityError
        """
        pass

    @abstractmethod
    async def complete_task_step(
        self,
        workflow_id: str,
        tenant_id: str,
        step_index: int,
        outputs: Optional[Dict[str, Any]] = None,
        provenance: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[float] = None,
        fencing_epoch: int = 0,
        lock_token: str = "",
        expected_revision: int = 0,
        output_data: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Atomically commits task completion, authentic provenance, and bumps workflow row_revision.
        Returns: new_revision: int
        Raises: FencingLeaseLostError, ConcurrencyConflictError
        """
        pass

    @abstractmethod
    async def pause_workflow_for_approval(
        self,
        workflow_id: str,
        tenant_id: str,
        step_index: int,
        approval_id: str,
        requested_action: str,
        risk_level: str,
        details: Dict[str, Any],
        expires_at: datetime,
        fencing_epoch: int,
        lock_token: str,
        expected_revision: int,
    ) -> int:
        """
        Atomically pauses workflow state and creates an ApprovalRequestModel in PostgreSQL.
        Returns: new_revision: int
        """
        pass

    @abstractmethod
    async def claim_approval_resume(
        self,
        approval_id: str,
        workflow_id: str,
        tenant_id: str,
    ) -> Tuple[bool, Optional[int], Optional[int], Optional[str], Optional[WorkflowModel], str]:
        """
        Atomically claims an approved decision and acquires the workflow execution lease.
        Validates all tenant, workflow, step, task, agent, and plan bindings.
        Returns: (success: bool, fencing_epoch, row_revision, lock_token, workflow_model, failure_reason: str)
        """
        pass

    @abstractmethod
    async def get_tasks_for_workflow(
        self,
        workflow_id: str,
        tenant_id: str,
    ) -> List[WorkflowTaskModel]:
        """Returns all persisted tasks for a workflow."""
        pass

    @abstractmethod
    async def update_workflow_plan(
        self,
        workflow_id: str,
        tenant_id: str,
        execution_plan: Dict[str, Any],
        plan_hash: str,
        replan_count: int,
        replan_metadata: Dict[str, Any],
        current_step_index: int,
        fencing_epoch: int,
        lock_token: str,
        expected_revision: int,
    ) -> int:
        """
        Atomically updates the workflow's execution plan, canonical plan hash,
        replan counter, replan metadata, current step cursor, and row revision.
        Enforces tenant scoping, active execution lease (lock_token, fencing_epoch,
        lock_expires_at >= now()), and optimistic concurrency control (expected_revision).
        Deletes any non-completed (superseded) task records to prevent index collisions,
        while preserving completed task rows, their authentic provenance, and original plan hashes.

        Returns: new_revision: int
        Raises: FencingLeaseLostError, ConcurrencyConflictError
        """
        pass


_UNSET = object()


class PostgresWorkflowRecoveryRepository(IWorkflowRecoveryRepository):
    """
    SQLAlchemy Async implementation of IWorkflowRecoveryRepository targeting Supabase PostgreSQL.
    Enforces atomic CAS transactions, row-level locks, fencing epochs, and authentic provenance.
    """

    def __init__(self, session_factory: Any = _UNSET):
        if session_factory is _UNSET:
            self._session_factory = AsyncSessionFactory
        else:
            self._session_factory = session_factory

    def _ensure_session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("Database session factory is not configured.")
        return self._session_factory

    async def create_workflow_intake(
        self,
        workflow_id: str,
        tenant_id: str,
        user_id: str,
        task_id: str,
        task_description: str,
        execution_plan: Optional[Dict[str, Any]] = None,
        plan_hash: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        idempotency_fingerprint: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[WorkflowModel, bool]:
        factory = self._ensure_session_factory()
        now_utc = datetime.now(timezone.utc)

        try:
            async with factory() as session:
                async with session.begin():
                    # Check Idempotency Key first if supplied
                    if idempotency_key:
                        idemp_stmt = select(WorkflowModel).where(
                            and_(
                                WorkflowModel.tenant_id == tenant_id,
                                WorkflowModel.idempotency_key == idempotency_key,
                            )
                        )
                        res = await session.execute(idemp_stmt)
                        existing = res.scalar_one_or_none()
                        if existing is not None:
                            if existing.idempotency_fingerprint != idempotency_fingerprint:
                                raise ValueError("Idempotency-Key reuse with altered request payload is prohibited.")
                            return existing, False

                    wf = WorkflowModel(
                        workflow_id=workflow_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        task_id=task_id,
                        task_description=task_description,
                        status=WorkflowStatus.PENDING.value,
                        current_step_index=0 if execution_plan else None,
                        execution_plan=execution_plan,
                        plan_hash=plan_hash,
                        fencing_epoch=0,
                        row_revision=1,
                        lock_token=None,
                        lock_expires_at=None,
                        idempotency_key=idempotency_key,
                        idempotency_fingerprint=idempotency_fingerprint,
                        execution_metadata=metadata or {},
                        created_at=now_utc,
                        updated_at=now_utc,
                    )
                    session.add(wf)
                    await session.flush()
                    return wf, True
        except IntegrityError:
            # Concurrent race on uniqueness constraint (e.g. idempotency_key)
            if idempotency_key:
                async with factory() as session:
                    idemp_stmt = select(WorkflowModel).where(
                        and_(
                            WorkflowModel.tenant_id == tenant_id,
                            WorkflowModel.idempotency_key == idempotency_key,
                        )
                    )
                    res = await session.execute(idemp_stmt)
                    winner = res.scalar_one_or_none()
                    if winner is not None:
                        if winner.idempotency_fingerprint != idempotency_fingerprint:
                            raise ValueError("Idempotency-Key reuse with altered request payload is prohibited.")
                        return winner, False
            raise ConcurrencyConflictError(f"Unique constraint collision on workflow intake: '{workflow_id}'")

    async def get_workflow(
        self,
        workflow_id: str,
        tenant_id: str,
    ) -> Optional[WorkflowModel]:
        factory = self._ensure_session_factory()
        async with factory() as session:
            stmt = (
                select(WorkflowModel)
                .where(
                    and_(
                        WorkflowModel.workflow_id == workflow_id,
                        WorkflowModel.tenant_id == tenant_id,
                    )
                )
            )
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def acquire_execution_lease(
        self,
        workflow_id: str,
        tenant_id: str,
        current_token: Optional[str] = None,
    ) -> Tuple[bool, Optional[int], Optional[int], Optional[str]]:
        import uuid
        factory = self._ensure_session_factory()
        fresh_token = f"tok-{uuid.uuid4().hex[:16]}"

        lease_conditions = [
            WorkflowModel.lock_expires_at.is_(None),
            WorkflowModel.lock_expires_at < func.now(),
        ]
        if current_token is not None:
            lease_conditions.append(
                and_(
                    WorkflowModel.lock_token == current_token,
                    WorkflowModel.lock_token.is_not(None),
                )
            )

        async with factory() as session:
            async with session.begin():
                stmt = (
                    update(WorkflowModel)
                    .where(
                        and_(
                            WorkflowModel.workflow_id == workflow_id,
                            WorkflowModel.tenant_id == tenant_id,
                            or_(*lease_conditions),
                        )
                    )
                    .values(
                        fencing_epoch=WorkflowModel.fencing_epoch + 1,
                        row_revision=WorkflowModel.row_revision + 1,
                        lock_token=fresh_token,
                        lock_expires_at=func.now() + text("interval '30 seconds'"),
                        updated_at=func.now(),
                    )
                    .returning(WorkflowModel.fencing_epoch, WorkflowModel.row_revision)
                )
                res = await session.execute(stmt)
                row = res.one_or_none()
                if row is None:
                    return False, None, None, None
                epoch, rev = row
                return True, epoch, rev, fresh_token

    async def renew_execution_lease(
        self,
        workflow_id: str,
        tenant_id: str,
        fencing_epoch: int,
        lock_token: str,
    ) -> bool:
        factory = self._ensure_session_factory()
        async with factory() as session:
            async with session.begin():
                stmt = (
                    update(WorkflowModel)
                    .where(
                        and_(
                            WorkflowModel.workflow_id == workflow_id,
                            WorkflowModel.tenant_id == tenant_id,
                            WorkflowModel.fencing_epoch == fencing_epoch,
                            WorkflowModel.lock_token == lock_token,
                            WorkflowModel.lock_expires_at >= func.now(),
                        )
                    )
                    .values(
                        lock_expires_at=func.now() + text("interval '30 seconds'"),
                        updated_at=func.now(),
                    )
                )
                res = await session.execute(stmt)
                return res.rowcount == 1

    async def release_execution_lease(
        self,
        workflow_id: str,
        tenant_id: str,
        fencing_epoch: int,
        lock_token: str,
        final_status: Optional[str] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> bool:
        factory = self._ensure_session_factory()
        values_dict: Dict[str, Any] = {
            "lock_token": None,
            "lock_expires_at": None,
            "row_revision": WorkflowModel.row_revision + 1,
            "updated_at": func.now(),
        }
        if final_status:
            values_dict["status"] = final_status
        if error:
            values_dict["error"] = error

        async with factory() as session:
            async with session.begin():
                stmt = (
                    update(WorkflowModel)
                    .where(
                        and_(
                            WorkflowModel.workflow_id == workflow_id,
                            WorkflowModel.tenant_id == tenant_id,
                            WorkflowModel.fencing_epoch == fencing_epoch,
                            WorkflowModel.lock_token == lock_token,
                        )
                    )
                    .values(**values_dict)
                )
                res = await session.execute(stmt)
                return res.rowcount == 1

    async def prepare_task_step(
        self,
        workflow_id: str,
        tenant_id: str,
        step_index: int,
        agent_id: str,
        name: str,
        plan_hash: str,
        step_hash: str,
        inputs: Dict[str, Any],
        fencing_epoch: int,
        lock_token: str,
        expected_revision: int,
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]], int]:
        factory = self._ensure_session_factory()
        async with factory() as session:
            async with session.begin():
                # 1. Advance workflow state & verify fencing lease
                wf_stmt = (
                    update(WorkflowModel)
                    .where(
                        and_(
                            WorkflowModel.workflow_id == workflow_id,
                            WorkflowModel.tenant_id == tenant_id,
                            WorkflowModel.fencing_epoch == fencing_epoch,
                            WorkflowModel.lock_token == lock_token,
                            WorkflowModel.row_revision == expected_revision,
                            WorkflowModel.lock_expires_at >= func.now(),
                        )
                    )
                    .values(
                        status=WorkflowStatus.RUNNING.value,
                        current_step_index=step_index,
                        row_revision=WorkflowModel.row_revision + 1,
                        updated_at=func.now(),
                    )
                    .returning(WorkflowModel.row_revision, WorkflowModel.execution_metadata)
                )
                res_wf = await session.execute(wf_stmt)
                row_wf = res_wf.one_or_none()
                if row_wf is None:
                    raise FencingLeaseLostError(
                        f"Fencing lease lost or revision conflict on workflow '{workflow_id}' before step {step_index}."
                    )
                new_revision = row_wf[0]
                exec_meta = row_wf[1] or {}

                # 2. Query task row with lock
                task_stmt = (
                    select(WorkflowTaskModel)
                    .where(
                        and_(
                            WorkflowTaskModel.workflow_id == workflow_id,
                            WorkflowTaskModel.step_index == step_index,
                        )
                    )
                    .with_for_update()
                )
                res_task = await session.execute(task_stmt)
                existing = res_task.scalar_one_or_none()

                if existing is not None:
                    # Identity Conflict Validation
                    is_plan_hash_match = (existing.plan_hash == plan_hash)
                    if not is_plan_hash_match and existing.status == TaskStatus.COMPLETED.value:
                        prev_hashes = exec_meta.get("previous_plan_hashes") if isinstance(exec_meta, dict) else []
                        if isinstance(prev_hashes, list) and existing.plan_hash in prev_hashes:
                            is_plan_hash_match = True

                    if (
                        existing.tenant_id != tenant_id
                        or existing.workflow_id != workflow_id
                        or existing.step_index != step_index
                        or existing.task_id != f"{workflow_id}:step-{step_index}"
                        or existing.agent_id != agent_id
                        or existing.name != name
                        or not is_plan_hash_match
                        or existing.step_hash != step_hash
                    ):
                        raise PlanIntegrityError(
                            f"Step {step_index} identity conflict in workflow '{workflow_id}'. Task definition does not match."
                        )

                    if existing.status == TaskStatus.COMPLETED.value:
                        return True, existing.output_data, existing.provenance, new_revision

                    # Update non-completed task to RUNNING
                    existing.status = TaskStatus.RUNNING.value
                    existing.input_data = redact_sensitive_data(inputs)
                    existing.updated_at = func.now()
                    return False, None, None, new_revision

                # Insert new RUNNING task
                new_task = WorkflowTaskModel(
                    task_id=f"{workflow_id}:step-{step_index}",
                    workflow_id=workflow_id,
                    tenant_id=tenant_id,
                    step_index=step_index,
                    agent_id=agent_id,
                    name=name,
                    plan_hash=plan_hash,
                    step_hash=step_hash,
                    status=TaskStatus.RUNNING.value,
                    input_data=redact_sensitive_data(inputs),
                )
                session.add(new_task)
                return False, None, None, new_revision

    async def complete_task_step(
        self,
        workflow_id: str,
        tenant_id: str,
        step_index: int,
        outputs: Optional[Dict[str, Any]] = None,
        provenance: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[float] = None,
        fencing_epoch: int = 0,
        lock_token: str = "",
        expected_revision: int = 0,
        output_data: Optional[Dict[str, Any]] = None,
    ) -> int:
        if outputs is None and output_data is not None:
            outputs = output_data
        outputs = outputs or {}
        provenance = provenance or {}
        factory = self._ensure_session_factory()
        async with factory() as session:
            async with session.begin():
                # 1. Update task row
                task_stmt = (
                    update(WorkflowTaskModel)
                    .where(
                        and_(
                            WorkflowTaskModel.workflow_id == workflow_id,
                            WorkflowTaskModel.tenant_id == tenant_id,
                            WorkflowTaskModel.step_index == step_index,
                            WorkflowTaskModel.status == TaskStatus.RUNNING.value,
                        )
                    )
                    .values(
                        status=TaskStatus.COMPLETED.value,
                        output_data=redact_sensitive_data(outputs),
                        provenance=provenance,
                        duration_ms=duration_ms,
                        updated_at=func.now(),
                    )
                )
                res_task = await session.execute(task_stmt)
                if res_task.rowcount != 1:
                    raise ConcurrencyConflictError(
                        f"Task step {step_index} was not in RUNNING status for workflow '{workflow_id}'."
                    )

                # 2. Bump workflow row_revision with fencing checks
                wf_stmt = (
                    update(WorkflowModel)
                    .where(
                        and_(
                            WorkflowModel.workflow_id == workflow_id,
                            WorkflowModel.tenant_id == tenant_id,
                            WorkflowModel.fencing_epoch == fencing_epoch,
                            WorkflowModel.lock_token == lock_token,
                            WorkflowModel.row_revision == expected_revision,
                            WorkflowModel.lock_expires_at >= func.now(),
                        )
                    )
                    .values(
                        row_revision=WorkflowModel.row_revision + 1,
                        updated_at=func.now(),
                    )
                    .returning(WorkflowModel.row_revision)
                )
                res_wf = await session.execute(wf_stmt)
                row_wf = res_wf.one_or_none()
                if row_wf is None:
                    raise FencingLeaseLostError(
                        f"Fencing lease lost or revision conflict on workflow '{workflow_id}' while completing step {step_index}."
                    )
                return row_wf[0]

    async def pause_workflow_for_approval(
        self,
        workflow_id: str,
        tenant_id: str,
        step_index: int,
        approval_id: str,
        requested_action: str,
        risk_level: str,
        details: Dict[str, Any],
        expires_at: datetime,
        fencing_epoch: int,
        lock_token: str,
        expected_revision: int,
    ) -> int:
        factory = self._ensure_session_factory()
        now_utc = datetime.now(timezone.utc)

        async with factory() as session:
            async with session.begin():
                # 1. Release lease and pause workflow
                wf_stmt = (
                    update(WorkflowModel)
                    .where(
                        and_(
                            WorkflowModel.workflow_id == workflow_id,
                            WorkflowModel.tenant_id == tenant_id,
                            WorkflowModel.fencing_epoch == fencing_epoch,
                            WorkflowModel.lock_token == lock_token,
                            WorkflowModel.row_revision == expected_revision,
                        )
                    )
                    .values(
                        status=WorkflowStatus.WAITING_APPROVAL.value,
                        current_step_index=step_index,
                        lock_token=None,
                        lock_expires_at=None,
                        row_revision=WorkflowModel.row_revision + 1,
                        updated_at=func.now(),
                    )
                    .returning(WorkflowModel.row_revision)
                )
                res_wf = await session.execute(wf_stmt)
                row_wf = res_wf.one_or_none()
                if row_wf is None:
                    raise FencingLeaseLostError(f"Lease lost while pausing workflow '{workflow_id}'.")
                new_rev = row_wf[0]

                # 2. Insert approval request with plan_hash in details
                appr_model = ApprovalRequestModel(
                    approval_id=approval_id,
                    workflow_id=workflow_id,
                    tenant_id=tenant_id,
                    task_id=f"{workflow_id}:step-{step_index}",
                    step_index=step_index,
                    requested_action=requested_action,
                    risk_level=risk_level,
                    status=ApprovalStatus.PENDING.value,
                    requested_at=now_utc,
                    expires_at=expires_at,
                    details=details,
                    resume_status="none",
                )
                session.add(appr_model)
                return new_rev

    async def claim_approval_resume(
        self,
        approval_id: str,
        workflow_id: str,
        tenant_id: str,
    ) -> Tuple[bool, Optional[int], Optional[int], Optional[str], Optional[WorkflowModel], str]:
        import uuid
        factory = self._ensure_session_factory()
        fresh_token = f"tok-{uuid.uuid4().hex[:16]}"
        now_utc = datetime.now(timezone.utc)

        async with factory() as session:
            async with session.begin():
                # 1. Lock approval row with FOR UPDATE
                appr_stmt = (
                    select(ApprovalRequestModel)
                    .where(
                        and_(
                            ApprovalRequestModel.approval_id == approval_id,
                            ApprovalRequestModel.workflow_id == workflow_id,
                            ApprovalRequestModel.tenant_id == tenant_id,
                        )
                    )
                    .with_for_update()
                )
                res_appr = await session.execute(appr_stmt)
                appr_row = res_appr.scalar_one_or_none()
                if appr_row is None:
                    return False, None, None, None, None, "approval_not_found"

                if (
                    appr_row.status != ApprovalStatus.APPROVED.value
                    or appr_row.decision != ApprovalStatus.APPROVED.value
                ):
                    return False, None, None, None, None, "approval_ineligible"

                if appr_row.expires_at is None:
                    return False, None, None, None, None, "approval_expired"

                # Check if approval is unexpired using database time on mapped column
                appr_unexp_stmt = (
                    select(ApprovalRequestModel.approval_id)
                    .where(
                        and_(
                            ApprovalRequestModel.approval_id == approval_id,
                            ApprovalRequestModel.workflow_id == workflow_id,
                            ApprovalRequestModel.tenant_id == tenant_id,
                            ApprovalRequestModel.expires_at.is_not(None),
                            ApprovalRequestModel.expires_at > func.now(),
                        )
                    )
                )
                appr_unexp_res = await session.execute(appr_unexp_stmt)
                if appr_unexp_res.scalar_one_or_none() is None:
                    return False, None, None, None, None, "approval_expired"

                # 2. Lock workflow row with FOR UPDATE
                wf_stmt = (
                    select(WorkflowModel)
                    .where(
                        and_(
                            WorkflowModel.workflow_id == workflow_id,
                            WorkflowModel.tenant_id == tenant_id,
                        )
                    )
                    .with_for_update()
                )
                res_wf = await session.execute(wf_stmt)
                wf_row = res_wf.scalar_one_or_none()
                if wf_row is None:
                    return False, None, None, None, None, "workflow_not_found"

                # Check if lease is active right now using database time
                if wf_row.lock_expires_at is not None:
                    lease_active_stmt = (
                        select(WorkflowModel.workflow_id)
                        .where(
                            and_(
                                WorkflowModel.workflow_id == workflow_id,
                                WorkflowModel.tenant_id == tenant_id,
                                WorkflowModel.lock_expires_at.is_not(None),
                                WorkflowModel.lock_expires_at >= func.now(),
                            )
                        )
                    )
                    lease_active_res = await session.execute(lease_active_stmt)
                    if lease_active_res.scalar_one_or_none() is not None:
                        return False, None, None, None, None, "workflow_lease_active_or_ineligible"

                # 3. Enforce distinct state-machine branches:
                if appr_row.resume_status == "pending":
                    # First-time resume: requires WAITING_APPROVAL
                    if wf_row.status != WorkflowStatus.WAITING_APPROVAL.value:
                        return False, None, None, None, None, "invalid_resume_state"
                elif appr_row.resume_status == "resumed":
                    # Abandoned execution recovery: requires RUNNING and expired lease
                    if wf_row.status != WorkflowStatus.RUNNING.value or wf_row.lock_expires_at is None:
                        return False, None, None, None, None, "invalid_resume_state"
                else:
                    return False, None, None, None, None, "resume_status_invalid"

                # 4. Step index binding
                if appr_row.step_index != wf_row.current_step_index:
                    return False, None, None, None, None, "step_index_mismatch"

                # 5. Plan hash & plan ID binding
                plan_hash = wf_row.plan_hash
                if appr_row.details.get("plan_hash") != plan_hash:
                    return False, None, None, None, None, "plan_hash_mismatch"

                exec_plan = wf_row.execution_plan or {}
                expected_plan_id = exec_plan.get("plan_id")
                appr_plan_id = appr_row.details.get("plan_id")
                if expected_plan_id and appr_plan_id and appr_plan_id != expected_plan_id:
                    return False, None, None, None, None, "plan_id_mismatch"

                # 6. Task ID, agent ID, requested action, and risk level binding
                expected_task_id = f"{workflow_id}:step-{appr_row.step_index}"
                if appr_row.task_id and appr_row.task_id != expected_task_id:
                    return False, None, None, None, None, "task_id_mismatch"

                steps = exec_plan.get("steps", [])
                step_def = next((s for s in steps if s.get("step_index") == appr_row.step_index), None)
                if step_def is not None:
                    step_agent = step_def.get("agent_id")
                    if hasattr(step_agent, "value"):
                        step_agent = step_agent.value
                    appr_agent = appr_row.details.get("agent_id")
                    if appr_agent and step_agent and appr_agent != step_agent:
                        return False, None, None, None, None, "agent_id_mismatch"

                    step_risk = step_def.get("risk_level")
                    if hasattr(step_risk, "value"):
                        step_risk = step_risk.value
                    if appr_row.risk_level and step_risk and appr_row.risk_level.lower() != str(step_risk).lower():
                        return False, None, None, None, None, "risk_level_mismatch"

                    step_op = str(step_def.get("operation") or "").lower()
                    step_desc = str(step_def.get("description") or "").lower()
                    req_act = (appr_row.requested_action or "").lower().strip()
                    if not req_act:
                        return False, None, None, None, None, "action_mismatch"

                    appr_op = str(appr_row.details.get("operation") or "").lower()
                    if appr_op and step_op and appr_op != step_op:
                        return False, None, None, None, None, "action_mismatch"

                    op_tokens = {t for t in step_op.split("_") if len(t) > 2}
                    desc_tokens = {t for t in step_desc.split() if len(t) > 3}
                    all_tokens = op_tokens | desc_tokens
                    act_tokens = {t for t in req_act.split() if len(t) > 2}
                    if not (req_act in step_desc or step_desc in req_act or req_act in step_op or (all_tokens & act_tokens)):
                        return False, None, None, None, None, "action_mismatch"

                # 7. Durable Task Completion Guard
                task_stmt = (
                    select(WorkflowTaskModel)
                    .where(
                        and_(
                            WorkflowTaskModel.workflow_id == workflow_id,
                            WorkflowTaskModel.step_index == appr_row.step_index,
                        )
                    )
                    .with_for_update()
                )
                res_task = await session.execute(task_stmt)
                task_row = res_task.scalar_one_or_none()
                if task_row is not None:
                    if task_row.status == TaskStatus.COMPLETED.value:
                        return False, None, None, None, None, "task_already_completed"
                    if task_row.tenant_id != tenant_id:
                        return False, None, None, None, None, "task_tenant_mismatch"
                    if task_row.plan_hash and task_row.plan_hash != plan_hash:
                        return False, None, None, None, None, "task_plan_hash_mismatch"

                # 8. Atomic Handoff Update
                is_first_claim = (appr_row.resume_status == "pending")
                appr_row.resume_status = "resumed"
                appr_row.updated_at = func.now()

                curr_claims = appr_row.details.get("claim_count", 0) if appr_row.details else 0
                new_claim_count = curr_claims + 1
                appr_row.details = {
                    **(appr_row.details or {}),
                    "claim_count": new_claim_count,
                    "is_first_claim": is_first_claim,
                    "is_recovery_claim": not is_first_claim,
                    "last_claimed_at": now_utc.isoformat(),
                }

                wf_row.status = WorkflowStatus.RUNNING.value
                wf_row.fencing_epoch += 1
                wf_row.row_revision += 1
                wf_row.lock_token = fresh_token
                wf_row.lock_expires_at = func.now() + text("interval '30 seconds'")
                wf_row.updated_at = func.now()

                curr_meta = dict(wf_row.execution_metadata or {})
                curr_meta["is_first_resume_claim"] = is_first_claim
                curr_meta["approval_claim_count"] = new_claim_count
                curr_meta["last_resumed_approval_id"] = approval_id
                wf_row.execution_metadata = curr_meta

                await session.flush()
                return True, wf_row.fencing_epoch, wf_row.row_revision, fresh_token, wf_row, "success"

    async def get_tasks_for_workflow(
        self,
        workflow_id: str,
        tenant_id: str,
    ) -> List[WorkflowTaskModel]:
        factory = self._ensure_session_factory()
        async with factory() as session:
            stmt = (
                select(WorkflowTaskModel)
                .where(
                    and_(
                        WorkflowTaskModel.workflow_id == workflow_id,
                        WorkflowTaskModel.tenant_id == tenant_id,
                    )
                )
                .order_by(WorkflowTaskModel.step_index.asc())
            )
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def update_workflow_plan(
        self,
        workflow_id: str,
        tenant_id: str,
        execution_plan: Dict[str, Any],
        plan_hash: str,
        replan_count: int,
        replan_metadata: Dict[str, Any],
        current_step_index: int,
        fencing_epoch: int,
        lock_token: str,
        expected_revision: int,
    ) -> int:
        factory = self._ensure_session_factory()
        async with factory() as session:
            async with session.begin():
                # 1. Fetch current workflow with row-level lock within tenant scope
                wf_stmt = (
                    select(WorkflowModel)
                    .where(
                        and_(
                            WorkflowModel.workflow_id == workflow_id,
                            WorkflowModel.tenant_id == tenant_id,
                        )
                    )
                    .with_for_update()
                )
                res = await session.execute(wf_stmt)
                wf = res.scalar_one_or_none()
                if wf is None:
                    raise FencingLeaseLostError(
                        f"Workflow '{workflow_id}' not found for tenant '{tenant_id}'."
                    )

                # 2. Enforce fencing epoch, lock token, active lease expiry, and expected row revision
                now_utc = datetime.now(timezone.utc)
                lock_exp = wf.lock_expires_at
                if lock_exp is not None and lock_exp.tzinfo is None:
                    lock_exp = lock_exp.replace(tzinfo=timezone.utc)

                if (
                    wf.fencing_epoch != fencing_epoch
                    or wf.lock_token != lock_token
                    or lock_exp is None
                    or lock_exp < now_utc
                ):
                    raise FencingLeaseLostError(
                        f"Fencing lease lost or expired for workflow '{workflow_id}'."
                    )

                if wf.row_revision != expected_revision:
                    raise ConcurrencyConflictError(
                        f"Row revision conflict on workflow '{workflow_id}': "
                        f"expected {expected_revision}, got {wf.row_revision}."
                    )

                # 3. Preserve historical plan hashes and audit provenance
                exec_meta = dict(wf.execution_metadata or {})
                exec_meta["replan_count"] = replan_count
                exec_meta["last_replan_metadata"] = replan_metadata

                prev_hashes = list(exec_meta.get("previous_plan_hashes") or [])
                if wf.plan_hash and wf.plan_hash not in prev_hashes:
                    prev_hashes.append(wf.plan_hash)
                exec_meta["previous_plan_hashes"] = prev_hashes

                replan_hist = list(exec_meta.get("replan_history") or [])
                replan_hist.append({
                    "replan_count": replan_count,
                    "previous_plan_hash": wf.plan_hash,
                    "new_plan_hash": plan_hash,
                    "failed_step_index": current_step_index,
                    "replan_metadata": replan_metadata,
                    "timestamp": now_utc.isoformat(),
                })
                exec_meta["replan_history"] = replan_hist

                # 4. Update workflow model
                wf.execution_plan = execution_plan
                wf.plan_hash = plan_hash
                wf.current_step_index = current_step_index
                wf.execution_metadata = exec_meta
                wf.row_revision = wf.row_revision + 1
                new_revision = wf.row_revision
                wf.updated_at = now_utc

                # 5. Clean up non-completed (superseded) tasks to prevent step_index collisions
                delete_stmt = (
                    delete(WorkflowTaskModel)
                    .where(
                        and_(
                            WorkflowTaskModel.workflow_id == workflow_id,
                            WorkflowTaskModel.tenant_id == tenant_id,
                            WorkflowTaskModel.status != TaskStatus.COMPLETED.value,
                        )
                    )
                )
                await session.execute(delete_stmt)
                return new_revision


class InMemoryWorkflowRecoveryRepository(IWorkflowRecoveryRepository):
    """
    Hermetic in-memory implementation of IWorkflowRecoveryRepository for fast, isolated unit tests.
    Async-safe with asyncio.Lock.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._workflows: Dict[str, WorkflowModel] = {}
        self._tasks: Dict[str, WorkflowTaskModel] = {} # Keyed by "{wf_id}:{step_index}"
        self._approvals: Dict[str, ApprovalRequestModel] = {}

    async def create_workflow_intake(
        self,
        workflow_id: str,
        tenant_id: str,
        user_id: str,
        task_id: str,
        task_description: str,
        execution_plan: Optional[Dict[str, Any]] = None,
        plan_hash: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        idempotency_fingerprint: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[WorkflowModel, bool]:
        async with self._lock:
            if idempotency_key:
                for w in self._workflows.values():
                    if w.tenant_id == tenant_id and w.idempotency_key == idempotency_key:
                        if w.idempotency_fingerprint != idempotency_fingerprint:
                            raise ValueError("Idempotency-Key reuse with altered request payload is prohibited.")
                        return w, False

            wf = WorkflowModel(
                workflow_id=workflow_id,
                tenant_id=tenant_id,
                user_id=user_id,
                task_id=task_id,
                task_description=task_description,
                status=WorkflowStatus.PENDING.value,
                current_step_index=0 if execution_plan else None,
                execution_plan=execution_plan,
                plan_hash=plan_hash,
                fencing_epoch=0,
                row_revision=1,
                idempotency_key=idempotency_key,
                idempotency_fingerprint=idempotency_fingerprint,
                execution_metadata=metadata or {},
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            self._workflows[workflow_id] = wf
            return wf, True

    async def get_workflow(
        self,
        workflow_id: str,
        tenant_id: str,
    ) -> Optional[WorkflowModel]:
        async with self._lock:
            wf = self._workflows.get(workflow_id)
            if wf and wf.tenant_id == tenant_id:
                return wf
            return None

    async def acquire_execution_lease(
        self,
        workflow_id: str,
        tenant_id: str,
        current_token: Optional[str] = None,
    ) -> Tuple[bool, Optional[int], Optional[int], Optional[str]]:
        import uuid
        async with self._lock:
            wf = self._workflows.get(workflow_id)
            if not wf or wf.tenant_id != tenant_id:
                return False, None, None, None

            now = datetime.now(timezone.utc)
            if wf.lock_expires_at and wf.lock_expires_at >= now:
                if current_token is None or wf.lock_token != current_token:
                    return False, None, None, None

            from datetime import timedelta
            fresh_token = f"tok-{uuid.uuid4().hex[:16]}"
            wf.fencing_epoch += 1
            wf.row_revision += 1
            wf.lock_token = fresh_token
            wf.lock_expires_at = now + timedelta(seconds=30)
            wf.updated_at = now
            return True, wf.fencing_epoch, wf.row_revision, fresh_token

    async def renew_execution_lease(
        self,
        workflow_id: str,
        tenant_id: str,
        fencing_epoch: int,
        lock_token: str,
    ) -> bool:
        from datetime import timedelta
        async with self._lock:
            wf = self._workflows.get(workflow_id)
            if not wf or wf.tenant_id != tenant_id:
                return False
            now = datetime.now(timezone.utc)
            if (
                wf.fencing_epoch == fencing_epoch
                and wf.lock_token == lock_token
                and wf.lock_expires_at
                and wf.lock_expires_at >= now
            ):
                wf.lock_expires_at = now + timedelta(seconds=30)
                wf.updated_at = now
                return True
            return False

    async def release_execution_lease(
        self,
        workflow_id: str,
        tenant_id: str,
        fencing_epoch: int,
        lock_token: str,
        final_status: Optional[str] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> bool:
        async with self._lock:
            wf = self._workflows.get(workflow_id)
            if not wf or wf.tenant_id != tenant_id:
                return False
            if wf.fencing_epoch == fencing_epoch and wf.lock_token == lock_token:
                wf.lock_token = None
                wf.lock_expires_at = None
                wf.row_revision += 1
                if final_status:
                    wf.status = final_status
                if error:
                    wf.error = error
                wf.updated_at = datetime.now(timezone.utc)
                return True
            return False

    async def prepare_task_step(
        self,
        workflow_id: str,
        tenant_id: str,
        step_index: int,
        agent_id: str,
        name: str,
        plan_hash: str,
        step_hash: str,
        inputs: Dict[str, Any],
        fencing_epoch: int,
        lock_token: str,
        expected_revision: int,
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]], int]:
        async with self._lock:
            wf = self._workflows.get(workflow_id)
            now = datetime.now(timezone.utc)
            if (
                not wf
                or wf.tenant_id != tenant_id
                or wf.fencing_epoch != fencing_epoch
                or wf.lock_token != lock_token
                or wf.row_revision != expected_revision
                or not wf.lock_expires_at
                or wf.lock_expires_at < now
            ):
                raise FencingLeaseLostError("Fencing lease lost or revision conflict before step execution.")

            wf.status = WorkflowStatus.RUNNING.value
            wf.current_step_index = step_index
            wf.row_revision += 1
            wf.updated_at = now
            new_rev = wf.row_revision

            task_key = f"{workflow_id}:{step_index}"
            existing = self._tasks.get(task_key)
            if existing:
                is_plan_hash_match = (existing.plan_hash == plan_hash)
                if not is_plan_hash_match and existing.status == TaskStatus.COMPLETED.value:
                    prev_hashes = (wf.execution_metadata or {}).get("previous_plan_hashes") or []
                    if isinstance(prev_hashes, list) and existing.plan_hash in prev_hashes:
                        is_plan_hash_match = True

                if (
                    existing.tenant_id != tenant_id
                    or existing.workflow_id != workflow_id
                    or existing.step_index != step_index
                    or existing.task_id != f"{workflow_id}:step-{step_index}"
                    or existing.agent_id != agent_id
                    or existing.name != name
                    or not is_plan_hash_match
                    or existing.step_hash != step_hash
                ):
                    raise PlanIntegrityError(f"Step {step_index} task identity conflict.")

                if existing.status == TaskStatus.COMPLETED.value:
                    return True, existing.output_data, existing.provenance, new_rev

                existing.status = TaskStatus.RUNNING.value
                existing.input_data = redact_sensitive_data(inputs)
                existing.updated_at = now
                return False, None, None, new_rev

            new_task = WorkflowTaskModel(
                task_id=f"{workflow_id}:step-{step_index}",
                workflow_id=workflow_id,
                tenant_id=tenant_id,
                step_index=step_index,
                agent_id=agent_id,
                name=name,
                plan_hash=plan_hash,
                step_hash=step_hash,
                status=TaskStatus.RUNNING.value,
                input_data=redact_sensitive_data(inputs),
                created_at=now,
                updated_at=now,
            )
            self._tasks[task_key] = new_task
            return False, None, None, new_rev

    async def complete_task_step(
        self,
        workflow_id: str,
        tenant_id: str,
        step_index: int,
        outputs: Optional[Dict[str, Any]] = None,
        provenance: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[float] = None,
        fencing_epoch: int = 0,
        lock_token: str = "",
        expected_revision: int = 0,
        output_data: Optional[Dict[str, Any]] = None,
    ) -> int:
        if outputs is None and output_data is not None:
            outputs = output_data
        outputs = outputs or {}
        provenance = provenance or {}
        async with self._lock:
            task_key = f"{workflow_id}:{step_index}"
            task = self._tasks.get(task_key)
            if not task or task.status != TaskStatus.RUNNING.value:
                raise ConcurrencyConflictError(f"Task step {step_index} was not in RUNNING status.")

            wf = self._workflows.get(workflow_id)
            now = datetime.now(timezone.utc)
            if (
                not wf
                or wf.tenant_id != tenant_id
                or wf.fencing_epoch != fencing_epoch
                or wf.lock_token != lock_token
                or wf.row_revision != expected_revision
                or not wf.lock_expires_at
                or wf.lock_expires_at < now
            ):
                raise FencingLeaseLostError("Fencing lease lost or revision conflict while completing task.")

            task.status = TaskStatus.COMPLETED.value
            task.output_data = redact_sensitive_data(outputs)
            task.provenance = provenance
            task.duration_ms = duration_ms
            task.updated_at = now

            wf.row_revision += 1
            wf.updated_at = now
            return wf.row_revision

    async def pause_workflow_for_approval(
        self,
        workflow_id: str,
        tenant_id: str,
        step_index: int,
        approval_id: str,
        requested_action: str,
        risk_level: str,
        details: Dict[str, Any],
        expires_at: datetime,
        fencing_epoch: int,
        lock_token: str,
        expected_revision: int,
    ) -> int:
        async with self._lock:
            wf = self._workflows.get(workflow_id)
            now = datetime.now(timezone.utc)
            if (
                not wf
                or wf.tenant_id != tenant_id
                or wf.fencing_epoch != fencing_epoch
                or wf.lock_token != lock_token
                or wf.row_revision != expected_revision
            ):
                raise FencingLeaseLostError("Fencing lease lost while pausing for approval.")

            wf.status = WorkflowStatus.WAITING_APPROVAL.value
            wf.current_step_index = step_index
            wf.lock_token = None
            wf.lock_expires_at = None
            wf.row_revision += 1
            wf.updated_at = now

            appr = ApprovalRequestModel(
                approval_id=approval_id,
                workflow_id=workflow_id,
                tenant_id=tenant_id,
                task_id=f"{workflow_id}:step-{step_index}",
                step_index=step_index,
                requested_action=requested_action,
                risk_level=risk_level,
                status=ApprovalStatus.PENDING.value,
                requested_at=now,
                expires_at=expires_at,
                details=details,
                resume_status="none",
                created_at=now,
                updated_at=now,
            )
            self._approvals[approval_id] = appr
            return wf.row_revision

    async def claim_approval_resume(
        self,
        approval_id: str,
        workflow_id: str,
        tenant_id: str,
    ) -> Tuple[bool, Optional[int], Optional[int], Optional[str], Optional[WorkflowModel], str]:
        import uuid
        from datetime import timedelta
        async with self._lock:
            appr = self._approvals.get(approval_id)
            now = datetime.now(timezone.utc)
            if not appr or appr.workflow_id != workflow_id or appr.tenant_id != tenant_id:
                return False, None, None, None, None, "approval_not_found"

            exp = appr.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= now:
                return False, None, None, None, None, "approval_expired"

            if appr.status != ApprovalStatus.APPROVED.value or appr.decision != ApprovalStatus.APPROVED.value:
                return False, None, None, None, None, "approval_ineligible"

            wf = self._workflows.get(workflow_id)
            if not wf or wf.tenant_id != tenant_id:
                return False, None, None, None, None, "workflow_not_found"

            # Check if lease active right now
            if wf.lock_expires_at and wf.lock_expires_at >= now:
                return False, None, None, None, None, "workflow_lease_active_or_ineligible"

            # Enforce distinct state-machine branches:
            if appr.resume_status == "pending":
                if wf.status != WorkflowStatus.WAITING_APPROVAL.value:
                    return False, None, None, None, None, "invalid_resume_state"
            elif appr.resume_status == "resumed":
                if wf.status != WorkflowStatus.RUNNING.value or wf.lock_expires_at is None:
                    return False, None, None, None, None, "invalid_resume_state"
            else:
                return False, None, None, None, None, "resume_status_invalid"

            if appr.step_index != wf.current_step_index:
                return False, None, None, None, None, "step_index_mismatch"

            plan_hash = wf.plan_hash
            if appr.details.get("plan_hash") != plan_hash:
                return False, None, None, None, None, "plan_hash_mismatch"

            exec_plan = wf.execution_plan or {}
            expected_plan_id = exec_plan.get("plan_id")
            appr_plan_id = appr.details.get("plan_id")
            if expected_plan_id and appr_plan_id and appr_plan_id != expected_plan_id:
                return False, None, None, None, None, "plan_id_mismatch"

            expected_task_id = f"{workflow_id}:step-{appr.step_index}"
            if appr.task_id and appr.task_id != expected_task_id:
                return False, None, None, None, None, "task_id_mismatch"

            steps = exec_plan.get("steps", [])
            step_def = next((s for s in steps if s.get("step_index") == appr.step_index), None)
            if step_def is not None:
                step_agent = step_def.get("agent_id")
                if hasattr(step_agent, "value"):
                    step_agent = step_agent.value
                appr_agent = appr.details.get("agent_id")
                if appr_agent and step_agent and appr_agent != step_agent:
                    return False, None, None, None, None, "agent_id_mismatch"

                step_risk = step_def.get("risk_level")
                if hasattr(step_risk, "value"):
                    step_risk = step_risk.value
                if appr.risk_level and step_risk and appr.risk_level.lower() != str(step_risk).lower():
                    return False, None, None, None, None, "risk_level_mismatch"

                step_op = str(step_def.get("operation") or "").lower()
                step_desc = str(step_def.get("description") or "").lower()
                req_act = (appr.requested_action or "").lower().strip()
                if not req_act:
                    return False, None, None, None, None, "action_mismatch"

                appr_op = str(appr.details.get("operation") or "").lower()
                if appr_op and step_op and appr_op != step_op:
                    return False, None, None, None, None, "action_mismatch"

                op_tokens = {t for t in step_op.split("_") if len(t) > 2}
                desc_tokens = {t for t in step_desc.split() if len(t) > 3}
                all_tokens = op_tokens | desc_tokens
                act_tokens = {t for t in req_act.split() if len(t) > 2}
                if not (req_act in step_desc or step_desc in req_act or req_act in step_op or (all_tokens & act_tokens)):
                    return False, None, None, None, None, "action_mismatch"

            # Durable Task Completion Guard
            task_key = f"{workflow_id}:{appr.step_index}"
            task_row = self._tasks.get(task_key)
            if task_row is not None:
                if task_row.status == TaskStatus.COMPLETED.value:
                    return False, None, None, None, None, "task_already_completed"
                if task_row.tenant_id != tenant_id:
                    return False, None, None, None, None, "task_tenant_mismatch"
                if task_row.plan_hash and task_row.plan_hash != plan_hash:
                    return False, None, None, None, None, "task_plan_hash_mismatch"

            fresh_token = f"tok-{uuid.uuid4().hex[:16]}"
            is_first_claim = (appr.resume_status == "pending")
            appr.resume_status = "resumed"
            appr.updated_at = now

            curr_claims = appr.details.get("claim_count", 0) if appr.details else 0
            new_claim_count = curr_claims + 1
            appr.details = {
                **(appr.details or {}),
                "claim_count": new_claim_count,
                "is_first_claim": is_first_claim,
                "is_recovery_claim": not is_first_claim,
                "last_claimed_at": now.isoformat(),
            }

            wf.status = WorkflowStatus.RUNNING.value
            wf.fencing_epoch += 1
            wf.row_revision += 1
            wf.lock_token = fresh_token
            wf.lock_expires_at = now + timedelta(seconds=30)
            wf.updated_at = now

            curr_meta = dict(wf.execution_metadata or {})
            curr_meta["is_first_resume_claim"] = is_first_claim
            curr_meta["approval_claim_count"] = new_claim_count
            curr_meta["last_resumed_approval_id"] = approval_id
            wf.execution_metadata = curr_meta

            return True, wf.fencing_epoch, wf.row_revision, fresh_token, wf, "success"


    async def get_tasks_for_workflow(
        self,
        workflow_id: str,
        tenant_id: str,
    ) -> List[WorkflowTaskModel]:
        async with self._lock:
            tasks = [
                t for t in self._tasks.values()
                if t.workflow_id == workflow_id and t.tenant_id == tenant_id
            ]
            return sorted(tasks, key=lambda x: x.step_index)

    async def update_workflow_plan(
        self,
        workflow_id: str,
        tenant_id: str,
        execution_plan: Dict[str, Any],
        plan_hash: str,
        replan_count: int,
        replan_metadata: Dict[str, Any],
        current_step_index: int,
        fencing_epoch: int,
        lock_token: str,
        expected_revision: int,
    ) -> int:
        async with self._lock:
            wf = self._workflows.get(workflow_id)
            if not wf or wf.tenant_id != tenant_id:
                raise FencingLeaseLostError(f"Workflow '{workflow_id}' not found for tenant '{tenant_id}'.")

            now = datetime.now(timezone.utc)
            lock_exp = wf.lock_expires_at
            if lock_exp is not None and lock_exp.tzinfo is None:
                lock_exp = lock_exp.replace(tzinfo=timezone.utc)

            if (
                wf.fencing_epoch != fencing_epoch
                or wf.lock_token != lock_token
                or lock_exp is None
                or lock_exp < now
            ):
                raise FencingLeaseLostError(f"Fencing lease lost or expired for workflow '{workflow_id}'.")

            if wf.row_revision != expected_revision:
                raise ConcurrencyConflictError(
                    f"Row revision conflict on workflow '{workflow_id}': expected {expected_revision}, got {wf.row_revision}."
                )

            exec_meta = dict(wf.execution_metadata or {})
            exec_meta["replan_count"] = replan_count
            exec_meta["last_replan_metadata"] = replan_metadata

            prev_hashes = list(exec_meta.get("previous_plan_hashes") or [])
            if wf.plan_hash and wf.plan_hash not in prev_hashes:
                prev_hashes.append(wf.plan_hash)
            exec_meta["previous_plan_hashes"] = prev_hashes

            replan_hist = list(exec_meta.get("replan_history") or [])
            replan_hist.append({
                "replan_count": replan_count,
                "previous_plan_hash": wf.plan_hash,
                "new_plan_hash": plan_hash,
                "failed_step_index": current_step_index,
                "replan_metadata": replan_metadata,
                "timestamp": now.isoformat(),
            })
            exec_meta["replan_history"] = replan_hist

            wf.execution_plan = execution_plan
            wf.plan_hash = plan_hash
            wf.current_step_index = current_step_index
            wf.execution_metadata = exec_meta
            wf.row_revision += 1
            wf.updated_at = now

            # Clean up non-completed tasks
            to_delete = []
            for k, task in self._tasks.items():
                if task.workflow_id == workflow_id and task.tenant_id == tenant_id:
                    if task.status != TaskStatus.COMPLETED.value:
                        to_delete.append(k)
            for k in to_delete:
                del self._tasks[k]

            return wf.row_revision
