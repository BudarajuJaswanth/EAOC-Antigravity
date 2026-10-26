"""
EAOC ExecutionPlan Validator
Implements deterministic plan validation per FR-PLAN-002:
1. Strict schema validation
2. Active AgentRegistry existence check
3. Trusted RBAC authorization enforcement
4. DAG dependency validation, forward-reference prohibition, and cycle detection (topological sort)
5. Input resolvability validation
6. PlanType vs step count constraint enforcement
7. Structured audit event logging (PLAN_VALIDATED, PLAN_VALIDATION_FAILED)
"""

from collections import deque
import re
from typing import Any, Dict, List, Optional, Set, Union

from eaoc.agents.planner.schemas import (
    ExecutionPlan,
    PlanStep,
    PlanType,
    PlanValidationError,
)
from eaoc.agents.registry import AgentRegistry
from eaoc.agents.reviewer_validation.engine import ReviewerEngine
from eaoc.observability import get_logger
from eaoc.schemas.domain import AgentID, RiskLevel

logger = get_logger("eaoc.agents.planner.validator")

# Authoritative Role -> Permitted AgentID mapping (Arch Part A Section 3.3, FRS FR-RBAC-001/002)
ROLE_PERMITTED_AGENTS: Dict[str, Set[AgentID]] = {
    "admin": {
        AgentID.RESEARCH_RAG,
        AgentID.SQL_DATA,
        AgentID.DOCUMENT,
        AgentID.ANALYTICS,
        AgentID.FINANCE,
        AgentID.HR,
        AgentID.IT_SUPPORT,
        AgentID.DEVELOPER,
        AgentID.REVIEWER_VALIDATION,
        AgentID.SECURITY_POLICY,
        AgentID.ORCHESTRATOR,
        AgentID.PLANNER,
    },
    "system": {
        AgentID.RESEARCH_RAG,
        AgentID.SQL_DATA,
        AgentID.DOCUMENT,
        AgentID.ANALYTICS,
        AgentID.FINANCE,
        AgentID.HR,
        AgentID.IT_SUPPORT,
        AgentID.DEVELOPER,
        AgentID.REVIEWER_VALIDATION,
        AgentID.SECURITY_POLICY,
        AgentID.ORCHESTRATOR,
        AgentID.PLANNER,
    },
    "orchestrator": {
        AgentID.RESEARCH_RAG,
        AgentID.SQL_DATA,
        AgentID.DOCUMENT,
        AgentID.ANALYTICS,
        AgentID.FINANCE,
        AgentID.HR,
        AgentID.IT_SUPPORT,
        AgentID.DEVELOPER,
        AgentID.REVIEWER_VALIDATION,
        AgentID.SECURITY_POLICY,
        AgentID.PLANNER,
    },
    "analyst": {
        AgentID.RESEARCH_RAG,
        AgentID.SQL_DATA,
        AgentID.DOCUMENT,
        AgentID.ANALYTICS,
        AgentID.REVIEWER_VALIDATION,
    },
    "finance_user": {
        AgentID.FINANCE,
        AgentID.SQL_DATA,
        AgentID.DOCUMENT,
        AgentID.ANALYTICS,
        AgentID.REVIEWER_VALIDATION,
    },
    "hr_user": {
        AgentID.HR,
        AgentID.RESEARCH_RAG,
        AgentID.DOCUMENT,
        AgentID.REVIEWER_VALIDATION,
    },
    "it_user": {
        AgentID.IT_SUPPORT,
        AgentID.RESEARCH_RAG,
        AgentID.SQL_DATA,
        AgentID.DOCUMENT,
        AgentID.REVIEWER_VALIDATION,
    },
    "developer": {
        AgentID.DEVELOPER,
        AgentID.RESEARCH_RAG,
        AgentID.SQL_DATA,
        AgentID.DOCUMENT,
        AgentID.ANALYTICS,
        AgentID.REVIEWER_VALIDATION,
    },
    "viewer": set(),
}

# Regex to detect references to prior step outputs in inputs
STEP_REF_PATTERN = re.compile(r"\$step\[(\d+)\]")


class PlanValidator:
    """
    Validates execution plans deterministically against AgentRegistry and RBAC policies.
    """

    def __init__(self, agent_registry: Optional[AgentRegistry] = None):
        self.agent_registry = agent_registry

    def validate_plan(
        self,
        plan: ExecutionPlan,
        trusted_roles: Optional[Union[str, List[str], Set[str]]] = None,
        enforce_registry: bool = True,
    ) -> bool:
        """
        Validates the execution plan in its entirety.
        Raises PlanValidationError if any check fails.
        Returns True if all checks pass.
        """
        task_id = plan.task_id
        plan_id = plan.plan_id

        try:
            # 1. Structural and Step Count Validation (FR-PLAN-003)
            self._validate_step_counts(plan)

            # 2. Step Ordering & Indexing Validation
            step_indices = self._validate_step_indices(plan)

            # 3. Agent Existence & Registry Lookup (FR-PLAN-002)
            if enforce_registry and self.agent_registry is not None:
                self._validate_agent_registry(plan)

            # 4. RBAC Authorization Enforcement (FR-RBAC-002, FR-ADAP-001)
            self._validate_authorization(plan, trusted_roles)

            # 5. DAG Dependencies and Cycle Detection (FR-PLAN-002)
            self._validate_dependencies_and_cycles(plan, step_indices)

            # 6. Input Resolvability Validation
            self._validate_input_resolvability(plan)

            # 7. Section 9.1 Deterministic Risk Level Validation (SEC-02)
            self._validate_risk_levels(plan)

            # Emit PLAN_VALIDATED audit event
            logger.info(
                f"Execution plan '{plan_id}' for task '{task_id}' successfully validated.",
                extra={
                    "event_name": "PLAN_VALIDATED",
                    "task_id": task_id,
                    "plan_id": plan_id,
                    "step_count": len(plan.steps),
                    "plan_type": plan.plan_type.value,
                },
            )
            return True

        except PlanValidationError as exc:
            logger.warning(
                f"Plan validation failed for plan '{plan_id}' on task '{task_id}': {exc.message}",
                extra={
                    "event_name": "PLAN_VALIDATION_FAILED",
                    "task_id": task_id,
                    "plan_id": plan_id,
                    "reason": exc.message,
                    "details": exc.details,
                },
            )
            raise

    def _validate_step_counts(self, plan: ExecutionPlan) -> None:
        """Enforces plan_type vs step count rules."""
        if not plan.steps:
            raise PlanValidationError(
                "ExecutionPlan must contain at least one step.",
                details={"plan_id": plan.plan_id, "step_count": 0},
            )

        if plan.plan_type == PlanType.SINGLE_AGENT:
            if len(plan.steps) != 1:
                raise PlanValidationError(
                    f"Single-agent plan must contain exactly 1 step, got {len(plan.steps)}.",
                    details={"plan_type": plan.plan_type.value, "step_count": len(plan.steps)},
                )
        elif plan.plan_type == PlanType.MULTI_AGENT:
            if len(plan.steps) < 2:
                raise PlanValidationError(
                    f"Multi-agent plan must contain at least 2 steps, got {len(plan.steps)}.",
                    details={"plan_type": plan.plan_type.value, "step_count": len(plan.steps)},
                )

    def _validate_step_indices(self, plan: ExecutionPlan) -> Set[int]:
        """Validates that step indices are contiguous, sequential, and unique."""
        seen_indices: Set[int] = set()
        for idx, step in enumerate(plan.steps):
            if step.step_index in seen_indices:
                raise PlanValidationError(
                    f"Duplicate step_index {step.step_index} detected in plan.",
                    details={"duplicate_index": step.step_index},
                )
            if step.step_index != idx:
                raise PlanValidationError(
                    f"Invalid step ordering: expected step_index {idx}, found {step.step_index}.",
                    details={"expected_index": idx, "actual_index": step.step_index},
                )
            seen_indices.add(step.step_index)
        return seen_indices

    def _validate_agent_registry(self, plan: ExecutionPlan) -> None:
        """Verifies all agent_ids exist in the active AgentRegistry."""
        if not self.agent_registry:
            return

        for step in plan.steps:
            if not self.agent_registry.has_agent(step.agent_id):
                raise PlanValidationError(
                    f"Agent '{step.agent_id.value}' referenced in step {step.step_index} is not registered in AgentRegistry.",
                    details={"step_index": step.step_index, "agent_id": step.agent_id.value},
                )

    def _validate_authorization(
        self,
        plan: ExecutionPlan,
        trusted_roles: Optional[Union[str, List[str], Set[str]]],
    ) -> None:
        """Enforces caller RBAC: all agents in plan must be permitted by trusted caller roles."""
        if trusted_roles is None:
            raise PlanValidationError(
                "Authorization context is missing: trusted roles must be provided to validate plan.",
                details={"reason": "missing_roles"},
            )

        # Normalize roles
        roles: Set[str] = set()
        if isinstance(trusted_roles, str):
            roles.add(trusted_roles.strip().lower())
        elif isinstance(trusted_roles, (list, set, tuple)):
            for r in trusted_roles:
                if isinstance(r, str) and r.strip():
                    roles.add(r.strip().lower())

        if not roles:
            raise PlanValidationError(
                "Authorization context contains no valid roles.",
                details={"reason": "empty_roles"},
            )

        # Compute union of permitted agents across all trusted roles
        permitted_agents: Set[AgentID] = set()
        for role in roles:
            if role in ROLE_PERMITTED_AGENTS:
                permitted_agents.update(ROLE_PERMITTED_AGENTS[role])

        # If user has viewer role or no permitted agents
        for step in plan.steps:
            if step.agent_id not in permitted_agents:
                raise PlanValidationError(
                    f"User roles {sorted(list(roles))} do not have RBAC permission to invoke agent '{step.agent_id.value}' in step {step.step_index}.",
                    details={
                        "step_index": step.step_index,
                        "agent_id": step.agent_id.value,
                        "caller_roles": sorted(list(roles)),
                    },
                )

    def _validate_dependencies_and_cycles(
        self, plan: ExecutionPlan, step_indices: Set[int]
    ) -> None:
        """
        Validates dependency references:
        - Must point to valid preceding step indices
        - Must not reference self or future steps
        - Detects cycles via topological sort (Kahn's algorithm)
        """
        in_degree: Dict[int, int] = {step.step_index: 0 for step in plan.steps}
        adj_list: Dict[int, List[int]] = {step.step_index: [] for step in plan.steps}

        for step in plan.steps:
            for dep in step.dependencies:
                # 1. Dependency must exist in the plan
                if dep not in step_indices:
                    raise PlanValidationError(
                        f"Step {step.step_index} references non-existent dependency step_index {dep}.",
                        details={"step_index": step.step_index, "invalid_dependency": dep},
                    )
                # 2. Dependency must strictly precede the current step
                if dep >= step.step_index:
                    raise PlanValidationError(
                        f"Step {step.step_index} has forward or self dependency on step {dep}. Dependencies must strictly precede the step.",
                        details={"step_index": step.step_index, "invalid_dependency": dep},
                    )

                adj_list[dep].append(step.step_index)
                in_degree[step.step_index] += 1

        # Kahn's algorithm for topological sort cycle detection
        queue = deque([idx for idx, deg in in_degree.items() if deg == 0])
        visited_count = 0

        while queue:
            node = queue.popleft()
            visited_count += 1
            for neighbor in adj_list[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited_count != len(plan.steps):
            raise PlanValidationError(
                "Circular dependency cycle detected in plan step graph.",
                details={"visited_steps": visited_count, "total_steps": len(plan.steps)},
            )

    def _validate_input_resolvability(self, plan: ExecutionPlan) -> None:
        """
        Validates that any input references to prior steps (e.g., $step[0].output or source_step)
        point to existing preceding steps declared in dependencies.
        """
        for step in plan.steps:
            dep_set = set(step.dependencies)

            def check_val(val: Any) -> None:
                if isinstance(val, str):
                    for match in STEP_REF_PATTERN.finditer(val):
                        ref_idx = int(match.group(1))
                        if ref_idx >= step.step_index or ref_idx not in dep_set:
                            raise PlanValidationError(
                                f"Step {step.step_index} inputs reference output of step {ref_idx}, "
                                f"but step {ref_idx} is not listed in dependencies {step.dependencies}.",
                                details={
                                    "step_index": step.step_index,
                                    "unresolved_reference": ref_idx,
                                },
                            )
                elif isinstance(val, dict):
                    if "source_step" in val and isinstance(val["source_step"], int):
                        src_idx = val["source_step"]
                        if src_idx >= step.step_index or src_idx not in dep_set:
                            raise PlanValidationError(
                                f"Step {step.step_index} inputs specify source_step={src_idx}, "
                                f"which is not declared in step dependencies {step.dependencies}.",
                                details={
                                    "step_index": step.step_index,
                                    "unresolved_reference": src_idx,
                                },
                            )
                    for sub_v in val.values():
                        check_val(sub_v)
                elif isinstance(val, list):
                    for item in val:
                        check_val(item)

            for in_val in step.inputs.values():
                check_val(in_val)

    def _validate_risk_levels(self, plan: ExecutionPlan) -> None:
        """
        Validates that no step in the plan has a risk_level lower than required
        by the authoritative Section 9.1 deterministic risk rules (SEC-02).
        """
        risk_rank = {
            RiskLevel.LOW: 1,
            RiskLevel.MEDIUM: 2,
            RiskLevel.HIGH: 3,
            RiskLevel.CRITICAL: 4,
        }
        for step in plan.steps:
            assessed = ReviewerEngine.evaluate_risk(
                operation_type=step.operation_type,
                data_class=step.data_class,
                agent_id=step.agent_id.value,
                operation=step.operation,
            )
            calc_str = assessed.risk_level.lower().strip()
            calc_level = (
                RiskLevel.HIGH
                if calc_str == "high"
                else (RiskLevel.MEDIUM if calc_str == "medium" else RiskLevel.LOW)
            )
            if risk_rank.get(calc_level, 1) > risk_rank.get(step.risk_level, 1):
                raise PlanValidationError(
                    f"Plan step {step.step_index} specifies risk_level '{step.risk_level.value}', "
                    f"but Section 9.1 deterministic rules require '{calc_level.value}' ({assessed.matched_rule}).",
                    details={
                        "step_index": step.step_index,
                        "specified_risk": step.risk_level.value,
                        "required_risk": calc_level.value,
                        "matched_rule": assessed.matched_rule,
                    },
                )
