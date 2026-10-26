"""
EAOC Deterministic Risk Gate
Implements Section 9.2 Risk Gate Architecture and Human-in-the-Loop decision verification:
- LOW: Proceed immediately (RISK_GATE_PASSED)
- MEDIUM: Proceed with enhanced audit logging (RISK_GATE_PASSED, enhanced_logging=True)
- HIGH: Pause execution, create ApprovalRequest, and require recorded human approval (RISK_GATE_TRIGGERED)
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
import uuid

from eaoc.agents.planner.schemas import PlanStep
from eaoc.agents.reviewer_validation.engine import ReviewerEngine
from eaoc.observability import get_logger
from eaoc.schemas.domain import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
)

logger = get_logger("eaoc.agents.orchestrator.risk_gate")

RISK_RANK: Dict[RiskLevel, int] = {
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


class RiskGateDecision(str):
    PROCEED = "PROCEED"
    PAUSE = "PAUSE"


class RiskGate:
    """
    Deterministic gate that evaluates step risk level before execution.
    Orchestrator and Planner can NEVER self-approve high-risk steps.
    """

    @classmethod
    def evaluate(
        cls,
        step: PlanStep,
        task_id: str,
        workflow_id: str,
        requested_by: Optional[str] = None,
        plan_hash: Optional[str] = None,
        plan_id: Optional[str] = None,
    ) -> Tuple[str, Optional[ApprovalRequest]]:
        """
        Evaluates step risk level per Section 9.2 rules.
        Never trusts caller-controlled risk labels: recalculates deterministic
        risk using Section 9.1 rules and upgrades if caller label was lower.
        Returns: (RiskGateDecision, Optional[ApprovalRequest])
        """
        step_index = step.step_index
        agent_id_str = step.agent_id.value

        # Recalculate deterministic risk using authoritative Section 9.1 rules
        calculated_assessment = ReviewerEngine.evaluate_risk(
            operation_type=step.operation_type,
            data_class=step.data_class,
            agent_id=step.agent_id.value,
            operation=step.operation,
        )
        calc_str = calculated_assessment.risk_level.lower().strip()
        deterministic_risk = (
            RiskLevel.HIGH
            if calc_str == "high"
            else (RiskLevel.MEDIUM if calc_str == "medium" else RiskLevel.LOW)
        )

        # Enforce that caller-supplied step.risk_level can NEVER downgrade a higher deterministic risk
        step_risk = step.risk_level
        if RISK_RANK.get(deterministic_risk, 1) > RISK_RANK.get(step_risk, 1):
            logger.warning(
                f"Step {step_index} risk level '{step_risk.value}' upgraded to '{deterministic_risk.value}' "
                f"per authoritative Section 9.1 rule ({calculated_assessment.matched_rule}).",
                extra={
                    "task_id": task_id,
                    "step_index": step_index,
                    "caller_risk": step_risk.value,
                    "enforced_risk": deterministic_risk.value,
                },
            )
            step_risk = deterministic_risk

        if step_risk == RiskLevel.LOW:
            logger.info(
                f"Risk gate passed for step {step_index} (level=low).",
                extra={
                    "event_name": "RISK_GATE_PASSED",
                    "task_id": task_id,
                    "step_index": step_index,
                    "risk_level": "low",
                },
            )
            return RiskGateDecision.PROCEED, None

        elif step_risk == RiskLevel.MEDIUM:
            logger.info(
                f"Risk gate passed for step {step_index} (level=medium, enhanced_logging=true).",
                extra={
                    "event_name": "RISK_GATE_PASSED",
                    "task_id": task_id,
                    "step_index": step_index,
                    "risk_level": "medium",
                    "enhanced_logging": True,
                },
            )
            return RiskGateDecision.PROCEED, None

        # HIGH or CRITICAL risk: MUST PAUSE FOR HUMAN APPROVAL
        approval_id = f"appr-{uuid.uuid4().hex[:12]}"
        now_utc = datetime.now(timezone.utc).isoformat()

        approval_request = ApprovalRequest(
            approval_id=approval_id,
            workflow_id=workflow_id,
            requested_action=f"Execute step {step_index}: {step.description} via {agent_id_str}",
            risk_level=step_risk,
            status=ApprovalStatus.PENDING,
            requested_at=now_utc,
            details={
                "step_index": step_index,
                "agent_id": agent_id_str,
                "task_id": task_id,
                "requested_by": requested_by,
                "operation_type": step.operation_type,
                "data_class": step.data_class,
                "operation": step.operation,
                "plan_hash": plan_hash,
                "plan_id": plan_id,
            },
        )

        logger.warning(
            f"Risk gate triggered on step {step_index} (risk_level={step_risk.value}). Pausing workflow for approval.",
            extra={
                "event_name": "RISK_GATE_TRIGGERED",
                "task_id": task_id,
                "step_index": step_index,
                "risk_level": step_risk.value,
                "approval_id": approval_id,
            },
        )
        logger.info(
            f"Approval requested: {approval_id} for step {step_index}.",
            extra={
                "event_name": "APPROVAL_REQUESTED",
                "task_id": task_id,
                "approval_id": approval_id,
                "step_index": step_index,
            },
        )

        return RiskGateDecision.PAUSE, approval_request

    @classmethod
    def validate_resume_decision(
        cls,
        decision: Optional[ApprovalDecision],
        pending_request: Optional[ApprovalRequest],
        step_index: int,
        task_id: str,
        step: Optional[PlanStep] = None,
        plan_hash: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Validates that a provided ApprovalDecision matches the pending ApprovalRequest.
        Strictly binds approval to workflow_id, approval_id, step_index, task_id, agent_id, risk_level, and plan_hash.
        Returns: (is_approved: bool, error_reason: Optional[str])
        """
        if pending_request is None:
            return False, "no_pending_approval_request"

        if decision is None:
            return False, "missing_approval_decision"

        # 1. Verify Request Status is PENDING
        if pending_request.status != ApprovalStatus.PENDING:
            return False, f"approval_request_not_pending: status is '{pending_request.status.value}'"

        # 2. Verify ID matching
        if decision.approval_id != pending_request.approval_id:
            return False, f"approval_id_mismatch: expected '{pending_request.approval_id}', got '{decision.approval_id}'"

        if decision.workflow_id != pending_request.workflow_id:
            return False, f"workflow_id_mismatch: expected '{pending_request.workflow_id}', got '{decision.workflow_id}'"

        # 3. Verify Step Index Binding (SEC-01)
        req_step_index = pending_request.details.get("step_index")
        if req_step_index is not None and req_step_index != step_index:
            return (
                False,
                f"step_index_mismatch: approval was requested for step {req_step_index}, "
                f"but resume attempted for step {step_index}",
            )

        # 4. Verify Task ID Binding if present
        req_task_id = pending_request.details.get("task_id")
        if req_task_id is not None and req_task_id != task_id:
            return (
                False,
                f"task_id_mismatch: approval was requested for task '{req_task_id}', "
                f"but resume attempted for task '{task_id}'",
            )

        # 5. Verify Protected Agent Binding if step provided (SEC-01)
        if step is not None:
            req_agent_id = pending_request.details.get("agent_id")
            if req_agent_id is not None and req_agent_id != step.agent_id.value:
                return (
                    False,
                    f"agent_id_mismatch: approval was requested for agent '{req_agent_id}', "
                    f"but resume attempted for agent '{step.agent_id.value}'",
                )
            if pending_request.risk_level != step.risk_level:
                return (
                    False,
                    f"risk_level_mismatch: approval risk '{pending_request.risk_level}' != step risk '{step.risk_level}'",
                )

        # 6. Verify Plan Hash Binding if present
        req_plan_hash = pending_request.details.get("plan_hash")
        if req_plan_hash and plan_hash and req_plan_hash != plan_hash:
            return (
                False,
                f"plan_hash_mismatch: approval was requested for plan hash '{req_plan_hash}', but resume has '{plan_hash}'",
            )

        # 7. Check decision status
        if decision.decision == ApprovalStatus.APPROVED:
            logger.info(
                f"Approval decision APPROVED received for approval {decision.approval_id}.",
                extra={
                    "event_name": "APPROVAL_RECEIVED",
                    "task_id": task_id,
                    "approval_id": decision.approval_id,
                    "decision": "APPROVED",
                    "decided_by": decision.approver_id,
                },
            )
            return True, None

        elif decision.decision == ApprovalStatus.REJECTED:
            logger.warning(
                f"Approval decision REJECTED received for approval {decision.approval_id}.",
                extra={
                    "event_name": "APPROVAL_RECEIVED",
                    "task_id": task_id,
                    "approval_id": decision.approval_id,
                    "decision": "REJECTED",
                    "decided_by": decision.approver_id,
                },
            )
            return False, "human_approval_rejected"

        return False, f"invalid_decision_status: {decision.decision}"
