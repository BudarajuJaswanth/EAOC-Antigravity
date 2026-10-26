"""
EAOC Developer Agent Schemas & Data Contracts
Defines typed Pydantic v2 input, output, review finding, test evidence,
and provenance models for AGT-DEV.
Strictly implements the five approved operations:
explain, review, document, generate, and debug_with_test.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class DevOperation(str, Enum):
    """Approved operations for the Developer Agent per Phase 0 specification."""
    EXPLAIN = "explain"
    REVIEW = "review"
    DOCUMENT = "document"
    GENERATE = "generate"
    DEBUG_WITH_TEST = "debug_with_test"


class ReviewFindingSeverity(str, Enum):
    """Severity classification for code review findings."""
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ReviewFinding(BaseModel):
    """Structured code review finding with severity and actionable recommendation."""
    line: Optional[int] = Field(
        default=None,
        ge=1,
        description="Line number in the code snippet where the issue was identified",
    )
    severity: ReviewFindingSeverity = Field(
        description="Severity classification of the code finding",
    )
    description: str = Field(
        description="Clear explanation of the detected issue or anti-pattern",
    )
    suggestion: Optional[str] = Field(
        default=None,
        description="Recommended fix or refactoring advice",
    )


class TestEvidence(BaseModel):
    """
    Evidence record for sandboxed test execution.
    Explicitly tracks whether execution was performed in a genuine container sandbox
    or by an in-memory simulated runner.
    """
    sandbox_id: str = Field(
        description="Unique identifier of the isolated sandbox environment",
    )
    command_run: str = Field(
        description="Exact test command executed within the sandbox",
    )
    stdout: str = Field(
        default="",
        description="Standard output stream captured from execution",
    )
    stderr: str = Field(
        default="",
        description="Standard error stream captured from execution",
    )
    exit_code: int = Field(
        description="Process exit code (0 indicates success)",
    )
    is_simulated: bool = Field(
        default=False,
        description="Explicit flag indicating if this test outcome was produced by a simulated runner",
    )
    executed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timezone-aware UTC timestamp of execution",
    )


class DevProvenance(BaseModel):
    """
    Mandatory provenance metadata for Developer Agent execution.
    Records logical agent ID ('AGT-DEV'), operation, code snippet SHA-256 hash,
    sandbox identifier, and simulation flags.
    """
    agent_id: str = Field(
        default="AGT-DEV",
        description="Logical agent identifier",
    )
    operation: DevOperation = Field(
        description="Executed developer operation",
    )
    language: Optional[str] = Field(
        default=None,
        description="Target programming language",
    )
    code_snippet_hash: Optional[str] = Field(
        default=None,
        description="SHA-256 cryptographic digest of the input code snippet",
    )
    llm_model_used: Optional[str] = Field(
        default=None,
        description="Model identifier used for inference",
    )
    sandbox_id: Optional[str] = Field(
        default=None,
        description="Sandbox environment identifier when sandbox execution was invoked",
    )
    test_command: Optional[str] = Field(
        default=None,
        description="Test command invoked during execution",
    )
    fix_verified: Optional[bool] = Field(
        default=None,
        description="True ONLY if real sandbox execution passed; never True for simulated runs",
    )
    is_simulated: bool = Field(
        default=False,
        description="Indicates whether test execution was simulated",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timezone-aware UTC execution timestamp",
    )


class DevInput(BaseModel):
    """
    Strict input schema for Developer Agent.
    Operates strictly on user-provided code and error context.
    Rejects prohibited repository writes, OS shell commands, and deployment requests.
    """
    dev_task: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Natural language description of the developer task or question",
    )
    operation: DevOperation = Field(
        default=DevOperation.EXPLAIN,
        description="Approved developer operation (explain | review | document | generate | debug_with_test)",
    )
    code_snippet: Optional[str] = Field(
        default=None,
        max_length=50000,
        description="User-provided source code snippet to analyze, review, or fix",
    )
    error_context: Optional[str] = Field(
        default=None,
        max_length=10000,
        description="Error trace, compiler output, or test failure message",
    )
    language: Optional[str] = Field(
        default="python",
        max_length=50,
        description="Programming language identifier (e.g. python, typescript, sql)",
    )
    test_command: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Test command to run in sandbox (only permitted for debug_with_test)",
    )
    task_id: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Optional workflow correlation task identifier",
    )


class DevOutput(BaseModel):
    """
    Strict output schema for Developer Agent.
    Includes explanations, review findings, generated code, test evidence,
    and mandatory AGT-DEV provenance.
    """
    operation: DevOperation = Field(
        description="Executed developer operation",
    )
    explanation: Optional[str] = Field(
        default=None,
        description="Natural language explanation of code or problem root cause",
    )
    identified_issues: Optional[List[str]] = Field(
        default=None,
        description="List of specific identified issues or anti-patterns",
    )
    findings: Optional[List[ReviewFinding]] = Field(
        default=None,
        description="Structured code review findings with severity and suggestions",
    )
    overall_assessment: Optional[str] = Field(
        default=None,
        description="Summary assessment for code review operations",
    )
    documentation: Optional[str] = Field(
        default=None,
        description="Synthesized markdown documentation for provided code",
    )
    generated_code: Optional[str] = Field(
        default=None,
        description="Synthesized code or proposed fix",
    )
    proposed_fix: Optional[str] = Field(
        default=None,
        description="Suggested patch or corrected code for debug_with_test",
    )
    ai_generated_label: bool = Field(
        default=True,
        description="Mandatory label indicating content was generated by AI",
    )
    test_evidence: Optional[TestEvidence] = Field(
        default=None,
        description="Evidence record from sandbox test execution",
    )
    fix_verified: Optional[bool] = Field(
        default=None,
        description="True ONLY if genuine sandbox test exit_code was 0; never True for simulated tests",
    )
    execution_status: Optional[str] = Field(
        default="completed",
        description="Execution status ('completed', 'execution_unavailable', 'simulated', 'rejected')",
    )
    provenance: DevProvenance = Field(
        description="Mandatory AGT-DEV execution provenance record",
    )
