"""
EAOC Agent Permission Manifest Store
Maintains immutable, in-memory permission manifests for all 12 approved EAOC agents (FR-PERM-001).
Denies by default; prevents runtime modification of manifests or privilege elevation.
"""

import copy
from typing import Dict, List, Optional, Union

from eaoc.agents.security_policy.schemas import AgentPermissionManifest
from eaoc.schemas.domain import AgentID

# Mapping between logical IDs, canonical enum values, and aliases
AGENT_ID_ALIASES: Dict[str, str] = {
    "AGT-PLAN": "planner",
    "PLANNER": "planner",
    "planner": "planner",
    "AGT-RAG": "research_rag",
    "RESEARCH_RAG": "research_rag",
    "research_rag": "research_rag",
    "AGT-SQL": "sql_data",
    "SQL_DATA": "sql_data",
    "sql_data": "sql_data",
    "AGT-DOC": "document",
    "DOCUMENT": "document",
    "document": "document",
    "AGT-ANA": "analytics",
    "ANALYTICS": "analytics",
    "analytics": "analytics",
    "AGT-FIN": "finance",
    "FINANCE": "finance",
    "finance": "finance",
    "AGT-HR": "hr",
    "HR": "hr",
    "hr": "hr",
    "AGT-IT": "it_support",
    "IT_SUPPORT": "it_support",
    "it_support": "it_support",
    "AGT-DEV": "developer",
    "DEVELOPER": "developer",
    "developer": "developer",
    "AGT-REV": "reviewer_validation",
    "REVIEWER_VALIDATION": "reviewer_validation",
    "reviewer_validation": "reviewer_validation",
    "AGT-SEC": "security_policy",
    "SECURITY_POLICY": "security_policy",
    "security_policy": "security_policy",
    "AGT-ORCH": "orchestrator",
    "ORCHESTRATOR": "orchestrator",
    "orchestrator": "orchestrator",
}

# Authoritative permission manifests for all 12 canonical agents
# Derived from Phase 0 FRS Section 8, Arch Part B Section 7 & 8, and tool registry metadata
DEFAULT_MANIFESTS: Dict[str, AgentPermissionManifest] = {
    "planner": AgentPermissionManifest(
        agent_id="AGT-PLAN",
        permitted_tools=["llm_inference", "audit_logger"],
        permitted_delegation_targets=[],
        permitted_schemas=[],
        permitted_namespaces=[],
        max_data_sensitivity="internal",
        write_permitted=False,
    ),
    "research_rag": AgentPermissionManifest(
        agent_id="AGT-RAG",
        permitted_tools=["pgvector_search", "rag_search_tool", "llm_inference", "audit_logger"],
        permitted_delegation_targets=[],
        permitted_schemas=[],
        permitted_namespaces=["general_research", "enterprise_docs"],
        max_data_sensitivity="confidential",
        write_permitted=False,
    ),
    "sql_data": AgentPermissionManifest(
        agent_id="AGT-SQL",
        permitted_tools=["postgresql_read", "sql_query_tool", "llm_inference", "audit_logger"],
        permitted_delegation_targets=[],
        permitted_schemas=["analytics"],
        permitted_namespaces=[],
        max_data_sensitivity="confidential",
        write_permitted=False,
    ),
    "document": AgentPermissionManifest(
        agent_id="AGT-DOC",
        permitted_tools=["document_retrieve", "llm_inference", "audit_logger"],
        permitted_delegation_targets=[],
        permitted_schemas=[],
        permitted_namespaces=["document_store"],
        max_data_sensitivity="restricted",
        write_permitted=False,
    ),
    "analytics": AgentPermissionManifest(
        agent_id="AGT-ANA",
        permitted_tools=["sql_query_tool", "llm_inference", "audit_logger"],
        permitted_delegation_targets=["AGT-SQL"],
        permitted_schemas=["analytics"],
        permitted_namespaces=[],
        max_data_sensitivity="internal",
        write_permitted=False,
    ),
    "finance": AgentPermissionManifest(
        agent_id="AGT-FIN",
        permitted_tools=["llm_inference", "audit_logger"],
        permitted_delegation_targets=["AGT-SQL"],
        permitted_schemas=[],
        permitted_namespaces=["financial_reports"],
        max_data_sensitivity="restricted",
        write_permitted=False,
    ),
    "hr": AgentPermissionManifest(
        agent_id="AGT-HR",
        permitted_tools=["llm_inference", "audit_logger"],
        permitted_delegation_targets=["AGT-SQL", "AGT-RAG"],
        permitted_schemas=[],
        permitted_namespaces=["hr_policy"],
        max_data_sensitivity="restricted",
        write_permitted=False,
    ),
    "it_support": AgentPermissionManifest(
        agent_id="AGT-IT",
        permitted_tools=["llm_inference", "audit_logger"],
        permitted_delegation_targets=["AGT-SQL", "AGT-RAG"],
        permitted_schemas=[],
        permitted_namespaces=["it_kb", "it_tickets"],
        max_data_sensitivity="internal",
        write_permitted=False,
    ),
    "developer": AgentPermissionManifest(
        agent_id="AGT-DEV",
        permitted_tools=["sandbox_execution", "llm_inference", "audit_logger"],
        permitted_delegation_targets=[],
        permitted_schemas=[],
        permitted_namespaces=[],
        max_data_sensitivity="internal",
        write_permitted=False,
    ),
    "reviewer_validation": AgentPermissionManifest(
        agent_id="AGT-REV",
        permitted_tools=["llm_inference", "audit_logger"],
        permitted_delegation_targets=[],
        permitted_schemas=[],
        permitted_namespaces=[],
        max_data_sensitivity="restricted",
        write_permitted=False,
    ),
    "security_policy": AgentPermissionManifest(
        agent_id="AGT-SEC",
        permitted_tools=["audit_logger"],
        permitted_delegation_targets=[],
        permitted_schemas=[],
        permitted_namespaces=[],
        max_data_sensitivity="restricted",
        write_permitted=False,
    ),
    "orchestrator": AgentPermissionManifest(
        agent_id="AGT-ORCH",
        permitted_tools=["llm_inference", "audit_logger"],
        permitted_delegation_targets=[
            "AGT-PLAN",
            "AGT-RAG",
            "AGT-SQL",
            "AGT-DOC",
            "AGT-ANA",
            "AGT-FIN",
            "AGT-HR",
            "AGT-IT",
            "AGT-DEV",
            "AGT-REV",
            "AGT-SEC",
        ],
        permitted_schemas=[],
        permitted_namespaces=[],
        max_data_sensitivity="restricted",
        write_permitted=False,
    ),
}


class ManifestStore:
    """
    Immutable in-memory manifest store for EAOC agent permission manifests.
    Guarantees:
    1. Returns deep copies to prevent caller mutation of stored definitions.
    2. Does not provide runtime methods to add or modify permissions.
    3. Fails closed / returns None if agent is unknown.
    """

    def __init__(self, manifests: Optional[Dict[str, AgentPermissionManifest]] = None):
        # Freeze default manifests with deepcopy
        base = manifests if manifests is not None else DEFAULT_MANIFESTS
        self._manifests: Dict[str, AgentPermissionManifest] = copy.deepcopy(base)

    @classmethod
    def resolve_canonical_id(cls, agent_identifier: Union[AgentID, str]) -> Optional[str]:
        """Resolves an AgentID enum, logical ID (e.g. AGT-HR), or string name to canonical key."""
        if agent_identifier is None:
            return None
        if isinstance(agent_identifier, AgentID):
            return agent_identifier.value

        raw_str = str(agent_identifier).strip()
        if raw_str in AGENT_ID_ALIASES:
            return AGENT_ID_ALIASES[raw_str]
        raw_upper = raw_str.upper()
        if raw_upper in AGENT_ID_ALIASES:
            return AGENT_ID_ALIASES[raw_upper]
        raw_lower = raw_str.lower()
        if raw_lower in AGENT_ID_ALIASES:
            return AGENT_ID_ALIASES[raw_lower]

        return None

    def get_manifest(
        self, agent_identifier: Union[AgentID, str]
    ) -> Optional[AgentPermissionManifest]:
        """
        Retrieves a deepcopy of the permission manifest for the given agent.
        Returns None if agent is unknown.
        """
        canonical_key = self.resolve_canonical_id(agent_identifier)
        if not canonical_key or canonical_key not in self._manifests:
            return None
        # Return a deepcopy to ensure immutability
        return copy.deepcopy(self._manifests[canonical_key])

    def has_manifest(self, agent_identifier: Union[AgentID, str]) -> bool:
        """Returns True if a manifest exists for the given agent."""
        canonical_key = self.resolve_canonical_id(agent_identifier)
        return bool(canonical_key and canonical_key in self._manifests)

    def list_manifests(self) -> List[AgentPermissionManifest]:
        """Returns a deepcopy list of all 12 agent manifests."""
        return [copy.deepcopy(m) for m in self._manifests.values()]
