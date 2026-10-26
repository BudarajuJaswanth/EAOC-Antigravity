"""
EAOC Tool Interceptor & Secure Execution Pipeline
Implements the canonical 8-step tool execution pipeline conforming to Phase 0:
- Arch Part B Section 7.2 (Tool Execution Pipeline)
- Arch Part B Section 7.4 (Tool Timeout and Error Handling)
- Arch Part B Section 8.3 & 8.6 (RBAC Decision Hierarchy & Security Violation Handling)
- FRS Part A FR-PERM-001 & FR-PERM-002 (Manifest Enforcement & Tool Call Interception)
- FRS Part B FR-TOOL-001 & FR-TOOL-002 (Tool Registry & Input Sanitization)
- Arch Part A Section 3.8 & 6.2 (Tool Layer & tool_call_id)

Pipeline steps:
1. Registry Lookup (ToolNotFoundError -> fail-closed)
2. Permission Evaluation via AGT-SEC (ToolPermissionDenied -> fail-closed, non-retryable)
3. Input Schema Validation (ToolInputValidationError -> TOOL_INPUT_INVALID audit)
4. Attempt Audit (TOOL_CALL_ATTEMPTED with UUID v4 tool_call_id; fail-closed on audit failure)
5. Tool Execution & Timeout Enforcement (Worker thread; TOOL_TIMEOUT / TOOL_EXECUTION_ERROR)
6. Output Schema Validation (ToolOutputSchemaError -> TOOL_CALL_FAILED)
7. Completion / Failure Audit (TOOL_CALL_COMPLETED / TOOL_CALL_FAILED with duration_ms)
8. Return typed ToolExecutionResult

Cancellation Limitation Note:
Python synchronous threads cannot be asynchronously terminated. When asyncio.wait_for
cancels the awaitable on timeout, control yields back immediately to the asyncio event loop
and returns a TOOL_TIMEOUT error result. However, the worker thread executing the underlying
synchronous tool may continue running until completion. EAOC does not claim hard OS thread cancellation.
"""

import asyncio
from datetime import datetime, timezone
import time
from typing import Any, Callable, Dict, Optional, Union
import uuid

from pydantic import ValidationError

from eaoc.agents.security_policy.engine import SecurityPolicyEngine
from eaoc.agents.security_policy.schemas import PolicyViolation
from eaoc.observability import get_logger, redact_sensitive_data
from eaoc.schemas.domain import (
    AgentID,
    ErrorInfo,
    TaskStatus,
    ToolExecutionContext,
    ToolExecutionResult,
)
from eaoc.tools.base import BaseTool
from eaoc.tools.exceptions import (
    ToolError,
    ToolExecutionError,
    ToolInputValidationError,
    ToolNotFoundError,
    ToolOutputSchemaError,
    ToolPermissionDenied,
    ToolTimeoutError,
)
from eaoc.tools.registry import ToolRegistry

logger = get_logger("eaoc.tools.interceptor")

# Tool timeout mappings from Phase 0 Arch Part B Section 7.4
DEFAULT_TOOL_TIMEOUTS: Dict[str, float] = {
    "llm_inference": 120.0,
    "pgvector_search": 10.0,
    "postgresql_read": 15.0,
    "document_retrieve": 5.0,
    "sandbox_execution": 60.0,
    "embedding_generate": 30.0,
}

# Timeout retryability classification from Phase 0 Arch Part B Section 7.4
TOOL_TIMEOUT_RETRYABLE: Dict[str, bool] = {
    "llm_inference": True,
    "pgvector_search": True,
    "postgresql_read": True,
    "document_retrieve": True,
    "sandbox_execution": False,  # Non-retryable
    "embedding_generate": True,
}

DEFAULT_TIMEOUT_SECONDS: float = 30.0

AuditCallback = Callable[[str, Dict[str, Any]], None]


class ToolInterceptor:
    """
    Tool Interceptor and Secure Execution Pipeline.

    Enforces pre-execution policy checks, schema validation, timeout limits,
    audit emission, and output verification on all EAOC enterprise tool calls.
    Fails closed: no tool is executed without an authorized policy decision.
    """

    def __init__(
        self,
        tool_registry: Optional[ToolRegistry] = None,
        policy_engine: Optional[SecurityPolicyEngine] = None,
        audit_callback: Optional[AuditCallback] = None,
        logger_instance: Optional[Any] = None,
        event_bus: Optional[Any] = None,
    ):
        """
        Dependency injection for registry, policy engine, audit loggers, and optional event bus.
        """
        self.tool_registry = tool_registry or ToolRegistry()
        self.policy_engine = policy_engine or SecurityPolicyEngine()
        self.audit_callback = audit_callback
        self.logger = logger_instance or logger
        self.event_bus = event_bus
        self._tracked_tasks: set[asyncio.Task] = set()

    def _emit_tool_stream_event(
        self,
        workflow_id: Optional[str],
        tool_call_id: str,
        target_tool_id: str,
        agent_id: str,
        action: str,
        parameters: Dict[str, Any],
        duration_ms: Optional[float] = None,
        output_summary: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """
        Asynchronously emits a sanitized TOOL_INVOKED stream event.
        Guaranteed non-blocking: failures to publish never affect tool execution.
        """
        if not self.event_bus or not workflow_id:
            return
        try:
            from eaoc.schemas.events import StreamEventType, WorkflowStreamEvent, format_event_id
            stage_map = {
                "attempted": "att",
                "completed": "cmp",
                "failed": "err",
            }
            stage_code = stage_map.get(action.lower().strip(), action[:3].lower())
            event_id = format_event_id(
                workflow_id=workflow_id,
                epoch=1,
                step_index=-1,
                event_type=StreamEventType.TOOL_INVOKED,
                seq=None,
                discriminator=f"tc-{tool_call_id}-{stage_code}",
            )
            event = WorkflowStreamEvent(
                event_id=event_id,
                event_type=StreamEventType.TOOL_INVOKED,
                workflow_id=workflow_id,
                tenant_id="default",
                step_index=-1,
                payload={
                    "tool_id": target_tool_id,
                    "tool_call_id": tool_call_id,
                    "agent_id": agent_id,
                    "action": action,
                    "duration_ms": duration_ms,
                    "parameters": redact_sensitive_data(parameters),
                    "output_summary": output_summary,
                    "error": error,
                },
            )
            task = asyncio.create_task(self.event_bus.publish(event))
            self._tracked_tasks.add(task)
            task.add_done_callback(self._tracked_tasks.discard)
        except Exception as exc:
            self.logger.warning(f"Failed to schedule tool stream event for '{tool_call_id}': {exc}")

    def _emit_audit(self, event_name: str, payload: Dict[str, Any]) -> None:
        """
        Emits a structured audit event to the logger and injected audit callback.
        Redacts sensitive data from the payload before logging or callback delivery.
        """
        safe_payload = redact_sensitive_data(payload)
        self.logger.info(
            f"Audit event '{event_name}' emitted.",
            extra={"event_name": event_name, **safe_payload},
        )
        if self.audit_callback:
            self.audit_callback(event_name, safe_payload)

    def _safe_emit_failure(
        self,
        tool_call_id: str,
        agent_id: str,
        tool_id: str,
        task_id: Optional[str],
        workflow_id: Optional[str],
        duration_ms: float,
        error_code: str,
        error_message: str,
    ) -> None:
        """Safely emits TOOL_CALL_FAILED without letting audit failures suppress the result."""
        payload = {
            "tool_call_id": tool_call_id,
            "agent_id": agent_id,
            "tool_id": tool_id,
            "task_id": task_id,
            "workflow_id": workflow_id,
            "duration_ms": duration_ms,
            "error_code": error_code,
            "error_message": redact_sensitive_data(error_message),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._emit_audit("TOOL_CALL_FAILED", payload)
        except Exception as exc:
            self.logger.error(
                f"Failed to emit TOOL_CALL_FAILED audit event: {redact_sensitive_data(str(exc))}",
                extra={"tool_call_id": tool_call_id, "event_name": "audit_emission_failed"},
            )

    def _safe_emit_completed(
        self,
        tool_call_id: str,
        agent_id: str,
        tool_id: str,
        task_id: Optional[str],
        workflow_id: Optional[str],
        duration_ms: float,
    ) -> None:
        """Safely emits TOOL_CALL_COMPLETED without letting audit failures suppress the result."""
        payload = {
            "tool_call_id": tool_call_id,
            "agent_id": agent_id,
            "tool_id": tool_id,
            "task_id": task_id,
            "workflow_id": workflow_id,
            "duration_ms": duration_ms,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._emit_audit("TOOL_CALL_COMPLETED", payload)
        except Exception as exc:
            self.logger.error(
                f"Failed to emit TOOL_CALL_COMPLETED audit event: {redact_sensitive_data(str(exc))}",
                extra={"tool_call_id": tool_call_id, "event_name": "audit_emission_failed"},
            )

    async def call(
        self,
        agent_id: Optional[Union[AgentID, str, ToolExecutionContext]] = None,
        tool_id: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        *,
        context: Optional[ToolExecutionContext] = None,
        timeout_seconds: Optional[float] = None,
        task_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> ToolExecutionResult:
        """
        Executes a registered tool through the canonical 8-step pipeline.

        Supports invocation with either:
        - ToolExecutionContext object as first argument
        - Disaggregated positional parameters (agent_id, tool_id, parameters)
        """
        # Unpack ToolExecutionContext if provided
        if isinstance(agent_id, ToolExecutionContext):
            context = agent_id
            agent_id = context.agent_id
            tool_id = context.tool_id
            parameters = context.parameters
            task_id = context.task_id
            workflow_id = context.workflow_id
        elif context is not None:
            agent_id = agent_id or context.agent_id
            tool_id = tool_id or context.tool_id
            parameters = parameters if parameters is not None else context.parameters
            task_id = task_id or context.task_id
            workflow_id = workflow_id or context.workflow_id

        # Generate unique UUID v4 tool_call_id for this single invocation
        tool_call_id = str(uuid.uuid4())

        canonical_agent_str = (
            agent_id.value
            if isinstance(agent_id, AgentID)
            else str(agent_id).strip()
            if agent_id is not None
            else "unknown"
        )
        target_tool_id = str(tool_id or "").strip()
        params = parameters or {}

        # -------------------------------------------------------------
        # STEP 1: Registry Lookup
        # -------------------------------------------------------------
        tool: Optional[BaseTool] = None
        if target_tool_id:
            tool = self.tool_registry.get_tool(target_tool_id)

        if tool is None:
            err_msg = f"Tool with ID '{target_tool_id}' is not registered in ToolRegistry."
            self.logger.warning(
                f"Tool lookup failed: {err_msg}",
                extra={
                    "event_name": "tool_lookup_failed",
                    "tool_call_id": tool_call_id,
                    "tool_id": target_tool_id,
                    "agent_id": canonical_agent_str,
                },
            )
            return ToolExecutionResult(
                execution_id=tool_call_id,
                tool_id=target_tool_id or "unknown",
                status=TaskStatus.FAILED,
                output_data={},
                duration_ms=0.0,
                error=ErrorInfo(
                    code="TOOL_NOT_FOUND",
                    message=redact_sensitive_data(err_msg),
                    error_type="ToolNotFoundError",
                    correlation_id=correlation_id,
                    retryable=False,
                ),
            )

        # -------------------------------------------------------------
        # STEP 2: Permission Check (AGT-SEC.check_manifest)
        # -------------------------------------------------------------
        try:
            if not self.policy_engine:
                raise RuntimeError("SecurityPolicyEngine dependency is unavailable.")

            allowed, violations, policy_refs, risk_level = (
                self.policy_engine.evaluate_tool_permission(
                    agent_id=canonical_agent_str,
                    tool_id=target_tool_id,
                    parameters=params,
                )
            )
        except Exception as exc:
            # Policy evaluation error -> FAIL CLOSED immediately
            safe_err = redact_sensitive_data(str(exc))
            self.logger.error(
                f"Policy evaluation raised exception for tool '{target_tool_id}': {safe_err}. Action blocked fail-closed.",
                extra={
                    "event_name": "policy_check_exception",
                    "tool_call_id": tool_call_id,
                    "tool_id": target_tool_id,
                    "agent_id": canonical_agent_str,
                },
            )
            return ToolExecutionResult(
                execution_id=tool_call_id,
                tool_id=target_tool_id,
                status=TaskStatus.FAILED,
                output_data={},
                duration_ms=0.0,
                error=ErrorInfo(
                    code="POLICY_CHECK_FAILED",
                    message=f"Security policy evaluation failed: {safe_err}",
                    error_type="ToolError",
                    correlation_id=correlation_id,
                    retryable=False,
                ),
            )

        if not allowed:
            violation_descs = (
                [v.description for v in violations]
                if violations
                else ["Action blocked by security policy."]
            )
            safe_message = "; ".join(violation_descs)

            # Audit: TOOL_PERMISSION_DENIED
            denial_payload = {
                "tool_call_id": tool_call_id,
                "agent_id": canonical_agent_str,
                "tool_id": target_tool_id,
                "task_id": task_id,
                "workflow_id": workflow_id,
                "violations": [v.model_dump() for v in violations],
                "policy_references": policy_refs,
                "risk_level": risk_level,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            try:
                self._emit_audit("TOOL_PERMISSION_DENIED", denial_payload)
            except Exception as audit_exc:
                self.logger.error(
                    f"Failed to emit TOOL_PERMISSION_DENIED audit log: {redact_sensitive_data(str(audit_exc))}",
                    extra={"tool_call_id": tool_call_id},
                )

            self.logger.warning(
                f"Tool permission denied: Agent '{canonical_agent_str}' is not authorized to invoke tool '{target_tool_id}'.",
                extra={
                    "event_name": "tool_permission_denied",
                    "tool_call_id": tool_call_id,
                    "tool_id": target_tool_id,
                    "agent_id": canonical_agent_str,
                    "violations": [v.model_dump() for v in violations],
                },
            )

            # Strictly non-retryable by definition (Arch Part B Section 8.6)
            return ToolExecutionResult(
                execution_id=tool_call_id,
                tool_id=target_tool_id,
                status=TaskStatus.FAILED,
                output_data={},
                duration_ms=0.0,
                error=ErrorInfo(
                    code="TOOL_PERMISSION_DENIED",
                    message=redact_sensitive_data(
                        f"Permission denied: Agent '{canonical_agent_str}' is not authorized to invoke tool '{target_tool_id}'. {safe_message}"
                    ),
                    error_type="ToolPermissionDenied",
                    correlation_id=correlation_id,
                    retryable=False,
                    details={
                        "violations": [v.model_dump() for v in violations],
                        "policy_references": policy_refs,
                        "risk_level": risk_level,
                    },
                ),
            )

        # -------------------------------------------------------------
        # STEP 3: Input Validation (Pydantic against tool.input_schema)
        # -------------------------------------------------------------
        validated_params: Dict[str, Any] = {}
        if tool.metadata.input_schema is None:
            validated_params = params
        else:
            try:
                validated_model = tool.metadata.input_schema.model_validate(params)
                validated_params = validated_model.model_dump()
            except (ValidationError, ValueError) as exc:
                safe_err = redact_sensitive_data(str(exc))
                input_invalid_payload = {
                    "tool_call_id": tool_call_id,
                    "agent_id": canonical_agent_str,
                    "tool_id": target_tool_id,
                    "task_id": task_id,
                    "workflow_id": workflow_id,
                    "error": safe_err,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                try:
                    self._emit_audit("TOOL_INPUT_INVALID", input_invalid_payload)
                except Exception as audit_exc:
                    self.logger.error(
                        f"Failed to emit TOOL_INPUT_INVALID audit log: {redact_sensitive_data(str(audit_exc))}",
                        extra={"tool_call_id": tool_call_id},
                    )

                self.logger.warning(
                    f"Tool input validation failed for '{target_tool_id}': {safe_err}",
                    extra={
                        "event_name": "tool_input_invalid",
                        "tool_call_id": tool_call_id,
                        "tool_id": target_tool_id,
                        "agent_id": canonical_agent_str,
                    },
                )
                return ToolExecutionResult(
                    execution_id=tool_call_id,
                    tool_id=target_tool_id,
                    status=TaskStatus.FAILED,
                    output_data={},
                    duration_ms=0.0,
                    error=ErrorInfo(
                        code="TOOL_INPUT_INVALID",
                        message=f"Parameter validation failed for tool '{target_tool_id}': {safe_err}",
                        error_type="ToolInputValidationError",
                        correlation_id=correlation_id,
                        retryable=False,
                    ),
                )

        # -------------------------------------------------------------
        # STEP 4: Audit: TOOL_CALL_ATTEMPTED
        # -------------------------------------------------------------
        attempt_payload = {
            "tool_call_id": tool_call_id,
            "agent_id": canonical_agent_str,
            "tool_id": target_tool_id,
            "task_id": task_id,
            "workflow_id": workflow_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "parameters": redact_sensitive_data(validated_params),
        }
        try:
            self._emit_audit("TOOL_CALL_ATTEMPTED", attempt_payload)
            self._emit_tool_stream_event(
                workflow_id=workflow_id,
                tool_call_id=tool_call_id,
                target_tool_id=target_tool_id,
                agent_id=canonical_agent_str,
                action="attempted",
                parameters=validated_params,
            )
        except Exception as audit_exc:
            # Audit failure before execution must fail closed!
            safe_audit_err = redact_sensitive_data(str(audit_exc))
            self.logger.error(
                f"Failed to record TOOL_CALL_ATTEMPTED audit log: {safe_audit_err}. Execution blocked fail-closed.",
                extra={"tool_call_id": tool_call_id},
            )
            return ToolExecutionResult(
                execution_id=tool_call_id,
                tool_id=target_tool_id,
                status=TaskStatus.FAILED,
                output_data={},
                duration_ms=0.0,
                error=ErrorInfo(
                    code="AUDIT_ATTEMPT_FAILED",
                    message=f"Failed to record TOOL_CALL_ATTEMPTED audit log: {safe_audit_err}",
                    error_type="ToolError",
                    correlation_id=correlation_id,
                    retryable=False,
                ),
            )

        # -------------------------------------------------------------
        # STEP 5: Execute Tool Function (with timeout enforcement)
        # -------------------------------------------------------------
        effective_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else DEFAULT_TOOL_TIMEOUTS.get(
                target_tool_id.lower().strip(), DEFAULT_TIMEOUT_SECONDS
            )
        )
        is_timeout_retryable = TOOL_TIMEOUT_RETRYABLE.get(
            target_tool_id.lower().strip(), False
        )

        start_time = time.perf_counter()
        raw_result: Optional[ToolExecutionResult] = None
        duration_ms: float = 0.0

        try:
            # Run synchronous tool execution in worker thread without blocking event loop
            raw_result = await asyncio.wait_for(
                asyncio.to_thread(tool.execute, validated_params),
                timeout=effective_timeout,
            )
            duration_ms = (time.perf_counter() - start_time) * 1000.0

        except asyncio.TimeoutError:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            timeout_msg = f"Tool '{target_tool_id}' execution timed out after {effective_timeout}s."
            self.logger.error(
                timeout_msg,
                extra={
                    "event_name": "tool_timeout",
                    "tool_call_id": tool_call_id,
                    "tool_id": target_tool_id,
                    "duration_ms": duration_ms,
                },
            )
            self._safe_emit_failure(
                tool_call_id=tool_call_id,
                agent_id=canonical_agent_str,
                tool_id=target_tool_id,
                task_id=task_id,
                workflow_id=workflow_id,
                duration_ms=duration_ms,
                error_code="TOOL_TIMEOUT",
                error_message=timeout_msg,
            )
            self._emit_tool_stream_event(
                workflow_id=workflow_id,
                tool_call_id=tool_call_id,
                target_tool_id=target_tool_id,
                agent_id=canonical_agent_str,
                action="failed",
                parameters=validated_params,
                duration_ms=duration_ms,
                error=timeout_msg,
            )
            return ToolExecutionResult(
                execution_id=tool_call_id,
                tool_id=target_tool_id,
                status=TaskStatus.FAILED,
                output_data={},
                duration_ms=duration_ms,
                error=ErrorInfo(
                    code="TOOL_TIMEOUT",
                    message=timeout_msg,
                    error_type="ToolTimeoutError",
                    correlation_id=correlation_id,
                    retryable=is_timeout_retryable,
                ),
            )

        except Exception as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            safe_err = redact_sensitive_data(str(exc))
            self.logger.error(
                f"Tool '{target_tool_id}' execution raised unexpected exception: {safe_err}",
                extra={
                    "event_name": "tool_execution_error",
                    "tool_call_id": tool_call_id,
                    "tool_id": target_tool_id,
                    "duration_ms": duration_ms,
                },
            )
            self._safe_emit_failure(
                tool_call_id=tool_call_id,
                agent_id=canonical_agent_str,
                tool_id=target_tool_id,
                task_id=task_id,
                workflow_id=workflow_id,
                duration_ms=duration_ms,
                error_code="TOOL_EXECUTION_ERROR",
                error_message=safe_err,
            )
            self._emit_tool_stream_event(
                workflow_id=workflow_id,
                tool_call_id=tool_call_id,
                target_tool_id=target_tool_id,
                agent_id=canonical_agent_str,
                action="failed",
                parameters=validated_params,
                duration_ms=duration_ms,
                error=safe_err,
            )
            return ToolExecutionResult(
                execution_id=tool_call_id,
                tool_id=target_tool_id,
                status=TaskStatus.FAILED,
                output_data={},
                duration_ms=duration_ms,
                error=ErrorInfo(
                    code="TOOL_EXECUTION_ERROR",
                    message=f"Tool execution failed: {safe_err}",
                    error_type="ToolExecutionError",
                    correlation_id=correlation_id,
                    retryable=False,
                ),
            )

        # Check if underlying tool execution reported failure
        if raw_result is None or raw_result.status == TaskStatus.FAILED:
            err = raw_result.error if (raw_result and raw_result.error) else None
            err_code = err.code if err else "TOOL_EXECUTION_FAILED"
            err_msg = err.message if err else f"Tool '{target_tool_id}' execution failed."
            safe_msg = redact_sensitive_data(err_msg)

            self._safe_emit_failure(
                tool_call_id=tool_call_id,
                agent_id=canonical_agent_str,
                tool_id=target_tool_id,
                task_id=task_id,
                workflow_id=workflow_id,
                duration_ms=duration_ms,
                error_code=err_code,
                error_message=safe_msg,
            )
            self._emit_tool_stream_event(
                workflow_id=workflow_id,
                tool_call_id=tool_call_id,
                target_tool_id=target_tool_id,
                agent_id=canonical_agent_str,
                action="failed",
                parameters=validated_params,
                duration_ms=duration_ms,
                error=safe_msg,
            )
            return ToolExecutionResult(
                execution_id=tool_call_id,
                tool_id=target_tool_id,
                status=TaskStatus.FAILED,
                output_data={},
                duration_ms=duration_ms,
                error=ErrorInfo(
                    code=err_code,
                    message=safe_msg,
                    error_type=err.error_type if err else "ToolError",
                    correlation_id=correlation_id,
                    retryable=err.retryable if err else False,
                    details=err.details if err else {},
                ),
            )

        # -------------------------------------------------------------
        # STEP 6: Output Validation (Pydantic against tool.output_schema)
        # -------------------------------------------------------------
        output_payload: Dict[str, Any] = {}
        if tool.metadata.output_schema is None:
            output_payload = raw_result.output_data or {}
        else:
            try:
                validated_out = tool.metadata.output_schema.model_validate(
                    raw_result.output_data or {}
                )
                output_payload = validated_out.model_dump()
            except (ValidationError, ValueError) as exc:
                safe_err = redact_sensitive_data(str(exc))
                self.logger.warning(
                    f"Tool output schema validation failed for '{target_tool_id}': {safe_err}",
                    extra={
                        "event_name": "tool_output_invalid",
                        "tool_call_id": tool_call_id,
                        "tool_id": target_tool_id,
                        "agent_id": canonical_agent_str,
                    },
                )
                self._safe_emit_failure(
                    tool_call_id=tool_call_id,
                    agent_id=canonical_agent_str,
                    tool_id=target_tool_id,
                    task_id=task_id,
                    workflow_id=workflow_id,
                    duration_ms=duration_ms,
                    error_code="TOOL_OUTPUT_SCHEMA_ERROR",
                    error_message=safe_err,
                )
                return ToolExecutionResult(
                    execution_id=tool_call_id,
                    tool_id=target_tool_id,
                    status=TaskStatus.FAILED,
                    output_data={},
                    duration_ms=duration_ms,
                    error=ErrorInfo(
                        code="TOOL_OUTPUT_SCHEMA_ERROR",
                        message=f"Output schema validation failed for tool '{target_tool_id}': {safe_err}",
                        error_type="ToolOutputSchemaError",
                        correlation_id=correlation_id,
                        retryable=False,
                    ),
                )

        # -------------------------------------------------------------
        # STEP 7: Audit: TOOL_CALL_COMPLETED
        # -------------------------------------------------------------
        self._safe_emit_completed(
            tool_call_id=tool_call_id,
            agent_id=canonical_agent_str,
            tool_id=target_tool_id,
            task_id=task_id,
            workflow_id=workflow_id,
            duration_ms=duration_ms,
        )
        self._emit_tool_stream_event(
            workflow_id=workflow_id,
            tool_call_id=tool_call_id,
            target_tool_id=target_tool_id,
            agent_id=canonical_agent_str,
            action="completed",
            parameters=validated_params,
            duration_ms=duration_ms,
            output_summary=str(output_payload)[:200] if output_payload else None,
        )

        # -------------------------------------------------------------
        # STEP 8: Return ToolExecutionResult to Agent
        # -------------------------------------------------------------
        return ToolExecutionResult(
            execution_id=tool_call_id,
            tool_id=target_tool_id,
            status=TaskStatus.COMPLETED,
            output_data=output_payload,
            duration_ms=duration_ms,
            error=None,
        )

    def call_sync(
        self,
        agent_id: Optional[Union[AgentID, str, ToolExecutionContext]] = None,
        tool_id: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        *,
        context: Optional[ToolExecutionContext] = None,
        timeout_seconds: Optional[float] = None,
        task_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> ToolExecutionResult:
        """
        Synchronous convenience wrapper for call().
        Runs the async call() coroutine to completion using the current event loop or asyncio.run.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        coro = self.call(
            agent_id=agent_id,
            tool_id=tool_id,
            parameters=parameters,
            context=context,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            workflow_id=workflow_id,
            correlation_id=correlation_id,
        )

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        else:
            return asyncio.run(coro)
