"""
EAOC Security Policy Rule Engine
Implements deterministic, read-only policy checks (FR-AGT-SEC-001, FR-PERM-001, FR-PERM-002):
1. Tool Permission Checks against Agent Permission Manifests
2. Schema-Level Access Control (FR-DATA-002)
3. Input Guardrail Checks (Prompt Injection Detection)
4. Output Guardrail Checks (Unredacted PII & Cross-Domain Leakage)
5. Deterministic Risk Classification
"""

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from eaoc.agents.security_policy.manifest_store import ManifestStore
from eaoc.agents.security_policy.schemas import (
    AgentPermissionManifest,
    PolicyViolation,
    PolicyViolationType,
)
from eaoc.observability import get_logger

logger = get_logger("eaoc.agents.security_policy.engine")

# SSN regex pattern (e.g., 000-00-0000)
SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# Prompt injection signatures
PROMPT_INJECTION_PATTERN = re.compile(
    r"(ignore\s+(?:all\s+)?previous\s+instructions|system\s+override|reveal\s+(?:all\s+)?passwords|"
    r"grant\s+(?:me\s+)?admin|you\s+are\s+now\s+in\s+admin\s+mode|bypass\s+(?:all\s+)?security|"
    r"disable\s+redaction|drop\s+table|export\s+AWS_|disregard\s+prior\s+rules)",
    re.IGNORECASE,
)

# Sensitive PII field names
SENSITIVE_PII_KEYS: Set[str] = {
    "ssn",
    "social_security_number",
    "dob",
    "date_of_birth",
    "credit_card",
    "card_number",
}

# Restricted financial keys for cross-domain leakage checks
FINANCIAL_KEYS: Set[str] = {
    "financial_records",
    "company_ebitda",
    "financial_write",
    "general_ledger",
    "payroll_records",
}


class SecurityPolicyEngine:
    """
    Deterministic rule engine for Security/Policy Agent.
    Evaluates policy rules against in-memory manifests without executing external side effects.
    """

    def __init__(self, manifest_store: Optional[ManifestStore] = None):
        self.manifest_store = manifest_store or ManifestStore()

    def evaluate_tool_permission(
        self,
        agent_id: Optional[str],
        tool_id: Optional[str],
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, List[PolicyViolation], List[str], str]:
        """
        FR-PERM-001, FR-PERM-002: Intercepts and validates tool invocations against agent manifest.
        Returns: (allowed, violations, policy_references, risk_level)
        """
        violations: List[PolicyViolation] = []
        policy_refs: List[str] = ["FR-PERM-001", "FR-PERM-002"]

        if not agent_id:
            violations.append(
                PolicyViolation(
                    policy_id="POL-AGT-001",
                    policy_name="Missing Agent Identifier",
                    violation_type=PolicyViolationType.MANIFEST_NOT_FOUND,
                    description="Tool execution request did not specify an agent_id.",
                )
            )
            return False, violations, policy_refs, "high"

        manifest = self.manifest_store.get_manifest(agent_id)
        if manifest is None:
            violations.append(
                PolicyViolation(
                    policy_id="POL-MAN-001",
                    policy_name="Missing Agent Manifest",
                    violation_type=PolicyViolationType.MANIFEST_NOT_FOUND,
                    description=f"No permission manifest found for agent '{agent_id}'. Action blocked.",
                )
            )
            return False, violations, policy_refs, "high"

        if not tool_id:
            violations.append(
                PolicyViolation(
                    policy_id="POL-TOOL-000",
                    policy_name="Missing Tool Identifier",
                    violation_type=PolicyViolationType.TOOL_NOT_PERMITTED,
                    description="Tool execution request did not specify a tool_id.",
                )
            )
            return False, violations, policy_refs, "medium"

        # Check permitted tools in manifest
        norm_tool_id = tool_id.lower().strip()
        permitted_norm = [t.lower().strip() for t in manifest.permitted_tools]

        if norm_tool_id not in permitted_norm:
            violations.append(
                PolicyViolation(
                    policy_id="POL-TOOL-001",
                    policy_name="Unauthorized Tool Invocation",
                    violation_type=PolicyViolationType.TOOL_NOT_PERMITTED,
                    description=(
                        f"Agent '{agent_id}' is not authorized to invoke tool '{tool_id}'. "
                        f"Permitted tools: {manifest.permitted_tools}."
                    ),
                )
            )
            return False, violations, policy_refs, "medium"

        # Check schema-level access if parameters specify a schema
        params = parameters or {}
        requested_schema = (
            params.get("schema")
            or params.get("schema_name")
            or params.get("target_schema")
        )
        if requested_schema:
            schema_str = str(requested_schema).lower().strip()
            manifest_schemas = [s.lower().strip() for s in manifest.permitted_schemas]
            if schema_str not in manifest_schemas:
                violations.append(
                    PolicyViolation(
                        policy_id="POL-SCHEMA-001",
                        policy_name="Unauthorized Schema Reference",
                        violation_type=PolicyViolationType.SCHEMA_ACCESS_DENIED,
                        description=(
                            f"Agent '{agent_id}' is not permitted to access database schema '{requested_schema}'. "
                            f"Permitted schemas: {manifest.permitted_schemas}."
                        ),
                    )
                )
                policy_refs.append("FR-DATA-002")
                return False, violations, policy_refs, "high"

        # Check write permission if parameters or tool indicates a write operation
        is_write = (
            params.get("operation") in ["write", "insert", "update", "delete", "drop"]
            or params.get("is_write") is True
            or "write" in norm_tool_id
        )
        if is_write and not manifest.write_permitted:
            violations.append(
                PolicyViolation(
                    policy_id="POL-WRITE-001",
                    policy_name="Unauthorized Write Operation",
                    violation_type=PolicyViolationType.WRITE_NOT_PERMITTED,
                    description=f"Write operation attempted by agent '{agent_id}' without write permission in manifest.",
                )
            )
            policy_refs.append("FR-DATA-001")
            return False, violations, policy_refs, "high"

        return True, [], policy_refs, "low"

    def evaluate_schema_access(
        self, agent_id: Optional[str], schema_name: Optional[str]
    ) -> Tuple[bool, List[PolicyViolation], List[str], str]:
        """
        FR-DATA-002: Verifies that requested database schema is explicitly allowlisted in agent manifest.
        """
        violations: List[PolicyViolation] = []
        policy_refs: List[str] = ["FR-DATA-002"]

        if not agent_id or not schema_name:
            violations.append(
                PolicyViolation(
                    policy_id="POL-SCHEMA-000",
                    policy_name="Invalid Schema Check Request",
                    violation_type=PolicyViolationType.SCHEMA_ACCESS_DENIED,
                    description="Missing agent_id or schema_name in schema check request.",
                )
            )
            return False, violations, policy_refs, "high"

        manifest = self.manifest_store.get_manifest(agent_id)
        if manifest is None:
            violations.append(
                PolicyViolation(
                    policy_id="POL-MAN-001",
                    policy_name="Missing Agent Manifest",
                    violation_type=PolicyViolationType.MANIFEST_NOT_FOUND,
                    description=f"No manifest found for agent '{agent_id}'. Schema access denied.",
                )
            )
            return False, violations, policy_refs, "high"

        schema_norm = schema_name.lower().strip()
        manifest_schemas = [s.lower().strip() for s in manifest.permitted_schemas]

        if schema_norm not in manifest_schemas:
            violations.append(
                PolicyViolation(
                    policy_id="POL-SCHEMA-001",
                    policy_name="Unauthorized Schema Access",
                    violation_type=PolicyViolationType.SCHEMA_ACCESS_DENIED,
                    description=(
                        f"Schema '{schema_name}' is not allowlisted for agent '{agent_id}'. "
                        f"Permitted schemas: {manifest.permitted_schemas}."
                    ),
                )
            )
            return False, violations, policy_refs, "high"

        return True, [], policy_refs, "low"

    def evaluate_input_guardrail(
        self, input_content: Optional[str]
    ) -> Tuple[bool, List[PolicyViolation], List[str], str]:
        """
        Evaluates task prompts or parameters for prompt injection patterns.
        """
        violations: List[PolicyViolation] = []
        policy_refs: List[str] = ["SEC-INPUT-001"]

        if not input_content:
            return True, [], policy_refs, "low"

        if PROMPT_INJECTION_PATTERN.search(input_content):
            violations.append(
                PolicyViolation(
                    policy_id="POL-INJ-001",
                    policy_name="Prompt Injection Signature Detected",
                    violation_type=PolicyViolationType.PROMPT_INJECTION_DETECTED,
                    description="Input content contains prompt injection or security override pattern.",
                )
            )
            return False, violations, policy_refs, "high"

        return True, [], policy_refs, "low"

    def evaluate_output_guardrail(
        self,
        output_content: Optional[Dict[str, Any]],
        role_context: Optional[str] = None,
    ) -> Tuple[bool, List[PolicyViolation], List[str], str]:
        """
        Evaluates output dictionaries for apparent unredacted PII and cross-domain data leakage.
        """
        violations: List[PolicyViolation] = []
        policy_refs: List[str] = ["SEC-OUTPUT-001"]

        if not output_content or not isinstance(output_content, dict):
            return True, [], policy_refs, "low"

        def inspect_value(key: str, val: Any) -> None:
            if isinstance(val, str):
                if SSN_PATTERN.search(val):
                    violations.append(
                        PolicyViolation(
                            policy_id="POL-DATA-001",
                            policy_name="Unredacted PII in Output",
                            violation_type=PolicyViolationType.DATA_POLICY_VIOLATION,
                            description=f"Apparent unredacted SSN detected in output field '{key}'.",
                        )
                    )
                key_lower = key.lower()
                if any(pii_k in key_lower for pii_k in SENSITIVE_PII_KEYS):
                    if not (val.startswith("****") or "[REDACTED]" in val or val.endswith("****")):
                        violations.append(
                            PolicyViolation(
                                policy_id="POL-DATA-002",
                                policy_name="Unmasked Sensitive Field",
                                violation_type=PolicyViolationType.DATA_POLICY_VIOLATION,
                                description=f"Unmasked sensitive field '{key}' detected in output.",
                            )
                        )
            elif isinstance(val, dict):
                for sub_k, sub_v in val.items():
                    inspect_value(f"{key}.{sub_k}", sub_v)
            elif isinstance(val, list):
                for idx, item in enumerate(val):
                    inspect_value(f"{key}[{idx}]", item)

        for k, v in output_content.items():
            inspect_value(k, v)

        # Cross-domain financial disclosure check
        if role_context:
            role_norm = role_context.lower().strip()
            if role_norm not in {"finance_user", "admin", "orchestrator", "system"}:
                for k in output_content.keys():
                    if k.lower() in FINANCIAL_KEYS:
                        violations.append(
                            PolicyViolation(
                                policy_id="POL-DATA-003",
                                policy_name="Unauthorized Cross-Domain Data Disclosure",
                                violation_type=PolicyViolationType.DATA_POLICY_VIOLATION,
                                description=(
                                    f"Restricted cross-domain financial field '{k}' disclosed to unauthorized role '{role_context}'."
                                ),
                            )
                        )

        if violations:
            return False, violations, policy_refs, "high"

        return True, [], policy_refs, "low"

    def evaluate_action_risk(
        self,
        operation_type: Optional[str] = None,
        data_class: Optional[str] = None,
        agent_id: Optional[str] = None,
        operation: Optional[str] = None,
    ) -> Tuple[bool, List[PolicyViolation], List[str], str]:
        """
        Evaluates general orchestrator action against Phase 0 risk rules (Section 9.1).
        """
        op_type = str(operation_type).lower().strip() if operation_type else ""
        d_class = str(data_class).lower().strip() if data_class else ""
        a_id = str(agent_id).upper().strip() if agent_id else ""
        op = str(operation).lower().strip() if operation else ""

        policy_refs = ["FR-RISK-001", "SECTION-9.1"]

        # Rule 1: operation_type IN [write, delete, modify, deploy] -> HIGH
        if op_type in {"write", "delete", "modify", "deploy"}:
            return True, [], policy_refs, "high"

        # Rule 2: data_class = financial_write -> HIGH
        if d_class == "financial_write":
            return True, [], policy_refs, "high"

        # Rule 3: data_class = hr_pii -> HIGH
        if d_class == "hr_pii":
            return True, [], policy_refs, "high"

        # Rule 4: data_class = restricted_document -> HIGH
        if d_class == "restricted_document":
            return True, [], policy_refs, "high"

        # Rule 5: agent_id = AGT-DEV AND operation = sandbox_execution -> MEDIUM
        if (a_id in {"AGT-DEV", "DEVELOPER"} or a_id.lower() == "developer") and op == "sandbox_execution":
            return True, [], policy_refs, "medium"

        # Rule 6: data_class IN [financial_read, hr_personal_non_pii, cross_domain] -> MEDIUM
        if d_class in {"financial_read", "hr_personal_non_pii", "cross_domain"}:
            return True, [], policy_refs, "medium"

        # Rule 7: agent_id IN [AGT-FIN, AGT-HR] AND operation = read -> MEDIUM
        if (a_id in {"AGT-FIN", "AGT-HR", "FINANCE", "HR"} or a_id.lower() in {"finance", "hr"}) and op == "read":
            return True, [], policy_refs, "medium"

        # Rule 8: DEFAULT -> LOW
        return True, [], policy_refs, "low"
