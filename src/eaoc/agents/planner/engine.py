"""
EAOC Deterministic Planning Engine
Implements deterministic task decomposition, capability mapping, Section 9.1 risk annotation,
contextual re-planning, and prompt-injection defense-in-depth per Phase 0 specifications.
"""

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from eaoc.agents.planner.schemas import (
    ExecutionPlan,
    PlanStep,
    PlanType,
    PlanValidationError,
)
from eaoc.agents.reviewer_validation.engine import ReviewerEngine
from eaoc.agents.security_policy.engine import PROMPT_INJECTION_PATTERN
from eaoc.observability import get_logger, redact_sensitive_data
from eaoc.schemas.domain import AgentID, RiskLevel

logger = get_logger("eaoc.agents.planner.engine")

# Domain keyword signatures for deterministic task mapping
DOMAIN_KEYWORDS: Dict[AgentID, List[str]] = {
    AgentID.FINANCE: [
        "invoice",
        "budget",
        "expense",
        "revenue",
        "ebitda",
        "payroll records",
        "financial",
        "q3 sales",
        "sales",
        "inv-",
    ],
    AgentID.HR: [
        "hr",
        "headcount",
        "employee",
        "personnel",
        "hiring",
        "leave",
        "onboarding",
        "staff",
    ],
    AgentID.IT_SUPPORT: [
        "ticket",
        "vpn",
        "incident",
        "password reset",
        "it support",
        "network",
        "laptop",
        "it kb",
    ],
    AgentID.DEVELOPER: [
        "code",
        "python",
        "script",
        "refactor",
        "bug fix",
        "developer",
        "sandbox",
        "syntax",
    ],
    AgentID.RESEARCH_RAG: [
        "rag",
        "knowledge base",
        "documentation",
        "handbook",
        "policy doc",
        "search research",
        "company policy",
    ],
    AgentID.DOCUMENT: [
        "document",
        "pdf",
        "contract",
        "ocr",
        "extract text",
        "form",
        "scanned",
    ],
    AgentID.SQL_DATA: [
        "sql",
        "database query",
        "raw sql",
        "postgres",
        "database table",
    ],
    AgentID.ANALYTICS: [
        "analytics",
        "trend",
        "aggregate",
        "forecast",
        "metrics analysis",
        "correlation",
    ],
    AgentID.SECURITY_POLICY: [
        "security check",
        "policy check",
        "compliance check",
        "permission audit",
        "manifest check",
    ],
    AgentID.REVIEWER_VALIDATION: [
        "fact check",
        "validate output",
        "reviewer check",
        "compliance review",
    ],
}


class PlannerEngine:
    """
    Deterministic rule-based planning engine.
    Decomposes natural language task requirements into typed ExecutionPlans without side effects.
    """

    @classmethod
    def sanitize_task_description(cls, description: str) -> Tuple[str, bool]:
        """
        Sanitizes task description for prompt-injection patterns as defense-in-depth.
        Returns: (sanitized_description, injection_detected)
        """
        if not description or not isinstance(description, str):
            return "", False

        cleaned = description.strip()
        injection_match = PROMPT_INJECTION_PATTERN.search(cleaned)
        if injection_match:
            logger.warning(
                f"Prompt injection pattern detected in task description: '{injection_match.group(0)}'",
                extra={"event_name": "prompt_injection_detected", "matched_pattern": injection_match.group(0)},
            )
            # Neutralize the matched injection token
            sanitized = PROMPT_INJECTION_PATTERN.sub("[SANITIZED_INJECTION]", cleaned)
            return sanitized, True

        return cleaned, False

    @classmethod
    def annotate_step_risk(cls, step: PlanStep) -> RiskLevel:
        """
        Annotates a PlanStep with deterministic risk level using Section 9.1 rules.
        Reuses ReviewerEngine.evaluate_risk to maintain a single source of truth.
        """
        assessment = ReviewerEngine.evaluate_risk(
            operation_type=step.operation_type,
            data_class=step.data_class,
            agent_id=step.agent_id.value,
            operation=step.operation,
        )

        level_str = assessment.risk_level.lower().strip()
        if level_str == "high":
            return RiskLevel.HIGH
        elif level_str == "medium":
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    @classmethod
    def decompose_task(
        cls,
        task_id: str,
        task_description: str,
        plan_id: Optional[str] = None,
    ) -> ExecutionPlan:
        """
        Decomposes task description into single_agent or multi_agent ExecutionPlan (FR-PLAN-001, FR-PLAN-003).
        Fails safely with PlanValidationError if task is empty, ambiguous, or unsupported.
        """
        sanitized_desc, _ = cls.sanitize_task_description(task_description)
        if not sanitized_desc:
            raise PlanValidationError(
                "Task description is empty or missing; cannot generate execution plan.",
                details={"task_id": task_id},
            )

        desc_lower = sanitized_desc.lower()

        # Identify relevant domains
        matched_domains: List[AgentID] = []
        for agent_id, keywords in DOMAIN_KEYWORDS.items():
            if any(re.search(rf"\b{re.escape(kw)}\b", desc_lower) for kw in keywords):
                matched_domains.append(agent_id)

        # Disambiguate SQL vs domain queries: if Finance or HR matched, do not duplicate raw SQL unless explicit
        if (AgentID.FINANCE in matched_domains or AgentID.HR in matched_domains) and AgentID.SQL_DATA in matched_domains:
            if "raw sql" not in desc_lower and "sql query" not in desc_lower:
                matched_domains.remove(AgentID.SQL_DATA)

        if not matched_domains:
            raise PlanValidationError(
                f"Task intent in '{sanitized_desc}' could not be unambiguously mapped to supported agent capabilities.",
                details={"task_id": task_id, "sanitized_task": sanitized_desc},
            )

        active_plan_id = plan_id or f"plan-{uuid.uuid4().hex[:12]}"
        created_at = datetime.now(timezone.utc).isoformat()

        # Single-Agent Plan Generation
        if len(matched_domains) == 1:
            target_agent = matched_domains[0]
            step_0 = PlanStep(
                step_index=0,
                agent_id=target_agent,
                description=f"Execute {target_agent.value} query for task: {sanitized_desc}",
                inputs={"query": sanitized_desc, "task_id": task_id},
                dependencies=[],
                operation_type="read",
                data_class=cls._infer_data_class(target_agent),
                operation="read",
            )
            step_0.risk_level = cls.annotate_step_risk(step_0)

            return ExecutionPlan(
                plan_id=active_plan_id,
                task_id=task_id,
                plan_type=PlanType.SINGLE_AGENT,
                steps=[step_0],
                planning_rationale=(
                    f"Task mapped to single domain '{target_agent.value}'. "
                    f"Created single-agent execution plan with step 0."
                ),
                created_at=created_at,
            )

        # Multi-Agent Plan Generation (Cross-Domain)
        steps: List[PlanStep] = []
        retrieval_step_indices: List[int] = []

        # Data gathering steps
        for idx, agent_id in enumerate(matched_domains):
            step = PlanStep(
                step_index=idx,
                agent_id=agent_id,
                description=f"Gather {agent_id.value} information for task: {sanitized_desc}",
                inputs={"query": sanitized_desc, "domain": agent_id.value, "task_id": task_id},
                dependencies=[],
                operation_type="read",
                data_class=cls._infer_data_class(agent_id),
                operation="read",
            )
            step.risk_level = cls.annotate_step_risk(step)
            steps.append(step)
            retrieval_step_indices.append(idx)

        # Cross-domain synthesis step (Analytics or Reviewer)
        synthesis_idx = len(steps)
        synthesis_agent = (
            AgentID.ANALYTICS if AgentID.ANALYTICS not in matched_domains else AgentID.REVIEWER_VALIDATION
        )
        synthesis_step = PlanStep(
            step_index=synthesis_idx,
            agent_id=synthesis_agent,
            description=f"Synthesize cross-domain findings across steps {retrieval_step_indices}",
            inputs={
                "source_steps": retrieval_step_indices,
                "step_references": [f"$step[{i}].output" for i in retrieval_step_indices],
                "task_id": task_id,
            },
            dependencies=list(retrieval_step_indices),
            operation_type="read",
            data_class="cross_domain",
            operation="read",
        )
        synthesis_step.risk_level = cls.annotate_step_risk(synthesis_step)
        steps.append(synthesis_step)

        rationale = (
            f"Cross-domain task requiring [{', '.join(a.value for a in matched_domains)}]. "
            f"Created multi-agent DAG with {len(retrieval_step_indices)} parallel retrieval steps "
            f"and synthesis step {synthesis_idx} ({synthesis_agent.value})."
        )

        return ExecutionPlan(
            plan_id=active_plan_id,
            task_id=task_id,
            plan_type=PlanType.MULTI_AGENT,
            steps=steps,
            planning_rationale=rationale,
            created_at=created_at,
        )

    @classmethod
    def replan_failed_task(
        cls,
        task_id: str,
        task_description: str,
        failed_step_index: int,
        failure_details: Optional[Dict[str, Any]] = None,
        completed_steps: Optional[List[Dict[str, Any]]] = None,
        plan_id: Optional[str] = None,
    ) -> ExecutionPlan:
        """
        PATH-H Contextual Re-planning (FR-ADAP-002, Arch Part A Section 2.3).
        Produces a revised execution plan for remaining work without repeating completed steps.
        """
        details = failure_details or {}
        failed_agent_str = details.get("agent_id") or details.get("failed_agent")
        failure_reason = details.get("error") or details.get("reason", "unknown_failure")

        # Fallback routing options for recoverable failures
        fallbacks: Dict[str, AgentID] = {
            "research_rag": AgentID.DOCUMENT,
            "document": AgentID.RESEARCH_RAG,
            "sql_data": AgentID.ANALYTICS,
            "analytics": AgentID.SQL_DATA,
        }

        # Check if alternative agent exists
        alternative_agent: Optional[AgentID] = None
        if failed_agent_str:
            norm_agent = failed_agent_str.lower().replace("agt-", "")
            for orig, alt in fallbacks.items():
                if orig in norm_agent:
                    alternative_agent = alt
                    break

        completed_indices: List[int] = []
        if completed_steps:
            for s in completed_steps:
                if isinstance(s, dict) and "step_index" in s:
                    completed_indices.append(int(s["step_index"]))

        active_plan_id = plan_id or f"replan-{uuid.uuid4().hex[:12]}"
        created_at = datetime.now(timezone.utc).isoformat()

        # If alternative agent is identified for the failed step
        if alternative_agent is not None:
            revised_step = PlanStep(
                step_index=0,
                agent_id=alternative_agent,
                description=(
                    f"Fallback execution for failed step {failed_step_index} ({failed_agent_str}) "
                    f"using alternative agent {alternative_agent.value}."
                ),
                inputs={
                    "query": task_description,
                    "task_id": task_id,
                    "previous_failure": failure_reason,
                    "completed_step_references": completed_indices,
                },
                dependencies=[],
                operation_type="read",
                data_class=cls._infer_data_class(alternative_agent),
                operation="read",
            )
            revised_step.risk_level = cls.annotate_step_risk(revised_step)

            rationale = (
                f"Contextual replan triggered after step {failed_step_index} failure ({failure_reason}). "
                f"Rerouted to alternative agent '{alternative_agent.value}' while preserving context of "
                f"{len(completed_indices)} completed steps."
            )

            return ExecutionPlan(
                plan_id=active_plan_id,
                task_id=task_id,
                plan_type=PlanType.SINGLE_AGENT,
                steps=[revised_step],
                planning_rationale=rationale,
                created_at=created_at,
            )

        # If no alternative agent exists, check if partial decomposition can proceed
        # If the failed step was a terminal failure with no viable recovery, fail closed
        raise PlanValidationError(
            f"Cannot replan: step {failed_step_index} failed permanently ({failure_reason}) "
            f"and no viable alternative agent is available.",
            details={
                "task_id": task_id,
                "failed_step_index": failed_step_index,
                "failure_reason": failure_reason,
            },
        )

    @staticmethod
    def _infer_data_class(agent_id: AgentID) -> str:
        """Helper to assign representative data class for initial risk estimation."""
        if agent_id == AgentID.FINANCE:
            return "financial_read"
        elif agent_id == AgentID.HR:
            return "hr_personal_non_pii"
        elif agent_id == AgentID.DOCUMENT:
            return "internal_document"
        elif agent_id == AgentID.ANALYTICS:
            return "cross_domain"
        return "internal"
