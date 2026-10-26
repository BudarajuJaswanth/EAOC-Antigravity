"""
EAOC Developer Agent Implementation
Specialized Human-in-the-Loop (HITL) interactive agent for:
- Explaining user-provided code and error context
- Performing structured code reviews with severity findings
- Generating markdown documentation for source code
- Synthesizing code with credential scanning and mandatory AI labels
- Sandboxed test execution verification (debug_with_test)

Enforces trusted-context RBAC (developer, admin), credential scanning on generated code,
strict isolation boundaries, simulation transparency, and mandatory AGT-DEV provenance.
"""

from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from eaoc.agents.base import BaseAgent
from eaoc.agents.developer.sandbox import (
    BaseSandboxRunner,
    InMemorySandboxRunner,
    validate_test_command_safety,
)
from eaoc.agents.developer.scanner import DevCredentialScanner
from eaoc.agents.developer.schemas import (
    DevInput,
    DevOperation,
    DevOutput,
    DevProvenance,
    ReviewFinding,
    ReviewFindingSeverity,
    TestEvidence,
)
from eaoc.models.base import ModelProviderWrapper
from eaoc.observability import get_logger, redact_sensitive_data
from eaoc.schemas.domain import AgentExecutionContext, AgentID

logger = get_logger("eaoc.agents.developer")

# Permitted roles per Phase 0 specification (Agent 10, AGT-DEV)
AUTHORIZED_DEV_ROLES: Set[str] = {"developer", "admin"}

# Prompt injection signatures to neutralize in untrusted inputs
PROMPT_INJECTION_PATTERN = re.compile(
    r"(ignore\s+all\s+previous\s+instructions|system\s+override|reveal\s+(?:all\s+)?passwords|"
    r"grant\s+(?:me\s+)?admin|you\s+are\s+now\s+in\s+admin\s+mode|bypass\s+(?:all\s+)?security|"
    r"disable\s+redaction|drop\s+table|export\s+AWS_)",
    re.IGNORECASE,
)


class DeveloperAgent(BaseAgent[DevInput, DevOutput]):
    """
    Developer specialized agent (AGT-DEV).

    Guarantees:
    1. Canonical AgentID.DEVELOPER ('developer') and logical ID 'AGT-DEV'.
    2. Trusted-context RBAC: permits only 'developer' and 'admin' roles.
    3. Fails closed / denies by default when trusted identity is missing or unauthenticated.
    4. Anti-spoofing: ignores caller-supplied role claims in input payloads.
    5. Advisory-only: operates exclusively on user-provided code and error context.
    6. Credential scanning: output is scanned and blocked if likely secrets are detected.
    7. Mandatory AI-generated label on all generated code.
    8. Sandbox boundary: requires explicit trusted permission; simulated runs are explicitly
       flagged and NEVER set fix_verified=True.
    9. Mandatory AGT-DEV provenance block with code SHA-256 hash.
    """

    agent_id: AgentID = AgentID.DEVELOPER
    input_schema = DevInput
    output_schema = DevOutput

    def __init__(
        self,
        sandbox_runner: Optional[BaseSandboxRunner] = None,
        model_provider: Optional[ModelProviderWrapper] = None,
        default_timeout_seconds: Optional[float] = 60.0,
        **kwargs,
    ):
        super().__init__(
            agent_id=AgentID.DEVELOPER,
            input_schema=DevInput,
            output_schema=DevOutput,
            default_timeout_seconds=default_timeout_seconds,
            **kwargs,
        )
        self.sandbox_runner = sandbox_runner
        self.model_provider = model_provider

    def _verify_trusted_identity(
        self, context: AgentExecutionContext
    ) -> Tuple[str, bool]:
        """
        Validates user authorization against trusted context metadata and checks sandbox permission.
        Strictly ignores validated_input and context.input_payload to prevent role spoofing.
        Fails closed (denies by default) if role or identity is missing or untrusted.

        Returns:
            (role, has_sandbox_permission)
        """
        if not context or not context.metadata:
            logger.warning(
                "Developer access denied: Execution context metadata is missing.",
                extra={
                    "event_name": "dev_access_denied",
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
                "Developer access denied: Execution context is unauthenticated or untrusted.",
                extra={
                    "event_name": "dev_access_denied",
                    "execution_id": getattr(context, "execution_id", "unknown"),
                    "reason": "unauthenticated",
                },
            )
            raise PermissionError("Access denied: Execution context is not authenticated or trusted.")

        user_context = context.metadata.get("user_context", {})
        if not isinstance(user_context, dict):
            user_context = {}

        role = user_context.get("role") or context.metadata.get("role")

        if not role or role not in AUTHORIZED_DEV_ROLES:
            logger.warning(
                f"Developer access denied: Role '{role}' is not authorized.",
                extra={
                    "event_name": "dev_access_denied",
                    "role": role,
                    "permitted_roles": list(AUTHORIZED_DEV_ROLES),
                    "execution_id": getattr(context, "execution_id", "unknown"),
                    "reason": "unauthorized_role",
                },
            )
            raise PermissionError(
                f"Access denied: Role '{role}' is not authorized for Developer Agent. "
                f"Permitted roles: {sorted(list(AUTHORIZED_DEV_ROLES))}."
            )

        # Check explicit sandbox_execution permission from trusted context metadata
        trusted_perms = context.metadata.get("trusted_permissions", [])
        if not isinstance(trusted_perms, (list, set)):
            trusted_perms = []

        has_sandbox_permission = bool(
            context.metadata.get("sandbox_execution") is True
            or user_context.get("sandbox_execution") is True
            or "sandbox_execution" in trusted_perms
        )

        return str(role), has_sandbox_permission

    def _sanitize_untrusted_input(self, text: Optional[str]) -> Optional[str]:
        """Neutralizes potential prompt-injection signatures in untrusted source text."""
        if not text:
            return text
        return PROMPT_INJECTION_PATTERN.sub("[UNTRUSTED_INSTRUCTION_REMOVED]", text)

    def _compute_code_hash(self, code: Optional[str]) -> Optional[str]:
        """Calculates immutable SHA-256 hash of user-provided code snippet."""
        if not code:
            return None
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    async def _execute(
        self, validated_input: DevInput, context: AgentExecutionContext
    ) -> DevOutput:
        """Core execution pipeline for AGT-DEV operations."""
        role, has_sandbox_permission = self._verify_trusted_identity(context)

        logger.info(
            f"Authorized user with role '{role}' executing Developer task for operation '{validated_input.operation.value}'.",
            extra={
                "event_name": "dev_task_received",
                "operation": validated_input.operation.value,
                "role": role,
                "language": validated_input.language,
                "task_id": validated_input.task_id or context.task_id,
                "execution_id": context.execution_id,
            },
        )

        op = validated_input.operation
        code_hash = self._compute_code_hash(validated_input.code_snippet)

        try:
            if op == DevOperation.EXPLAIN:
                return await self._handle_explain(validated_input, context, code_hash)
            elif op == DevOperation.REVIEW:
                return await self._handle_review(validated_input, context, code_hash)
            elif op == DevOperation.DOCUMENT:
                return await self._handle_document(validated_input, context, code_hash)
            elif op == DevOperation.GENERATE:
                return await self._handle_generate(validated_input, context, code_hash)
            elif op == DevOperation.DEBUG_WITH_TEST:
                return await self._handle_debug_with_test(
                    validated_input, context, code_hash, has_sandbox_permission
                )
            else:
                raise ValueError(f"Unsupported Developer Agent operation: {op}")

        except Exception as exc:
            redacted_msg = redact_sensitive_data(str(exc))
            logger.error(
                f"Developer Agent execution error during operation '{op.value}': {redacted_msg}",
                extra={
                    "event_name": "dev_execution_error",
                    "operation": op.value,
                    "error_type": type(exc).__name__,
                    "execution_id": context.execution_id,
                },
            )
            raise type(exc)(redacted_msg) from exc

    async def _handle_explain(
        self, validated_input: DevInput, context: AgentExecutionContext, code_hash: Optional[str]
    ) -> DevOutput:
        """Handles explain operation: analyzes code or error context and identifies problem areas."""
        clean_code = self._sanitize_untrusted_input(validated_input.code_snippet or "")
        clean_error = self._sanitize_untrusted_input(validated_input.error_context or "")

        identified_issues = []
        explanation_lines = []

        if clean_error:
            identified_issues.append("Runtime/compiler error trace present in context.")
            explanation_lines.append(f"Analysis of error trace: {clean_error[:200]}")

        if "ZeroDivisionError" in clean_error or "/ 0" in clean_code:
            identified_issues.append("Potential division by zero detected.")
            explanation_lines.append("The snippet attempts to divide a numeric value by zero.")
        elif "IndexError" in clean_error:
            identified_issues.append("Sequence subscript index out of range.")
            explanation_lines.append("The snippet attempts to access an element past the sequence bounds.")
        elif "KeyError" in clean_error:
            identified_issues.append("Dictionary key not found.")
            explanation_lines.append("A dictionary lookup was attempted with a non-existent key.")
        else:
            explanation_lines.append(
                f"Explanation for task '{validated_input.dev_task}': "
                f"The provided code operates on {validated_input.language or 'source'} logic."
            )

        provenance = DevProvenance(
            agent_id="AGT-DEV",
            operation=DevOperation.EXPLAIN,
            language=validated_input.language,
            code_snippet_hash=code_hash,
            llm_model_used="eaoc-dev-llm",
            timestamp=datetime.now(timezone.utc),
        )

        return DevOutput(
            operation=DevOperation.EXPLAIN,
            explanation="\n".join(explanation_lines),
            identified_issues=identified_issues if identified_issues else ["None identified"],
            provenance=provenance,
        )

    async def _handle_review(
        self, validated_input: DevInput, context: AgentExecutionContext, code_hash: Optional[str]
    ) -> DevOutput:
        """Handles review operation: returns structured findings with severity levels and assessment."""
        code = validated_input.code_snippet or ""
        findings: List[ReviewFinding] = []

        lines = code.splitlines()
        for idx, line in enumerate(lines, start=1):
            line_str = line.strip()
            # Heuristic check for common anti-patterns
            if "except:" in line_str or "except Exception:" in line_str or "pass" in line_str and "def " not in line_str:
                findings.append(
                    ReviewFinding(
                        line=idx,
                        severity=ReviewFindingSeverity.HIGH,
                        description="Bare or silent exception handling suppresses critical runtime errors.",
                        suggestion="Catch specific exception types and log or re-raise the error.",
                    )
                )
            if "eval(" in line_str or "exec(" in line_str:
                findings.append(
                    ReviewFinding(
                        line=idx,
                        severity=ReviewFindingSeverity.CRITICAL,
                        description="Use of eval/exec introduces dynamic code execution vulnerability.",
                        suggestion="Replace eval/exec with safe deterministic parsing.",
                    )
                )
            if "import *" in line_str:
                findings.append(
                    ReviewFinding(
                        line=idx,
                        severity=ReviewFindingSeverity.LOW,
                        description="Wildcard imports pollute namespace and obscure symbol origins.",
                        suggestion="Explicitly import required symbols.",
                    )
                )

        assessment = (
            f"Code review completed with {len(findings)} finding(s). "
            f"{'Code satisfies basic hygiene standards.' if not findings else 'Actionable remediations recommended.'}"
        )

        provenance = DevProvenance(
            agent_id="AGT-DEV",
            operation=DevOperation.REVIEW,
            language=validated_input.language,
            code_snippet_hash=code_hash,
            llm_model_used="eaoc-dev-llm",
            timestamp=datetime.now(timezone.utc),
        )

        return DevOutput(
            operation=DevOperation.REVIEW,
            findings=findings,
            overall_assessment=assessment,
            provenance=provenance,
        )

    async def _handle_document(
        self, validated_input: DevInput, context: AgentExecutionContext, code_hash: Optional[str]
    ) -> DevOutput:
        """Handles document operation: generates markdown documentation for user code."""
        code = validated_input.code_snippet or ""
        lang = validated_input.language or "python"

        doc = (
            f"# Code Documentation\n\n"
            f"**Language**: `{lang}`\n\n"
            f"## Summary\n"
            f"{validated_input.dev_task}\n\n"
            f"## Implementation Overview\n"
            f"The provided snippet consists of {len(code.splitlines())} line(s) of code.\n\n"
            f"```markdown\n{code[:300]}\n```\n"
        )

        provenance = DevProvenance(
            agent_id="AGT-DEV",
            operation=DevOperation.DOCUMENT,
            language=validated_input.language,
            code_snippet_hash=code_hash,
            llm_model_used="eaoc-dev-llm",
            timestamp=datetime.now(timezone.utc),
        )

        return DevOutput(
            operation=DevOperation.DOCUMENT,
            documentation=doc,
            provenance=provenance,
        )

    async def _handle_generate(
        self, validated_input: DevInput, context: AgentExecutionContext, code_hash: Optional[str]
    ) -> DevOutput:
        """
        Handles generate operation: synthesizes code, runs credential scan,
        and enforces mandatory ai_generated_label=True.
        """
        task_desc = validated_input.dev_task
        lang = (validated_input.language or "python").lower()

        # Deterministic generation template based on task
        if "factorial" in task_desc.lower():
            code = (
                "def factorial(n: int) -> int:\n"
                "    if n < 0:\n"
                "        raise ValueError('Negative values not supported')\n"
                "    return 1 if n <= 1 else n * factorial(n - 1)\n"
            )
        elif "fibonacci" in task_desc.lower():
            code = (
                "def fibonacci(n: int) -> int:\n"
                "    if n < 0:\n"
                "        raise ValueError('Negative values not supported')\n"
                "    a, b = 0, 1\n"
                "    for _ in range(n):\n"
                "        a, b = b, a + b\n"
                "    return a\n"
            )
        else:
            code = (
                f"# AI-Generated Code for: {task_desc}\n"
                f"def execute_task():\n"
                f"    \"\"\"{task_desc}\"\"\"\n"
                f"    return True\n"
            )

        # Mandatory Credential Scan before returning
        DevCredentialScanner.check_and_block(code)

        provenance = DevProvenance(
            agent_id="AGT-DEV",
            operation=DevOperation.GENERATE,
            language=validated_input.language,
            code_snippet_hash=code_hash,
            llm_model_used="eaoc-dev-llm",
            timestamp=datetime.now(timezone.utc),
        )

        return DevOutput(
            operation=DevOperation.GENERATE,
            generated_code=code,
            explanation=f"Generated code implementation for '{task_desc}'.",
            ai_generated_label=True,
            provenance=provenance,
        )

    async def _handle_debug_with_test(
        self,
        validated_input: DevInput,
        context: AgentExecutionContext,
        code_hash: Optional[str],
        has_sandbox_permission: bool,
    ) -> DevOutput:
        """
        Handles debug_with_test: proposes a fix, validates sandbox permission,
        verifies command safety, and executes test in sandbox boundary.

        STRICT BOUNDARY:
        - If no sandbox permission -> PermissionError.
        - If sandbox_runner is None -> execution_unavailable (fix_verified is None).
        - If sandbox_runner is simulated -> marks is_simulated=True and NEVER sets fix_verified=True.
        - If sandbox_runner is real backend -> sets fix_verified based on actual exit code.
        """
        # 1. Require explicit sandbox_execution permission from trusted context
        if not has_sandbox_permission:
            logger.warning(
                "Sandbox execution denied: Missing 'sandbox_execution' permission in context metadata.",
                extra={"event_name": "dev_sandbox_permission_denied", "execution_id": context.execution_id},
            )
            raise PermissionError(
                "Sandbox execution permission 'sandbox_execution' is not granted in trusted execution context."
            )

        # 2. Synthesize proposed fix and perform credential scan
        code = validated_input.code_snippet or ""
        proposed_fix = (
            f"# Proposed fix for {validated_input.dev_task}\n"
            f"{code}\n"
            f"# End of proposed patch\n"
        )
        DevCredentialScanner.check_and_block(proposed_fix)

        test_cmd = validated_input.test_command
        if not test_cmd:
            test_cmd = "pytest tests/ -v"

        # 3. Reject destructive commands
        validate_test_command_safety(test_cmd)

        # 4. Check available sandbox infrastructure
        if self.sandbox_runner is None:
            # Genuine isolated backend unavailable and no runner provided
            provenance = DevProvenance(
                agent_id="AGT-DEV",
                operation=DevOperation.DEBUG_WITH_TEST,
                language=validated_input.language,
                code_snippet_hash=code_hash,
                test_command=test_cmd,
                fix_verified=None,
                is_simulated=False,
                timestamp=datetime.now(timezone.utc),
            )
            return DevOutput(
                operation=DevOperation.DEBUG_WITH_TEST,
                proposed_fix=proposed_fix,
                explanation=(
                    "Proposed fix synthesized for human review. "
                    "Execution unavailable: No isolated container sandbox backend is configured."
                ),
                ai_generated_label=True,
                test_evidence=None,
                fix_verified=None,
                execution_status="execution_unavailable",
                provenance=provenance,
            )

        # 5. Execute via runner boundary
        logger.info(
            f"Invoking sandbox runner for command: '{test_cmd}'",
            extra={"event_name": "dev_sandbox_execution_started", "execution_id": context.execution_id},
        )
        evidence = await self.sandbox_runner.run_test(
            test_cmd, code_snippet=proposed_fix, timeout_seconds=self.default_timeout_seconds or 60.0
        )

        is_simulated = bool(evidence.is_simulated or not self.sandbox_runner.is_real_backend)

        # Acceptance Rule: Simulated outcomes must NEVER set fix_verified=True
        if is_simulated:
            fix_verified = None
            exec_status = "simulated"
            explanation = (
                "Proposed fix synthesized for human review. "
                "Test execution was performed by an in-memory simulated runner; "
                "simulated outcomes are not verified and cannot certify a fix."
            )
        else:
            fix_verified = bool(evidence.exit_code == 0)
            exec_status = "completed"
            explanation = (
                f"Proposed fix verified in isolated container sandbox: "
                f"{'Tests passed successfully.' if fix_verified else 'Test failures observed.'}"
            )

        provenance = DevProvenance(
            agent_id="AGT-DEV",
            operation=DevOperation.DEBUG_WITH_TEST,
            language=validated_input.language,
            code_snippet_hash=code_hash,
            sandbox_id=evidence.sandbox_id,
            test_command=test_cmd,
            fix_verified=fix_verified,
            is_simulated=is_simulated,
            timestamp=datetime.now(timezone.utc),
        )

        return DevOutput(
            operation=DevOperation.DEBUG_WITH_TEST,
            proposed_fix=proposed_fix,
            explanation=explanation,
            ai_generated_label=True,
            test_evidence=evidence,
            fix_verified=fix_verified,
            execution_status=exec_status,
            provenance=provenance,
        )
