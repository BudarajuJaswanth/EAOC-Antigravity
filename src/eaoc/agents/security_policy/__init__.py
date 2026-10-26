"""
EAOC Security / Policy Agent Package
Provides the Security / Policy Agent (AGT-SEC), schemas, manifest store, and engine.
"""

from eaoc.agents.security_policy.agent import (
    AUTHORIZED_SECURITY_ROLES,
    SecurityPolicyAgent,
)
from eaoc.agents.security_policy.engine import SecurityPolicyEngine
from eaoc.agents.security_policy.manifest_store import (
    DEFAULT_MANIFESTS,
    ManifestStore,
)
from eaoc.agents.security_policy.schemas import (
    AgentPermissionManifest,
    PolicyCheckOperation,
    PolicyViolation,
    PolicyViolationType,
    SecurityPolicyInput,
    SecurityPolicyOutput,
    SecurityPolicyProvenance,
)

__all__ = [
    "SecurityPolicyAgent",
    "SecurityPolicyEngine",
    "ManifestStore",
    "DEFAULT_MANIFESTS",
    "AUTHORIZED_SECURITY_ROLES",
    "PolicyCheckOperation",
    "PolicyViolationType",
    "PolicyViolation",
    "AgentPermissionManifest",
    "SecurityPolicyProvenance",
    "SecurityPolicyInput",
    "SecurityPolicyOutput",
]
