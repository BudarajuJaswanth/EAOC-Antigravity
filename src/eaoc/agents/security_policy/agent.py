"""
EAOC Security / Policy Agent Implementation
Specialized runtime policy enforcement and guardrail component (Agent 12, AGT-SEC):
- Enforces agent permission manifests before tool execution (FR-PERM-001, FR-PERM-002)
- Performs schema-level access control (FR-DATA-002)
- Performs input guardrail checks (prompt injection detection)
- Performs output guardrail checks (PII minimization & cross-domain leakage)
- Classifies action risk level using deterministic rules
- Emits structured audit events (POLICY_VIOLATION_DETECTED, POLICY_ACTION_BLOCKED)
- Guarantees fail-closed semantics and immutable policy evaluation
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from eaoc.agents.base import BaseAgent
from eaoc.agents.security_policy.engine import SecurityPolicyEngine
from eaoc.agents.security_policy.manifest_store import ManifestStore
from eaoc.agents.security_policy.schemas import (
    AgentPermissionManifest,
    PolicyCheckOperation,
    PolicyViolation,
    PolicyViolationType,
    SecurityPolicyInput,
    SecurityPolicyOutput,
    SecurityPolicyProvenance,
)
from eaoc.observability import get_logger, redact_sensitive_data
from eaoc.schemas.domain import AgentExecutionContext, AgentID

logger = get_logger("eaoc.agents.security_policy")

# Permitted roles per Phase 0 specification (Agent 12, AGT-SEC)
# Security / Policy Agent is invokable only by Orchestrator and administrative system callers
AUTHORIZED_SECURITY_ROLES: Set[str] = {
    "orchestrator",
    "system",
    "admin",
}


class SecurityPolicyAgent(BaseAgent[SecurityPolicyInput, SecurityPolicyOutput]):
    """
    Security / Policy Agent (AGT-SEC).

    Guarantees:
    1. Canonical AgentID.SECURITY_POLICY ('security_policy') and logical ID 'AGT-SEC'.
    2. Trusted-context RBAC: permits only orchestrator, system, or admin callers.
    3. Fails closed: denies by default if context is missing, unauthenticated, or unauthorized.
    4. Anti-spoofing: ignores caller-supplied role claims in input payloads.
    5. Manifest enforcement: tool calls not explicitly allowlisted in agent manifests are blocked.
    6. Schema allowlisting: unauthorized database schemas are blocked immediately.
    7. Input/output guardrails: flags prompt injections, unredacted PII, and cross-domain leakage.
    8. Fail-closed error handling: engine errors or missing manifests always return allowed=False.
    9. Mandatory AGT-SEC audit provenance with UTC-aware timestamps and violation counts.
    """

    agent_id: AgentID = AgentID.SECURITY_POLICY
    input_schema = SecurityPolicyInput
    output_schema = SecurityPolicyOutput

    def __init__(
        self,
        engine: Optional[SecurityPolicyEngine] = None,
        manifest_store: Optional[ManifestStore] = None,
        default_timeout_seconds: Optional[float] = 10.0,
        **kwargs,
    ):
        super().__init__(
            agent_id=AgentID.SECURITY_POLICY,
            input_schema=SecurityPolicyInput,
            output_schema=SecurityPolicyOutput,
            default_timeout_seconds=default_timeout_seconds,
            **kwargs,
        )
        self.manifest_store = manifest_store or ManifestStore()
        self.engine = engine or SecurityPolicyEngine(manifest_store=self.manifest_store)

    def _verify_trusted_identity(self, context: AgentExecutionContext) -> str:
        """
        Validates authorization against trusted execution context metadata.
        Strictly ignores validated_input and context.input_payload to prevent role spoofing.
        Fails closed (denies by default) if role or identity is missing or untrusted.
        """
        if not context or not context.metadata:
            logger.warning(
                "Security/Policy access denied: Execution context metadata is missing.",
                extra={
                    "event_name": "security_access_denied",
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
                "Security/Policy access denied: Execution context is unauthenticated or untrusted.",
                extra={
                    "event_name": "security_access_denied",
                    "execution_id": getattr(context, "execution_id", "unknown"),
                    "reason": "unauthenticated",
                },
            )
            raise PermissionError("Access denied: Execution context is not authenticated or trusted.")

        user_context = context.metadata.get("user_context", {})
        if not isinstance(user_context, dict):
            user_context = {}

        role = user_context.get("role") or context.metadata.get("role")

        if not role or role not in AUTHORIZED_SECURITY_ROLES:
            logger.warning(
                f"Security/Policy access denied: Role '{role}' is not authorized.",
                extra={
                    "event_name": "security_access_denied",
                    "role": role,
                    "permitted_roles": list(AUTHORIZED_SECURITY_ROLES),
                    "execution_id": getattr(context, "execution_id", "unknown"),
                    "reason": "unauthorized_role",
                },
            )
            raise PermissionError(
                f"Access denied: Role '{role}' is not authorized for Security/Policy Agent. "
                f"Permitted roles: {sorted(list(AUTHORIZED_SECURITY_ROLES))}."
            )

        return str(role)

    async def _execute(
        self, validated_input: SecurityPolicyInput, context: AgentExecutionContext
    ) -> SecurityPolicyOutput:
        """Core execution pipeline for AGT-SEC policy evaluation."""
        role = self._verify_trusted_identity(context)
        check_id = validated_input.check_id or f"sec-chk-{uuid.uuid4().hex[:12]}"
        op = validated_input.operation
        agent_id = validated_input.agent_id
        tool_id = validated_input.tool_id

        logger.info(
            f"Policy check requested for operation '{op.value}' by role '{role}'.",
            extra={
                "event_name": "policy_check_requested",
                "check_id": check_id,
                "agent_id": agent_id,
                "action_type": op.value,
            },
        )

        allowed: bool = False
        violations: List[PolicyViolation] = []
        policy_refs: List[str] = []
        risk_level: str = "low"
        manifest_res: Optional[AgentPermissionManifest] = None

        try:
            if op == PolicyCheckOperation.CHECK_TOOL_PERMISSION:
                allowed, violations, policy_refs, risk_level = self.engine.evaluate_tool_permission(
                    agent_id=agent_id,
                    tool_id=tool_id,
                    parameters=validated_input.parameters,
                )

            elif op == PolicyCheckOperation.CHECK_SCHEMA_ACCESS:
                allowed, violations, policy_refs, risk_level = self.engine.evaluate_schema_access(
                    agent_id=agent_id,
                    schema_name=validated_input.schema_name,
                )

            elif op == PolicyCheckOperation.CHECK_INPUT_GUARDRAIL:
                allowed, violations, policy_refs, risk_level = self.engine.evaluate_input_guardrail(
                    input_content=validated_input.input_content,
                )

            elif op == PolicyCheckOperation.CHECK_OUTPUT_GUARDRAIL:
                allowed, violations, policy_refs, risk_level = self.engine.evaluate_output_guardrail(
                    output_content=validated_input.output_content,
                    role_context=validated_input.role_context,
                )

            elif op == PolicyCheckOperation.CHECK_MANIFEST:
                policy_refs = ["FR-PERM-001"]
                if not agent_id:
                    allowed = False
                    violations.append(
                        PolicyViolation(
                            policy_id="POL-AGT-001",
                            policy_name="Missing Agent Identifier",
                            violation_type=PolicyViolationType.MANIFEST_NOT_FOUND,
                            description="check_manifest request did not provide an agent_id.",
                        )
                    )
                else:
                    manifest = self.manifest_store.get_manifest(agent_id)
                    if manifest is None:
                        allowed = False
                        violations.append(
                            PolicyViolation(
                                policy_id="POL-MAN-001",
                                policy_name="Missing Agent Manifest",
                                violation_type=PolicyViolationType.MANIFEST_NOT_FOUND,
                                description=f"Manifest not found for agent '{agent_id}'.",
                            )
                        )
                    else:
                        allowed = True
                        manifest_res = manifest

            elif op == PolicyCheckOperation.EVALUATE_ACTION:
                allowed, violations, policy_refs, risk_level = self.engine.evaluate_action_risk(
                    operation_type=validated_input.operation_type,
                    data_class=validated_input.data_class,
                    agent_id=agent_id,
                    operation=validated_input.parameters.get("operation")
                    if validated_input.parameters
                    else None,
                )

        except Exception as exc:
            # Fail closed: policy engine errors always return allowed=False
            logger.error(
                f"Policy engine error during check '{check_id}': {exc}",
                extra={
                    "event_name": "policy_engine_error",
                    "check_id": check_id,
                    "error": redact_sensitive_data(str(exc)),
                },
            )
            allowed = False
            violations.append(
                PolicyViolation(
                    policy_id="POL-ERR-500",
                    policy_name="Policy Engine Execution Failure",
                    violation_type=PolicyViolationType.POLICY_ENGINE_ERROR,
                    description=f"Policy evaluation failed internally: {redact_sensitive_data(str(exc))}.",
                )
            )
            policy_refs.append("NFR-FAULT-001")
            risk_level = "high"

        # Emit structured audit events
        if allowed:
            logger.info(
                f"Policy check '{check_id}' passed for agent '{agent_id}'.",
                extra={
                    "event_name": "policy_check_passed",
                    "check_id": check_id,
                    "agent_id": agent_id,
                    "risk_level": risk_level,
                },
            )
        else:
            for v in violations:
                logger.warning(
                    f"Policy violation detected for check '{check_id}': {v.violation_type.value}",
                    extra={
                        "event_name": "policy_violation_detected",
                        "check_id": check_id,
                        "agent_id": agent_id,
                        "tool_id": tool_id,
                        "violation_type": v.violation_type.value,
                        "policy_id": v.policy_id,
                    },
                )
            logger.warning(
                f"Policy action blocked for check '{check_id}'.",
                extra={
                    "event_name": "policy_action_blocked",
                    "check_id": check_id,
                    "agent_id": agent_id,
                    "tool_id": tool_id,
                    "reason": violations[0].description if violations else "policy_violation",
                },
            )

        now_utc = datetime.now(timezone.utc)
        provenance = SecurityPolicyProvenance(
            agent_id="AGT-SEC",
            check_id=check_id,
            action_evaluated=op.value,
            agent_id_evaluated=agent_id,
            tool_id_evaluated=tool_id,
            allowed=allowed,
            violation_count=len(violations),
            policy_references_applied=policy_refs,
            checked_at=now_utc,
        )

        return SecurityPolicyOutput(
            check_id=check_id,
            allowed=allowed,
            violations=violations,
            policy_references=policy_refs,
            risk_level=risk_level,
            checked_at=now_utc,
            manifest=manifest_res,
            provenance=provenance,
        )
