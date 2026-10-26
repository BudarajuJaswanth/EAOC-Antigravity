"""
EAOC Approval Request Repository
Provides storage abstractions for Human-in-the-Loop (HITL) approval requests,
decisions, expiration handling, and resume-recovery state.
Supabase PostgreSQL is the exclusive primary store.
"""

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, update, and_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eaoc.db.models.approval import ApprovalRequestModel
from eaoc.db.session import AsyncSessionFactory
from eaoc.observability import redact_sensitive_data
from eaoc.schemas.domain import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
)

logger = logging.getLogger("eaoc.state.approval_repository")

VALID_RESUME_STATUSES = {"none", "pending", "resumed", "failed"}


class IApprovalRepository(ABC):
    """
    Storage-agnostic approval repository interface.
    Enforces tenant scoping, atomic conditional decisions, and resume-recovery tracking.
    """

    @abstractmethod
    async def create_request(
        self,
        request: ApprovalRequest,
        tenant_id: str,
        expires_at: datetime,
    ) -> ApprovalRequest:
        """
        Persists a newly created approval request.
        Must raise ValueError if approval_id already exists.
        """
        pass

    @abstractmethod
    async def get_by_id(
        self,
        approval_id: str,
        tenant_id: str,
    ) -> Optional[ApprovalRequest]:
        """
        Retrieves an approval request within tenant scope.
        If pending and expired, updates the status to EXPIRED and returns it.
        Returns None if not found or if cross-tenant.
        """
        pass

    @abstractmethod
    async def get_model_by_id(
        self,
        approval_id: str,
        tenant_id: str,
    ) -> Optional[ApprovalRequestModel]:
        """
        Retrieves the full ORM model within tenant scope.
        If pending and expired, updates the status to EXPIRED and returns it.
        """
        pass

    @abstractmethod
    async def get_decision(
        self,
        approval_id: str,
        tenant_id: str,
    ) -> Optional[ApprovalDecision]:
        """
        Retrieves recorded approval decision within tenant scope.
        Returns None if no decision has been committed or if cross-tenant.
        """
        pass

    @abstractmethod
    async def list_pending(
        self,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ApprovalRequest]:
        """
        Lists pending, unexpired requests for a tenant with validated pagination.
        """
        pass

    @abstractmethod
    async def list_pending_models(
        self,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ApprovalRequestModel]:
        """
        Lists full models for pending, unexpired requests for a tenant with validated pagination.
        """
        pass

    @abstractmethod
    async def count_pending(
        self,
        tenant_id: str,
    ) -> int:
        """
        Counts unexpired pending requests within tenant scope.
        """
        pass

    @abstractmethod
    async def list_by_workflow(
        self,
        workflow_id: str,
        tenant_id: str,
    ) -> List[ApprovalRequest]:
        """
        Lists all approval requests for a specific workflow within tenant scope.
        """
        pass

    @abstractmethod
    async def record_conditional_decision(
        self,
        approval_id: str,
        tenant_id: str,
        decision: ApprovalStatus,
        approver_id: str,
        reason: Optional[str] = None,
    ) -> Tuple[bool, Optional[ApprovalRequest], str]:
        """
        Atomically records a decision using Compare-And-Swap (CAS).
        Returns: (success: bool, updated_record: Optional[ApprovalRequest], failure_code: str)
        Failure codes: 'success', 'not_found', 'already_decided', 'expired'.
        """
        pass

    @abstractmethod
    async def update_resume_status(
        self,
        approval_id: str,
        tenant_id: str,
        resume_status: str,
        resume_error: Optional[str] = None,
    ) -> bool:
        """
        Updates the resume-recovery status and safe error details.
        Does NOT alter or reset the human decision.
        """
        pass


_UNSET = object()


class PostgresApprovalRepository(IApprovalRepository):
    """
    SQLAlchemy Async implementation of IApprovalRepository targeting Supabase PostgreSQL.
    Enforces tenant scoping, atomic conditional updates, and durable expiration tracking.
    """

    def __init__(
        self,
        session_factory: Any = _UNSET,
    ):
        if session_factory is _UNSET:
            self._session_factory = AsyncSessionFactory
        else:
            self._session_factory = session_factory

    def _ensure_session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError(
                "Supabase database connection is not configured. "
                "Please configure a valid DATABASE_URL in your environment."
            )
        return self._session_factory

    async def create_request(
        self,
        request: ApprovalRequest,
        tenant_id: str,
        expires_at: datetime,
    ) -> ApprovalRequest:
        if not request.approval_id or not request.approval_id.strip():
            raise ValueError("approval_id must be a non-empty string.")
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string.")

        factory = self._ensure_session_factory()
        async with factory() as session:
            # Check for duplicate approval_id
            existing = await session.get(ApprovalRequestModel, request.approval_id)
            if existing:
                raise ValueError(f"ApprovalRequest with ID '{request.approval_id}' already exists.")

            model = ApprovalRequestModel.from_domain_request(
                request=request,
                tenant_id=tenant_id.strip(),
                expires_at=expires_at,
            )
            session.add(model)
            await session.commit()
            await session.refresh(model)
            return model.to_domain_request()

    async def get_by_id(
        self,
        approval_id: str,
        tenant_id: str,
    ) -> Optional[ApprovalRequest]:
        model = await self.get_model_by_id(approval_id, tenant_id)
        return model.to_domain_request() if model else None

    async def get_model_by_id(
        self,
        approval_id: str,
        tenant_id: str,
    ) -> Optional[ApprovalRequestModel]:
        if not approval_id or not tenant_id:
            return None

        factory = self._ensure_session_factory()
        now_utc = datetime.now(timezone.utc)

        async with factory() as session:
            stmt = select(ApprovalRequestModel).where(
                and_(
                    ApprovalRequestModel.approval_id == approval_id,
                    ApprovalRequestModel.tenant_id == tenant_id,
                )
            )
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()
            if model is None:
                return None

            # Mark EXPIRED if observed expired while pending
            if model.status == ApprovalStatus.PENDING.value:
                exp_dt = model.expires_at
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if exp_dt <= now_utc:
                    model.status = ApprovalStatus.EXPIRED.value
                    await session.commit()
                    await session.refresh(model)

            return model

    async def get_decision(
        self,
        approval_id: str,
        tenant_id: str,
    ) -> Optional[ApprovalDecision]:
        if not approval_id or not tenant_id:
            return None

        factory = self._ensure_session_factory()
        async with factory() as session:
            stmt = select(ApprovalRequestModel).where(
                and_(
                    ApprovalRequestModel.approval_id == approval_id,
                    ApprovalRequestModel.tenant_id == tenant_id,
                )
            )
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()
            if model is None:
                return None
            return model.to_domain_decision()

    async def list_pending(
        self,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ApprovalRequest]:
        models = await self.list_pending_models(tenant_id, limit, offset)
        return [m.to_domain_request() for m in models]

    async def list_pending_models(
        self,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ApprovalRequestModel]:
        if not tenant_id:
            return []
        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        factory = self._ensure_session_factory()
        now_utc = datetime.now(timezone.utc)

        async with factory() as session:
            stmt = (
                select(ApprovalRequestModel)
                .where(
                    and_(
                        ApprovalRequestModel.tenant_id == tenant_id,
                        ApprovalRequestModel.status == ApprovalStatus.PENDING.value,
                        ApprovalRequestModel.expires_at > now_utc,
                    )
                )
                .order_by(ApprovalRequestModel.requested_at.asc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def count_pending(
        self,
        tenant_id: str,
    ) -> int:
        if not tenant_id:
            return 0
        factory = self._ensure_session_factory()
        now_utc = datetime.now(timezone.utc)

        async with factory() as session:
            from sqlalchemy import func
            stmt = (
                select(func.count())
                .select_from(ApprovalRequestModel)
                .where(
                    and_(
                        ApprovalRequestModel.tenant_id == tenant_id,
                        ApprovalRequestModel.status == ApprovalStatus.PENDING.value,
                        ApprovalRequestModel.expires_at > now_utc,
                    )
                )
            )
            result = await session.execute(stmt)
            return int(result.scalar() or 0)

    async def list_by_workflow(
        self,
        workflow_id: str,
        tenant_id: str,
    ) -> List[ApprovalRequest]:
        if not workflow_id or not tenant_id:
            return []

        factory = self._ensure_session_factory()
        async with factory() as session:
            stmt = (
                select(ApprovalRequestModel)
                .where(
                    and_(
                        ApprovalRequestModel.workflow_id == workflow_id,
                        ApprovalRequestModel.tenant_id == tenant_id,
                    )
                )
                .order_by(ApprovalRequestModel.requested_at.asc())
            )
            result = await session.execute(stmt)
            models = result.scalars().all()
            return [m.to_domain_request() for m in models]

    async def record_conditional_decision(
        self,
        approval_id: str,
        tenant_id: str,
        decision: ApprovalStatus,
        approver_id: str,
        reason: Optional[str] = None,
    ) -> Tuple[bool, Optional[ApprovalRequest], str]:
        if decision not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            raise ValueError(f"Decision must be APPROVED or REJECTED; got '{decision}'.")
        if not approver_id or not approver_id.strip():
            raise ValueError("approver_id must be a non-empty string.")

        factory = self._ensure_session_factory()
        now_utc = datetime.now(timezone.utc)

        async with factory() as session:
            # 1. Attempt Atomic Compare-And-Swap (CAS) Update
            stmt = (
                update(ApprovalRequestModel)
                .where(
                    and_(
                        ApprovalRequestModel.approval_id == approval_id,
                        ApprovalRequestModel.tenant_id == tenant_id,
                        ApprovalRequestModel.status == ApprovalStatus.PENDING.value,
                        ApprovalRequestModel.expires_at > now_utc,
                    )
                )
                .values(
                    status=decision.value,
                    decision=decision.value,
                    approver_id=approver_id.strip(),
                    decision_reason=reason.strip() if reason else None,
                    decided_at=now_utc,
                    resume_status="pending",
                )
                .returning(ApprovalRequestModel)
            )

            result = await session.execute(stmt)
            updated_model = result.scalar_one_or_none()

            if updated_model is not None:
                await session.commit()
                return True, updated_model.to_domain_request(), "success"

            # 2. CAS failed: Diagnose reason
            check_stmt = select(ApprovalRequestModel).where(
                and_(
                    ApprovalRequestModel.approval_id == approval_id,
                    ApprovalRequestModel.tenant_id == tenant_id,
                )
            )
            check_res = await session.execute(check_stmt)
            existing = check_res.scalar_one_or_none()

            if existing is None:
                return False, None, "not_found"

            if existing.status != ApprovalStatus.PENDING.value:
                return False, existing.to_domain_request(), "already_decided"

            exp_dt = existing.expires_at
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if exp_dt <= now_utc:
                # Mark record EXPIRED in database
                existing.status = ApprovalStatus.EXPIRED.value
                await session.commit()
                return False, existing.to_domain_request(), "expired"

            return False, existing.to_domain_request(), "conflict"

    async def update_resume_status(
        self,
        approval_id: str,
        tenant_id: str,
        resume_status: str,
        resume_error: Optional[str] = None,
    ) -> bool:
        if resume_status not in VALID_RESUME_STATUSES:
            raise ValueError(f"Invalid resume_status '{resume_status}'. Allowed: {VALID_RESUME_STATUSES}")

        safe_error = redact_sensitive_data(resume_error) if resume_error else None

        factory = self._ensure_session_factory()
        async with factory() as session:
            stmt = (
                update(ApprovalRequestModel)
                .where(
                    and_(
                        ApprovalRequestModel.approval_id == approval_id,
                        ApprovalRequestModel.tenant_id == tenant_id,
                    )
                )
                .values(
                    resume_status=resume_status,
                    resume_error=safe_error,
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0


class InMemoryApprovalRepository(IApprovalRepository):
    """
    In-memory implementation of IApprovalRepository for hermetic unit testing.
    Thread-safe and async-safe via asyncio.Lock.
    """

    def __init__(self):
        self._records: Dict[str, ApprovalRequestModel] = {}
        self._lock = asyncio.Lock()

    async def create_request(
        self,
        request: ApprovalRequest,
        tenant_id: str,
        expires_at: datetime,
    ) -> ApprovalRequest:
        if not request.approval_id or not request.approval_id.strip():
            raise ValueError("approval_id must be a non-empty string.")
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string.")

        async with self._lock:
            if request.approval_id in self._records:
                raise ValueError(f"ApprovalRequest with ID '{request.approval_id}' already exists.")

            model = ApprovalRequestModel.from_domain_request(
                request=request,
                tenant_id=tenant_id.strip(),
                expires_at=expires_at,
            )
            self._records[request.approval_id] = model
            return model.to_domain_request()

    async def get_by_id(
        self,
        approval_id: str,
        tenant_id: str,
    ) -> Optional[ApprovalRequest]:
        model = await self.get_model_by_id(approval_id, tenant_id)
        return model.to_domain_request() if model else None

    async def get_model_by_id(
        self,
        approval_id: str,
        tenant_id: str,
    ) -> Optional[ApprovalRequestModel]:
        async with self._lock:
            model = self._records.get(approval_id)
            if model is None or model.tenant_id != tenant_id:
                return None

            now_utc = datetime.now(timezone.utc)
            exp_dt = model.expires_at
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if model.status == ApprovalStatus.PENDING.value and exp_dt <= now_utc:
                model.status = ApprovalStatus.EXPIRED.value

            return model

    async def get_decision(
        self,
        approval_id: str,
        tenant_id: str,
    ) -> Optional[ApprovalDecision]:
        async with self._lock:
            model = self._records.get(approval_id)
            if model is None or model.tenant_id != tenant_id:
                return None
            return model.to_domain_decision()

    async def list_pending(
        self,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ApprovalRequest]:
        models = await self.list_pending_models(tenant_id, limit, offset)
        return [m.to_domain_request() for m in models]

    async def list_pending_models(
        self,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ApprovalRequestModel]:
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        now_utc = datetime.now(timezone.utc)

        async with self._lock:
            matching: List[ApprovalRequestModel] = []
            for m in self._records.values():
                if m.tenant_id != tenant_id:
                    continue
                exp_dt = m.expires_at
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if m.status == ApprovalStatus.PENDING.value and exp_dt > now_utc:
                    matching.append(m)

            matching.sort(key=lambda x: x.requested_at)
            return matching[offset : offset + limit]

    async def count_pending(
        self,
        tenant_id: str,
    ) -> int:
        now_utc = datetime.now(timezone.utc)
        async with self._lock:
            count = 0
            for m in self._records.values():
                if m.tenant_id != tenant_id:
                    continue
                exp_dt = m.expires_at
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if m.status == ApprovalStatus.PENDING.value and exp_dt > now_utc:
                    count += 1
            return count

    async def list_by_workflow(
        self,
        workflow_id: str,
        tenant_id: str,
    ) -> List[ApprovalRequest]:
        async with self._lock:
            matching = [
                m
                for m in self._records.values()
                if m.workflow_id == workflow_id and m.tenant_id == tenant_id
            ]
            matching.sort(key=lambda x: x.requested_at)
            return [m.to_domain_request() for m in matching]

    async def record_conditional_decision(
        self,
        approval_id: str,
        tenant_id: str,
        decision: ApprovalStatus,
        approver_id: str,
        reason: Optional[str] = None,
    ) -> Tuple[bool, Optional[ApprovalRequest], str]:
        if decision not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            raise ValueError(f"Decision must be APPROVED or REJECTED; got '{decision}'.")
        if not approver_id or not approver_id.strip():
            raise ValueError("approver_id must be a non-empty string.")

        now_utc = datetime.now(timezone.utc)

        async with self._lock:
            model = self._records.get(approval_id)
            if model is None or model.tenant_id != tenant_id:
                return False, None, "not_found"

            exp_dt = model.expires_at
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)

            if model.status != ApprovalStatus.PENDING.value:
                return False, model.to_domain_request(), "already_decided"

            if exp_dt <= now_utc:
                model.status = ApprovalStatus.EXPIRED.value
                return False, model.to_domain_request(), "expired"

            # Apply decision
            model.status = decision.value
            model.decision = decision.value
            model.approver_id = approver_id.strip()
            model.decision_reason = reason.strip() if reason else None
            model.decided_at = now_utc
            model.resume_status = "pending"
            return True, model.to_domain_request(), "success"

    async def update_resume_status(
        self,
        approval_id: str,
        tenant_id: str,
        resume_status: str,
        resume_error: Optional[str] = None,
    ) -> bool:
        if resume_status not in VALID_RESUME_STATUSES:
            raise ValueError(f"Invalid resume_status '{resume_status}'. Allowed: {VALID_RESUME_STATUSES}")

        safe_error = redact_sensitive_data(resume_error) if resume_error else None

        async with self._lock:
            model = self._records.get(approval_id)
            if model is None or model.tenant_id != tenant_id:
                return False

            model.resume_status = resume_status
            model.resume_error = safe_error
            return True
