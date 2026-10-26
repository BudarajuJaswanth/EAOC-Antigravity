"""
EAOC LangGraph Lifecycle Node Contracts & Handlers (Part 2.18.2)

Implements discrete, typed lifecycle node functions for LangGraph execution.
Delegates all database operations and side-effects strictly through:
- IWorkflowRecoveryRepository (durable PostgreSQL state, leasing, fencing)
- PlanValidator (role-permitted agent and DAG validation)
- AgentRegistry (dispatching to all 12 approved logical agents)
- RiskGate (Section 9.2 human approval decisions)
- ReviewerEngine (Section 9.1 deterministic quality gate)
- IWorkflowEventBus (canonical SSE event publishing)
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from eaoc.agents.orchestrator.risk_gate import RiskGate, RiskGateDecision
from eaoc.agents.orchestrator.schemas import (
    FinalResponse,
    OrchestratorFailureClass,
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
from eaoc.agents.reviewer_validation.engine import ReviewerEngine
from eaoc.agents.reviewer_validation.schemas import (
    ReviewRecommendation,
    ReviewerInput,
    ReviewerOutput,
)
from eaoc.observability import Timer, get_logger, redact_sensitive_data
from eaoc.orchestration.state import GraphWorkflowState, sanitize_state_for_logging
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
from eaoc.schemas.events import StreamEventType, WorkflowStreamEvent, format_event_id
from eaoc.services.event_bus import IWorkflowEventBus
from eaoc.state.interfaces import IWorkflowStateStore
from eaoc.state.workflow_repository import (
    ConcurrencyConflictError,
    FencingLeaseLostError,
    IWorkflowRecoveryRepository,
    PlanIntegrityError,
)

logger = get_logger("eaoc.orchestration.nodes")

# Non-replannable failure classes per AgentSpec Part D Table 6
PROHIBITED_REPLAN_CLASSES = {
    OrchestratorFailureClass.PERMISSION_DENIED.value,
    OrchestratorFailureClass.POLICY_VIOLATION.value,
    OrchestratorFailureClass.HUMAN_APPROVAL_REJECTED.value,
    OrchestratorFailureClass.APPROVAL_TIMEOUT.value,
    OrchestratorFailureClass.SANDBOX_UNAVAILABLE.value,
}

# Strictly read-only operation types and tools for crash replay safety
READ_ONLY_OPERATIONS = {"read", "query", "retrieve", "search"}
READ_ONLY_TOOLS = {
    "postgresql_read",
    "pgvector_search",
    "document_retrieve",
    "rag_search_tool",
    "embedding_generate",
}


def compute_plan_hash(plan: ExecutionPlan) -> str:
    """Computes deterministic SHA-256 hash of canonical plan representation."""
    import hashlib
    import json

    canonical_steps = []
    for s in plan.steps:
        canonical_steps.append({
            "step_index": s.step_index,
            "agent_id": s.agent_id.value if hasattr(s.agent_id, "value") else str(s.agent_id),
            "description": s.description,
            "risk_level": s.risk_level.value if hasattr(s.risk_level, "value") else str(s.risk_level),
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
        "plan_type": plan.plan_type.value if hasattr(plan.plan_type, "value") else str(plan.plan_type),
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
        "agent_id": step.agent_id.value if hasattr(step.agent_id, "value") else str(step.agent_id),
        "description": step.description,
        "risk_level": step.risk_level.value if hasattr(step.risk_level, "value") else str(step.risk_level),
        "operation_type": step.operation_type,
        "data_class": step.data_class,
        "operation": step.operation,
        "dependencies": sorted(step.dependencies),
        "inputs": sorted(step.inputs.items()),
        "optional": step.optional,
    }
    raw = json.dumps(canonical_step, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class OrchestrationNodes:
    """
    Stateful handler class binding dependencies to atomic LangGraph node functions.
    Preserves all Part 2.1–2.17 contracts, fencing validations, and security policies.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        workflow_repo: Optional[IWorkflowRecoveryRepository] = None,
        tool_registry: Optional[Any] = None,
        approval_repo: Optional[Any] = None,
        state_store: Optional[IWorkflowStateStore] = None,
        event_bus: Optional[IWorkflowEventBus] = None,
        max_retries: int = 2,
        initial_backoff_seconds: float = 2.0,
        backoff_multiplier: float = 2.0,
        max_replan_attempts: int = 2,
    ):
        self.registry = registry
        self.workflow_repo = workflow_repo
        self.tool_registry = tool_registry
        self.approval_repo = approval_repo
        self.state_store = state_store
        self.event_bus = event_bus
        self.max_retries = max_retries
        self.initial_backoff_seconds = initial_backoff_seconds
        self.backoff_multiplier = backoff_multiplier
        self.max_replan_attempts = max_replan_attempts
        self.plan_validator = PlanValidator(agent_registry=registry)
        self.reviewer_engine = ReviewerEngine()

        self._seq: int = 0
        self._seq_lock = asyncio.Lock()

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
        """Emits canonical SSE stream event with monotonic sequence allocation."""
        if not self.event_bus:
            return

        async with self._seq_lock:
            self._seq += 1
            seq = self._seq

        event_id = format_event_id(
            workflow_id=workflow_id,
            epoch=epoch,
            step_index=step_index,
            event_type=event_type,
            seq=seq,
            discriminator=discriminator,
        )

        event = WorkflowStreamEvent(
            event_id=event_id,
            event_type=event_type,
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            epoch=epoch,
            step_index=step_index,
            seq=seq,
            payload=redact_sensitive_data(payload),
        )

        try:
            await self.event_bus.publish(event)
        except Exception as exc:
            logger.warning(
                f"Failed to publish stream event '{event_id}': {exc}",
                extra={"workflow_id": workflow_id, "event_id": event_id},
            )

    # -------------------------------------------------------------------------
    # Node 1: init_and_recover
    # -------------------------------------------------------------------------
    async def node_init_and_recover(self, state: GraphWorkflowState) -> Dict[str, Any]:
        """
        Initializes graph execution context, validates fencing credentials,
        and rehydrates completed tasks from PostgreSQL.
        """
        workflow_id = state.get("workflow_id", "")
        tenant_id = state.get("tenant_id", "default")
        fencing_epoch = state.get("fencing_epoch", 0)
        lock_token = state.get("lock_token")
        current_revision = state.get("current_revision", 1)

        completed_steps = dict(state.get("completed_steps") or {})
        provenance_chain = list(state.get("provenance_chain") or [])
        interrupted_steps = list(state.get("interrupted_steps") or [])

        is_first_claim = state.get("is_first_claim", True)
        is_recovery_claim = state.get("is_recovery_claim", False)
        recovered_plan = None
        recovered_plan_hash = None
        recovered_steps = None
        replan_count = state.get("replan_count", 0)

        if self.workflow_repo and workflow_id:
            try:
                wf_model = await self.workflow_repo.get_workflow(workflow_id, tenant_id)
                if wf_model:
                    fencing_epoch = wf_model.fencing_epoch
                    current_revision = wf_model.row_revision
                    exec_meta = wf_model.execution_metadata if isinstance(getattr(wf_model, "execution_metadata", None), dict) else {}
                    if isinstance(exec_meta, dict) and "replan_count" in exec_meta:
                        try:
                            replan_count = max(replan_count, int(exec_meta.get("replan_count", 0)))
                        except (ValueError, TypeError):
                            pass

                    # Rehydrate plan from repository if state doesn't already have one
                    db_plan = getattr(wf_model, "execution_plan", None)
                    if not state.get("execution_plan") and isinstance(db_plan, (dict, ExecutionPlan)):
                        recovered_plan = db_plan
                        recovered_plan_hash = getattr(wf_model, "plan_hash", None)
                        if isinstance(recovered_plan, dict) and "steps" in recovered_plan:
                            recovered_steps = recovered_plan["steps"]
                        elif isinstance(recovered_plan, ExecutionPlan):
                            recovered_steps = [s.model_dump() for s in recovered_plan.steps]

                    claim_count = exec_meta.get("approval_claim_count")
                    if "is_first_resume_claim" in exec_meta:
                        is_first_claim = bool(exec_meta.get("is_first_resume_claim"))
                        is_recovery_claim = not is_first_claim
                    elif isinstance(claim_count, int) and claim_count > 1:
                        is_first_claim = False
                        is_recovery_claim = True

                    tasks_list = await self.workflow_repo.get_tasks_for_workflow(workflow_id, tenant_id)
                    for t in tasks_list:
                        if t.status == TaskStatus.COMPLETED.value:
                            completed_steps[t.step_index] = t.output_data or {}
                            if t.provenance:
                                provenance_chain.append(t.provenance)
                        elif t.status == TaskStatus.RUNNING.value:
                            if t.step_index not in interrupted_steps:
                                interrupted_steps.append(t.step_index)
            except Exception as e:
                logger.error(f"Error rehydrating workflow {workflow_id} from repository: {e}")

        res = {
            "workflow_id": workflow_id,
            "tenant_id": tenant_id,
            "fencing_epoch": fencing_epoch,
            "lock_token": lock_token,
            "current_revision": current_revision,
            "completed_steps": completed_steps,
            "interrupted_steps": interrupted_steps,
            "provenance_chain": provenance_chain,
            "step_cursor": 0,
            "status": state.get("status") or WorkflowStatus.RUNNING.value,
            "is_first_claim": is_first_claim,
            "is_recovery_claim": is_recovery_claim,
            "replan_count": replan_count,
        }
        if recovered_plan is not None:
            res["execution_plan"] = recovered_plan
        if recovered_plan_hash is not None:
            res["plan_hash"] = recovered_plan_hash
        if recovered_steps is not None:
            res["steps"] = recovered_steps
        return res


    # -------------------------------------------------------------------------
    # Node 2: plan_resolution
    # -------------------------------------------------------------------------
    async def node_plan_resolution(self, state: GraphWorkflowState) -> Dict[str, Any]:
        """
        Validates supplied plan, rehydrates recovered plan, or generates a new plan via PlannerAgent.
        Binds canonical plan_hash and step sequence.
        """
        workflow_id = state["workflow_id"]
        tenant_id = state.get("tenant_id", "default")
        task_id = state.get("task_id", f"task-{workflow_id}")
        user_context = state.get("user_context") or {}
        fencing_epoch = state.get("fencing_epoch", 1)

        current_plan: Optional[ExecutionPlan] = None

        if state.get("execution_plan") is not None:
            raw_plan = state["execution_plan"]
            if isinstance(raw_plan, ExecutionPlan):
                current_plan = raw_plan
            elif isinstance(raw_plan, dict):
                try:
                    current_plan = ExecutionPlan.model_validate(raw_plan)
                except Exception as e:
                    return {
                        "status": WorkflowStatus.FAILED.value,
                        "failure_class": OrchestratorFailureClass.PLAN_VALIDATION_FAILED.value,
                        "error_info": {
                            "code": "PLAN_VALIDATION_FAILED",
                            "message": f"Plan validation failed: {str(e)}",
                            "error_type": "PlanValidationError",
                        },
                        "next_route": "fail_workflow",
                    }
            else:
                return {
                    "status": WorkflowStatus.FAILED.value,
                    "failure_class": OrchestratorFailureClass.PLAN_VALIDATION_FAILED.value,
                    "error_info": {
                        "code": "PLAN_VALIDATION_FAILED",
                        "message": "Execution plan is invalid or malformed.",
                        "error_type": "PlanValidationError",
                    },
                    "next_route": "fail_workflow",
                }

            # Validate plan
            try:
                self.plan_validator.validate_plan(
                    current_plan,
                    trusted_roles=user_context.get("roles", []),
                    enforce_registry=True,
                )
            except PlanValidationError as e:
                return {
                    "status": WorkflowStatus.FAILED.value,
                    "failure_class": OrchestratorFailureClass.PLAN_VALIDATION_FAILED.value,
                    "error_info": {
                        "code": "PLAN_VALIDATION_FAILED",
                        "message": f"Plan validation failed: {str(e)}",
                        "error_type": "PlanValidationError",
                    },
                    "next_route": "fail_workflow",
                }
        else:
            # Dispatch plan generation to PlannerAgent
            planner_input = {
                "task_id": task_id,
                "task_description": state.get("task_description", "execute workflow"),
                "user_context": user_context,
                "is_replan": False,
            }
            exec_ctx = AgentExecutionContext(
                execution_id=f"plan-exec-{uuid.uuid4().hex[:8]}",
                workflow_id=workflow_id,
                task_id=task_id,
                agent_id=AgentID.PLANNER,
                request_id=state.get("request_id", ""),
                correlation_id=state.get("correlation_id", ""),
                input_payload=planner_input,
            )
            result = await self.registry.dispatch(exec_ctx)
            if result.status != TaskStatus.COMPLETED or not result.output_payload:
                return {
                    "status": WorkflowStatus.FAILED.value,
                    "failure_class": OrchestratorFailureClass.LLM_FAILURE.value,
                    "error_info": {
                        "code": "PLANNER_ERROR",
                        "message": result.error.message if result.error else "PlannerAgent unavailable",
                        "error_type": "PlannerError",
                    },
                    "next_route": "fail_workflow",
                }
            planner_output = PlannerOutput.model_validate(result.output_payload)
            current_plan = planner_output.plan

        plan_hash = compute_plan_hash(current_plan)
        steps_list = [s.model_dump() for s in current_plan.steps]

        await self._emit_stream_event(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            epoch=fencing_epoch or 1,
            step_index=-1,
            event_type=StreamEventType.WORKFLOW_STARTED,
            payload={"status": "RUNNING", "step_count": len(steps_list)},
            discriminator="start",
        )

        return {
            "execution_plan": current_plan.model_dump(),
            "plan_hash": plan_hash,
            "steps": steps_list,
            "step_cursor": 0,
            "status": WorkflowStatus.RUNNING.value,
            "next_route": "prepare_step",
        }

    # -------------------------------------------------------------------------
    # Node 3: prepare_step
    # -------------------------------------------------------------------------
    async def node_prepare_step(self, state: GraphWorkflowState) -> Dict[str, Any]:
        """
        Prepares next step, checks dependencies, and enforces fencing/fast-forward in PostgreSQL.
        Applies conservative fail-closed recovery for ambiguous incomplete mutating steps.
        """
        workflow_id = state["workflow_id"]
        tenant_id = state.get("tenant_id", "default")
        fencing_epoch = state.get("fencing_epoch", 1)
        lock_token = state.get("lock_token", "")
        current_revision = state.get("current_revision", 1)
        plan_hash = state.get("plan_hash", "")
        cursor = state.get("step_cursor", 0)
        steps = state.get("steps") or []

        if cursor >= len(steps):
            return {"next_route": "reviewer_gate"}

        raw_step = steps[cursor]
        step = PlanStep.model_validate(raw_step)
        step_idx = step.step_index

        completed_steps = dict(state.get("completed_steps") or {})
        provenance_chain = list(state.get("provenance_chain") or [])

        # Check if step was interrupted under a previous execution
        interrupted_steps = set(state.get("interrupted_steps") or [])
        is_interrupted = (step_idx in interrupted_steps) or state.get("is_interrupted", False)

        # Fenced prepare_task_step in PostgreSQL
        if self.workflow_repo:
            if not lock_token or fencing_epoch is None:
                raise FencingLeaseLostError(f"Lease credentials missing before step {step_idx}.")

            step_hash = compute_step_hash(step)
            try:
                is_completed, task_output, task_prov, new_revision = await self.workflow_repo.prepare_task_step(
                    workflow_id=workflow_id,
                    tenant_id=tenant_id,
                    step_index=step_idx,
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
                    completed_steps[step_idx] = task_output or {}
                    if task_prov:
                        provenance_chain.append(task_prov)
                    return {
                        "current_step_index": step_idx,
                        "current_revision": current_revision,
                        "completed_steps": completed_steps,
                        "provenance_chain": provenance_chain,
                        "is_step_fast_forwarded": True,
                        "step_cursor": cursor + 1,
                        "next_route": "check_more_steps",
                    }
            except FencingLeaseLostError:
                raise
            except Exception as exc:
                return {
                    "status": WorkflowStatus.FAILED.value,
                    "failure_class": OrchestratorFailureClass.UNKNOWN_FAILURE.value,
                    "error_info": {"code": "STEP_PREPARATION_ERROR", "message": str(exc)},
                    "next_route": "fail_workflow",
                }

        # Fix A: Replay safety enforcement for interrupted steps
        if is_interrupted:
            op_type = (step.operation_type or "").strip().lower()

            # 1. Unknown or missing operation type -> fail closed
            if not op_type:
                return {
                    "status": WorkflowStatus.FAILED.value,
                    "failure_class": OrchestratorFailureClass.UNKNOWN_FAILURE.value,
                    "error_info": {
                        "code": "AMBIGUOUS_SIDE_EFFECT",
                        "message": (
                            f"Step {step_idx} was interrupted under a previous execution. "
                            "Operation type is unspecified or unknown; cannot rule out external side effects. "
                            "Failing closed per replay safety policy."
                        ),
                        "error_type": "AmbiguousSideEffectError",
                    },
                    "next_route": "fail_workflow",
                }

            # Check tool metadata if tool is specified in inputs or operation
            tool_name = step.inputs.get("tool") or step.inputs.get("tool_name") or step.operation
            tool_clean = tool_name.strip().lower() if (tool_name and isinstance(tool_name, str)) else None

            # 2. Fix B: Check if side effects are affirmatively ruled out (Read-Only)
            # Both operation classification and tool identity must be trusted and allowlisted
            is_read_only = False
            if op_type in READ_ONLY_OPERATIONS:
                if tool_clean and tool_clean in READ_ONLY_TOOLS:
                    if self.tool_registry and hasattr(self.tool_registry, "get_tool"):
                        reg_tool = self.tool_registry.get_tool(tool_clean)
                        is_read_only = (reg_tool is not None)
                    else:
                        is_read_only = True

            # 3. Fix B: Check if external idempotency is verified by trusted tool contract
            # NEVER trust caller-supplied step.inputs["is_idempotent"]
            has_verified_idempotency = False
            if tool_clean and self.tool_registry and hasattr(self.tool_registry, "get_tool"):
                reg_tool = self.tool_registry.get_tool(tool_clean)
                if reg_tool is not None:
                    metadata = getattr(reg_tool, "metadata", None)
                    is_contract_idempotent = bool(
                        getattr(metadata, "is_idempotent", False)
                        or getattr(reg_tool, "is_idempotent", False)
                    )
                    has_idempotency_key = bool(step.inputs.get("idempotency_key"))
                    if is_contract_idempotent and has_idempotency_key:
                        has_verified_idempotency = True

            # If not read-only and not externally idempotent, check Fix A: Valid Approved Continuation
            if not (is_read_only or has_verified_idempotency):
                is_valid_approved_continuation = False
                decision_dict = state.get("approval_decision")

                if decision_dict:
                    # Fix A: Distinguish approved continuation from crashed/abandoned task
                    task_id = state.get("task_id", f"{workflow_id}:step-{step_idx}")
                    exec_plan = state.get("execution_plan") or {}
                    plan_id = exec_plan.get("plan_id", f"plan-{workflow_id}")

                    # 1. Parse decision
                    try:
                        decision = ApprovalDecision.model_validate(decision_dict)
                    except Exception as e:
                        return {
                            "status": WorkflowStatus.FAILED.value,
                            "failure_class": OrchestratorFailureClass.POLICY_VIOLATION.value,
                            "error_info": {
                                "code": "INVALID_APPROVAL_DECISION",
                                "message": f"Malformed approval decision: {str(e)}",
                                "error_type": "InvalidApprovalError",
                            },
                            "next_route": "fail_workflow",
                        }

                    # 2. Check if approval was already claimed / consumed under a prior execution
                    is_prior_claim = bool(
                        state.get("approval_consumed") is True
                        or state.get("is_first_claim") is False
                        or state.get("is_recovery_claim") is True
                    )
                    if is_prior_claim:
                        return {
                            "status": WorkflowStatus.FAILED.value,
                            "failure_class": OrchestratorFailureClass.UNKNOWN_FAILURE.value,
                            "error_info": {
                                "code": "AMBIGUOUS_SIDE_EFFECT",
                                "message": (
                                    f"Approval '{decision.approval_id}' for step {step_idx} was previously claimed or resumed "
                                    "under an execution that crashed. External side effects cannot be ruled out and "
                                    "no verified external idempotency guarantee exists. Failing closed per replay safety policy."
                                ),
                                "error_type": "AmbiguousSideEffectError",
                            },
                            "next_route": "fail_workflow",
                        }

                    # 3. Resolve approval request (from state or authoritative reconstruction)
                    req_dict = state.get("approval_request")
                    if req_dict:
                        try:
                            req = ApprovalRequest.model_validate(req_dict)
                        except Exception:
                            req = None
                    else:
                        req = None

                    if req is None:
                        _, req = RiskGate.evaluate(
                            step=step,
                            task_id=task_id,
                            workflow_id=workflow_id,
                            requested_by=state.get("user_id"),
                            plan_hash=plan_hash,
                            plan_id=plan_id,
                        )
                        if req:
                            req.approval_id = decision.approval_id

                    if req and req.details:
                        req_claim_count = req.details.get("claim_count")
                        if req.details.get("is_recovery_claim") is True or (isinstance(req_claim_count, int) and req_claim_count > 1):
                            return {
                                "status": WorkflowStatus.FAILED.value,
                                "failure_class": OrchestratorFailureClass.UNKNOWN_FAILURE.value,
                                "error_info": {
                                    "code": "AMBIGUOUS_SIDE_EFFECT",
                                    "message": (
                                        f"Approval '{decision.approval_id}' for step {step_idx} was previously claimed or resumed "
                                        "under an execution that crashed. External side effects cannot be ruled out and "
                                        "no verified external idempotency guarantee exists. Failing closed per replay safety policy."
                                    ),
                                    "error_type": "AmbiguousSideEffectError",
                                },
                                "next_route": "fail_workflow",
                            }

                    # 4. Authoritative binding validation using RiskGate.validate_resume_decision
                    is_authorized, reason = RiskGate.validate_resume_decision(
                        decision=decision,
                        pending_request=req,
                        step_index=step_idx,
                        task_id=task_id,
                        step=step,
                        plan_hash=plan_hash,
                    )

                    if not is_authorized:
                        failure_class = (
                            OrchestratorFailureClass.HUMAN_APPROVAL_REJECTED.value
                            if reason == "human_approval_rejected"
                            else (
                                OrchestratorFailureClass.APPROVAL_TIMEOUT.value
                                if "expired" in (reason or "")
                                else OrchestratorFailureClass.POLICY_VIOLATION.value
                            )
                        )
                        return {
                            "status": WorkflowStatus.FAILED.value,
                            "failure_class": failure_class,
                            "error_info": {
                                "code": "APPROVAL_VALIDATION_FAILED",
                                "message": f"Approval validation failed for step {step_idx}: {reason}",
                                "error_type": "ApprovalValidationError",
                            },
                            "next_route": "fail_workflow",
                        }

                    # 4a. Expiration check on approval request (fails closed with APPROVAL_TIMEOUT)
                    if req and req.details.get("expires_at"):
                        try:
                            exp_dt = datetime.fromisoformat(req.details["expires_at"])
                            if exp_dt <= datetime.now(timezone.utc):
                                return {
                                    "status": WorkflowStatus.FAILED.value,
                                    "failure_class": OrchestratorFailureClass.APPROVAL_TIMEOUT.value,
                                    "error_info": {
                                        "code": "APPROVAL_EXPIRED",
                                        "message": f"Approval '{decision.approval_id}' for step {step_idx} has expired.",
                                        "error_type": "ApprovalExpiredError",
                                    },
                                    "next_route": "fail_workflow",
                                }
                        except Exception:
                            pass

                    # 4b. Tenant & Requested Action verification
                    req_tenant = req.details.get("tenant_id") if (req and req.details) else None
                    if req_tenant and req_tenant != tenant_id:
                        return {
                            "status": WorkflowStatus.FAILED.value,
                            "failure_class": OrchestratorFailureClass.POLICY_VIOLATION.value,
                            "error_info": {
                                "code": "TENANT_MISMATCH",
                                "message": f"Approval tenant mismatch: expected '{tenant_id}', got '{req_tenant}'.",
                                "error_type": "ApprovalValidationError",
                            },
                            "next_route": "fail_workflow",
                        }

                    expected_agent_str = step.agent_id.value if hasattr(step.agent_id, "value") else str(step.agent_id)
                    expected_action = f"Execute step {step_idx}: {step.description} via {expected_agent_str}"
                    if req and req.requested_action:
                        if not req.requested_action.startswith(f"Execute step {step_idx}"):
                            return {
                                "status": WorkflowStatus.FAILED.value,
                                "failure_class": OrchestratorFailureClass.POLICY_VIOLATION.value,
                                "error_info": {
                                    "code": "REQUESTED_ACTION_MISMATCH",
                                    "message": f"Approval action mismatch: expected step {step_idx}, got '{req.requested_action}'.",
                                    "error_type": "ApprovalValidationError",
                                },
                                "next_route": "fail_workflow",
                            }
                        if ":" in req.requested_action and req.requested_action != expected_action:
                            return {
                                "status": WorkflowStatus.FAILED.value,
                                "failure_class": OrchestratorFailureClass.POLICY_VIOLATION.value,
                                "error_info": {
                                    "code": "REQUESTED_ACTION_MISMATCH",
                                    "message": f"Approval action mismatch: expected '{expected_action}', got '{req.requested_action}'.",
                                    "error_type": "ApprovalValidationError",
                                },
                                "next_route": "fail_workflow",
                            }

                    # 4c. Authoritative Approval Repository verification if present
                    if self.approval_repo and hasattr(self.approval_repo, "get_model_by_id"):
                        try:
                            persisted_appr = await self.approval_repo.get_model_by_id(decision.approval_id, tenant_id)
                            if persisted_appr is None:
                                return {
                                    "status": WorkflowStatus.FAILED.value,
                                    "failure_class": OrchestratorFailureClass.POLICY_VIOLATION.value,
                                    "error_info": {
                                        "code": "APPROVAL_NOT_FOUND",
                                        "message": f"Approval '{decision.approval_id}' not found for tenant '{tenant_id}'.",
                                        "error_type": "ApprovalNotFoundError",
                                    },
                                    "next_route": "fail_workflow",
                                }
                            if persisted_appr.workflow_id != workflow_id:
                                return {
                                    "status": WorkflowStatus.FAILED.value,
                                    "failure_class": OrchestratorFailureClass.POLICY_VIOLATION.value,
                                    "error_info": {
                                        "code": "WORKFLOW_ID_MISMATCH",
                                        "message": f"Approval workflow mismatch: expected '{workflow_id}', got '{persisted_appr.workflow_id}'.",
                                        "error_type": "ApprovalValidationError",
                                    },
                                    "next_route": "fail_workflow",
                                }
                            if persisted_appr.step_index is not None and persisted_appr.step_index != step_idx:
                                return {
                                    "status": WorkflowStatus.FAILED.value,
                                    "failure_class": OrchestratorFailureClass.POLICY_VIOLATION.value,
                                    "error_info": {
                                        "code": "STEP_INDEX_MISMATCH",
                                        "message": f"Approval step mismatch: expected {step_idx}, got {persisted_appr.step_index}.",
                                        "error_type": "ApprovalValidationError",
                                    },
                                    "next_route": "fail_workflow",
                                }
                            if persisted_appr.task_id and persisted_appr.task_id != task_id:
                                return {
                                    "status": WorkflowStatus.FAILED.value,
                                    "failure_class": OrchestratorFailureClass.POLICY_VIOLATION.value,
                                    "error_info": {
                                        "code": "TASK_ID_MISMATCH",
                                        "message": f"Approval task mismatch: expected '{task_id}', got '{persisted_appr.task_id}'.",
                                        "error_type": "ApprovalValidationError",
                                    },
                                    "next_route": "fail_workflow",
                                }
                            if persisted_appr.status != ApprovalStatus.APPROVED.value:
                                return {
                                    "status": WorkflowStatus.FAILED.value,
                                    "failure_class": OrchestratorFailureClass.POLICY_VIOLATION.value,
                                    "error_info": {
                                        "code": "APPROVAL_NOT_APPROVED",
                                        "message": f"Persisted approval request '{decision.approval_id}' is not in APPROVED status: '{persisted_appr.status}'.",
                                        "error_type": "ApprovalInvalidStateError",
                                    },
                                    "next_route": "fail_workflow",
                                }
                            appr_details = persisted_appr.details if isinstance(getattr(persisted_appr, "details", None), dict) else {}
                            claim_count = appr_details.get("claim_count")
                            if appr_details.get("is_recovery_claim") is True or (isinstance(claim_count, int) and claim_count > 1):
                                is_prior_claim = True
                            elif getattr(persisted_appr, "resume_status", None) == "resumed" and not state.get("is_first_claim", False) and claim_count != 1:
                                is_prior_claim = True

                            if is_prior_claim:
                                return {
                                    "status": WorkflowStatus.FAILED.value,
                                    "failure_class": OrchestratorFailureClass.UNKNOWN_FAILURE.value,
                                    "error_info": {
                                        "code": "AMBIGUOUS_SIDE_EFFECT",
                                        "message": (
                                            f"Persisted approval request '{decision.approval_id}' was previously claimed or resumed "
                                            "under an execution that crashed. External side effects cannot be ruled out and "
                                            "no verified external idempotency guarantee exists. Failing closed per replay safety policy."
                                        ),
                                        "error_type": "AmbiguousSideEffectError",
                                    },
                                    "next_route": "fail_workflow",
                                }
                        except Exception as e:
                            logger.warning(f"Authoritative approval repository check warning: {e}")


                    # 6. Authoritative repository check if repo is present
                    if self.workflow_repo and hasattr(self.workflow_repo, "get_workflow"):
                        try:
                            wf = await self.workflow_repo.get_workflow(workflow_id, tenant_id)
                            if wf and isinstance(getattr(wf, "plan_hash", None), str):
                                if plan_hash and wf.plan_hash != plan_hash:
                                    return {
                                        "status": WorkflowStatus.FAILED.value,
                                        "failure_class": OrchestratorFailureClass.POLICY_VIOLATION.value,
                                        "error_info": {
                                            "code": "PLAN_HASH_MISMATCH",
                                            "message": f"Plan hash mismatch on resume: workflow '{wf.plan_hash}' != state '{plan_hash}'",
                                            "error_type": "PlanIntegrityError",
                                        },
                                        "next_route": "fail_workflow",
                                    }
                                meta = wf.execution_metadata if isinstance(getattr(wf, "execution_metadata", None), dict) else {}
                                claim_count = meta.get("approval_claim_count")
                                if meta.get("is_first_resume_claim") is False or (isinstance(claim_count, int) and claim_count > 1):
                                    return {
                                        "status": WorkflowStatus.FAILED.value,
                                        "failure_class": OrchestratorFailureClass.UNKNOWN_FAILURE.value,
                                        "error_info": {
                                            "code": "AMBIGUOUS_SIDE_EFFECT",
                                            "message": (
                                                f"Workflow metadata indicates approval for step {step_idx} was previously claimed or resumed "
                                                "under an execution that crashed. External side effects cannot be ruled out and "
                                                "no verified external idempotency guarantee exists. Failing closed per replay safety policy."
                                            ),
                                            "error_type": "AmbiguousSideEffectError",
                                        },
                                        "next_route": "fail_workflow",
                                    }
                        except Exception as e:
                            logger.warning(f"Workflow repository check warning during resume: {e}")

                    # 7. Fail closed if this approval was previously claimed/resumed under a crashed execution
                    if is_prior_claim:
                        return {
                            "status": WorkflowStatus.FAILED.value,
                            "failure_class": OrchestratorFailureClass.UNKNOWN_FAILURE.value,
                            "error_info": {
                                "code": "AMBIGUOUS_SIDE_EFFECT",
                                "message": (
                                    f"Approval '{decision.approval_id}' for step {step_idx} was previously claimed or resumed "
                                    "under an execution that crashed. External side effects cannot be ruled out and "
                                    "no verified external idempotency guarantee exists. Failing closed per replay safety policy."
                                ),
                                "error_type": "AmbiguousSideEffectError",
                            },
                            "next_route": "fail_workflow",
                        }

                    is_valid_approved_continuation = True


                if not is_valid_approved_continuation:
                    return {
                        "status": WorkflowStatus.FAILED.value,
                        "failure_class": OrchestratorFailureClass.UNKNOWN_FAILURE.value,
                        "error_info": {
                            "code": "AMBIGUOUS_SIDE_EFFECT",
                            "message": (
                                f"Step {step_idx} with operation '{op_type}' was interrupted under a previous execution. "
                                "External side effects cannot be ruled out and no valid approval or external idempotency "
                                "is established. Failing closed per replay safety policy."
                            ),
                            "error_type": "AmbiguousSideEffectError",
                        },
                        "next_route": "fail_workflow",
                    }

        # Check dependencies
        for dep in step.dependencies:
            if dep not in completed_steps:
                return {
                    "status": WorkflowStatus.FAILED.value,
                    "failure_class": OrchestratorFailureClass.UNKNOWN_FAILURE.value,
                    "error_info": {
                        "code": "DEPENDENCY_ERROR",
                        "message": f"Dependency step {dep} has not completed successfully for step {step_idx}.",
                        "error_type": "DependencyError",
                    },
                    "next_route": "fail_workflow",
                }

        # Emit step_started event only when step is cleared to execute
        await self._emit_stream_event(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            epoch=fencing_epoch or 1,
            step_index=step_idx,
            event_type=StreamEventType.STEP_STARTED,
            payload={
                "description": step.description,
                "assigned_agent_id": step.agent_id.value,
                "dependencies": sorted(step.dependencies),
                "risk_level": step.risk_level.value if hasattr(step.risk_level, "value") else str(step.risk_level),
            },
            discriminator="start",
        )

        return {
            "current_step_index": step_idx,
            "current_revision": current_revision,
            "is_step_fast_forwarded": False,
            "next_route": "evaluate_risk_gate",
        }

    # -------------------------------------------------------------------------
    # Node 4: evaluate_risk_gate
    # -------------------------------------------------------------------------
    async def node_evaluate_risk_gate(self, state: GraphWorkflowState) -> Dict[str, Any]:
        """
        Evaluates step risk level using deterministic Section 9.2 rules.
        Decoupled from crash recovery: risk evaluates approval requirement, not mutation.
        """
        cursor = state.get("step_cursor", 0)
        steps = state.get("steps") or []
        step = PlanStep.model_validate(steps[cursor])
        workflow_id = state.get("workflow_id", "")
        task_id = state.get("task_id") or f"{workflow_id}:step-{step.step_index}"
        plan_hash = state.get("plan_hash", "")
        exec_plan = state.get("execution_plan") or {}
        plan_id = exec_plan.get("plan_id", f"plan-{workflow_id}")

        decision, approval_req = RiskGate.evaluate(
            step=step,
            task_id=task_id,
            workflow_id=workflow_id,
            requested_by=state.get("user_id"),
            plan_hash=plan_hash,
            plan_id=plan_id,
        )

        if decision == RiskGateDecision.PAUSE:
            # Check for matching resume decision
            approval_decision_dict = state.get("approval_decision")
            approval_decision = (
                ApprovalDecision.model_validate(approval_decision_dict)
                if approval_decision_dict
                else None
            )

            # Resolve pending_request: check state first, or bind reconstructed approval_id if present
            pending_req = None
            if state.get("approval_request"):
                try:
                    pending_req = ApprovalRequest.model_validate(state["approval_request"])
                except Exception:
                    pass
            if pending_req is None:
                pending_req = approval_req
                if approval_decision and pending_req:
                    pending_req.approval_id = approval_decision.approval_id

            is_authorized, reason = RiskGate.validate_resume_decision(
                decision=approval_decision,
                pending_request=pending_req,
                step_index=step.step_index,
                task_id=task_id,
                step=step,
                plan_hash=plan_hash,
            )

            if is_authorized:
                return {
                    "approval_decision": approval_decision.model_dump() if approval_decision else None,
                    "approval_request": None,
                    "status": WorkflowStatus.RUNNING.value,
                    "next_route": "dispatch_step",
                }
            elif reason == "human_approval_rejected":
                return {
                    "status": WorkflowStatus.FAILED.value,
                    "failure_class": OrchestratorFailureClass.HUMAN_APPROVAL_REJECTED.value,
                    "error_info": {
                        "code": "HUMAN_APPROVAL_REJECTED",
                        "message": "Human approval was rejected by approver.",
                        "error_type": "ApprovalRejectedError",
                    },
                    "next_route": "fail_workflow",
                }
            else:
                return {
                    "approval_request": approval_req.model_dump() if approval_req else None,
                    "status": WorkflowStatus.WAITING_APPROVAL.value,
                    "next_route": "pause_for_approval",
                }

        return {"next_route": "dispatch_step"}

    # -------------------------------------------------------------------------
    # Node 5: pause_for_approval
    # -------------------------------------------------------------------------
    async def node_pause_for_approval(self, state: GraphWorkflowState) -> Dict[str, Any]:
        """
        Pauses workflow state, commits ApprovalRequest to PostgreSQL, emits WAITING_APPROVAL,
        and releases the execution lease.
        """
        workflow_id = state["workflow_id"]
        tenant_id = state.get("tenant_id", "default")
        fencing_epoch = state.get("fencing_epoch", 1)
        lock_token = state.get("lock_token", "")
        current_revision = state.get("current_revision", 1)
        step_idx = state.get("current_step_index", -1)
        raw_req = state.get("approval_request") or {}

        if self.workflow_repo and lock_token:
            exp_str = raw_req.get("details", {}).get("expires_at")
            expires_at = (
                datetime.fromisoformat(exp_str)
                if exp_str
                else (datetime.now(timezone.utc) + timedelta(hours=24))
            )
            try:
                new_rev = await self.workflow_repo.pause_workflow_for_approval(
                    workflow_id=workflow_id,
                    tenant_id=tenant_id,
                    step_index=step_idx,
                    approval_id=raw_req.get("approval_id", f"appr-{uuid.uuid4().hex[:12]}"),
                    requested_action=raw_req.get("requested_action", "execution"),
                    risk_level=raw_req.get("risk_level", "HIGH"),
                    details=raw_req.get("details", {}),
                    expires_at=expires_at,
                    fencing_epoch=fencing_epoch,
                    lock_token=lock_token,
                    expected_revision=current_revision,
                )
                current_revision = new_rev
            except FencingLeaseLostError:
                raise
            except Exception as e:
                logger.error(f"Failed to record pause in repository for {workflow_id}: {e}")
                return {
                    "status": WorkflowStatus.FAILED.value,
                    "failure_class": OrchestratorFailureClass.DATABASE_UNAVAILABLE.value,
                    "error_info": {
                        "code": "APPROVAL_PERSISTENCE_FAILED",
                        "message": f"Failed to record approval pause in repository: {str(e)}",
                        "error_type": type(e).__name__,
                    },
                    "next_route": "fail_workflow",
                }

        # Fix B: Emit WAITING_APPROVAL event ONLY AFTER successful durable persistence.
        # Note: Post-commit publication still has an inherent process crash window
        # and does not guarantee durable or exactly-once delivery without an outbox table.
        await self._emit_stream_event(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            epoch=fencing_epoch or 1,
            step_index=step_idx,
            event_type=StreamEventType.WAITING_APPROVAL,
            payload={
                "approval_id": raw_req.get("approval_id"),
                "step_index": step_idx,
                "requested_action": raw_req.get("requested_action"),
                "risk_level": raw_req.get("risk_level"),
                "details": raw_req.get("details", {}),
            },
            discriminator="pause",
        )

        return {
            "status": WorkflowStatus.WAITING_APPROVAL.value,
            "current_revision": current_revision,
            "next_route": "END",
        }

    # -------------------------------------------------------------------------
    # Node 6: dispatch_step
    # -------------------------------------------------------------------------
    async def node_dispatch_step(self, state: GraphWorkflowState) -> Dict[str, Any]:
        """
        Enforces RBAC, assembles redacted inputs, and dispatches agent via AgentRegistry with retries.
        """
        cursor = state.get("step_cursor", 0)
        steps = state.get("steps") or []
        step = PlanStep.model_validate(steps[cursor])
        step_idx = step.step_index
        user_context = state.get("user_context") or {}
        completed_steps = state.get("completed_steps") or {}

        # 1. RBAC Check
        roles = user_context.get("roles", [])
        permitted_agents = set()
        for r in roles:
            permitted_agents.update(ROLE_PERMITTED_AGENTS.get(r, []))
        if not roles:
            permitted_agents = set(AgentID)

        if step.agent_id not in permitted_agents:
            return {
                "status": WorkflowStatus.FAILED.value,
                "failure_class": OrchestratorFailureClass.PERMISSION_DENIED.value,
                "error_info": {
                    "code": "PERMISSION_DENIED",
                    "message": f"User roles {roles} not permitted to dispatch agent '{step.agent_id.value}'.",
                    "error_type": "PermissionError",
                },
                "last_dispatch_success": False,
                "next_route": "handle_step_failure",
            }

        # 2. Context assembly with redaction
        assembled_inputs = dict(step.inputs)
        for dep in step.dependencies:
            if dep in completed_steps:
                assembled_inputs[f"step_{dep}_output"] = redact_sensitive_data(completed_steps[dep])

        # 3. Agent Dispatch with retries
        timer = Timer()
        timer.start()
        attempt = 0
        backoff = self.initial_backoff_seconds
        last_error = None
        dispatch_success = False
        step_output = {}

        while attempt <= self.max_retries:
            attempt += 1
            try:
                exec_ctx = AgentExecutionContext(
                    execution_id=f"step-exec-{uuid.uuid4().hex[:8]}",
                    workflow_id=state["workflow_id"],
                    task_id=state.get("task_id", ""),
                    agent_id=step.agent_id,
                    request_id=state.get("request_id", ""),
                    correlation_id=state.get("correlation_id", ""),
                    input_payload=assembled_inputs,
                )
                result = await self.registry.dispatch(exec_ctx)
                if result.status == TaskStatus.COMPLETED:
                    dispatch_success = True
                    step_output = result.output_payload or {}
                    break
                else:
                    last_error = result.error
            except Exception as e:
                last_error = ErrorInfo(code="STEP_EXCEPTION", message=str(e), error_type=type(e).__name__)

            if attempt <= self.max_retries:
                await asyncio.sleep(backoff)
                backoff *= self.backoff_multiplier

        duration = timer.stop()

        step_record = {
            "step_index": step_idx,
            "agent_id": step.agent_id.value,
            "status": TaskStatus.COMPLETED.value if dispatch_success else TaskStatus.FAILED.value,
            "output_data": step_output,
            "duration_ms": duration,
            "error": last_error.model_dump() if last_error else None,
        }

        records = dict(state.get("step_execution_records") or {})
        records[step_idx] = step_record

        consumed = True if state.get("approval_decision") else state.get("approval_consumed")

        if dispatch_success:
            return {
                "step_execution_records": records,
                "last_dispatch_success": True,
                "last_step_output": step_output,
                "approval_consumed": consumed,
                "next_route": "record_step_success",
            }
        else:
            return {
                "step_execution_records": records,
                "last_dispatch_success": False,
                "approval_consumed": consumed,
                "error_info": last_error.model_dump() if last_error else {"code": "STEP_FAILED", "message": "Failed"},
                "next_route": "handle_step_failure",
            }

    # -------------------------------------------------------------------------
    # Node 7: record_step_success
    # -------------------------------------------------------------------------
    async def node_record_step_success(self, state: GraphWorkflowState) -> Dict[str, Any]:
        """
        Commits task completion and authentic provenance into PostgreSQL.
        Emits STEP_COMPLETED event and advances cursor.
        """
        workflow_id = state["workflow_id"]
        tenant_id = state.get("tenant_id", "default")
        fencing_epoch = state.get("fencing_epoch", 1)
        lock_token = state.get("lock_token", "")
        current_revision = state.get("current_revision", 1)
        cursor = state.get("step_cursor", 0)
        steps = state.get("steps") or []
        step = PlanStep.model_validate(steps[cursor])
        step_idx = step.step_index
        step_output = state.get("last_step_output") or {}

        completed_steps = dict(state.get("completed_steps") or {})
        completed_steps[step_idx] = step_output

        prov_entry = {
            "step_index": step_idx,
            "agent_id": step.agent_id.value,
            "action_summary": step.description,
            "status": "COMPLETED",
            "result_reference": f"step_{step_idx}_completed",
        }
        provenance_chain = list(state.get("provenance_chain") or [])
        provenance_chain.append(prov_entry)

        if self.workflow_repo and lock_token:
            try:
                new_rev = await self.workflow_repo.complete_task_step(
                    workflow_id=workflow_id,
                    tenant_id=tenant_id,
                    step_index=step_idx,
                    output_data=redact_sensitive_data(step_output),
                    provenance=prov_entry,
                    fencing_epoch=fencing_epoch,
                    lock_token=lock_token,
                    expected_revision=current_revision,
                )
                current_revision = new_rev
            except FencingLeaseLostError:
                raise
            except Exception as e:
                logger.error(f"Failed to commit task step completion for {workflow_id} step {step_idx}: {e}")
                return {
                    "status": WorkflowStatus.FAILED.value,
                    "failure_class": OrchestratorFailureClass.DATABASE_UNAVAILABLE.value,
                    "error_info": {
                        "code": "STEP_COMPLETION_PERSISTENCE_FAILED",
                        "message": f"Failed to persist task step completion in repository: {str(e)}",
                        "error_type": type(e).__name__,
                    },
                    "next_route": "fail_workflow",
                }

        # Fix B: Emit STEP_COMPLETED event ONLY AFTER successful durable persistence.
        # Note: Post-commit publication still has an inherent process crash window
        # and does not guarantee durable or exactly-once delivery without an outbox table.
        await self._emit_stream_event(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            epoch=fencing_epoch or 1,
            step_index=step_idx,
            event_type=StreamEventType.STEP_COMPLETED,
            payload={"status": "COMPLETED", "summary": step.description},
            discriminator="done",
        )

        return {
            "completed_steps": completed_steps,
            "provenance_chain": provenance_chain,
            "current_revision": current_revision,
            "step_cursor": cursor + 1,
            "next_route": "check_more_steps",
        }

    # -------------------------------------------------------------------------
    # Node 8: handle_step_failure
    # -------------------------------------------------------------------------
    async def node_handle_step_failure(self, state: GraphWorkflowState) -> Dict[str, Any]:
        """
        Handles permanent step failure: skips optional steps, routes prohibited classes to fail,
        or triggers contextual re-planning.
        """
        cursor = state.get("step_cursor", 0)
        steps = state.get("steps") or []
        step = PlanStep.model_validate(steps[cursor])
        step_idx = step.step_index
        err_info = state.get("error_info") or {}
        replan_count = state.get("replan_count", 0)
        fencing_epoch = state.get("fencing_epoch", 1)

        await self._emit_stream_event(
            workflow_id=state["workflow_id"],
            tenant_id=state.get("tenant_id", "default"),
            epoch=fencing_epoch or 1,
            step_index=step_idx,
            event_type=StreamEventType.STEP_FAILED,
            payload={"error": err_info.get("code"), "message": err_info.get("message")},
            discriminator="fail",
        )

        if step.optional:
            return {
                "has_partial_result": True,
                "step_cursor": cursor + 1,
                "next_route": "check_more_steps",
            }

        # Map failure class
        code = (err_info.get("code") or "").lower()
        if "permission" in code:
            failure_class = OrchestratorFailureClass.PERMISSION_DENIED.value
        elif "policy" in code:
            failure_class = OrchestratorFailureClass.POLICY_VIOLATION.value
        elif "approval" in code:
            failure_class = OrchestratorFailureClass.HUMAN_APPROVAL_REJECTED.value
        else:
            failure_class = OrchestratorFailureClass.UNKNOWN_FAILURE.value

        if failure_class in PROHIBITED_REPLAN_CLASSES:
            return {
                "status": WorkflowStatus.FAILED.value,
                "failure_class": failure_class,
                "next_route": "fail_workflow",
            }

        if replan_count < self.max_replan_attempts:
            return {
                "failure_class": failure_class,
                "next_route": "contextual_replan",
            }
        else:
            return {
                "status": WorkflowStatus.FAILED.value,
                "failure_class": failure_class,
                "error_info": {
                    "code": "MAX_REPLANS_EXHAUSTED",
                    "message": f"Step {step_idx} failed and max replans ({self.max_replan_attempts}) exhausted.",
                },
                "next_route": "fail_workflow",
            }

    # -------------------------------------------------------------------------
    # Node 9: contextual_replan
    # -------------------------------------------------------------------------
    async def node_contextual_replan(self, state: GraphWorkflowState) -> Dict[str, Any]:
        """
        Dispatches contextual re-planning to PlannerAgent. Replaces remaining steps in state.
        Atomically persists the revised execution plan, canonical plan hash, durable replan
        counter, and replan metadata to PostgreSQL while preserving completed steps.
        """
        cursor = state.get("step_cursor", 0)
        replan_count = state.get("replan_count", 0) + 1
        workflow_id = state["workflow_id"]
        tenant_id = state.get("tenant_id", "default")
        fencing_epoch = state.get("fencing_epoch", 1)
        lock_token = state.get("lock_token", "")
        current_revision = state.get("current_revision", 1)
        task_id = state.get("task_id", "")
        user_context = state.get("user_context") or {}
        failed_step_index = state.get("current_step_index", 0)

        # 1. Enforce replan budget
        if replan_count > self.max_replan_attempts:
            return {
                "status": WorkflowStatus.FAILED.value,
                "failure_class": OrchestratorFailureClass.UNKNOWN_FAILURE.value,
                "error_info": {
                    "code": "MAX_REPLANS_EXHAUSTED",
                    "message": f"Step {failed_step_index} failed and max replans ({self.max_replan_attempts}) exhausted.",
                },
                "replan_count": replan_count,
                "next_route": "fail_workflow",
            }

        # 2. Dispatch replan to PlannerAgent
        completed_steps_records = state.get("completed_steps") or {}
        replan_input = {
            "task_id": task_id,
            "task_description": state.get("task_description", "replan"),
            "failed_step_index": failed_step_index,
            "failure_error": state.get("error_info"),
            "completed_steps_records": completed_steps_records,
            "user_context": user_context,
            "is_replan": True,
        }

        roles = user_context.get("roles") or user_context.get("role") or ["orchestrator", "admin"]
        exec_ctx = AgentExecutionContext(
            execution_id=f"replan-exec-{uuid.uuid4().hex[:8]}",
            workflow_id=workflow_id,
            task_id=task_id,
            agent_id=AgentID.PLANNER,
            request_id=state.get("request_id", ""),
            correlation_id=state.get("correlation_id", ""),
            input_payload=replan_input,
            metadata={
                "is_authenticated": True,
                "is_trusted": True,
                "user_context": user_context,
                "roles": roles,
            },
        )

        result = await self.registry.dispatch(exec_ctx)
        if result.status != TaskStatus.COMPLETED or not result.output_payload:
            return {
                "status": WorkflowStatus.FAILED.value,
                "failure_class": OrchestratorFailureClass.PLAN_VALIDATION_FAILED.value,
                "error_info": {"code": "REPLANNING_FAILED", "message": "Contextual replanning failed"},
                "replan_count": replan_count,
                "next_route": "fail_workflow",
            }

        planner_out = PlannerOutput.model_validate(result.output_payload)
        sub_plan = planner_out.plan
        replacement_steps = list(sub_plan.steps)

        # 3. Construct Whole-Workflow Revised Plan
        # Preserve completed steps 0 .. failed_step_index - 1
        orig_steps_raw = state.get("steps") or []
        whole_steps: List[PlanStep] = []

        for idx in range(failed_step_index):
            if idx < len(orig_steps_raw):
                s = PlanStep.model_validate(orig_steps_raw[idx])
                whole_steps.append(s)

        # Place replacement step(s) starting at failed_step_index
        next_step_idx = failed_step_index
        orig_failed_step = None
        if failed_step_index < len(orig_steps_raw):
            orig_failed_step = PlanStep.model_validate(orig_steps_raw[failed_step_index])

        for r_step in replacement_steps:
            r_step_dict = r_step.model_dump()
            r_step_dict["step_index"] = next_step_idx
            if orig_failed_step and orig_failed_step.dependencies:
                r_step_dict["dependencies"] = [d for d in orig_failed_step.dependencies if d < next_step_idx]
            elif next_step_idx > 0 and not r_step_dict.get("dependencies"):
                r_step_dict["dependencies"] = [next_step_idx - 1]
            whole_steps.append(PlanStep.model_validate(r_step_dict))
            next_step_idx += 1

        # Preserve and re-index any downstream steps
        for idx in range(failed_step_index + 1, len(orig_steps_raw)):
            downstream_dict = PlanStep.model_validate(orig_steps_raw[idx]).model_dump()
            downstream_dict["step_index"] = next_step_idx
            new_deps = []
            for dep in downstream_dict.get("dependencies", []):
                if dep == failed_step_index:
                    new_deps.append(failed_step_index)
                elif dep < failed_step_index:
                    new_deps.append(dep)
                else:
                    offset = len(replacement_steps) - 1
                    new_deps.append(dep + offset)
            downstream_dict["dependencies"] = new_deps
            whole_steps.append(PlanStep.model_validate(downstream_dict))
            next_step_idx += 1

        plan_type = PlanType.SINGLE_AGENT if len(whole_steps) == 1 else PlanType.MULTI_AGENT
        whole_plan = ExecutionPlan(
            plan_id=f"replan-{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            plan_type=plan_type,
            steps=whole_steps,
            planning_rationale=(
                f"Whole-workflow contextual replan replacing step {failed_step_index} "
                f"with {whole_steps[failed_step_index].agent_id.value} while preserving "
                f"{failed_step_index} completed steps."
            ),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # 4. Validate whole plan
        try:
            trusted_roles = (
                user_context.get("roles")
                or user_context.get("role")
                or ["orchestrator", "admin"]
            )
            self.plan_validator.validate_plan(
                plan=whole_plan,
                trusted_roles=trusted_roles,
                enforce_registry=self.registry is not None,
            )
        except PlanValidationError as exc:
            return {
                "status": WorkflowStatus.FAILED.value,
                "failure_class": OrchestratorFailureClass.PLAN_VALIDATION_FAILED.value,
                "error_info": {"code": "REPLAN_VALIDATION_FAILED", "message": exc.message},
                "replan_count": replan_count,
                "next_route": "fail_workflow",
            }

        new_hash = compute_plan_hash(whole_plan)
        steps_list = [s.model_dump() for s in whole_plan.steps]

        # 5. Persist atomically to repository
        if self.workflow_repo:
            try:
                new_revision = await self.workflow_repo.update_workflow_plan(
                    workflow_id=workflow_id,
                    tenant_id=tenant_id,
                    execution_plan=whole_plan.model_dump(),
                    plan_hash=new_hash,
                    replan_count=replan_count,
                    replan_metadata={
                        "failed_step_index": failed_step_index,
                        "failure_error": state.get("error_info"),
                        "replacement_agent": whole_steps[failed_step_index].agent_id.value,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    current_step_index=failed_step_index,
                    fencing_epoch=fencing_epoch,
                    lock_token=lock_token,
                    expected_revision=current_revision,
                )
                current_revision = new_revision
            except FencingLeaseLostError:
                raise
            except ConcurrencyConflictError as c_err:
                return {
                    "status": WorkflowStatus.FAILED.value,
                    "failure_class": OrchestratorFailureClass.UNKNOWN_FAILURE.value,
                    "error_info": {"code": "CONCURRENCY_CONFLICT", "message": str(c_err)},
                    "replan_count": replan_count,
                    "next_route": "fail_workflow",
                }
            except Exception as exc:
                return {
                    "status": WorkflowStatus.FAILED.value,
                    "failure_class": OrchestratorFailureClass.UNKNOWN_FAILURE.value,
                    "error_info": {"code": "PLAN_PERSISTENCE_ERROR", "message": str(exc)},
                    "replan_count": replan_count,
                    "next_route": "fail_workflow",
                }

        # 6. Return updated state
        return {
            "execution_plan": whole_plan.model_dump(),
            "plan_hash": new_hash,
            "steps": steps_list,
            "step_cursor": 0,
            "current_step_index": failed_step_index,
            "current_revision": current_revision,
            "replan_count": replan_count,
            "next_route": "prepare_step",
        }

    # -------------------------------------------------------------------------
    # Node 10: reviewer_gate
    # -------------------------------------------------------------------------
    async def node_reviewer_gate(self, state: GraphWorkflowState) -> Dict[str, Any]:
        """
        Executes ReviewerValidation quality gate across completed step outputs.
        """
        completed_steps = state.get("completed_steps") or {}
        workflow_id = state["workflow_id"]
        task_id = state.get("task_id", "")

        aggregated_output = {str(k): v for k, v in completed_steps.items()}

        reviewer_input = ReviewerInput(
            task_id=task_id,
            assembled_output=aggregated_output,
            step_outputs=[v for v in completed_steps.values() if isinstance(v, dict)],
        )

        review_res = self.reviewer_engine.evaluate(reviewer_input)

        if review_res.recommendation == ReviewRecommendation.APPROVE:
            return {"next_route": "finalize_workflow"}
        elif review_res.recommendation == ReviewRecommendation.ESCALATE:
            review_req = ApprovalRequest(
                approval_id=f"appr-rev-{uuid.uuid4().hex[:8]}",
                workflow_id=workflow_id,
                task_id=task_id,
                requested_action="Reviewer Escalation",
                risk_level=RiskLevel.HIGH,
                requested_at=datetime.now(timezone.utc).isoformat(),
                details={"findings": [f.model_dump() for f in review_res.findings]},
            )
            return {
                "approval_request": review_req.model_dump(),
                "status": WorkflowStatus.WAITING_APPROVAL.value,
                "next_route": "pause_for_approval",
            }
        else:
            return {
                "status": WorkflowStatus.FAILED.value,
                "failure_class": OrchestratorFailureClass.REVIEWER_REJECTED.value,
                "error_info": {
                    "code": "REVIEWER_REJECTED",
                    "message": "Reviewer validation rejected the final output.",
                    "error_type": "ReviewerRejectionError",
                },
                "next_route": "fail_workflow",
            }

    # -------------------------------------------------------------------------
    # Node 11: finalize_workflow
    # -------------------------------------------------------------------------
    async def node_finalize_workflow(self, state: GraphWorkflowState) -> Dict[str, Any]:
        """
        Releases execution lease with status COMPLETED and constructs FinalResponse.
        """
        workflow_id = state["workflow_id"]
        tenant_id = state.get("tenant_id", "default")
        fencing_epoch = state.get("fencing_epoch", 1)
        lock_token = state.get("lock_token", "")
        completed_steps = state.get("completed_steps") or {}
        provenance_chain = state.get("provenance_chain") or []
        has_partial = state.get("has_partial_result", False)

        if self.workflow_repo and lock_token and tenant_id:
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

        await self._emit_stream_event(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            epoch=fencing_epoch or 1,
            step_index=-1,
            event_type=StreamEventType.WORKFLOW_COMPLETED,
            payload={"status": "COMPLETED", "has_partial_result": has_partial},
            discriminator="complete",
        )

        final_resp = FinalResponse(
            task_id=state.get("task_id", ""),
            status=WorkflowStatus.COMPLETED,
            result=redact_sensitive_data({str(k): v for k, v in completed_steps.items()}),
            provenance_summary=[p for p in provenance_chain],
            partial_result=has_partial,
        )

        return {
            "status": WorkflowStatus.COMPLETED.value,
            "final_response": final_resp.model_dump(),
            "next_route": "END",
        }

    # -------------------------------------------------------------------------
    # Node 12: fail_workflow
    # -------------------------------------------------------------------------
    async def node_fail_workflow(self, state: GraphWorkflowState) -> Dict[str, Any]:
        """
        Releases execution lease with status FAILED and emits WORKFLOW_FAILED event.
        """
        workflow_id = state["workflow_id"]
        tenant_id = state.get("tenant_id", "default")
        fencing_epoch = state.get("fencing_epoch", 1)
        lock_token = state.get("lock_token", "")
        err = state.get("error_info") or {}

        if self.workflow_repo and lock_token and tenant_id:
            try:
                await self.workflow_repo.release_execution_lease(
                    workflow_id=workflow_id,
                    tenant_id=tenant_id,
                    fencing_epoch=fencing_epoch,
                    lock_token=lock_token,
                    final_status=WorkflowStatus.FAILED.value,
                    error=err,
                )
            except Exception:
                pass

        await self._emit_stream_event(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            epoch=fencing_epoch or 1,
            step_index=-1,
            event_type=StreamEventType.WORKFLOW_FAILED,
            payload={
                "status": "FAILED",
                "failure_class": state.get("failure_class", "UNKNOWN_FAILURE"),
                "reason": err.get("message", "Workflow failed"),
            },
            discriminator="fail",
        )

        return {
            "status": WorkflowStatus.FAILED.value,
            "next_route": "END",
        }
