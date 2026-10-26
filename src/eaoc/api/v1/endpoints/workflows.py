"""
EAOC API v1 Workflow Intake & Request Lifecycle Endpoints
Exposes REST endpoints for submitting workflow requests, retrieving workflow status,
and safely resuming HITL-paused workflows via atomic execution lease acquisition.
Binds observability trace context, enforces PENDING initial state, and persists to
Supabase PostgreSQL recovery repository and ephemeral state store.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from eaoc.api.dependencies.auth import get_trusted_identity
from eaoc.config import get_settings
from eaoc.observability import redact_sensitive_data
from eaoc.observability.context import generate_id, get_correlation_id, get_request_id
from eaoc.schemas.auth import AuthenticatedUser
from eaoc.schemas.events import (
    StreamEventType,
    WorkflowStreamEvent,
    format_event_id,
    is_artificial_error_cursor,
    parse_event_id,
)
from eaoc.services.event_bus import IWorkflowEventBus, RedisWorkflowEventBus
from eaoc.schemas.domain import (
    ApprovalDecision,
    ApprovalStatus,
    ExecutionMetadata,
    RequestContext,
    WorkflowState,
    WorkflowStatus,
)
from eaoc.state.interfaces import IWorkflowStateStore
from eaoc.state.workflow_repository import (
    IWorkflowRecoveryRepository,
    PostgresWorkflowRecoveryRepository,
)
from eaoc.agents.orchestrator.engine import OrchestratorEngine
from eaoc.agents.orchestrator.schemas import OrchestratorInput
from eaoc.agents.registry import AgentRegistry
from eaoc.agents.planner.validator import PlanValidator

logger = logging.getLogger("eaoc.api.workflows")

router = APIRouter(prefix="/workflows", tags=["Workflows"])


class WorkflowCreateRequest(BaseModel):
    """
    Intake API request payload schema for workflow submission.
    """
    input_payload: Dict[str, Any] = Field(
        ...,
        description="Required user prompt or structured parameter payload"
    )
    task_type: Optional[str] = Field(
        default=None,
        description="Optional high-level workflow classification (e.g. research, sql_query, document_summary)"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional caller-supplied request metadata"
    )


class WorkflowResumeRequest(BaseModel):
    """
    Resume request schema to restart an approved workflow execution.
    """
    approval_id: str = Field(
        ...,
        description="Unique approval request identifier corresponding to the paused workflow step."
    )


def compute_idempotency_fingerprint(payload: Dict[str, Any]) -> str:
    """Computes deterministic SHA-256 digest of intake payload for replay verification."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_state_store() -> IWorkflowStateStore:
    """
    Dependency provider returning the active workflow state store instance.
    Defaults to RedisWorkflowStateStore. Can be overridden in test suites.
    """
    from eaoc.state.redis_store import RedisWorkflowStateStore
    return RedisWorkflowStateStore()


async def get_trusted_identity_optional(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    settings: Any = Depends(get_settings),
) -> Optional[AuthenticatedUser]:
    """
    Extracts and validates trusted identity if Authorization header is present.
    If absent, returns None (allowing unauthenticated access if permitted).
    If present but invalid/expired, fails closed with HTTP 401.
    """
    if not authorization:
        return None
    from eaoc.api.dependencies.auth import get_trusted_identity
    return await get_trusted_identity(authorization=authorization, settings=settings)


def get_workflow_repository() -> IWorkflowRecoveryRepository:
    """
    Dependency provider returning the active durable workflow recovery repository.
    Defaults to PostgresWorkflowRecoveryRepository. Can be overridden in test suites.
    """
    return PostgresWorkflowRecoveryRepository()


def get_event_bus() -> IWorkflowEventBus:
    """
    Dependency provider returning the active workflow event bus instance.
    Defaults to RedisWorkflowEventBus. Can be overridden in test suites.
    """
    return RedisWorkflowEventBus()


def get_orchestrator_engine(
    repo: IWorkflowRecoveryRepository = Depends(get_workflow_repository),
    state_store: IWorkflowStateStore = Depends(get_state_store),
    event_bus: IWorkflowEventBus = Depends(get_event_bus),
) -> OrchestratorEngine:
    """
    Dependency provider returning an OrchestratorEngine bound to recovery, state stores, and event bus.
    """
    return OrchestratorEngine(
        registry=AgentRegistry(),
        state_store=state_store,
        workflow_repo=repo,
        event_bus=event_bus,
    )


@router.post(
    "",
    response_model=WorkflowState,
    status_code=status.HTTP_201_CREATED,
    summary="Submit Workflow Request",
    description="Validates intake payload, binds observability trace context, initializes PENDING WorkflowState, and persists to recovery store.",
)
async def create_workflow(
    request: Request,
    payload: WorkflowCreateRequest,
    response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    state_store: IWorkflowStateStore = Depends(get_state_store),
    repo: IWorkflowRecoveryRepository = Depends(get_workflow_repository),
) -> WorkflowState:
    """
    HTTP POST /api/v1/workflows handler.
    Creates a new workflow request and persists initial WorkflowState.
    """
    # 1. Bind Observability Trace Identifiers from Context
    request_id = get_request_id() or request.headers.get("X-Request-ID") or getattr(request.state, "request_id", None) or generate_id("req")
    correlation_id = get_correlation_id() or request.headers.get("X-Correlation-ID") or getattr(request.state, "correlation_id", None) or generate_id("corr")

    # Resolve identity if Authorization header present, else preserve unauthenticated boundary
    user_id = None
    tenant_id = "default"
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        try:
            settings = get_settings()
            if settings.JWT_SECRET_KEY and settings.JWT_SECRET_KEY.strip():
                from eaoc.api.dependencies.auth import validate_access_token
                token = auth_header.split(" ", 1)[1].strip()
                auth_user = validate_access_token(
                    token=token,
                    secret_key=settings.JWT_SECRET_KEY,
                    algorithm=settings.JWT_ALGORITHM,
                    expected_issuer=settings.JWT_ISSUER,
                    expected_audience=settings.JWT_AUDIENCE,
                )
                user_id = auth_user.user_id
                tenant_id = auth_user.tenant_id
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication token.",
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )

    # 2. Generate Random, Collision-Resistant Workflow ID
    workflow_id = f"wf-{uuid.uuid4().hex[:12]}"
    now_utc = datetime.now(timezone.utc).isoformat()

    # 3. Handle Idempotency Check in Durable Repository
    if idempotency_key:
        fingerprint = compute_idempotency_fingerprint(payload.model_dump())
        try:
            wf_model, is_new = await repo.create_workflow_intake(
                workflow_id=workflow_id,
                tenant_id=tenant_id,
                user_id=user_id or "anonymous",
                task_id=f"task-{workflow_id}",
                task_description=payload.input_payload.get("prompt") or payload.input_payload.get("task_description") or "workflow_execution",
                execution_plan=payload.metadata.get("execution_plan"),
                plan_hash=payload.metadata.get("plan_hash"),
                idempotency_key=idempotency_key,
                idempotency_fingerprint=fingerprint,
                metadata=payload.metadata,
            )
            if not is_new:
                # Replay existing workflow
                domain_state = wf_model.to_domain_state()
                response.headers["Location"] = f"/api/v1/workflows/{domain_state.workflow_id}"
                return domain_state
            workflow_id = wf_model.workflow_id
        except ValueError as val_err:
            if "Idempotency-Key reuse" in str(val_err):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Idempotency-Key reuse with altered request payload is prohibited.",
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(val_err),
            )
        except HTTPException:
            raise
        except Exception as exc:
            safe_err = redact_sensitive_data(exc)
            logger.error(f"Durable intake write failed: {safe_err}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Durable workflow intake repository is unavailable: {safe_err[:120]}",
            )

    # Unauthenticated boundary preservation for Part 1.11 compatibility
    request_ctx = RequestContext(
        request_id=request_id,
        correlation_id=correlation_id,
        user_id=user_id,
        tenant_id=tenant_id if user_id else None,
        metadata=payload.metadata,
    )

    # 4. Construct Initial Canonical WorkflowState
    wf_state = WorkflowState(
        workflow_id=workflow_id,
        request_context=request_ctx,
        status=WorkflowStatus.PENDING,
        current_task_id=None,
        tasks={},
        execution_metadata=ExecutionMetadata(),
        created_at=now_utc,
        updated_at=now_utc,
    )

    # 5. Persist to State Store & Return Response
    try:
        created_state = await state_store.create_state(wf_state)
        response.headers["Location"] = f"/api/v1/workflows/{workflow_id}"
        logger.info(
            f"Initialized workflow request '{workflow_id}' with status PENDING.",
            extra={"workflow_id": workflow_id, "request_id": request_id}
        )
        return created_state
    except ValueError as val_err:
        logger.warning(f"Workflow ID collision or conflict for '{workflow_id}': {val_err}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Workflow request conflict: {redact_sensitive_data(val_err)}"
        )
    except Exception as exc:
        safe_err = redact_sensitive_data(exc)
        logger.error(f"Failed to persist workflow state for '{workflow_id}': {safe_err}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Workflow state persistence store is unavailable: {safe_err[:120]}"
        )


@router.get(
    "/{workflow_id}",
    response_model=WorkflowState,
    status_code=status.HTTP_200_OK,
    summary="Get Workflow Status & State",
    description="Retrieves current WorkflowState by workflow_id.",
)
async def get_workflow_state(
    workflow_id: str,
    user: Optional[AuthenticatedUser] = Depends(get_trusted_identity_optional),
    state_store: IWorkflowStateStore = Depends(get_state_store),
    repo: IWorkflowRecoveryRepository = Depends(get_workflow_repository),
) -> WorkflowState:
    """
    HTTP GET /api/v1/workflows/{workflow_id} handler.
    Retrieves workflow state by workflow_id, enforcing tenant isolation.
    """
    tenant_id = user.tenant_id if user else None

    # Check durable repository first
    try:
        wf = await repo.get_workflow(workflow_id=workflow_id, tenant_id=tenant_id or "default")
        if wf is not None:
            tasks = await repo.get_tasks_for_workflow(workflow_id=workflow_id, tenant_id=wf.tenant_id)
            domain_tasks = {str(t.step_index): t.to_domain_task() for t in tasks}
            return wf.to_domain_state(domain_tasks=domain_tasks)
    except Exception:
        pass

    try:
        wf_state = await state_store.get_state(workflow_id)
        if wf_state is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow with ID '{workflow_id}' was not found."
            )

        # Enforce tenant isolation on cached state
        cached_tenant = wf_state.request_context.tenant_id if wf_state.request_context else None
        if tenant_id is not None:
            if cached_tenant and cached_tenant != tenant_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Workflow with ID '{workflow_id}' was not found."
                )
        else:
            if cached_tenant and cached_tenant != "default":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Workflow with ID '{workflow_id}' was not found."
                )

        return wf_state
    except HTTPException:
        raise
    except Exception as exc:
        safe_err = redact_sensitive_data(exc)
        logger.error(f"Failed to retrieve workflow state for '{workflow_id}': {safe_err}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Workflow state store error: {safe_err[:120]}"
        )


@router.post(
    "/{workflow_id}/resume",
    response_model=WorkflowState,
    status_code=status.HTTP_200_OK,
    summary="Resume Paused Workflow",
    description="Atomically claims the execution lease for an approved workflow and resumes execution from the pending step.",
)
async def resume_workflow(
    workflow_id: str,
    payload: WorkflowResumeRequest,
    response: Response,
    user: AuthenticatedUser = Depends(get_trusted_identity),
    repo: IWorkflowRecoveryRepository = Depends(get_workflow_repository),
    orchestrator: OrchestratorEngine = Depends(get_orchestrator_engine),
    state_store: IWorkflowStateStore = Depends(get_state_store),
) -> WorkflowState:
    """
    HTTP POST /api/v1/workflows/{workflow_id}/resume
    Claims execution lease on an approved workflow and dispatches Orchestrator execution.
    """
    # Enforce RBAC roles: approver, admin, or operator
    allowed_roles = {"approver", "admin", "operator"}
    if not any(r in allowed_roles for r in user.roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User roles {user.roles} do not have permission to resume workflows.",
        )

    # 1. Atomic Approval Binding & Execution Lease Claim
    claimed, epoch, rev, token, wf_model, reason = await repo.claim_approval_resume(
        approval_id=payload.approval_id,
        workflow_id=workflow_id,
        tenant_id=user.tenant_id,
    )

    if not claimed or wf_model is None:
        if reason in ("approval_ineligible", "approval_expired", "approval_ineligible_or_expired", "approval_not_found"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Approval request is not eligible for resume or has expired.",
            )
        elif reason == "workflow_lease_active_or_ineligible":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Workflow already has an active execution lease held by another executor or is in terminal status.",
            )
        elif reason == "task_already_completed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Step has already been completed and cannot be resumed.",
            )
        elif reason in (
            "step_index_mismatch",
            "plan_hash_mismatch",
            "plan_id_mismatch",
            "task_id_mismatch",
            "agent_id_mismatch",
            "action_mismatch",
            "risk_level_mismatch",
            "invalid_resume_state",
            "task_tenant_mismatch",
            "task_plan_hash_mismatch",
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Resume validation conflict: {reason}",
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot resume workflow: {reason}",
            )

    # 2. Dispatch Orchestrator Execution with Lease Credentials
    input_data = OrchestratorInput(
        task_id=wf_model.task_id,
        task_description=wf_model.task_description,
        workflow_id=workflow_id,
        approval_decision=ApprovalDecision(
            approval_id=payload.approval_id,
            workflow_id=workflow_id,
            decision=ApprovalStatus.APPROVED,
            approver_id=user.user_id,
            decided_at=datetime.now(timezone.utc).isoformat(),
        ),
        user_context={
            "user_id": user.user_id,
            "tenant_id": user.tenant_id,
            "roles": user.roles,
        },
        metadata={
            "fencing_epoch": epoch,
            "lock_token": token,
            "expected_revision": rev,
            "tenant_id": user.tenant_id,
            "is_first_resume_claim": (wf_model.execution_metadata or {}).get("is_first_resume_claim", True),
            "approval_claim_count": (wf_model.execution_metadata or {}).get("approval_claim_count", 1),
        },
    )

    output = await orchestrator.execute_workflow(input_data)

    # 3. Reload Updated Durable State
    wf_after = await repo.get_workflow(workflow_id, user.tenant_id)
    tasks_after = await repo.get_tasks_for_workflow(workflow_id, user.tenant_id)
    domain_tasks = {str(t.step_index): t.to_domain_task() for t in tasks_after}
    final_state = wf_after.to_domain_state(domain_tasks=domain_tasks) if wf_after else None

    if final_state is None:
        final_state = await state_store.get_state(workflow_id)
        if final_state is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Workflow state could not be reloaded after execution.",
            )

    if output.status == WorkflowStatus.WAITING_APPROVAL:
        response.status_code = status.HTTP_202_ACCEPTED

    return final_state


@router.get(
    "/{workflow_id}/stream",
    summary="Real-Time Workflow Execution Stream (SSE)",
    description="Streams real-time execution events for a workflow using Server-Sent Events (SSE). Requires valid Authorization header.",
)
async def stream_workflow(
    workflow_id: str,
    request: Request,
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
    user: AuthenticatedUser = Depends(get_trusted_identity),
    repo: IWorkflowRecoveryRepository = Depends(get_workflow_repository),
    event_bus: IWorkflowEventBus = Depends(get_event_bus),
) -> StreamingResponse:
    """
    HTTP GET /api/v1/workflows/{workflow_id}/stream
    Delivers a real-time Server-Sent Events stream for execution monitoring.
    Enforces header-only Bearer JWT authentication and strict tenant isolation.
    """
    # 1. Enforce tenant isolation & verify workflow existence
    wf = await repo.get_workflow(workflow_id=workflow_id, tenant_id=user.tenant_id)
    if wf is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow with ID '{workflow_id}' was not found.",
        )

    # Parse Last-Event-ID if provided
    last_cursor = None
    raw_cursor = last_event_id if isinstance(last_event_id, str) else None
    if raw_cursor and not is_artificial_error_cursor(raw_cursor):
        parsed = parse_event_id(raw_cursor)
        if parsed and parsed.get("workflow_id") == workflow_id:
            last_cursor = parsed

    # 2. Subscribe-First: open subscription queue before reading tasks snapshot
    subscriber_queue: asyncio.Queue[WorkflowStreamEvent] = asyncio.Queue(maxsize=256)
    stop_event = asyncio.Event()

    async def _live_listener():
        try:
            async for event in event_bus.subscribe(user.tenant_id, workflow_id):
                if stop_event.is_set():
                    break
                try:
                    subscriber_queue.put_nowait(event)
                except asyncio.QueueFull:
                    # Queue overflow: slow consumer detected
                    overflow_event = WorkflowStreamEvent(
                        event_id=f"err:{workflow_id}:{int(time.time())}:overflow",
                        event_type=StreamEventType.ERROR,
                        workflow_id=workflow_id,
                        tenant_id=user.tenant_id,
                        payload={
                            "code": "BUFFER_OVERFLOW",
                            "message": "Subscriber consumption queue exceeded capacity. Reconnect to resume.",
                            "retryable": True,
                        },
                    )
                    try:
                        subscriber_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    subscriber_queue.put_nowait(overflow_event)
                    stop_event.set()
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"Live listener exception for '{workflow_id}': {exc}")

    listener_task = asyncio.create_task(_live_listener())

    # 3. Snapshot Retrieval & Durable Milestone Synthesis
    tasks = await repo.get_tasks_for_workflow(workflow_id=workflow_id, tenant_id=user.tenant_id)
    sorted_tasks = sorted(tasks, key=lambda t: t.step_index)

    synthesized_events: list[WorkflowStreamEvent] = []
    snapshot_epoch = wf.fencing_epoch
    synthesized_milestone_keys: set[str] = set()

    # Workflow-level start milestone
    if wf.status != WorkflowStatus.PENDING.value:
        start_id = format_event_id(workflow_id, snapshot_epoch, -1, StreamEventType.WORKFLOW_STARTED, None, "start")
        synthesized_events.append(WorkflowStreamEvent(
            event_id=start_id,
            event_type=StreamEventType.WORKFLOW_STARTED,
            workflow_id=workflow_id,
            tenant_id=user.tenant_id,
            epoch=snapshot_epoch,
            step_index=-1,
            payload={"status": wf.status, "task_description": wf.task_description},
        ))
        synthesized_milestone_keys.add(f"{snapshot_epoch}:-1:{StreamEventType.WORKFLOW_STARTED.value}")

    # Task-level milestones
    for task in sorted_tasks:
        s_idx = task.step_index
        # Started milestone
        if task.status in ("RUNNING", "COMPLETED", "WAITING_APPROVAL", "FAILED", "CANCELLED"):
            step_start_id = format_event_id(workflow_id, snapshot_epoch, s_idx, StreamEventType.STEP_STARTED, None, "start")
            agent_str = getattr(task, "agent_id", getattr(task, "assigned_agent_id", "unknown"))
            synthesized_events.append(WorkflowStreamEvent(
                event_id=step_start_id,
                event_type=StreamEventType.STEP_STARTED,
                workflow_id=workflow_id,
                tenant_id=user.tenant_id,
                epoch=snapshot_epoch,
                step_index=s_idx,
                payload={"description": task.name, "assigned_agent_id": str(agent_str), "status": task.status},
            ))
            synthesized_milestone_keys.add(f"{snapshot_epoch}:{s_idx}:{StreamEventType.STEP_STARTED.value}")

        # Completed milestone
        if task.status == "COMPLETED":
            step_done_id = format_event_id(workflow_id, snapshot_epoch, s_idx, StreamEventType.STEP_COMPLETED, None, "done")
            synthesized_events.append(WorkflowStreamEvent(
                event_id=step_done_id,
                event_type=StreamEventType.STEP_COMPLETED,
                workflow_id=workflow_id,
                tenant_id=user.tenant_id,
                epoch=snapshot_epoch,
                step_index=s_idx,
                payload={"status": "COMPLETED", "summary": task.name},
            ))
            synthesized_milestone_keys.add(f"{snapshot_epoch}:{s_idx}:{StreamEventType.STEP_COMPLETED.value}")

        # Waiting approval milestone
        elif task.status == "WAITING_APPROVAL":
            step_pause_id = format_event_id(workflow_id, snapshot_epoch, s_idx, StreamEventType.WAITING_APPROVAL, None, "pause")
            synthesized_events.append(WorkflowStreamEvent(
                event_id=step_pause_id,
                event_type=StreamEventType.WAITING_APPROVAL,
                workflow_id=workflow_id,
                tenant_id=user.tenant_id,
                epoch=snapshot_epoch,
                step_index=s_idx,
                payload={"status": "WAITING_APPROVAL", "summary": task.name},
            ))
            synthesized_milestone_keys.add(f"{snapshot_epoch}:{s_idx}:{StreamEventType.WAITING_APPROVAL.value}")

        # Failed milestone
        elif task.status in ("FAILED", "CANCELLED"):
            step_fail_id = format_event_id(workflow_id, snapshot_epoch, s_idx, StreamEventType.STEP_FAILED, None, "fail")
            synthesized_events.append(WorkflowStreamEvent(
                event_id=step_fail_id,
                event_type=StreamEventType.STEP_FAILED,
                workflow_id=workflow_id,
                tenant_id=user.tenant_id,
                epoch=snapshot_epoch,
                step_index=s_idx,
                payload={"status": task.status, "summary": task.name},
            ))
            synthesized_milestone_keys.add(f"{snapshot_epoch}:{s_idx}:{StreamEventType.STEP_FAILED.value}")

    # Terminal workflow milestone if completed/failed in DB
    if wf.status == WorkflowStatus.COMPLETED.value:
        term_id = format_event_id(workflow_id, snapshot_epoch, -1, StreamEventType.WORKFLOW_COMPLETED, None, "complete")
        synthesized_events.append(WorkflowStreamEvent(
            event_id=term_id,
            event_type=StreamEventType.WORKFLOW_COMPLETED,
            workflow_id=workflow_id,
            tenant_id=user.tenant_id,
            epoch=snapshot_epoch,
            step_index=-1,
            payload={"status": "COMPLETED"},
        ))
        synthesized_milestone_keys.add(f"{snapshot_epoch}:-1:{StreamEventType.WORKFLOW_COMPLETED.value}")
    elif wf.status == WorkflowStatus.FAILED.value:
        term_id = format_event_id(workflow_id, snapshot_epoch, -1, StreamEventType.WORKFLOW_FAILED, None, "fail")
        synthesized_events.append(WorkflowStreamEvent(
            event_id=term_id,
            event_type=StreamEventType.WORKFLOW_FAILED,
            workflow_id=workflow_id,
            tenant_id=user.tenant_id,
            epoch=snapshot_epoch,
            step_index=-1,
            payload={"status": "FAILED", "error": wf.error},
        ))
        synthesized_milestone_keys.add(f"{snapshot_epoch}:-1:{StreamEventType.WORKFLOW_FAILED.value}")

    # 4. Filter against Last-Event-ID
    events_to_emit: list[WorkflowStreamEvent] = []
    for ev in synthesized_events:
        if last_cursor and last_cursor["epoch"] == ev.epoch and last_cursor["step_index"] == ev.step_index:
            # If client cursor acknowledged this step and stage, skip this specific event
            if last_cursor["event_type"] == ev.event_type.value:
                continue
            # If client already acknowledged step_completed, skip step_started
            if last_cursor["event_type"] in ("step_completed", "step_failed") and ev.event_type.value == "step_started":
                continue
        elif last_cursor and last_cursor["epoch"] == ev.epoch and ev.step_index >= 0 and last_cursor["step_index"] > ev.step_index:
            # Client cursor is strictly past this step index in same epoch
            continue
        events_to_emit.append(ev)

    # 5. Generator yielding SSE wire frames
    async def sse_event_generator():
        seen_event_ids: set[str] = set()
        start_time = time.time()
        max_duration_seconds = 1800.0  # 30 minutes

        try:
            # A. Emit synthesized catch-up events
            for ev in events_to_emit:
                seen_event_ids.add(ev.event_id)
                yield ev.to_sse_frame()

            # If workflow is already terminal in snapshot, close cleanly
            if wf.status in (WorkflowStatus.COMPLETED.value, WorkflowStatus.FAILED.value):
                return

            # B. Stream reconciled buffered & live events
            while not stop_event.is_set():
                if await request.is_disconnected():
                    break

                if (time.time() - start_time) > max_duration_seconds:
                    yield f"event: error\nid: err:{workflow_id}:{int(time.time())}:max_duration\nretry: 3000\ndata: {{\"code\":\"MAX_DURATION_REACHED\",\"message\":\"Max stream duration reached. Reconnect to resume.\",\"retryable\":true}}\n\n"
                    break

                try:
                    event = await asyncio.wait_for(subscriber_queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # 15s keepalive ping
                    now_str = datetime.now(timezone.utc).isoformat()
                    yield f"event: keepalive\nid: evt:v1:{workflow_id}:e{snapshot_epoch}:s-1:keepalive:sqsyn:ping\ndata: {{\"server_time\":\"{now_str}\"}}\n\n"
                    continue

                if event.event_type == StreamEventType.ERROR:
                    yield event.to_sse_frame()
                    break

                # Snapshot Reconciliation check:
                # 1. Stale lease check
                if event.epoch < snapshot_epoch:
                    continue

                # 2. Duplicate milestone check in same epoch
                if event.epoch == snapshot_epoch and event.event_type != StreamEventType.TOOL_INVOKED:
                    m_key = f"{event.epoch}:{event.step_index}:{event.event_type.value}"
                    if m_key in synthesized_milestone_keys:
                        continue

                # 3. Exact event_id deduplication
                if event.event_id in seen_event_ids:
                    continue

                seen_event_ids.add(event.event_id)
                if len(seen_event_ids) > 500:
                    seen_event_ids.pop()

                yield event.to_sse_frame()

                # If event is terminal, end stream cleanly
                if event.event_type in (StreamEventType.WORKFLOW_COMPLETED, StreamEventType.WORKFLOW_FAILED):
                    break

        finally:
            stop_event.set()
            listener_task.cancel()
            try:
                await listener_task
            except (asyncio.CancelledError, Exception):
                pass
            while not subscriber_queue.empty():
                try:
                    subscriber_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    return StreamingResponse(
        sse_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

