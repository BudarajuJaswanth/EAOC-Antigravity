"""
EAOC LangGraph State Contracts & Security Schema (Part 2.18)

Defines the typed graph-state schema (GraphWorkflowState) for stateful multi-agent
orchestration within LangGraph 1.2.12+.

Security Classification Matrix:
- Trusted Identity: tenant_id, user_id (derived exclusively from validated JWT claims)
- Credential / Secret: lock_token (ephemeral lease token; MUST NEVER be logged or leaked)
- Durable Reference: workflow_id, task_id, plan_hash, fencing_epoch, current_revision,
                     provenance_chain, status, approval_request, approval_decision
- Sensitive Data: completed_steps, final_response (redacted before cross-boundary return)
- Transient Execution Data: steps, step_cursor, step_execution_records, replan_count,
                           error_info, failure_class, next_route
"""

from typing import Any, Dict, List, Optional
from typing_extensions import TypedDict

from eaoc.observability import redact_sensitive_data
from eaoc.schemas.domain import WorkflowStatus


class GraphWorkflowState(TypedDict, total=False):
    """
    Authoritative in-memory state dictionary for LangGraph workflow execution.
    Transient during graph execution; durable state resides in Supabase PostgreSQL.
    """

    # 1. Identity & Scoping (Trusted Context)
    workflow_id: str
    task_id: str
    tenant_id: str
    user_id: str
    user_context: Dict[str, Any]
    request_id: str
    correlation_id: str

    # 2. Concurrency, Fencing & Leases (Credential & Reference)
    fencing_epoch: int
    lock_token: Optional[str]  # Ephemeral lease credential (NEVER LOG OR EXPOSE)
    current_revision: int

    # 3. Execution Plan & Deterministic Hash
    execution_plan: Optional[Dict[str, Any]]
    plan_hash: Optional[str]
    steps: List[Dict[str, Any]]
    step_cursor: int
    current_step_index: Optional[int]

    # 4. Step Execution Ledger & Provenance
    completed_steps: Dict[int, Dict[str, Any]]  # Sensitive: Raw step outputs
    step_execution_records: Dict[int, Dict[str, Any]]
    provenance_chain: List[Dict[str, Any]]
    interrupted_steps: List[int]  # Step indices identified as RUNNING during recovery

    # 5. Failure & Replanning Counters
    replan_count: int
    has_partial_result: bool
    error_info: Optional[Dict[str, Any]]
    failure_class: Optional[str]

    # 6. Human-in-the-Loop (HITL) State
    approval_request: Optional[Dict[str, Any]]
    approval_decision: Optional[Dict[str, Any]]
    approval_consumed: Optional[bool]
    is_first_claim: Optional[bool]
    is_recovery_claim: Optional[bool]

    # 7. Lifecycle Status & Final Aggregated Output
    status: str  # PENDING, RUNNING, WAITING_APPROVAL, COMPLETED, FAILED
    final_response: Optional[Dict[str, Any]]  # Sensitive: Redacted aggregated result

    # 8. Transient Routing & Control Flags
    next_route: Optional[str]
    is_step_fast_forwarded: bool
    last_dispatch_success: bool


# Explicit categorization lists for security policy enforcement
CREDENTIAL_FIELDS = {"lock_token"}
SENSITIVE_DATA_FIELDS = {"completed_steps", "final_response"}
TRUSTED_IDENTITY_FIELDS = {"tenant_id", "user_id", "user_context"}
DURABLE_REFERENCE_FIELDS = {
    "workflow_id",
    "task_id",
    "plan_hash",
    "fencing_epoch",
    "current_revision",
    "status",
}


def sanitize_state_for_logging(state: GraphWorkflowState) -> Dict[str, Any]:
    """
    Produces a safe, scrubbed copy of GraphWorkflowState for logging and diagnostics:
    - Replaces lock_token with '[REDACTED_CREDENTIAL]'
    - Passes sensitive data through redact_sensitive_data
    - Strips internal auth secrets from user_context
    """
    safe_copy: Dict[str, Any] = {}
    for key, value in state.items():
        if key in CREDENTIAL_FIELDS:
            safe_copy[key] = "[REDACTED_CREDENTIAL]" if value else None
        elif key in SENSITIVE_DATA_FIELDS:
            safe_copy[key] = redact_sensitive_data(value)
        elif key == "user_context" and isinstance(value, dict):
            ctx_copy = dict(value)
            for secret_key in ("token", "jwt", "password", "secret", "credentials"):
                if secret_key in ctx_copy:
                    ctx_copy[secret_key] = "[REDACTED]"
            safe_copy[key] = ctx_copy
        else:
            safe_copy[key] = value
    return safe_copy
