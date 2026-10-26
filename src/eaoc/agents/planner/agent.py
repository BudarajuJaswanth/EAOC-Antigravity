"""
EAOC Planner Agent Implementation
Specialized task decomposition and plan generation agent (Agent 2, AGT-PLAN):
- Analyzes task requirements and generates structured ExecutionPlans (FR-PLAN-001)
- Validates ExecutionPlans against AgentRegistry and RBAC policies (FR-PLAN-002)
- Classifies tasks into single_agent or multi_agent execution graphs (FR-PLAN-003)
- Performs dynamic agent selection based on task intent (FR-ADAP-001)
- Supports contextual re-planning for failed steps (FR-ADAP-002, PATH-H)
- Applies Section 9.1 deterministic risk rules to all plan steps
- Emits structured audit events across all lifecycle phases
- Preserves confidential data via sensitive data redaction
- Never executes steps, accesses domain stores, or invokes domain tools
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import uuid

from eaoc.agents.base import BaseAgent
from eaoc.agents.planner.engine import PlannerEngine
from eaoc.agents.planner.schemas import (
    ExecutionPlan,
    PlannerInput,
    PlannerOutput,
    PlanValidationError,
)
from eaoc.agents.planner.validator import PlanValidator, ROLE_PERMITTED_AGENTS
from eaoc.agents.registry import AgentRegistry
from eaoc.observability import get_logger, redact_sensitive_data
from eaoc.schemas.domain import AgentExecutionContext, AgentID

logger = get_logger("eaoc.agents.planner")

# Roles authorized to invoke the Planner Agent
AUTHORIZED_CALLER_ROLES: Set[str] = {
    "admin",
    "system",
    "orchestrator",
    "analyst",
    "finance_user",
    "hr_user",
    "it_user",
    "developer",
}


class PlannerAgent(BaseAgent[PlannerInput, PlannerOutput]):
    """
    Planner Agent (AGT-PLAN).
    Decomposes task intent into validated ExecutionPlans without executing steps or side effects.
    """

    agent_id: AgentID = AgentID.PLANNER
    input_schema = PlannerInput
    output_schema = PlannerOutput

    def __init__(
        self,
        agent_registry: Optional[AgentRegistry] = None,
        validator: Optional[PlanValidator] = None,
        default_timeout_seconds: Optional[float] = 10.0,
        **kwargs,
    ):
        super().__init__(
            agent_id=AgentID.PLANNER,
            input_schema=PlannerInput,
            output_schema=PlannerOutput,
            default_timeout_seconds=default_timeout_seconds,
            **kwargs,
        )
        self.agent_registry = agent_registry
        self.validator = validator or PlanValidator(agent_registry=agent_registry)

    def _verify_trusted_identity(
        self, context: AgentExecutionContext
    ) -> Set[str]:
        """
        Validates authorization against trusted execution context metadata.
        Strictly ignores unauthenticated payload claims to prevent role spoofing.
        Fails closed (denies by default) if context is missing, unauthenticated, or untrusted.
        """
        if not context or not context.metadata:
            logger.warning(
                "Planner access denied: Execution context metadata is missing.",
                extra={
                    "event_name": "planner_access_denied",
                    "execution_id": getattr(context, "execution_id", "unknown"),
                    "reason": "missing_metadata",
                },
            )
            raise PermissionError("Access denied: Trusted execution context metadata is missing.")

        # Explicitly deny if context is marked unauthenticated or untrusted
        if (
            context.metadata.get("is_authenticated") is False
            or context.metadata.get("authenticated") is False
            or context.metadata.get("is_trusted") is False
        ):
            logger.warning(
                "Planner access denied: Execution context is unauthenticated or untrusted.",
                extra={
                    "event_name": "planner_access_denied",
                    "execution_id": getattr(context, "execution_id", "unknown"),
                    "reason": "unauthenticated",
                },
            )
            raise PermissionError("Access denied: Execution context is not authenticated or trusted.")

        user_context = context.metadata.get("user_context", {})
        if not isinstance(user_context, dict):
            user_context = {}

        # Extract roles from trusted context
        roles: Set[str] = set()
        raw_roles = (
            user_context.get("roles")
            or user_context.get("role")
            or context.metadata.get("roles")
            or context.metadata.get("role")
            or context.metadata.get("user_role")
        )

        if isinstance(raw_roles, str) and raw_roles.strip():
            roles.add(raw_roles.strip().lower())
        elif isinstance(raw_roles, (list, set, tuple)):
            for r in raw_roles:
                if isinstance(r, str) and r.strip():
                    roles.add(r.strip().lower())

        if not roles:
            logger.warning(
                "Planner access denied: No authorized role found in trusted context.",
                extra={
                    "event_name": "planner_access_denied",
                    "execution_id": getattr(context, "execution_id", "unknown"),
                    "reason": "missing_role",
                },
            )
            raise PermissionError("Access denied: No valid role found in trusted execution context.")

        # Check if caller has permission to invoke Planner (viewer role has no agent invocation permission)
        if "viewer" in roles and len(roles) == 1:
            logger.warning(
                "Planner access denied: Role 'viewer' is not authorized to invoke Planner.",
                extra={
                    "event_name": "planner_access_denied",
                    "execution_id": getattr(context, "execution_id", "unknown"),
                    "reason": "viewer_not_authorized",
                },
            )
            raise PermissionError("Access denied: Role 'viewer' has read-only access and cannot invoke Planner.")

        if not any(r in AUTHORIZED_CALLER_ROLES for r in roles):
            logger.warning(
                f"Planner access denied: Roles {sorted(list(roles))} are not authorized.",
                extra={
                    "event_name": "planner_access_denied",
                    "roles": sorted(list(roles)),
                    "execution_id": getattr(context, "execution_id", "unknown"),
                    "reason": "unauthorized_role",
                },
            )
            raise PermissionError(
                f"Access denied: Roles {sorted(list(roles))} are not authorized to invoke Planner Agent."
            )

        return roles

    async def _execute(
        self, validated_input: PlannerInput, context: AgentExecutionContext
    ) -> PlannerOutput:
        """
        Executes task decomposition and validation pipeline for PlannerAgent.
        """
        task_id = validated_input.task_id
        trusted_roles = self._verify_trusted_identity(context)
        is_replan = validated_input.is_replan or validated_input.failed_step_index is not None

        # Emit start audit event
        timestamp_start = datetime.now(timezone.utc).isoformat()
        if is_replan:
            logger.info(
                f"Contextual replanning started for task '{task_id}'.",
                extra={
                    "event_name": "REPLANNING_STARTED",
                    "task_id": task_id,
                    "reason": str(validated_input.failure_details),
                    "failed_step_index": validated_input.failed_step_index,
                },
            )
        else:
            logger.info(
                f"Plan generation started for task '{task_id}'.",
                extra={
                    "event_name": "PLAN_GENERATION_STARTED",
                    "task_id": task_id,
                    "timestamp": timestamp_start,
                },
            )

        # Generate ExecutionPlan via deterministic PlannerEngine
        try:
            if is_replan:
                raw_plan = PlannerEngine.replan_failed_task(
                    task_id=task_id,
                    task_description=validated_input.task_description,
                    failed_step_index=validated_input.failed_step_index or 0,
                    failure_details=validated_input.failure_details,
                    completed_steps=validated_input.completed_steps,
                )
            else:
                raw_plan = PlannerEngine.decompose_task(
                    task_id=task_id,
                    task_description=validated_input.task_description,
                )
        except PlanValidationError as exc:
            # Emit PLAN_VALIDATION_FAILED audit event
            logger.warning(
                f"Plan generation error for task '{task_id}': {exc.message}",
                extra={
                    "event_name": "PLAN_VALIDATION_FAILED",
                    "task_id": task_id,
                    "reason": exc.message,
                },
            )
            raise

        # Emit PLAN_GENERATED audit event
        agent_sequence = [s.agent_id.value for s in raw_plan.steps]
        logger.info(
            f"Execution plan '{raw_plan.plan_id}' generated for task '{task_id}'.",
            extra={
                "event_name": "PLAN_GENERATED",
                "task_id": task_id,
                "plan_type": raw_plan.plan_type.value,
                "step_count": len(raw_plan.steps),
                "agent_sequence": agent_sequence,
            },
        )

        # Validate the generated plan using PlanValidator
        self.validator.validate_plan(
            plan=raw_plan,
            trusted_roles=trusted_roles,
            enforce_registry=self.agent_registry is not None,
        )

        if is_replan:
            logger.info(
                f"Contextual replanning completed for task '{task_id}'.",
                extra={
                    "event_name": "REPLANNING_COMPLETED",
                    "task_id": task_id,
                    "revised_step_count": len(raw_plan.steps),
                },
            )

        # Redact sensitive data from rationale and step inputs before exposing
        safe_rationale = redact_sensitive_data(raw_plan.planning_rationale)
        redacted_steps = []
        for s in raw_plan.steps:
            s_dict = s.model_dump()
            s_dict["inputs"] = redact_sensitive_data(s_dict["inputs"])
            redacted_steps.append(s_dict)

        # Construct safe provenance record
        provenance: Dict[str, Any] = {
            "planner_invocation_id": f"inv-{uuid.uuid4().hex[:12]}",
            "plan_type": raw_plan.plan_type.value,
            "agent_sequence": agent_sequence,
            "planning_rationale": safe_rationale,
            "risk_annotations": {str(s.step_index): s.risk_level.value for s in raw_plan.steps},
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "is_replan": is_replan,
        }
        if is_replan:
            provenance["replanning_reason"] = redact_sensitive_data(
                str(validated_input.failure_details or {})
            )
            provenance["failed_step_index"] = validated_input.failed_step_index

        safe_provenance = redact_sensitive_data(provenance)

        return PlannerOutput(
            plan_id=raw_plan.plan_id,
            task_id=task_id,
            plan=raw_plan,
            rationale=safe_rationale,
            is_replan=is_replan,
            provenance=safe_provenance,
            error=None,
        )
