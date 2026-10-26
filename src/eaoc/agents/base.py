"""
EAOC Base Agent Contract & Execution Interface
Defines the abstract base contract for all 12 approved logical agents in EAOC.
Enforces typed Pydantic input/output validation, execution context matching,
cancellable asynchronous timeout management, and secret-redacted error handling.
"""

from abc import ABC, abstractmethod
import asyncio
from typing import Any, Dict, Generic, Optional, Type, TypeVar
from pydantic import BaseModel, ValidationError

from eaoc.observability import Timer, get_logger, redact_sensitive_data
from eaoc.schemas.domain import (
    AgentExecutionContext,
    AgentExecutionResult,
    AgentID,
    ErrorInfo,
    TaskStatus,
)

logger = get_logger("eaoc.agents.base")

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class BaseAgent(ABC, Generic[InputT, OutputT]):
    """
    Abstract base class for all 12 EAOC logical agents.

    Subclasses must declare:
      - agent_id: An approved AgentID enum member.
      - input_schema: A Pydantic BaseModel class for input validation.
      - output_schema: A Pydantic BaseModel class for output validation.
      - _execute(self, validated_input: InputT, context: AgentExecutionContext) -> OutputT

    Note on timeout semantics:
      Timeout cancellation uses `asyncio.wait_for`, which cancels the underlying
      asyncio.Task at the next yield point / await statement. Synchronous CPU-bound
      blocking operations will not be interrupted until control is yielded back to
      the event loop.
    """

    agent_id: AgentID
    input_schema: Type[InputT]
    output_schema: Type[OutputT]
    default_timeout_seconds: Optional[float] = None

    def __init__(
        self,
        agent_id: Optional[AgentID] = None,
        input_schema: Optional[Type[InputT]] = None,
        output_schema: Optional[Type[OutputT]] = None,
        default_timeout_seconds: Optional[float] = None,
    ):
        if agent_id is not None:
            self.agent_id = agent_id
        if input_schema is not None:
            self.input_schema = input_schema
        if output_schema is not None:
            self.output_schema = output_schema
        if default_timeout_seconds is not None:
            self.default_timeout_seconds = default_timeout_seconds

        # Validate agent_id declaration
        if not hasattr(self, "agent_id") or not isinstance(self.agent_id, AgentID):
            raise ValueError(
                f"Agent {self.__class__.__name__} must define a valid AgentID enum member."
            )

        # Validate schema declarations
        if not hasattr(self, "input_schema") or not (
            isinstance(self.input_schema, type) and issubclass(self.input_schema, BaseModel)
        ):
            raise ValueError(
                f"Agent {self.__class__.__name__} must define 'input_schema' as a Pydantic BaseModel subclass."
            )
        if not hasattr(self, "output_schema") or not (
            isinstance(self.output_schema, type) and issubclass(self.output_schema, BaseModel)
        ):
            raise ValueError(
                f"Agent {self.__class__.__name__} must define 'output_schema' as a Pydantic BaseModel subclass."
            )

        # Validate default timeout configuration
        if self.default_timeout_seconds is not None:
            if (
                isinstance(self.default_timeout_seconds, bool)
                or not isinstance(self.default_timeout_seconds, (int, float))
                or self.default_timeout_seconds <= 0
            ):
                raise ValueError(
                    f"default_timeout_seconds must be a positive number, got {self.default_timeout_seconds!r}."
                )

    def validate_input(self, raw_input: Any) -> InputT:
        """
        Validates raw input payload against the agent's input_schema.
        Accepts dictionaries or existing instances of input_schema.
        """
        if isinstance(raw_input, self.input_schema):
            return raw_input
        if isinstance(raw_input, dict):
            return self.input_schema.model_validate(raw_input)
        raise ValueError(
            f"Expected input as dict or {self.input_schema.__name__}, got {type(raw_input).__name__}."
        )

    def validate_output(self, raw_output: Any) -> OutputT:
        """
        Validates raw output payload against the agent's output_schema.
        Accepts dictionaries or existing instances of output_schema.
        """
        if isinstance(raw_output, self.output_schema):
            return raw_output
        if isinstance(raw_output, dict):
            return self.output_schema.model_validate(raw_output)
        raise ValueError(
            f"Expected output as dict or {self.output_schema.__name__}, got {type(raw_output).__name__}."
        )

    @abstractmethod
    async def _execute(
        self, validated_input: InputT, context: AgentExecutionContext
    ) -> OutputT:
        """
        Subclass-specific agent execution logic.
        Must be implemented by concrete agent classes.
        """
        pass

    async def execute(
        self,
        context: AgentExecutionContext,
        timeout_seconds: Optional[float] = None,
    ) -> AgentExecutionResult:
        """
        Main execution entry point for the logical agent.
        Orchestrates input validation, context ID verification, cancellable async
        timeout execution, output validation, and secret-safe error capture.
        """
        timer = Timer().start()

        # 1. Verify agent_id match
        if context.agent_id != self.agent_id:
            duration = timer.stop()
            received_id = (
                context.agent_id.value
                if isinstance(context.agent_id, AgentID)
                else str(context.agent_id)
            )
            msg = (
                f"Agent ID mismatch: Context directed to '{received_id}', "
                f"but agent implementation is '{self.agent_id.value}'."
            )
            logger.warning(
                msg,
                extra={
                    "event_name": "agent_id_mismatch",
                    "expected_agent_id": self.agent_id.value,
                    "received_agent_id": received_id,
                    "execution_id": context.execution_id,
                },
            )
            return AgentExecutionResult(
                execution_id=context.execution_id,
                agent_id=self.agent_id,
                status=TaskStatus.FAILED,
                output_payload={},
                duration_ms=duration,
                error=ErrorInfo(
                    code="AGENT_ID_MISMATCH",
                    message=msg,
                    error_type="ValueError",
                    request_id=context.request_id,
                    correlation_id=context.correlation_id,
                    retryable=False,
                    details={
                        "expected_agent_id": self.agent_id.value,
                        "received_agent_id": received_id,
                    },
                ),
            )

        # 2. Validate per-call timeout override if supplied
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or timeout_seconds <= 0
            ):
                duration = timer.stop()
                return AgentExecutionResult(
                    execution_id=context.execution_id,
                    agent_id=self.agent_id,
                    status=TaskStatus.FAILED,
                    output_payload={},
                    duration_ms=duration,
                    error=ErrorInfo(
                        code="INVALID_TIMEOUT_CONFIG",
                        message="Timeout configuration must be a positive number of seconds.",
                        error_type="ValueError",
                        request_id=context.request_id,
                        correlation_id=context.correlation_id,
                        retryable=False,
                        details={"invalid_timeout": str(timeout_seconds)},
                    ),
                )

        # 3. Validate input payload
        try:
            validated_input = self.validate_input(context.input_payload)
        except (ValidationError, ValueError) as exc:
            duration = timer.stop()
            safe_err = redact_sensitive_data(
                exc.errors() if hasattr(exc, "errors") and callable(exc.errors) else str(exc)
            )
            logger.warning(
                f"Agent '{self.agent_id.value}' input validation failed.",
                extra={"event_name": "agent_input_validation_failed", "execution_id": context.execution_id},
            )
            return AgentExecutionResult(
                execution_id=context.execution_id,
                agent_id=self.agent_id,
                status=TaskStatus.FAILED,
                output_payload={},
                duration_ms=duration,
                error=ErrorInfo(
                    code="INVALID_INPUT",
                    message=f"Agent '{self.agent_id.value}' input payload failed schema validation.",
                    error_type="ValidationError" if isinstance(exc, ValidationError) else "ValueError",
                    request_id=context.request_id,
                    correlation_id=context.correlation_id,
                    retryable=False,
                    details={"validation_error": safe_err},
                ),
            )

        # 4. Execute concrete _execute with cancellable timeout
        effective_timeout = (
            timeout_seconds if timeout_seconds is not None else self.default_timeout_seconds
        )

        try:
            if effective_timeout is not None:
                raw_output = await asyncio.wait_for(
                    self._execute(validated_input, context),
                    timeout=effective_timeout,
                )
            else:
                raw_output = await self._execute(validated_input, context)
        except asyncio.TimeoutError:
            duration = timer.stop()
            logger.error(
                f"Agent '{self.agent_id.value}' timed out after {effective_timeout}s.",
                extra={"event_name": "agent_timeout", "execution_id": context.execution_id},
            )
            return AgentExecutionResult(
                execution_id=context.execution_id,
                agent_id=self.agent_id,
                status=TaskStatus.FAILED,
                output_payload={},
                duration_ms=duration,
                error=ErrorInfo(
                    code="EXECUTION_TIMEOUT",
                    message=f"Agent execution timed out after {effective_timeout}s.",
                    error_type="TimeoutError",
                    request_id=context.request_id,
                    correlation_id=context.correlation_id,
                    retryable=True,
                    details={"timeout_seconds": effective_timeout},
                ),
            )
        except Exception as exc:
            duration = timer.stop()
            safe_err = redact_sensitive_data(str(exc))
            logger.error(
                f"Agent '{self.agent_id.value}' execution raised {exc.__class__.__name__}: {safe_err}",
                extra={"event_name": "agent_execution_error", "execution_id": context.execution_id},
            )
            return AgentExecutionResult(
                execution_id=context.execution_id,
                agent_id=self.agent_id,
                status=TaskStatus.FAILED,
                output_payload={},
                duration_ms=duration,
                error=ErrorInfo(
                    code="EXECUTION_ERROR",
                    message="An error occurred during agent execution.",
                    error_type=exc.__class__.__name__,
                    request_id=context.request_id,
                    correlation_id=context.correlation_id,
                    retryable=False,
                    details={"error": safe_err},
                ),
            )

        # 5. Validate output payload
        try:
            validated_output = self.validate_output(raw_output)
        except (ValidationError, ValueError) as exc:
            duration = timer.stop()
            safe_err = redact_sensitive_data(
                exc.errors() if hasattr(exc, "errors") and callable(exc.errors) else str(exc)
            )
            logger.warning(
                f"Agent '{self.agent_id.value}' output validation failed.",
                extra={"event_name": "agent_output_validation_failed", "execution_id": context.execution_id},
            )
            return AgentExecutionResult(
                execution_id=context.execution_id,
                agent_id=self.agent_id,
                status=TaskStatus.FAILED,
                output_payload={},
                duration_ms=duration,
                error=ErrorInfo(
                    code="INVALID_OUTPUT",
                    message=f"Agent '{self.agent_id.value}' output payload failed schema validation.",
                    error_type="ValidationError" if isinstance(exc, ValidationError) else "ValueError",
                    request_id=context.request_id,
                    correlation_id=context.correlation_id,
                    retryable=False,
                    details={"validation_error": safe_err},
                ),
            )

        # 6. Return successful execution result
        duration = timer.stop()
        return AgentExecutionResult(
            execution_id=context.execution_id,
            agent_id=self.agent_id,
            status=TaskStatus.COMPLETED,
            output_payload=validated_output.model_dump(),
            duration_ms=duration,
            error=None,
        )
