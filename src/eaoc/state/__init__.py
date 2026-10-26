"""
EAOC State Management Package
Exports IWorkflowStateStore interface and core workflow state contracts.
"""

from eaoc.schemas.domain import WorkflowState, WorkflowStatus
from eaoc.state.approval_repository import (
    IApprovalRepository,
    InMemoryApprovalRepository,
    PostgresApprovalRepository,
)
from eaoc.state.interfaces import IWorkflowStateStore
from eaoc.state.workflow_repository import (
    ConcurrencyConflictError,
    FencingLeaseLostError,
    IWorkflowRecoveryRepository,
    InMemoryWorkflowRecoveryRepository,
    PlanIntegrityError,
    PostgresWorkflowRecoveryRepository,
)

__all__ = [
    "IWorkflowStateStore",
    "WorkflowState",
    "WorkflowStatus",
    "IApprovalRepository",
    "PostgresApprovalRepository",
    "InMemoryApprovalRepository",
    "IWorkflowRecoveryRepository",
    "PostgresWorkflowRecoveryRepository",
    "InMemoryWorkflowRecoveryRepository",
    "FencingLeaseLostError",
    "ConcurrencyConflictError",
    "PlanIntegrityError",
]
