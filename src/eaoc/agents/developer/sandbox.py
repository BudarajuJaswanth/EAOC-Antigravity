"""
EAOC Developer Agent Sandbox Execution Contract & Boundaries
Defines the abstract interface for isolated sandbox test execution and an in-memory
runner used strictly for testing permissions and simulated outcomes.

STRICT ACCEPTANCE BOUNDARIES:
1. In-memory runners are NOT real code execution and are ALWAYS marked with is_simulated=True.
2. Simulated outcomes must NEVER be reported as genuine test evidence and must NEVER set fix_verified=True.
3. Prohibited destructive commands (rm, del, format, shutdown, kill, sudo, etc.) are rejected unconditionally.
4. No host subprocess, PowerShell, shell, or arbitrary OS commands are EVER invoked.
5. In the absence of a genuine container daemon backend, execution is reported as unavailable or simulated.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import re
from typing import Optional

from eaoc.agents.developer.schemas import TestEvidence
from eaoc.observability import get_logger

logger = get_logger("eaoc.agents.developer.sandbox")

# Strictly prohibited destructive commands (rejected under all roles, including admin)
PROHIBITED_COMMAND_PATTERNS = [
    re.compile(r"\brm\s+-[rf]+", re.IGNORECASE),
    re.compile(r"\bdel\s+/[sfq]+", re.IGNORECASE),
    re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),
    re.compile(r"\b(?:shutdown|reboot|poweroff|init\s+0)\b", re.IGNORECASE),
    re.compile(r"\b(?:kill|pkill|killall)\b", re.IGNORECASE),
    re.compile(r"\bdd\s+if=", re.IGNORECASE),
    re.compile(r"\bmkfs(?:\.[a-z0-9]+)?\b", re.IGNORECASE),
    re.compile(r"\b(?:sudo|su)\b", re.IGNORECASE),
    re.compile(r"\b(?:chmod|chown)\s+-[rR]", re.IGNORECASE),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", re.IGNORECASE),  # fork bomb
]


def validate_test_command_safety(command: str) -> None:
    """
    Validates that a test command does not contain destructive OS-level instructions.
    Raises ValueError if prohibited operations are detected.
    """
    if not command or not isinstance(command, str):
        return

    cmd_stripped = command.strip()
    for pattern in PROHIBITED_COMMAND_PATTERNS:
        if pattern.search(cmd_stripped):
            msg = f"Prohibited destructive command pattern detected in test_command: '{command}'."
            logger.warning(
                msg,
                extra={"event_name": "dev_destructive_command_rejected", "command": command},
            )
            raise ValueError(msg)


class BaseSandboxRunner(ABC):
    """
    Abstract boundary contract for isolated container test execution.
    Genuine container backends implement this interface to run isolated tests.
    """

    @property
    @abstractmethod
    def is_real_backend(self) -> bool:
        """
        True ONLY if this runner executes within an actual isolated container sandbox.
        Returns False for mock, in-memory, or simulated test runners.
        """
        pass

    @property
    def runner_type(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    async def run_test(
        self,
        command: str,
        code_snippet: Optional[str] = None,
        timeout_seconds: float = 60.0,
    ) -> TestEvidence:
        """
        Executes the specified test command within the sandbox.
        Returns a structured TestEvidence record.
        """
        pass


class InMemorySandboxRunner(BaseSandboxRunner):
    """
    In-memory simulated sandbox runner.
    Used STRICTLY for testing permission checks, timeout boundaries, and simulated workflows.

    GUARANTEE:
    - is_real_backend is always False.
    - Every generated TestEvidence has is_simulated=True.
    - Zero host subprocess or OS command execution occurs.
    """

    def __init__(
        self,
        simulated_exit_code: int = 0,
        simulated_stdout: str = "Ran 3 tests in 0.05s\nOK",
        simulated_stderr: str = "",
        simulated_sandbox_id: str = "sim-box-test-01",
    ):
        self.simulated_exit_code = simulated_exit_code
        self.simulated_stdout = simulated_stdout
        self.simulated_stderr = simulated_stderr
        self.simulated_sandbox_id = simulated_sandbox_id

    @property
    def is_real_backend(self) -> bool:
        """Always False for in-memory simulated runner."""
        return False

    async def run_test(
        self,
        command: str,
        code_snippet: Optional[str] = None,
        timeout_seconds: float = 60.0,
    ) -> TestEvidence:
        # Enforce destructive command rejection even in simulation
        validate_test_command_safety(command)

        logger.info(
            f"Executing simulated test runner for command: '{command}' in sandbox '{self.simulated_sandbox_id}'.",
            extra={
                "event_name": "dev_sandbox_simulated_execution",
                "command": command,
                "sandbox_id": self.simulated_sandbox_id,
            },
        )

        return TestEvidence(
            sandbox_id=self.simulated_sandbox_id,
            command_run=command,
            stdout=self.simulated_stdout,
            stderr=self.simulated_stderr,
            exit_code=self.simulated_exit_code,
            is_simulated=True,
            executed_at=datetime.now(timezone.utc),
        )
