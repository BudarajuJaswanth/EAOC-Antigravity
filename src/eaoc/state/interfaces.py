"""
EAOC Storage-Agnostic Workflow State Store Interface
Defines the canonical IWorkflowStateStore Abstract Base Class.
Zero dependencies on Redis or PostgreSQL implementations.
"""

from abc import ABC, abstractmethod
from typing import Optional
from eaoc.schemas.domain import WorkflowState


class IWorkflowStateStore(ABC):
    """
    Storage-agnostic workflow state store interface.
    Enforces standardized async state management operations.
    """

    @abstractmethod
    async def create_state(self, state: WorkflowState) -> WorkflowState:
        """
        Creates a new workflow state.
        Must raise ValueError if a state with state.workflow_id already exists.
        """
        pass

    @abstractmethod
    async def get_state(self, workflow_id: str) -> Optional[WorkflowState]:
        """
        Retrieves workflow state by workflow_id.
        Returns WorkflowState if present; None if absent.
        """
        pass

    @abstractmethod
    async def update_state(self, state: WorkflowState) -> WorkflowState:
        """
        Updates an existing workflow state.
        Must raise KeyError if workflow_id does not exist in store.
        """
        pass

    @abstractmethod
    async def delete_state(self, workflow_id: str) -> bool:
        """
        Removes workflow state by workflow_id.
        Returns True if found and deleted; False if absent.
        """
        pass

    @abstractmethod
    async def exists(self, workflow_id: str) -> bool:
        """
        Checks whether a workflow state with workflow_id exists in store.
        Returns True if present; False otherwise.
        """
        pass
