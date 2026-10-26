"""
EAOC Orchestrator Engine & Workflow Coordinator
Deterministic in-process workflow execution coordinator per Phase 0 specifications:
- AgentSpec Part A: Agent 1 (Orchestrator)
- FRS Part A: FR-ORCH-001, FR-ORCH-002, FR-ORCH-003, FR-ADAP-002, FR-AGT-ORCH-001
- Arch Part A: Sections 2.1, 2.2, 3.4
- Arch Part B: Sections 9.2, 10.1, 10.2
- Arch Part C: Sections 11, 12

Coordinates execution strictly via AgentRegistry and PlanValidator, enforces RBAC
via ROLE_PERMITTED_AGENTS, applies deterministic RiskGate, coordinates retries and
contextual re-planning, and applies ReviewerValidationAgent quality gate.
"""

import asyncio
from datetime import datetime, timezone, timedelta
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from eaoc.agents.orchestrator.risk_gate import RiskGate, RiskGateDecision
from eaoc.agents.orchestrator.schemas import (
    FinalResponse,
    OrchestratorFailureClass,
    OrchestratorInput,
    OrchestratorOutput,
    ProvenanceEntry,
    StepExecutionRecord,
)
from eaoc.agents.planner.schemas import (
    ExecutionPlan,
    PlanStep,
    PlanType,
    PlanValidationError,
    PlannerInput,
    PlannerOutput,
)
from eaoc.agents.planner.validator import PlanValidator, ROLE_PERMITTED_AGENTS
from eaoc.agents.registry import AgentRegistry
from eaoc.agents.reviewer_validation.schemas import (
    ReviewRecommendation,
    ReviewerInput,
    ReviewerOutput,
)
from eaoc.observability import Timer, get_logger, redact_sensitive_data
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ErrorInfo,
    RequestContext,
    RiskLevel,
    Task,
    TaskStatus,
    WorkflowState,
    WorkflowStatus,
)
from eaoc.state.interfaces import IWorkflowStateStore
from eaoc.state.workflow_repository import (
    ConcurrencyConflictError,
    FencingLeaseLostError,
    IWorkflowRecoveryRepository,
    PlanIntegrityError,
)
from eaoc.schemas.events import StreamEventType, WorkflowStreamEvent, format_event_id
from eaoc.services.event_bus import IWorkflowEventBus

from eaoc.orchestration.graph import compile_workflow_graph
from eaoc.orchestration.nodes import OrchestrationNodes
from eaoc.orchestration.state import GraphWorkflowState

logger = get_logger("eaoc.agents.orchestrator.engine")


def compute_plan_hash(plan: ExecutionPlan) -> str:
    """Computes deterministic SHA-256 hash of canonical plan representation."""
    import hashlib
    import json

    canonical_steps = []
    for s in plan.steps:
        canonical_steps.append({
            "step_index": s.step_index,
            "agent_id": s.agent_id.value,
            "description": s.description,
            "risk_level": s.risk_level.value,
            "operation_type": s.operation_type,
            "data_class": s.data_class,
            "operation": s.operation,
            "dependencies": sorted(s.dependencies),
            "inputs": sorted(s.inputs.items()),
            "optional": s.optional,
        })
    canonical_dict = {
        "plan_id": plan.plan_id,
        "task_id": plan.task_id,
        "plan_type": plan.plan_type.value,
        "steps": canonical_steps,
    }
    raw = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_step_hash(step: PlanStep) -> str:
    """Computes deterministic SHA-256 hash of canonical step definition."""
    import hashlib
    import json

    canonical_step = {
        "step_index": step.step_index,
        "agent_id": step.agent_id.value,
        "description": step.description,
        "risk_level": step.risk_level.value,
        "operation_type": step.operation_type,
        "data_class": step.data_class,
        "operation": step.operation,
        "dependencies": sorted(step.dependencies),
        "inputs": sorted(step.inputs.items()),
        "optional": step.optional,
    }
    raw = json.dumps(canonical_step, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class OrchestratorEngine:
    """
    Deterministic in-process workflow execution coordinator.
    Strictly coordinates agents through AgentRegistry without performing direct domain logic.
    Supports execution delegation to compiled LangGraph StateGraph (Part 2.18.5).
    """

    def __init__(
        self,
        registry: AgentRegistry,
        state_store: Optional[IWorkflowStateStore] = None,
        workflow_repo: Optional[IWorkflowRecoveryRepository] = None,
        max_retries: int = 2,
        initial_backoff_seconds: float = 2.0,
        backoff_multiplier: float = 2.0,
        max_replan_attempts: int = 2,
        event_bus: Optional[IWorkflowEventBus] = None,
        use_langgraph: bool = False,
    ):
        self.registry = registry
        self.state_store = state_store
        self.workflow_repo = workflow_repo
        self.max_retries = max_retries
        self.initial_backoff_seconds = initial_backoff_seconds
        self.backoff_multiplier = backoff_multiplier
        self.max_replan_attempts = max_replan_attempts
        self.event_bus = event_bus
        self.use_langgraph = use_langgraph
        self._seq: int = 0
        self._seq_lock = asyncio.Lock()
        self._tracked_tasks: Set[asyncio.Task] = set()
        self.plan_validator = PlanValidator(agent_registry=registry)

        self._graph_nodes = OrchestrationNodes(
            registry=registry,
            workflow_repo=workflow_repo,
            state_store=state_store,
            event_bus=event_bus,
            max_retries=max_retries,
            initial_backoff_seconds=initial_backoff_seconds,
            backoff_multiplier=backoff_multiplier,
            max_replan_attempts=max_replan_attempts,
        )
        self._compiled_graph = compile_workflow_graph(self._graph_nodes)

    async def _emit_stream_event(
        self,
        workflow_id: str,
        tenant_id: str,
        epoch: int,
        step_index: int,
        event_type: StreamEventType,
        payload: Dict[str, Any],
        discriminator: str = "main",
    ) -> None:
        """
        Emits a stream event to the injected event bus in a non-blocking background task.
        Safe against slow subscribers and event bus failures.
        """
        if not self.event_bus:
            return

        async with self._seq_lock:
            self._seq += 1
            seq_num = self._seq

        event_id = format_event_id(
            workflow_id=workflow_id,
            epoch=epoch,
            step_index=step_index,
            event_type=event_type,
            seq=seq_num,
            discriminator=discriminator,
        )

        event = WorkflowStreamEvent(
            event_id=event_id,
            event_type=event_type,
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            epoch=epoch,
            step_index=step_index,
            seq=seq_num,
            payload=payload,
        )

        task = asyncio.create_task(self.event_bus.publish(event))
        self._tracked_tasks.add(task)
        task.add_done_callback(self._tracked_tasks.discard)

    async def _execute_via_langgraph(
        self,
        input_data: OrchestratorInput,
        context: Optional[AgentExecutionContext] = None,
    ) -> OrchestratorOutput:
        """
        Executes workflow lifecycle through compiled LangGraph StateGraph.
        Translates input_data into GraphWorkflowState and maps terminal state back to OrchestratorOutput.
        Guarantees strict tenant isolation, replay safety, lease fencing, and event monotonicity.
        """
        task_id = input_data.task_id
        workflow_id = input_data.workflow_id or f"wf-{uuid.uuid4().hex[:12]}"
        request_id = (
            context.request_id
            if context
            else (input_data.metadata.get("request_id") or f"req-{uuid.uuid4().hex[:8]}")
        )
        correlation_id = (
            context.correlation_id
            if context
            else (input_data.metadata.get("correlation_id") or f"corr-{uuid.uuid4().hex[:8]}")
        )

        user_context = input_data.user_context or {}
        user_id = user_context.get("user_id")
        tenant_id = user_context.get("tenant_id") or input_data.metadata.get("tenant_id")

        # 1. Strict identity & lease validation for protected execution
        if self.workflow_repo is not None:
            if not tenant_id or not user_id:
                logger.warning(
                    f"Workflow execution rejected for task {task_id}: missing trusted tenant_id or user_id context.",
                    extra={"task_id": task_id, "workflow_id": workflow_id},
                )
                return OrchestratorOutput(
                    task_id=task_id,
                    workflow_id=workflow_id,
                    status=WorkflowStatus.FAILED,
                    error=ErrorInfo(
                        code="UNTRUSTED_IDENTITY",
                        message="Missing required tenant_id or user_id in trusted execution context.",
                        error_type="PermissionError",
                        request_id=request_id,
                        correlation_id=correlation_id,
                    ),
                )
            fencing_epoch = input_data.metadata.get("fencing_epoch")
            lock_token = input_data.metadata.get("lock_token")
            current_revision = input_data.metadata.get("expected_revision")
            if fencing_epoch is None or not lock_token or current_revision is None:
                raise FencingLeaseLostError(
                    f"Execution lease credentials missing for workflow '{workflow_id}' before graph execution."
                )
        else:
            tenant_id = tenant_id or "default"
            user_id = user_id or "unknown_user"
            fencing_epoch = input_data.metadata.get("fencing_epoch", 1)
            lock_token = input_data.metadata.get("lock_token")
            current_revision = input_data.metadata.get("expected_revision", 1)

        # 2. Approval resume claim tracking (distinguish first-time resume from recovery claim)
        is_first_claim = None
        is_recovery_claim = None
        if input_data.approval_decision is not None:
            if "is_first_resume_claim" in input_data.metadata:
                is_first_claim = bool(input_data.metadata["is_first_resume_claim"])
                is_recovery_claim = not is_first_claim
            elif "approval_claim_count" in input_data.metadata:
                claim_count = input_data.metadata["approval_claim_count"]
                is_first_claim = (claim_count == 1)
                is_recovery_claim = (claim_count > 1)
            else:
                # Do not default missing claim metadata to first-time resume!
                is_first_claim = False
                is_recovery_claim = True

        # 3. Assemble initial GraphWorkflowState
        plan_dict = input_data.execution_plan.model_dump() if input_data.execution_plan else None
        plan_steps = (
            [s.model_dump() for s in input_data.execution_plan.steps]
            if input_data.execution_plan
            else []
        )

        interrupted_steps = list(input_data.metadata.get("interrupted_steps") or [])
        if is_recovery_claim and not interrupted_steps:
            # In recovery claim of an approval, step 0 is interrupted
            interrupted_steps = [0]
        is_interrupted = bool(
            input_data.metadata.get("is_interrupted", False)
            or is_recovery_claim
            or bool(interrupted_steps)
        )

        initial_state: GraphWorkflowState = {
            "workflow_id": workflow_id,
            "task_id": task_id,
            "task_description": input_data.task_description,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "user_context": user_context,
            "request_id": request_id,
            "correlation_id": correlation_id,
            "fencing_epoch": fencing_epoch,
            "lock_token": lock_token,
            "current_revision": current_revision,
            "execution_plan": plan_dict,
            "plan_hash": None,
            "steps": plan_steps,
            "step_cursor": 0,
            "current_step_index": None,
            "completed_steps": {},
            "step_execution_records": {},
            "provenance_chain": [],
            "interrupted_steps": interrupted_steps,
            "is_interrupted": is_interrupted,
            "replan_count": 0,
            "has_partial_result": False,
            "approval_request": None,
            "approval_decision": (
                input_data.approval_decision.model_dump()
                if input_data.approval_decision
                else None
            ),
            "status": WorkflowStatus.PENDING.value,
            "is_step_fast_forwarded": False,
            "last_dispatch_success": False,
        }
        if is_first_claim is not None:
            initial_state["is_first_claim"] = is_first_claim
        if is_recovery_claim is not None:
            initial_state["is_recovery_claim"] = is_recovery_claim

        # 4. Invoke graph execution with task tracking and cancellation protection
        current_task = asyncio.current_task()
        if current_task is not None:
            self._tracked_tasks.add(current_task)

        try:
            final_state = await self._compiled_graph.ainvoke(initial_state)
        finally:
            if current_task is not None:
                self._tracked_tasks.discard(current_task)

        # 5. Map final graph state to OrchestratorOutput
        status_raw = final_state.get("status")
        try:
            out_status = WorkflowStatus(status_raw) if status_raw else WorkflowStatus.FAILED
        except ValueError:
            out_status = WorkflowStatus.FAILED

        final_resp = None
        raw_final_resp = final_state.get("final_response")
        if isinstance(raw_final_resp, dict):
            final_resp = FinalResponse.model_validate(raw_final_resp)
        elif isinstance(raw_final_resp, FinalResponse):
            final_resp = raw_final_resp

        prov_chain: List[ProvenanceEntry] = []
        raw_prov = final_state.get("provenance_chain") or []
        for p in raw_prov:
            if isinstance(p, dict):
                p_copy = dict(p)
                if "agent" in p_copy and "agent_id" not in p_copy:
                    p_copy["agent_id"] = p_copy.pop("agent")
                prov_chain.append(ProvenanceEntry.model_validate(p_copy))
            elif isinstance(p, ProvenanceEntry):
                prov_chain.append(p)

        appr_req = None
        raw_appr = final_state.get("approval_request")
        if isinstance(raw_appr, dict):
            appr_req = ApprovalRequest.model_validate(raw_appr)
        elif isinstance(raw_appr, ApprovalRequest):
            appr_req = raw_appr

        err_info = None
        raw_err = final_state.get("error_info")
        if isinstance(raw_err, dict):
            err_info = ErrorInfo.model_validate(raw_err)
        elif isinstance(raw_err, ErrorInfo):
            err_info = raw_err

        replan_count = int(final_state.get("replan_count", 0))

        return OrchestratorOutput(
            task_id=final_state.get("task_id", task_id),
            workflow_id=final_state.get("workflow_id", workflow_id),
            status=out_status,
            final_response=final_resp,
            provenance_chain=prov_chain,
            approval_request=appr_req,
            replan_count=replan_count,
            error=err_info,
        )

    async def execute_workflow(
        self,
        input_data: OrchestratorInput,
        context: Optional[AgentExecutionContext] = None,
    ) -> OrchestratorOutput:
        """
        Executes a workflow end-to-end per the authoritative 15-step lifecycle.
        Delegates to compiled LangGraph StateGraph when use_langgraph is True.
        """
        if self.use_langgraph:
            return await self._execute_via_langgraph(input_data=input_data, context=context)
        task_id = input_data.task_id
        workflow_id = input_data.workflow_id or f"wf-{uuid.uuid4().hex[:12]}"
        request_id = context.request_id if context else (input_data.metadata.get("request_id") or f"req-{uuid.uuid4().hex[:8]}")
        correlation_id = context.correlation_id if context else (input_data.metadata.get("correlation_id") or f"corr-{uuid.uuid4().hex[:8]}")

        user_context = input_data.user_context or {}
        user_id = user_context.get("user_id", "unknown_user")

        # 1. Audit: TASK_RECEIVED
        logger.info(
            f"Workflow task received: {task_id}",
            extra={
                "event_name": "TASK_RECEIVED",
                "task_id": task_id,
                "user_id": user_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

        # 2. Initialize or load WorkflowState
        workflow_state = await self._initialize_or_load_state(
            workflow_id=workflow_id,
            task_id=task_id,
            request_id=request_id,
            correlation_id=correlation_id,
            user_context=user_context,
            approval_decision=input_data.approval_decision,
        )

        provenance_chain: List[ProvenanceEntry] = []
        completed_steps: Dict[int, Dict[str, Any]] = {}
        step_execution_records: Dict[int, StepExecutionRecord] = {}
        replan_count = 0
        has_partial_result = False



        tenant_id = user_context.get("tenant_id") or "default"
        fencing_epoch = input_data.metadata.get("fencing_epoch")
        lock_token = input_data.metadata.get("lock_token")
        current_revision = input_data.metadata.get("expected_revision")

        # 3. Plan Resolution & Validation
        current_plan: ExecutionPlan
        if input_data.execution_plan is not None:
            # Supplied plan validation
            try:
                self.plan_validator.validate_plan(
                    input_data.execution_plan,
                    trusted_roles=user_context.get("roles", []),
                    enforce_registry=True,
                )
                current_plan = input_data.execution_plan
            except PlanValidationError as e:
                return await self._fail_workflow(
                    workflow_state=workflow_state,
                    task_id=task_id,
                    reason=f"Plan validation failed: {str(e)}",
                    failure_class=OrchestratorFailureClass.PLAN_VALIDATION_FAILED,
                    error_type="PlanValidationError",
                    request_id=request_id,
                    correlation_id=correlation_id,
                    provenance_chain=provenance_chain,
                    fencing_epoch=fencing_epoch,
                    lock_token=lock_token,
                    tenant_id=tenant_id,
                )

            logger.info(
                f"Supplied plan accepted for task {task_id}",
                extra={
                    "event_name": "PLAN_RECEIVED",
                    "task_id": task_id,
                    "step_count": len(current_plan.steps),
                    "plan_type": current_plan.plan_type.value,
                },
            )
        elif workflow_state.execution_plan is not None:
            # Recovered execution plan from durable state
            current_plan = ExecutionPlan.model_validate(workflow_state.execution_plan)
            logger.info(
                f"Recovered execution plan for task {task_id}",
                extra={
                    "event_name": "PLAN_RECOVERED",
                    "task_id": task_id,
                    "step_count": len(current_plan.steps),
                },
            )
        else:
            # Plan generation via PlannerAgent
            planner_invocation_id = f"plan-inv-{uuid.uuid4().hex[:8]}"
            logger.info(
                f"Dispatching plan generation for task {task_id}",
                extra={
                    "event_name": "PLAN_DISPATCHED",
                    "task_id": task_id,
                    "planner_invocation_id": planner_invocation_id,
                },
            )

            planner_result = await self._dispatch_planner(
                task_id=task_id,
                task_description=input_data.task_description,
                user_context=user_context,
                request_id=request_id,
                correlation_id=correlation_id,
            )

            if not planner_result or not planner_result.plan:
                return await self._fail_workflow(
                    workflow_state=workflow_state,
                    task_id=task_id,
                    reason=planner_result.error if planner_result else "planner_unavailable",
                    failure_class=OrchestratorFailureClass.LLM_FAILURE,
                    error_type="PlannerError",
                    request_id=request_id,
                    correlation_id=correlation_id,
                    provenance_chain=provenance_chain,
                    fencing_epoch=fencing_epoch,
                    lock_token=lock_token,
                    tenant_id=tenant_id,
                )

            current_plan = planner_result.plan
            logger.info(
                f"Generated plan received for task {task_id}",
                extra={
                    "event_name": "PLAN_RECEIVED",
                    "task_id": task_id,
                    "step_count": len(current_plan.steps),
                    "plan_type": current_plan.plan_type.value,
                },
            )

        # Bind accepted plan hash and state
        plan_hash = compute_plan_hash(current_plan)
        workflow_state.plan_hash = plan_hash
        workflow_state.execution_plan = current_plan.model_dump()

        # Transition workflow to RUNNING
        workflow_state.status = WorkflowStatus.RUNNING
        await self._persist_state(workflow_state)
        await self._emit_stream_event(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            epoch=fencing_epoch or 1,
            step_index=-1,
            event_type=StreamEventType.WORKFLOW_STARTED,
            payload={"status": "RUNNING", "step_count": len(current_plan.steps)},
            discriminator="start",
        )

        # 4. Step Execution Loop with topological progression
        steps_to_execute = list(current_plan.steps)
        step_idx = 0

        while step_idx < len(steps_to_execute):
            step = steps_to_execute[step_idx]
            current_step_index = step.step_index
            workflow_state.current_task_id = f"step-{current_step_index}"

            await self._emit_stream_event(
                workflow_id=workflow_id,
                tenant_id=tenant_id,
                epoch=fencing_epoch or 1,
                step_index=current_step_index,
                event_type=StreamEventType.STEP_STARTED,
                payload={
                    "description": step.description,
                    "assigned_agent_id": step.agent_id.value,
                    "dependencies": sorted(step.dependencies),
                    "risk_level": step.risk_level.value if hasattr(step.risk_level, "value") else str(step.risk_level),
                },
                discriminator="start",
            )

            # Fenced task identity validation & transition
            if self.workflow_repo:
                if not lock_token or fencing_epoch is None or current_revision is None:
                    raise FencingLeaseLostError(
                        f"Execution lease credentials missing for workflow '{workflow_id}' before step {current_step_index}."
                    )
                step_hash = compute_step_hash(step)
                is_completed, task_output, task_prov, new_revision = await self.workflow_repo.prepare_task_step(
                    workflow_id=workflow_id,
                    tenant_id=tenant_id,
                    step_index=current_step_index,
                    agent_id=step.agent_id.value,
                    name=step.description,
                    plan_hash=plan_hash,
                    step_hash=step_hash,
                    inputs=step.inputs,
                    fencing_epoch=fencing_epoch,
                    lock_token=lock_token,
                    expected_revision=current_revision,
                )
                current_revision = new_revision
                if is_completed:
                    # Verified completed step reuse byte-for-byte
                    completed_steps[current_step_index] = task_output or {}
                    if task_prov and isinstance(task_prov, dict):
                        provenance_chain.append(ProvenanceEntry(**task_prov))
                    elif task_prov and isinstance(task_prov, list):
                        for p in task_prov:
                            if isinstance(p, dict):
                                provenance_chain.append(ProvenanceEntry(**p))
                            elif isinstance(p, ProvenanceEntry):
                                provenance_chain.append(p)
                    else:
                        provenance_chain.append(
                            ProvenanceEntry(
                                step_index=current_step_index,
                                agent_id=step.agent_id.value,
                                action_summary=step.description,
                                status="COMPLETED",
                                result_reference=f"step_{current_step_index}_completed",
                            )
                        )
                    step_idx += 1
                    continue
            elif workflow_state.tasks and str(current_step_index) in workflow_state.tasks:
                # In-memory / repository-less fallback: strictly validate task identity against accepted step definition
                cached_task = workflow_state.tasks[str(current_step_index)]
                agent_val = cached_task.assigned_agent_id.value if hasattr(cached_task.assigned_agent_id, "value") else str(cached_task.assigned_agent_id)
                if agent_val != step.agent_id.value or cached_task.name != step.description:
                    raise PlanIntegrityError(
                        f"Step {current_step_index} identity conflict in memory. Stored task '{cached_task.name}' ({agent_val}) does not match plan step '{step.description}' ({step.agent_id.value})."
                    )
                if cached_task.status == TaskStatus.COMPLETED and cached_task.output_data is not None:
                    completed_steps[current_step_index] = cached_task.output_data
                    provenance_chain.append(
                        ProvenanceEntry(
                            step_index=current_step_index,
                            agent_id=agent_val,
                            action_summary=cached_task.name,
                            status="COMPLETED",
                            result_reference=f"step_{current_step_index}_completed",
                        )
                    )
                    step_idx += 1
                    continue

            # Step 4.1: RBAC verification before dispatch
            permitted_agents = self._get_permitted_agents(user_context)
            if step.agent_id not in permitted_agents:
                failure_msg = (
                    f"User role context {user_context.get('roles', [])} is not authorized "
                    f"to dispatch agent '{step.agent_id.value}'."
                )
                logger.warning(
                    failure_msg,
                    extra={
                        "event_name": "STEP_FAILED",
                        "task_id": task_id,
                        "step_index": current_step_index,
                        "agent_id": step.agent_id.value,
                        "failure_class": OrchestratorFailureClass.PERMISSION_DENIED.value,
                    },
                )
                return await self._fail_workflow(
                    workflow_state=workflow_state,
                    task_id=task_id,
                    reason=failure_msg,
                    failure_class=OrchestratorFailureClass.PERMISSION_DENIED,
                    error_type="PermissionError",
                    request_id=request_id,
                    correlation_id=correlation_id,
                    provenance_chain=provenance_chain,
                    fencing_epoch=fencing_epoch,
                    lock_token=lock_token,
                    tenant_id=tenant_id,
                )

            # Step 4.2: Dependency check
            for dep in step.dependencies:
                if dep not in completed_steps:
                    dep_record = step_execution_records.get(dep)
                    # If upstream was optional and skipped, we might proceed or fail based on step requirements
                    if dep_record and dep_record.status == TaskStatus.CANCELLED:
                        pass
                    else:
                        failure_msg = f"Dependency step {dep} has not completed successfully for step {current_step_index}."
                        return await self._fail_workflow(
                            workflow_state=workflow_state,
                            task_id=task_id,
                            reason=failure_msg,
                            failure_class=OrchestratorFailureClass.UNKNOWN_FAILURE,
                            error_type="DependencyError",
                            request_id=request_id,
                            correlation_id=correlation_id,
                            provenance_chain=provenance_chain,
                            fencing_epoch=fencing_epoch,
                            lock_token=lock_token,
                            tenant_id=tenant_id,
                        )

            # Step 4.3: Inter-step context assembly (redacted, dependency-specific)
            assembled_step_inputs = dict(step.inputs)
            for dep in step.dependencies:
                if dep in completed_steps:
                    dep_output = completed_steps[dep]
                    assembled_step_inputs[f"step_{dep}_output"] = redact_sensitive_data(dep_output)

            # Step 4.4: Deterministic Risk Gate Evaluation
            decision, approval_req = RiskGate.evaluate(
                step=step,
                task_id=task_id,
                workflow_id=workflow_id,
                requested_by=user_id,
                plan_hash=plan_hash,
                plan_id=current_plan.plan_id,
            )

            if decision == RiskGateDecision.PAUSE:
                # Check if this invocation has a matching ApprovalDecision (Resume case)
                is_authorized, reason = RiskGate.validate_resume_decision(
                    decision=input_data.approval_decision,
                    pending_request=workflow_state.approval_request or approval_req,
                    step_index=current_step_index,
                    task_id=task_id,
                    step=step,
                    plan_hash=plan_hash,
                )

                if is_authorized:
                    # Human approval granted; record decision and proceed
                    workflow_state.approval_decision = input_data.approval_decision
                    # Mark pending request consumed so it cannot be reused (SEC-01)
                    if workflow_state.approval_request:
                        workflow_state.approval_request.status = ApprovalStatus.APPROVED
                    workflow_state.approval_request = None
                    workflow_state.status = WorkflowStatus.RUNNING
                    await self._persist_state(workflow_state)
                elif reason == "human_approval_rejected":
                    # Explicit rejection terminates the workflow safely
                    workflow_state.approval_decision = input_data.approval_decision
                    return await self._fail_workflow(
                        workflow_state=workflow_state,
                        task_id=task_id,
                        reason="Human approval was rejected by approver.",
                        failure_class=OrchestratorFailureClass.HUMAN_APPROVAL_REJECTED,
                        error_type="ApprovalRejectedError",
                        request_id=request_id,
                        correlation_id=correlation_id,
                        provenance_chain=provenance_chain,
                        fencing_epoch=fencing_epoch,
                        lock_token=lock_token,
                        tenant_id=tenant_id,
                    )
                else:
                    # Pause workflow and wait for human approval
                    workflow_state.status = WorkflowStatus.WAITING_APPROVAL
                    workflow_state.approval_request = approval_req
                    await self._persist_state(workflow_state)
                    await self._emit_stream_event(
                        workflow_id=workflow_id,
                        tenant_id=tenant_id,
                        epoch=fencing_epoch or 1,
                        step_index=current_step_index,
                        event_type=StreamEventType.WAITING_APPROVAL,
                        payload={
                            "approval_id": approval_req.approval_id,
                            "step_index": current_step_index,
                            "requested_action": approval_req.requested_action,
                            "risk_level": approval_req.risk_level.value if hasattr(approval_req.risk_level, "value") else str(approval_req.risk_level),
                            "details": approval_req.details,
                            "expires_at": getattr(approval_req, "expires_at", None) or (approval_req.details.get("expires_at") if isinstance(approval_req.details, dict) else None),
                        },
                        discriminator="pause",
                    )
                    if self.workflow_repo and lock_token and fencing_epoch is not None and current_revision is not None:
                        exp_str = getattr(approval_req, "expires_at", None) or (approval_req.details.get("expires_at") if isinstance(approval_req.details, dict) else None)
                        exp = (
                            datetime.fromisoformat(exp_str)
                            if exp_str
                            else (datetime.now(timezone.utc) + timedelta(hours=24))
                        )
                        current_revision = await self.workflow_repo.pause_workflow_for_approval(
                            workflow_id=workflow_id,
                            tenant_id=tenant_id,
                            step_index=current_step_index,
                            approval_id=approval_req.approval_id,
                            requested_action=approval_req.requested_action,
                            risk_level=approval_req.risk_level.value if hasattr(approval_req.risk_level, "value") else str(approval_req.risk_level),
                            details=approval_req.details,
                            expires_at=exp,
                            fencing_epoch=fencing_epoch,
                            lock_token=lock_token,
                            expected_revision=current_revision,
                        )
                    return OrchestratorOutput(
                        task_id=task_id,
                        workflow_id=workflow_id,
                        status=WorkflowStatus.WAITING_APPROVAL,
                        approval_request=approval_req,
                        provenance_chain=provenance_chain,
                        replan_count=replan_count,
                    )

            # Step 4.5: Agent Dispatch with Exponential Backoff Retries
            step_record, dispatch_success = await self._dispatch_step_with_retries(
                step=step,
                inputs=assembled_step_inputs,
                task_id=task_id,
                workflow_id=workflow_id,
                request_id=request_id,
                correlation_id=correlation_id,
            )

            step_execution_records[current_step_index] = step_record

            if dispatch_success:
                completed_steps[current_step_index] = step_record.output_data
                workflow_state.tasks[str(current_step_index)] = Task(
                    task_id=f"step-{current_step_index}",
                    workflow_id=workflow_id,
                    name=step.description,
                    assigned_agent_id=step.agent_id,
                    status=TaskStatus.COMPLETED,
                    dependencies=[str(d) for d in step.dependencies],
                    input_data=redact_sensitive_data(assembled_step_inputs),
                    output_data=redact_sensitive_data(step_record.output_data),
                )
                prov_entry = ProvenanceEntry(
                    step_index=current_step_index,
                    agent_id=step.agent_id.value,
                    action_summary=step.description,
                    status="COMPLETED",
                    result_reference=f"step_{current_step_index}_completed",
                )
                provenance_chain.append(prov_entry)
                await self._persist_state(workflow_state)
                await self._emit_stream_event(
                    workflow_id=workflow_id,
                    tenant_id=tenant_id,
                    epoch=fencing_epoch or 1,
                    step_index=current_step_index,
                    event_type=StreamEventType.STEP_COMPLETED,
                    payload={
                        "status": "COMPLETED",
                        "duration_ms": step_record.duration_ms if hasattr(step_record, "duration_ms") else None,
                        "summary": step.description,
                    },
                    discriminator="done",
                )
                if self.workflow_repo and lock_token and fencing_epoch is not None and current_revision is not None:
                    current_revision = await self.workflow_repo.complete_task_step(
                        workflow_id=workflow_id,
                        tenant_id=tenant_id,
                        step_index=current_step_index,
                        output_data=redact_sensitive_data(step_record.output_data or {}),
                        provenance=prov_entry.model_dump(),
                        fencing_epoch=fencing_epoch,
                        lock_token=lock_token,
                        expected_revision=current_revision,
                    )
                step_idx += 1
            else:
                # Dispatch failed after retries
                provenance_chain.append(
                    ProvenanceEntry(
                        step_index=current_step_index,
                        agent_id=step.agent_id.value,
                        action_summary=step.description,
                        status="FAILED",
                        result_reference=step_record.error.code if step_record.error else "FAILED",
                    )
                )
                await self._emit_stream_event(
                    workflow_id=workflow_id,
                    tenant_id=tenant_id,
                    epoch=fencing_epoch or 1,
                    step_index=current_step_index,
                    event_type=StreamEventType.STEP_FAILED,
                    payload={
                        "error": step_record.error.code if step_record.error else "FAILED",
                        "message": step_record.error.message if step_record.error else "Step execution failed",
                    },
                    discriminator="fail",
                )

                if step.optional:
                    # Optional step failure: skip and mark partial result
                    logger.info(
                        f"Optional step {current_step_index} failed; skipping per plan definition.",
                        extra={"task_id": task_id, "step_index": current_step_index},
                    )
                    has_partial_result = True
                    step_record.status = TaskStatus.CANCELLED
                    workflow_state.tasks[str(current_step_index)] = Task(
                        task_id=f"step-{current_step_index}",
                        workflow_id=workflow_id,
                        name=step.description,
                        assigned_agent_id=step.agent_id,
                        status=TaskStatus.CANCELLED,
                        dependencies=[str(d) for d in step.dependencies],
                        error=step_record.error,
                    )
                    await self._persist_state(workflow_state)
                    step_idx += 1
                else:
                    # Required step failure:
                    failure_class = self._map_error_to_failure_class(step_record.error)

                    # Fix 3 (ARCH-01): Do not replan after prohibited policy/permission/approval failures
                    # per AgentSpec Part D Table 6 and Arch Part C Section 12
                    prohibited_replan_classes = {
                        OrchestratorFailureClass.PERMISSION_DENIED,
                        OrchestratorFailureClass.POLICY_VIOLATION,
                        OrchestratorFailureClass.HUMAN_APPROVAL_REJECTED,
                        OrchestratorFailureClass.APPROVAL_TIMEOUT,
                        OrchestratorFailureClass.SANDBOX_UNAVAILABLE,
                    }
                    if failure_class in prohibited_replan_classes:
                        logger.warning(
                            f"Step {current_step_index} failed with non-replannable failure class '{failure_class.value}'. Halting workflow without re-planning.",
                            extra={
                                "task_id": task_id,
                                "step_index": current_step_index,
                                "failure_class": failure_class.value,
                            },
                        )
                        return await self._fail_workflow(
                            workflow_state=workflow_state,
                            task_id=task_id,
                            reason=step_record.error.message if step_record.error else f"Step {current_step_index} failed with {failure_class.value}",
                            failure_class=failure_class,
                            error_type="NonReplannableFailure",
                            request_id=request_id,
                            correlation_id=correlation_id,
                            provenance_chain=provenance_chain,
                            replan_count=replan_count,
                        )

                    # Attempt contextual re-planning (PATH-H) for permitted failure classes
                    if replan_count < self.max_replan_attempts:
                        replan_count += 1
                        logger.warning(
                            f"Required step {current_step_index} failed permanently. Triggering contextual re-planning ({replan_count}/{self.max_replan_attempts}).",
                            extra={
                                "event_name": "REPLANNING_TRIGGERED",
                                "task_id": task_id,
                                "reason": step_record.error.message if step_record.error else "step_execution_failed",
                            },
                        )

                        revised_plan = await self._replan_from_failure(
                            task_id=task_id,
                            task_description=input_data.task_description,
                            failed_step_index=current_step_index,
                            failure_error=step_record.error,
                            completed_steps_records=completed_steps,
                            user_context=user_context,
                            request_id=request_id,
                            correlation_id=correlation_id,
                        )

                        if revised_plan and revised_plan.steps:
                            # Resume execution with revised steps
                            steps_to_execute = list(revised_plan.steps)
                            step_idx = 0
                            await self._persist_state(workflow_state)
                            continue
                        else:
                            # Re-planning returned invalid or empty plan
                            return await self._fail_workflow(
                                workflow_state=workflow_state,
                                task_id=task_id,
                                reason="Contextual re-planning failed to produce a valid recovery plan.",
                                failure_class=OrchestratorFailureClass.PLAN_VALIDATION_FAILED,
                                error_type="ReplanningError",
                                request_id=request_id,
                                correlation_id=correlation_id,
                                provenance_chain=provenance_chain,
                                replan_count=replan_count,
                            )
                    else:
                        # Re-planning limit exhausted
                        failure_msg = f"Step {current_step_index} permanently failed and max re-planning attempts ({self.max_replan_attempts}) exhausted."
                        return await self._fail_workflow(
                            workflow_state=workflow_state,
                            task_id=task_id,
                            reason=failure_msg,
                            failure_class=self._map_error_to_failure_class(step_record.error),
                            error_type="MaxReplansExhausted",
                            request_id=request_id,
                            correlation_id=correlation_id,
                            provenance_chain=provenance_chain,
                            replan_count=replan_count,
                        )

        # 5. Quality Gate: ReviewerValidationAgent Validation
        workflow_state.status = WorkflowStatus.RUNNING
        review_outcome, review_error, review_request = await self._invoke_reviewer_gate(
            task_id=task_id,
            completed_steps=completed_steps,
            provenance_chain=provenance_chain,
            workflow_id=workflow_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        if review_outcome == ReviewRecommendation.APPROVE:
            # Assembled validated response
            workflow_state.status = WorkflowStatus.COMPLETED
            await self._persist_state(workflow_state)
            if self.workflow_repo and lock_token and fencing_epoch is not None and tenant_id:
                try:
                    await self.workflow_repo.release_execution_lease(
                        workflow_id=workflow_id,
                        tenant_id=tenant_id,
                        fencing_epoch=fencing_epoch,
                        lock_token=lock_token,
                        final_status=WorkflowStatus.COMPLETED.value,
                    )
                except Exception:
                    pass

            logger.info(
                f"Workflow completed successfully for task {task_id}.",
                extra={
                    "event_name": "TASK_COMPLETED",
                    "task_id": task_id,
                    "final_status": "COMPLETED",
                },
            )

            await self._emit_stream_event(
                workflow_id=workflow_id,
                tenant_id=tenant_id,
                epoch=fencing_epoch or 1,
                step_index=-1,
                event_type=StreamEventType.WORKFLOW_COMPLETED,
                payload={
                    "status": "COMPLETED",
                    "step_count": len(steps_to_execute),
                    "has_partial_result": has_partial_result,
                },
                discriminator="complete",
            )

            final_response = FinalResponse(
                task_id=task_id,
                status=WorkflowStatus.COMPLETED,
                result=redact_sensitive_data({str(k): v for k, v in completed_steps.items()}),
                provenance_summary=[p.model_dump() for p in provenance_chain],
                partial_result=has_partial_result,
            )

            return OrchestratorOutput(
                task_id=task_id,
                workflow_id=workflow_id,
                status=WorkflowStatus.COMPLETED,
                final_response=final_response,
                provenance_chain=provenance_chain,
                replan_count=replan_count,
            )

        elif review_outcome == ReviewRecommendation.ESCALATE:
            # Escalate: human approval required by reviewer
            workflow_state.status = WorkflowStatus.WAITING_APPROVAL
            workflow_state.approval_request = review_request
            await self._persist_state(workflow_state)

            return OrchestratorOutput(
                task_id=task_id,
                workflow_id=workflow_id,
                status=WorkflowStatus.WAITING_APPROVAL,
                approval_request=review_request,
                provenance_chain=provenance_chain,
                replan_count=replan_count,
            )

        else:
            # Reviewer REJECT
            return await self._fail_workflow(
                workflow_state=workflow_state,
                task_id=task_id,
                reason=review_error or "Reviewer validation rejected the final output.",
                failure_class=OrchestratorFailureClass.REVIEWER_REJECTED,
                error_type="ReviewerRejectionError",
                request_id=request_id,
                correlation_id=correlation_id,
                provenance_chain=provenance_chain,
                replan_count=replan_count,
            )

    async def _dispatch_step_with_retries(
        self,
        step: PlanStep,
        inputs: Dict[str, Any],
        task_id: str,
        workflow_id: str,
        request_id: str,
        correlation_id: str,
    ) -> Tuple[StepExecutionRecord, bool]:
        """
        Dispatches a plan step with bounded exponential backoff retries for transient errors.
        Never retries non-retryable failures (permissions, schemas, policy).
        """
        step_index = step.step_index
        agent_id = step.agent_id
        attempt = 0
        backoff = self.initial_backoff_seconds

        last_record = StepExecutionRecord(
            step_index=step_index,
            agent_id=agent_id,
            status=TaskStatus.PENDING,
            input_data=redact_sensitive_data(inputs),
        )

        while attempt <= self.max_retries:
            attempt += 1
            last_record.retry_count = attempt - 1

            logger.info(
                f"Dispatching step {step_index} to agent {agent_id.value} (attempt {attempt}/{self.max_retries + 1}).",
                extra={
                    "event_name": "STEP_DISPATCHED",
                    "task_id": task_id,
                    "step_index": step_index,
                    "agent_id": agent_id.value,
                },
            )

            exec_context = AgentExecutionContext(
                execution_id=f"exec-{uuid.uuid4().hex[:8]}",
                workflow_id=workflow_id,
                task_id=f"step-{step_index}",
                agent_id=agent_id,
                request_id=request_id,
                correlation_id=correlation_id,
                input_payload=inputs,
            )

            timer = Timer().start()
            try:
                result: AgentExecutionResult = await self.registry.dispatch(exec_context)
                duration = timer.stop()
                last_record.duration_ms = duration

                if result.status == TaskStatus.COMPLETED:
                    logger.info(
                        f"Step {step_index} completed by agent {agent_id.value}.",
                        extra={
                            "event_name": "STEP_COMPLETED",
                            "task_id": task_id,
                            "step_index": step_index,
                            "agent_id": agent_id.value,
                            "outcome": "success",
                        },
                    )
                    last_record.status = TaskStatus.COMPLETED
                    last_record.output_data = result.output_payload
                    return last_record, True

                # Step returned FAILED
                last_record.status = TaskStatus.FAILED
                last_record.error = result.error

                failure_class = self._map_error_to_failure_class(result.error)
                logger.warning(
                    f"Step {step_index} failed on attempt {attempt}: {result.error.message if result.error else 'unknown'}",
                    extra={
                        "event_name": "STEP_FAILED",
                        "task_id": task_id,
                        "step_index": step_index,
                        "agent_id": agent_id.value,
                        "failure_class": failure_class.value,
                    },
                )

                # Check retry eligibility
                if not self._is_retryable(failure_class, result.error):
                    logger.info(
                        f"Failure class '{failure_class.value}' is non-retryable; stopping retry loop for step {step_index}."
                    )
                    return last_record, False

            except Exception as exc:
                duration = timer.stop()
                last_record.duration_ms = duration
                last_record.status = TaskStatus.FAILED
                last_record.error = ErrorInfo(
                    code="STEP_EXCEPTION",
                    message=str(exc),
                    error_type=type(exc).__name__,
                    request_id=request_id,
                    correlation_id=correlation_id,
                    retryable=False,
                )
                logger.warning(
                    f"Unexpected exception during step {step_index}: {str(exc)}",
                    extra={
                        "event_name": "STEP_FAILED",
                        "task_id": task_id,
                        "step_index": step_index,
                        "agent_id": agent_id.value,
                        "failure_class": OrchestratorFailureClass.UNKNOWN_FAILURE.value,
                    },
                )
                return last_record, False

            # If retryable and attempts remain, back off
            if attempt <= self.max_retries:
                await asyncio.sleep(backoff)
                backoff *= self.backoff_multiplier

        return last_record, False

    async def _dispatch_planner(
        self,
        task_id: str,
        task_description: str,
        user_context: Dict[str, Any],
        request_id: str,
        correlation_id: str,
    ) -> Optional[PlannerOutput]:
        """Dispatches plan generation to PlannerAgent via AgentRegistry."""
        planner_context = AgentExecutionContext(
            execution_id=f"plan-exec-{uuid.uuid4().hex[:8]}",
            workflow_id=f"wf-{uuid.uuid4().hex[:8]}",
            task_id=task_id,
            agent_id=AgentID.PLANNER,
            request_id=request_id,
            correlation_id=correlation_id,
            input_payload={
                "task_id": task_id,
                "task_description": task_description,
                "user_context": user_context,
                "is_replan": False,
            },
        )

        result = await self.registry.dispatch(planner_context)
        if result.status == TaskStatus.COMPLETED and result.output_payload:
            try:
                return PlannerOutput.model_validate(result.output_payload)
            except Exception as e:
                logger.error(f"Failed to validate PlannerOutput: {e}")
                return None
        return None

    async def _replan_from_failure(
        self,
        task_id: str,
        task_description: str,
        failed_step_index: int,
        failure_error: Optional[ErrorInfo],
        completed_steps_records: Dict[int, Dict[str, Any]],
        user_context: Dict[str, Any],
        request_id: str,
        correlation_id: str,
    ) -> Optional[ExecutionPlan]:
        """Dispatches contextual re-planning request to PlannerAgent."""
        completed_steps_list = [
            {"step_index": idx, "output": out}
            for idx, out in completed_steps_records.items()
        ]
        failure_details = {
            "error_code": failure_error.code if failure_error else "unknown",
            "message": failure_error.message if failure_error else "unknown",
        }

        planner_context = AgentExecutionContext(
            execution_id=f"replan-exec-{uuid.uuid4().hex[:8]}",
            workflow_id=f"wf-{uuid.uuid4().hex[:8]}",
            task_id=task_id,
            agent_id=AgentID.PLANNER,
            request_id=request_id,
            correlation_id=correlation_id,
            input_payload={
                "task_id": task_id,
                "task_description": task_description,
                "failed_step_index": failed_step_index,
                "failure_details": failure_details,
                "completed_steps": completed_steps_list,
                "is_replan": True,
                "user_context": user_context,
            },
        )

        result = await self.registry.dispatch(planner_context)
        if result.status == TaskStatus.COMPLETED and result.output_payload:
            try:
                planner_output = PlannerOutput.model_validate(result.output_payload)
                if planner_output.plan:
                    self.plan_validator.validate_plan(
                        planner_output.plan,
                        trusted_roles=user_context.get("roles", []),
                        enforce_registry=True,
                    )
                    return planner_output.plan
            except Exception as e:
                logger.warning(f"Re-planning plan failed validation: {e}")
                return None
        return None

    async def _invoke_reviewer_gate(
        self,
        task_id: str,
        completed_steps: Dict[int, Dict[str, Any]],
        provenance_chain: List[ProvenanceEntry],
        workflow_id: str,
        request_id: str,
        correlation_id: str,
    ) -> Tuple[ReviewRecommendation, Optional[str], Optional[ApprovalRequest]]:
        """
        Dispatches output verification to ReviewerValidationAgent.
        Returns: (ReviewRecommendation, Optional[error_string], Optional[ApprovalRequest])
        """
        # If ReviewerValidationAgent is not registered, fail closed per specifications
        if not self.registry.has_agent(AgentID.REVIEWER_VALIDATION):
            return ReviewRecommendation.REJECT, "ReviewerValidationAgent not registered", None

        reviewer_context = AgentExecutionContext(
            execution_id=f"rev-exec-{uuid.uuid4().hex[:8]}",
            workflow_id=workflow_id,
            task_id=task_id,
            agent_id=AgentID.REVIEWER_VALIDATION,
            request_id=request_id,
            correlation_id=correlation_id,
            input_payload={
                "task_id": task_id,
                "assembled_output": {str(k): v for k, v in completed_steps.items()},
                "step_outputs": [out for out in completed_steps.values()],
                "provenance_chain": [p.model_dump() for p in provenance_chain],
            },
        )

        res = await self.registry.dispatch(reviewer_context)
        if res.status != TaskStatus.COMPLETED or not res.output_payload:
            return ReviewRecommendation.REJECT, "ReviewerValidationAgent dispatch failed", None

        try:
            reviewer_output = ReviewerOutput.model_validate(res.output_payload)
            if reviewer_output.recommendation == ReviewRecommendation.APPROVE:
                return ReviewRecommendation.APPROVE, None, None
            elif reviewer_output.recommendation == ReviewRecommendation.ESCALATE:
                approval_id = f"appr-{uuid.uuid4().hex[:12]}"
                approval_req = ApprovalRequest(
                    approval_id=approval_id,
                    workflow_id=workflow_id,
                    requested_action="Reviewer escalated workflow for manual human verification.",
                    risk_level=RiskLevel.HIGH,
                    status=ApprovalStatus.PENDING,
                    requested_at=datetime.now(timezone.utc).isoformat(),
                    details={
                        "task_id": task_id,
                        "findings": [f.model_dump() for f in reviewer_output.findings],
                    },
                )
                logger.info(
                    f"Reviewer escalated task {task_id}; approval {approval_id} requested.",
                    extra={
                        "event_name": "APPROVAL_REQUESTED",
                        "task_id": task_id,
                        "approval_id": approval_id,
                        "step_index": -1,
                    },
                )
                return ReviewRecommendation.ESCALATE, None, approval_req
            else:
                findings_desc = "; ".join([f.description for f in reviewer_output.findings])
                return ReviewRecommendation.REJECT, findings_desc or "Reviewer rejected output", None
        except Exception as e:
            return ReviewRecommendation.REJECT, f"Failed to parse ReviewerOutput: {e}", None

    def _get_permitted_agents(self, user_context: Dict[str, Any]) -> Set[AgentID]:
        """Resolves permitted agents for the caller based on ROLE_PERMITTED_AGENTS."""
        roles = user_context.get("roles", [])
        if isinstance(roles, str):
            roles = [roles]
        permitted: Set[AgentID] = set()
        for r in roles:
            if r in ROLE_PERMITTED_AGENTS:
                permitted.update(ROLE_PERMITTED_AGENTS[r])
        return permitted

    def _map_error_to_failure_class(self, error: Optional[ErrorInfo]) -> OrchestratorFailureClass:
        """Deterministically classifies failure based on Section 12.1 taxonomy."""
        if not error:
            return OrchestratorFailureClass.UNKNOWN_FAILURE
        code = (error.code or "").lower()
        err_type = (error.error_type or "").lower()

        if "timeout" in code or "timeout" in err_type:
            return OrchestratorFailureClass.LLM_FAILURE
        if "permission" in code or "unauthorized" in code or "permission" in err_type:
            return OrchestratorFailureClass.PERMISSION_DENIED
        if "policy" in code or "policy" in err_type:
            return OrchestratorFailureClass.POLICY_VIOLATION
        if "schema" in code or "validation" in code or "validation" in err_type:
            return OrchestratorFailureClass.SCHEMA_VIOLATION
        if "tool" in code or "tool" in err_type:
            return OrchestratorFailureClass.TOOL_ERROR
        if "database" in code or "postgres" in code:
            return OrchestratorFailureClass.DATABASE_UNAVAILABLE
        if "approval" in code:
            return OrchestratorFailureClass.HUMAN_APPROVAL_REJECTED

        return OrchestratorFailureClass.UNKNOWN_FAILURE

    def _is_retryable(self, failure_class: OrchestratorFailureClass, error: Optional[ErrorInfo]) -> bool:
        """Determines retry eligibility per Arch Part C Section 12.1."""
        non_retryable = {
            OrchestratorFailureClass.PERMISSION_DENIED,
            OrchestratorFailureClass.POLICY_VIOLATION,
            OrchestratorFailureClass.SCHEMA_VIOLATION,
            OrchestratorFailureClass.DOCUMENT_NOT_FOUND,
            OrchestratorFailureClass.APPROVAL_TIMEOUT,
            OrchestratorFailureClass.SANDBOX_UNAVAILABLE,
            OrchestratorFailureClass.HUMAN_APPROVAL_REJECTED,
            OrchestratorFailureClass.REVIEWER_REJECTED,
        }
        if failure_class in non_retryable:
            return False
        if error and error.retryable is False:
            return False
        return True

    async def _initialize_or_load_state(
        self,
        workflow_id: str,
        task_id: str,
        request_id: str,
        correlation_id: str,
        user_context: Dict[str, Any],
        approval_decision: Optional[ApprovalDecision] = None,
    ) -> WorkflowState:
        """Initializes or loads WorkflowState from workflow_repo or state store."""
        now = datetime.now(timezone.utc).isoformat()
        tenant_id = user_context.get("tenant_id") or "default"

        if self.workflow_repo:
            wf_model = await self.workflow_repo.get_workflow(workflow_id, tenant_id)
            if wf_model:
                tasks_list = await self.workflow_repo.get_tasks_for_workflow(workflow_id, tenant_id)
                domain_tasks = {str(t.step_index): t.to_domain_task() for t in tasks_list}
                st = wf_model.to_domain_state(domain_tasks=domain_tasks)
                if approval_decision:
                    st.approval_decision = approval_decision
                return st

        if self.state_store:
            existing = await self.state_store.get_state(workflow_id)
            if existing:
                if approval_decision:
                    existing.approval_decision = approval_decision
                existing.updated_at = now
                return existing

        req_ctx = RequestContext(
            request_id=request_id,
            correlation_id=correlation_id,
            user_id=user_context.get("user_id", "unknown_user"),
            tenant_id=tenant_id,
            roles=user_context.get("roles", []),
            permissions=user_context.get("permissions", []),
        )

        new_state = WorkflowState(
            workflow_id=workflow_id,
            request_context=req_ctx,
            status=WorkflowStatus.PENDING,
            current_task_id=task_id,
            created_at=now,
            updated_at=now,
            approval_decision=approval_decision,
        )

        await self._persist_state(new_state, is_new=True)
        return new_state

    async def _persist_state(self, state: WorkflowState, is_new: bool = False) -> None:
        """
        Persists workflow state to state store with 3 retries.
        Fails closed if persistence fails permanently per AgentSpec Part A Section R.
        """
        if not self.state_store:
            return

        state.updated_at = datetime.now(timezone.utc).isoformat()
        attempts = 0
        last_exc: Optional[Exception] = None

        while attempts < 3:
            attempts += 1
            try:
                if is_new:
                    await self.state_store.create_state(state)
                else:
                    try:
                        await self.state_store.update_state(state)
                    except (KeyError, ValueError):
                        await self.state_store.create_state(state)
                return
            except Exception as e:
                last_exc = e
                await asyncio.sleep(0.05)

        logger.critical(
            f"Failed to persist state for workflow {state.workflow_id} after 3 attempts: {last_exc}",
            extra={"workflow_id": state.workflow_id},
        )
        raise RuntimeError(f"State store persistence failure for workflow {state.workflow_id}: {last_exc}")

    async def _fail_workflow(
        self,
        workflow_state: WorkflowState,
        task_id: str,
        reason: str,
        failure_class: OrchestratorFailureClass,
        error_type: str,
        request_id: str,
        correlation_id: str,
        provenance_chain: List[ProvenanceEntry],
        replan_count: int = 0,
        fencing_epoch: Optional[int] = None,
        lock_token: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> OrchestratorOutput:
        """Records terminal workflow failure, logs audit event, persists state, and returns OrchestratorOutput."""
        logger.error(
            f"Workflow {workflow_state.workflow_id} failed: {reason}",
            extra={
                "event_name": "TASK_FAILED",
                "task_id": task_id,
                "reason": reason,
                "failure_class": failure_class.value,
            },
        )

        error_info = ErrorInfo(
            code=failure_class.value.upper(),
            message=reason,
            error_type=error_type,
            request_id=request_id,
            correlation_id=correlation_id,
            retryable=False,
            details={"failure_class": failure_class.value},
        )

        workflow_state.status = WorkflowStatus.FAILED
        workflow_state.error = error_info
        try:
            await self._persist_state(workflow_state)
        except Exception:
            # If persisting failure state itself fails, still return output
            pass

        await self._emit_stream_event(
            workflow_id=workflow_state.workflow_id,
            tenant_id=tenant_id or "default",
            epoch=fencing_epoch or 1,
            step_index=-1,
            event_type=StreamEventType.WORKFLOW_FAILED,
            payload={
                "status": "FAILED",
                "failure_class": failure_class.value if hasattr(failure_class, "value") else str(failure_class),
                "error_type": error_type,
                "reason": reason,
            },
            discriminator="fail",
        )

        if self.workflow_repo and lock_token and fencing_epoch is not None and tenant_id:
            try:
                await self.workflow_repo.release_execution_lease(
                    workflow_id=workflow_state.workflow_id,
                    tenant_id=tenant_id,
                    fencing_epoch=fencing_epoch,
                    lock_token=lock_token,
                    final_status=WorkflowStatus.FAILED.value,
                    error=error_info.model_dump(),
                )
            except Exception:
                pass

        return OrchestratorOutput(
            task_id=task_id,
            workflow_id=workflow_state.workflow_id,
            status=WorkflowStatus.FAILED,
            error=error_info,
            provenance_chain=provenance_chain,
            replan_count=replan_count,
        )
