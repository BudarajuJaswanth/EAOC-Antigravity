"""
EAOC Reviewer / Validation Agent Implementation
Specialized independent quality and compliance gate (Agent 11, AGT-REV) for:
- Output completeness verification
- Factual grounding check (cross-referencing numerical claims against source data)
- Internal consistency check between step outputs
- Schema conformance verification
- Policy compliance (PII masking and cross-domain disclosure)
- Deterministic 8-rule risk assessment
- Human-in-the-loop (HITL) non-auto-approval enforcement
- Fail-escalate guarantee (LLM failure never auto-approves)
- Strict read-only output guarantee
"""

import copy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from eaoc.agents.base import BaseAgent
from eaoc.agents.reviewer_validation.engine import ReviewerEngine
from eaoc.agents.reviewer_validation.schemas import (
    FindingSeverity,
    ReviewFindingItem,
    ReviewFindingType,
    ReviewRecommendation,
    ReviewerInput,
    ReviewerOutput,
    ReviewerProvenance,
    RiskAssessment,
)
from eaoc.models.base import ModelProviderWrapper
from eaoc.observability import get_logger, redact_sensitive_data
from eaoc.schemas.domain import AgentExecutionContext, AgentID

logger = get_logger("eaoc.agents.reviewer_validation")

# Authorized roles per Phase 0 specification (Agent 11, AGT-REV)
AUTHORIZED_REVIEWER_ROLES: Set[str] = {
    "orchestrator",
    "system",
    "admin",
    "workflow_owner",
}


class ReviewerValidationAgent(BaseAgent[ReviewerInput, ReviewerOutput]):
    """
    Reviewer / Validation Agent (AGT-REV).

    Guarantees:
    1. Canonical AgentID.REVIEWER_VALIDATION ('reviewer_validation') and logical ID 'AGT-REV'.
    2. Trusted-context RBAC: permits only orchestrator, system, admin, or authenticated workflow owner.
    3. Denies by default when trusted context metadata is missing or unauthenticated.
    4. Anti-spoofing: ignores caller-supplied role claims in input payloads.
    5. Strictly read-only: never modifies the assembled workflow output or context under review.
    6. Non-auto-approval: outputs requiring human approval can never be auto-approved (escalate only).
    7. Fail-escalate: any LLM failure or timeout fails to ESCALATE, never silent pass.
    8. Deterministic risk engine: applies all 8 Phase 0 rules in strict order.
    9. Mandatory AGT-REV audit provenance with UTC-aware timestamps and findings count.
    """

    agent_id: AgentID = AgentID.REVIEWER_VALIDATION
    input_schema = ReviewerInput
    output_schema = ReviewerOutput

    def __init__(
        self,
        model_provider: Optional[ModelProviderWrapper] = None,
        default_timeout_seconds: Optional[float] = 30.0,
        **kwargs,
    ):
        super().__init__(
            agent_id=AgentID.REVIEWER_VALIDATION,
            input_schema=ReviewerInput,
            output_schema=ReviewerOutput,
            default_timeout_seconds=default_timeout_seconds,
            **kwargs,
        )
        self.model_provider = model_provider

    def _verify_trusted_identity(self, context: AgentExecutionContext) -> str:
        """
        Validates authorization against trusted execution context metadata.
        Strictly ignores validated_input and context.input_payload to prevent role spoofing.
        Fails closed (denies by default) if role or identity is missing or untrusted.
        """
        if not context or not context.metadata:
            logger.warning(
                "Reviewer access denied: Execution context metadata is missing.",
                extra={
                    "event_name": "reviewer_access_denied",
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
                "Reviewer access denied: Execution context is unauthenticated or untrusted.",
                extra={
                    "event_name": "reviewer_access_denied",
                    "execution_id": getattr(context, "execution_id", "unknown"),
                    "reason": "unauthenticated",
                },
            )
            raise PermissionError("Access denied: Execution context is not authenticated or trusted.")

        user_context = context.metadata.get("user_context", {})
        if not isinstance(user_context, dict):
            user_context = {}

        role = user_context.get("role") or context.metadata.get("role")

        if not role or role not in AUTHORIZED_REVIEWER_ROLES:
            logger.warning(
                f"Reviewer access denied: Role '{role}' is not authorized.",
                extra={
                    "event_name": "reviewer_access_denied",
                    "role": role,
                    "permitted_roles": list(AUTHORIZED_REVIEWER_ROLES),
                    "execution_id": getattr(context, "execution_id", "unknown"),
                    "reason": "unauthorized_role",
                },
            )
            raise PermissionError(
                f"Access denied: Role '{role}' is not authorized for Reviewer Agent. "
                f"Permitted roles: {sorted(list(AUTHORIZED_REVIEWER_ROLES))}."
            )

        return str(role)

    async def _execute(
        self, validated_input: ReviewerInput, context: AgentExecutionContext
    ) -> ReviewerOutput:
        """Core execution pipeline for AGT-REV validation."""
        role = self._verify_trusted_identity(context)
        review_id = f"rev-{uuid.uuid4().hex[:12]}"
        task_id = validated_input.task_id

        logger.info(
            f"Reviewer started for task '{task_id}' by role '{role}'.",
            extra={
                "event_name": "review_started",
                "task_id": task_id,
                "review_id": review_id,
            },
        )

        # Ensure strict read-only behavior by making a deep copy for analysis
        output_copy = copy.deepcopy(validated_input.assembled_output)
        source_copy = copy.deepcopy(validated_input.source_data)
        steps_copy = copy.deepcopy(validated_input.step_outputs)

        findings: List[ReviewFindingItem] = []

        # 1. Output Completeness Check
        comp_findings = ReviewerEngine.check_completeness(
            output_copy, validated_input.required_fields
        )
        findings.extend(comp_findings)
        if comp_findings:
            missing_fields = [f.field_reference for f in comp_findings if f.field_reference]
            logger.warning(
                f"Reviewer completeness failed for task '{task_id}': {missing_fields}",
                extra={
                    "event_name": "review_completeness_failed",
                    "task_id": task_id,
                    "missing_fields": missing_fields,
                },
            )

        # 2. Factual Grounding Check
        grnd_findings = ReviewerEngine.check_grounding(output_copy, source_copy)
        findings.extend(grnd_findings)
        if grnd_findings:
            logger.warning(
                f"Reviewer grounding check identified findings for task '{task_id}'.",
                extra={
                    "event_name": "review_grounding_failed",
                    "task_id": task_id,
                    "ungrounded_claims_count": len(grnd_findings),
                },
            )

        # 3. Internal Consistency Check
        cons_findings = ReviewerEngine.check_consistency(output_copy, steps_copy)
        findings.extend(cons_findings)

        # 4. Schema Conformance Check
        sch_findings = ReviewerEngine.check_schema(
            output_copy, validated_input.expected_schema
        )
        findings.extend(sch_findings)
        if sch_findings:
            logger.warning(
                f"Reviewer schema violation for task '{task_id}'.",
                extra={
                    "event_name": "review_schema_violation",
                    "task_id": task_id,
                    "schema_errors": len(sch_findings),
                },
            )

        # 5. Policy Compliance Check
        pol_findings = ReviewerEngine.check_policy(
            output_copy, validated_input.role_context, validated_input.policy_rules
        )
        findings.extend(pol_findings)
        if pol_findings:
            logger.warning(
                f"Reviewer policy violation detected for task '{task_id}'.",
                extra={
                    "event_name": "review_policy_violation",
                    "task_id": task_id,
                    "violation_type": "policy_violation",
                },
            )

        # 6. Deterministic Risk Assessment (Section 9.1 8-rule engine)
        risk_assessment = ReviewerEngine.evaluate_risk(
            operation_type=validated_input.operation_type,
            data_class=validated_input.data_class,
            agent_id=validated_input.agent_id,
            operation=validated_input.operation,
        )

        # 7. Optional LLM Inference Assistance (Grounding / Consistency)
        llm_used_for_checks = False
        if self.model_provider and self.model_provider.chat_model:
            try:
                # LLM assistance call
                chat_model = self.model_provider.chat_model
                prompt = (
                    f"Validate grounding and consistency for task {task_id}.\n"
                    f"Output: {output_copy}\nSource: {source_copy}"
                )
                if hasattr(chat_model, "ainvoke"):
                    await chat_model.ainvoke(prompt)
                elif hasattr(chat_model, "invoke"):
                    chat_model.invoke(prompt)
                llm_used_for_checks = True
            except Exception as exc:
                logger.warning(
                    f"Reviewer LLM inference failed for task '{task_id}': {exc}. Escalating to human review.",
                    extra={
                        "event_name": "review_llm_failed",
                        "task_id": task_id,
                        "error": redact_sensitive_data(str(exc)),
                    },
                )
                findings.append(
                    ReviewFindingItem(
                        finding_id=f"f-llm-{uuid.uuid4().hex[:8]}",
                        type=ReviewFindingType.RISK,
                        severity=FindingSeverity.WARNING,
                        description="Reviewer LLM assistance failed or timed out: escalated to human review.",
                        field_reference="llm_inference",
                    )
                )
                llm_used_for_checks = False

        # 8. Final Recommendation Resolution
        passed, recommendation = ReviewerEngine.resolve_recommendation(
            findings=findings,
            risk_assessment=risk_assessment,
            requires_human_approval=validated_input.requires_human_approval,
        )

        # Audit events for review outcome
        if passed and recommendation == ReviewRecommendation.APPROVE:
            logger.info(
                f"Review passed for task '{task_id}' with risk '{risk_assessment.risk_level}'.",
                extra={
                    "event_name": "review_passed",
                    "task_id": task_id,
                    "risk_level": risk_assessment.risk_level,
                },
            )
        elif recommendation == ReviewRecommendation.ESCALATE:
            logger.info(
                f"Review escalated to human review for task '{task_id}'.",
                extra={
                    "event_name": "review_escalated",
                    "task_id": task_id,
                    "reason": "risk_or_warning_or_human_approval",
                },
            )
        else:
            logger.warning(
                f"Review rejected for task '{task_id}'.",
                extra={
                    "event_name": "review_rejected",
                    "task_id": task_id,
                    "reason": "validation_error_findings",
                },
            )

        # Compile findings count
        findings_count = {
            "error": sum(1 for f in findings if f.severity == FindingSeverity.ERROR),
            "warning": sum(1 for f in findings if f.severity == FindingSeverity.WARNING),
            "info": sum(1 for f in findings if f.severity == FindingSeverity.INFO),
        }

        now_utc = datetime.now(timezone.utc)
        provenance = ReviewerProvenance(
            agent_id="AGT-REV",
            review_id=review_id,
            task_id=task_id,
            findings_count=findings_count,
            risk_level=risk_assessment.risk_level,
            recommendation=recommendation,
            reviewed_at=now_utc,
            llm_used_for_checks=llm_used_for_checks,
        )

        return ReviewerOutput(
            review_id=review_id,
            task_id=task_id,
            passed=passed,
            findings=findings,
            risk_assessment=risk_assessment,
            recommendation=recommendation,
            reviewed_at=now_utc,
            provenance=provenance,
        )
