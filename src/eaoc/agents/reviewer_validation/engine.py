"""
EAOC Reviewer / Validation Rule Engine
Implements deterministic, read-only validation checks and Phase 0 deterministic risk rules:
1. Output Completeness Check (FR-REV-001)
2. Factual Grounding Check (FR-REV-002)
3. Internal Consistency Check
4. Schema Conformance Check
5. Policy Compliance Check
6. Deterministic 8-Rule Risk Assessment Engine (Section 9.1)
7. Final Recommendation & Quality Gate Resolution
"""

import copy
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from eaoc.agents.reviewer_validation.schemas import (
    FindingSeverity,
    ReviewFindingItem,
    ReviewFindingType,
    ReviewRecommendation,
    RiskAssessment,
)
from eaoc.observability import get_logger

logger = get_logger("eaoc.agents.reviewer_validation.engine")

# SSN regex pattern (e.g., 000-00-0000)
SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# Monetary / numerical claim patterns (e.g., $500K, $500,000, $400M)
MONETARY_CLAIM_PATTERN = re.compile(
    r"\$([0-9]+(?:\.[0-9]+)?)\s*([KkMmBb])?|\$([0-9]{1,3}(?:,[0-9]{3})+)",
    re.IGNORECASE,
)

# Sensitive PII field names to flag if unredacted
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


def parse_numerical_value(val: Any) -> Optional[float]:
    """Parses numeric, string-numeric, or abbreviated currency values to float."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        cleaned = val.strip().replace("$", "").replace(",", "")
        match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([KkMmBb])?$", cleaned)
        if match:
            num = float(match.group(1))
            suffix = match.group(2)
            if suffix:
                s = suffix.upper()
                if s == "K":
                    return num * 1000.0
                elif s == "M":
                    return num * 1000000.0
                elif s == "B":
                    return num * 1000000000.0
            return num
    return None


def extract_numbers_from_text(text: str) -> List[Tuple[str, float]]:
    """Extracts monetary/numerical claims and their float equivalents from text."""
    results: List[Tuple[str, float]] = []
    if not text or not isinstance(text, str):
        return results

    for match in MONETARY_CLAIM_PATTERN.finditer(text):
        raw_match = match.group(0)
        parsed = parse_numerical_value(raw_match)
        if parsed is not None:
            results.append((raw_match, parsed))

    return results


class ReviewerEngine:
    """
    Deterministic rule engine for Reviewer/Validation Agent.
    Strictly read-only; never mutates input structures.
    """

    @classmethod
    def check_completeness(
        cls,
        assembled_output: Dict[str, Any],
        required_fields: Optional[List[str]],
    ) -> List[ReviewFindingItem]:
        """
        FR-REV-001: Verifies all required output fields from ExecutionPlan are present and non-null.
        Missing or null required fields create ERROR findings.
        """
        findings: List[ReviewFindingItem] = []
        if not required_fields:
            return findings

        for field in required_fields:
            if field not in assembled_output or assembled_output[field] is None:
                findings.append(
                    ReviewFindingItem(
                        finding_id=f"f-comp-{uuid.uuid4().hex[:8]}",
                        type=ReviewFindingType.COMPLETENESS,
                        severity=FindingSeverity.ERROR,
                        description=f"Required output field '{field}' is missing or null.",
                        field_reference=field,
                    )
                )

        return findings

    @classmethod
    def check_grounding(
        cls,
        assembled_output: Dict[str, Any],
        source_data: Optional[Any],
    ) -> List[ReviewFindingItem]:
        """
        FR-REV-002: Verifies numerical claims and data references against source data.
        - Flag conflicting figures (ERROR).
        - Flag missing source evidence for claims (WARNING -> triggers ESCALATE).
        """
        findings: List[ReviewFindingItem] = []

        # If assembled output has data but source data is completely empty/None
        has_content = bool(assembled_output)
        source_is_empty = source_data is None or source_data == {} or source_data == []

        if has_content and source_is_empty:
            findings.append(
                ReviewFindingItem(
                    finding_id=f"f-grnd-{uuid.uuid4().hex[:8]}",
                    type=ReviewFindingType.GROUNDING,
                    severity=FindingSeverity.WARNING,
                    description="Missing source evidence for claims in assembled output.",
                    field_reference="source_data",
                )
            )
            return findings

        if not source_data:
            return findings

        # Check structured key-value numerical comparisons
        if isinstance(source_data, dict):
            for k, out_val in assembled_output.items():
                if k in source_data:
                    src_val = source_data[k]
                    num_out = parse_numerical_value(out_val)
                    num_src = parse_numerical_value(src_val)
                    if num_out is not None and num_src is not None:
                        if abs(num_out - num_src) > 0.01:
                            findings.append(
                                ReviewFindingItem(
                                    finding_id=f"f-grnd-{uuid.uuid4().hex[:8]}",
                                    type=ReviewFindingType.GROUNDING,
                                    severity=FindingSeverity.ERROR,
                                    description=(
                                        f"Grounding conflict: Claimed value '{out_val}' for field '{k}' "
                                        f"conflicts with source data value '{src_val}'."
                                    ),
                                    field_reference=k,
                                )
                            )

        # Check narrative text numerical claims
        for out_key, out_val in assembled_output.items():
            if isinstance(out_val, str):
                text_claims = extract_numbers_from_text(out_val)
                if not text_claims:
                    continue

                # Search source_data for conflicting values
                if isinstance(source_data, dict):
                    for src_key, src_val in source_data.items():
                        num_src = parse_numerical_value(src_val)
                        if num_src is None and isinstance(src_val, str):
                            src_claims = extract_numbers_from_text(src_val)
                            if src_claims:
                                num_src = src_claims[0][1]

                        if num_src is not None:
                            # If keys are related (e.g. revenue, budget, sales) or claims present
                            for raw_claim, num_claim in text_claims:
                                if abs(num_claim - num_src) > 0.01:
                                    # If the field names match or narrative mentions the key
                                    if src_key.lower() in out_key.lower() or src_key.lower() in out_val.lower():
                                        findings.append(
                                            ReviewFindingItem(
                                                finding_id=f"f-grnd-{uuid.uuid4().hex[:8]}",
                                                type=ReviewFindingType.GROUNDING,
                                                severity=FindingSeverity.ERROR,
                                                description=(
                                                    f"Grounding conflict: Narrative claim '{raw_claim}' conflicts "
                                                    f"with source data figure for '{src_key}' ('{src_val}')."
                                                ),
                                                field_reference=out_key,
                                            )
                                        )

        return findings

    @classmethod
    def check_consistency(
        cls,
        assembled_output: Dict[str, Any],
        step_outputs: Optional[List[Dict[str, Any]]],
    ) -> List[ReviewFindingItem]:
        """
        Verifies internal consistency across step outputs and assembled output.
        Contradictions between step outputs create ERROR findings.
        """
        findings: List[ReviewFindingItem] = []
        if not step_outputs or len(step_outputs) < 2:
            return findings

        # Track key-values across steps
        seen_values: Dict[str, Tuple[int, Any]] = {}

        for step_idx, step_out in enumerate(step_outputs):
            if not isinstance(step_out, dict):
                continue
            for k, v in step_out.items():
                if isinstance(v, (str, int, float, bool)):
                    if k in seen_values:
                        prev_step_idx, prev_val = seen_values[k]
                        if v != prev_val:
                            findings.append(
                                ReviewFindingItem(
                                    finding_id=f"f-cons-{uuid.uuid4().hex[:8]}",
                                    type=ReviewFindingType.CONSISTENCY,
                                    severity=FindingSeverity.ERROR,
                                    description=(
                                        f"Internal consistency conflict for key '{k}': Step {prev_step_idx} "
                                        f"reported '{prev_val}' while Step {step_idx} reported '{v}'."
                                    ),
                                    field_reference=k,
                                )
                            )
                    else:
                        seen_values[k] = (step_idx, v)

        return findings

    @classmethod
    def check_schema(
        cls,
        assembled_output: Dict[str, Any],
        expected_schema: Optional[Dict[str, Any]],
    ) -> List[ReviewFindingItem]:
        """
        Verifies output schema conformance against expected schema definition.
        Does not claim conformance to unknown schemas if expected_schema is not provided.
        """
        findings: List[ReviewFindingItem] = []
        if not expected_schema:
            return findings

        type_map = {
            "str": str,
            "string": str,
            "int": int,
            "integer": int,
            "float": (int, float),
            "number": (int, float),
            "bool": bool,
            "boolean": bool,
            "dict": dict,
            "object": dict,
            "list": list,
            "array": list,
        }

        # Check required fields in expected_schema
        req = expected_schema.get("required", [])
        if isinstance(req, list):
            for rf in req:
                if rf not in assembled_output or assembled_output[rf] is None:
                    findings.append(
                        ReviewFindingItem(
                            finding_id=f"f-sch-{uuid.uuid4().hex[:8]}",
                            type=ReviewFindingType.SCHEMA,
                            severity=FindingSeverity.ERROR,
                            description=f"Schema violation: required schema property '{rf}' is missing or null.",
                            field_reference=rf,
                        )
                    )

        # Check property types
        props = expected_schema.get("properties", expected_schema.get("field_types", {}))
        if isinstance(props, dict):
            for prop_name, prop_spec in props.items():
                if prop_name in assembled_output and assembled_output[prop_name] is not None:
                    val = assembled_output[prop_name]
                    exp_type_str = (
                        prop_spec.get("type", "") if isinstance(prop_spec, dict) else str(prop_spec)
                    ).lower()
                    target_type = type_map.get(exp_type_str)
                    if target_type and not isinstance(val, target_type):
                        findings.append(
                            ReviewFindingItem(
                                finding_id=f"f-sch-{uuid.uuid4().hex[:8]}",
                                type=ReviewFindingType.SCHEMA,
                                severity=FindingSeverity.ERROR,
                                description=(
                                    f"Schema violation: property '{prop_name}' expected type '{exp_type_str}', "
                                    f"got '{type(val).__name__}'."
                                ),
                                field_reference=prop_name,
                            )
                        )

        return findings

    @classmethod
    def check_policy(
        cls,
        assembled_output: Dict[str, Any],
        role_context: Optional[str] = None,
        policy_rules: Optional[Dict[str, Any]] = None,
    ) -> List[ReviewFindingItem]:
        """
        Verifies policy compliance: flags apparent unredacted PII or cross-domain disclosure.
        """
        findings: List[ReviewFindingItem] = []

        def inspect_value(key: str, val: Any) -> None:
            if isinstance(val, str):
                # Check SSN pattern
                if SSN_PATTERN.search(val):
                    findings.append(
                        ReviewFindingItem(
                            finding_id=f"f-pol-{uuid.uuid4().hex[:8]}",
                            type=ReviewFindingType.POLICY,
                            severity=FindingSeverity.ERROR,
                            description=f"Policy violation: apparent unredacted SSN detected in field '{key}'.",
                            field_reference=key,
                        )
                    )
                # Check sensitive PII key with unmasked value
                key_lower = key.lower()
                if any(pii_k in key_lower for pii_k in SENSITIVE_PII_KEYS):
                    if not (val.startswith("****") or "[REDACTED]" in val or val.endswith("****")):
                        findings.append(
                            ReviewFindingItem(
                                finding_id=f"f-pol-{uuid.uuid4().hex[:8]}",
                                type=ReviewFindingType.POLICY,
                                severity=FindingSeverity.ERROR,
                                description=f"Policy violation: unredacted sensitive PII in field '{key}'.",
                                field_reference=key,
                            )
                        )

            elif isinstance(val, dict):
                for sub_k, sub_v in val.items():
                    inspect_value(f"{key}.{sub_k}", sub_v)
            elif isinstance(val, list):
                for idx, item in enumerate(val):
                    inspect_value(f"{key}[{idx}]", item)

        for k, v in assembled_output.items():
            inspect_value(k, v)

        # Check cross-domain disclosure (e.g. non-finance role receiving restricted finance records)
        if role_context:
            role_norm = role_context.lower().strip()
            if role_norm not in {"finance_user", "admin", "orchestrator", "system"}:
                for k in assembled_output.keys():
                    if k.lower() in FINANCIAL_KEYS:
                        findings.append(
                            ReviewFindingItem(
                                finding_id=f"f-pol-{uuid.uuid4().hex[:8]}",
                                type=ReviewFindingType.POLICY,
                                severity=FindingSeverity.ERROR,
                                description=(
                                    f"Policy violation: restricted financial data '{k}' returned to "
                                    f"unauthorized role '{role_context}'."
                                ),
                                field_reference=k,
                            )
                        )

        return findings

    @classmethod
    def evaluate_risk(
        cls,
        operation_type: Optional[str] = None,
        data_class: Optional[str] = None,
        agent_id: Optional[str] = None,
        operation: Optional[str] = None,
    ) -> RiskAssessment:
        """
        Section 9.1: Deterministic Risk Classification Engine.
        Evaluates the 8 Phase 0 rules in strict order (first match wins):
        1. operation_type IN [write, delete, modify, deploy] -> HIGH
        2. data_class = financial_write -> HIGH
        3. data_class = hr_pii -> HIGH
        4. data_class = restricted_document -> HIGH
        5. agent_id = AGT-DEV AND operation = sandbox_execution -> MEDIUM
        6. data_class IN [financial_read, hr_personal_non_pii, cross_domain] -> MEDIUM
        7. agent_id IN [AGT-FIN, AGT-HR] AND operation = read -> MEDIUM
        8. DEFAULT -> LOW
        """
        op_type = str(operation_type).lower().strip() if operation_type else ""
        d_class = str(data_class).lower().strip() if data_class else ""
        a_id = str(agent_id).upper().strip() if agent_id else ""
        op = str(operation).lower().strip() if operation else ""

        # Rule 1: operation_type IN [write, delete, modify, deploy] -> HIGH
        if op_type in {"write", "delete", "modify", "deploy"}:
            return RiskAssessment(
                risk_level="high",
                risk_factors=[f"Operation type '{op_type}' is high-risk."],
                matched_rule="Rule 1: operation_type IN [write, delete, modify, deploy]",
            )

        # Rule 2: data_class = financial_write -> HIGH
        if d_class == "financial_write":
            return RiskAssessment(
                risk_level="high",
                risk_factors=["Restricted data class 'financial_write'."],
                matched_rule="Rule 2: data_class = financial_write",
            )

        # Rule 3: data_class = hr_pii -> HIGH
        if d_class == "hr_pii":
            return RiskAssessment(
                risk_level="high",
                risk_factors=["Sensitive data class 'hr_pii'."],
                matched_rule="Rule 3: data_class = hr_pii",
            )

        # Rule 4: data_class = restricted_document -> HIGH
        if d_class == "restricted_document":
            return RiskAssessment(
                risk_level="high",
                risk_factors=["Restricted data class 'restricted_document'."],
                matched_rule="Rule 4: data_class = restricted_document",
            )

        # Rule 5: agent_id = AGT-DEV AND operation = sandbox_execution -> MEDIUM
        if (a_id in {"AGT-DEV", "DEVELOPER"} or a_id.lower() == "developer") and op == "sandbox_execution":
            return RiskAssessment(
                risk_level="medium",
                risk_factors=["Developer agent sandbox execution."],
                matched_rule="Rule 5: agent_id = AGT-DEV AND operation = sandbox_execution",
            )

        # Rule 6: data_class IN [financial_read, hr_personal_non_pii, cross_domain] -> MEDIUM
        if d_class in {"financial_read", "hr_personal_non_pii", "cross_domain"}:
            return RiskAssessment(
                risk_level="medium",
                risk_factors=[f"Data classification '{d_class}' is medium risk."],
                matched_rule="Rule 6: data_class IN [financial_read, hr_personal_non_pii, cross_domain]",
            )

        # Rule 7: agent_id IN [AGT-FIN, AGT-HR] AND operation = read -> MEDIUM
        if (a_id in {"AGT-FIN", "AGT-HR", "FINANCE", "HR"} or a_id.lower() in {"finance", "hr"}) and op == "read":
            return RiskAssessment(
                risk_level="medium",
                risk_factors=[f"Sensitive domain agent '{a_id}' read operation."],
                matched_rule="Rule 7: agent_id IN [AGT-FIN, AGT-HR] AND operation = read",
            )

        # Rule 8: DEFAULT -> LOW
        return RiskAssessment(
            risk_level="low",
            risk_factors=["Standard low-risk read-only operation."],
            matched_rule="Rule 8: DEFAULT",
        )

    @classmethod
    def resolve_recommendation(
        cls,
        findings: List[ReviewFindingItem],
        risk_assessment: RiskAssessment,
        requires_human_approval: bool,
    ) -> Tuple[bool, ReviewRecommendation]:
        """
        Determines passed flag and final recommendation.
        - Any ERROR finding -> passed=False, recommendation=REJECT.
        - Else: passed=True:
            - If requires_human_approval is True -> ESCALATE.
            - If any WARNING finding -> ESCALATE.
            - If risk_level in [high, medium] -> ESCALATE.
            - If risk_level is low -> APPROVE.
        """
        has_error = any(f.severity == FindingSeverity.ERROR for f in findings)
        if has_error:
            return False, ReviewRecommendation.REJECT

        # All checks passed without error
        if requires_human_approval:
            return True, ReviewRecommendation.ESCALATE

        has_warning = any(f.severity == FindingSeverity.WARNING for f in findings)
        if has_warning:
            return True, ReviewRecommendation.ESCALATE

        return True, ReviewRecommendation.APPROVE

    @classmethod
    def evaluate(cls, inp: Any) -> Any:
        """
        Executes deterministic quality gate checks and risk assessment across input.
        """
        assembled = getattr(inp, "assembled_output", {}) if isinstance(getattr(inp, "assembled_output", None), dict) else {}
        findings: List[ReviewFindingItem] = []
        findings.extend(cls.check_completeness(assembled, getattr(inp, "required_fields", None)))
        source_data = getattr(inp, "source_data", None)
        if source_data:
            findings.extend(cls.check_grounding(assembled, source_data))
        findings.extend(cls.check_consistency(assembled, getattr(inp, "step_outputs", None)))
        findings.extend(cls.check_schema(assembled, getattr(inp, "expected_schema", None)))
        findings.extend(cls.check_policy(assembled))
        risk = cls.evaluate_risk(
            operation_type=getattr(inp, "operation_type", None),
            data_class=getattr(inp, "data_class", None),
            agent_id=getattr(inp, "agent_id", None),
            operation=getattr(inp, "operation", None),
        )
        passed, rec = cls.resolve_recommendation(
            findings, risk, getattr(inp, "requires_human_approval", False)
        )

        class ReviewResult:
            def __init__(self, passed_val, rec_val, findings_val, risk_val):
                self.passed = passed_val
                self.recommendation = rec_val
                self.findings = findings_val
                self.risk_assessment = risk_val

        return ReviewResult(passed, rec, findings, risk)
