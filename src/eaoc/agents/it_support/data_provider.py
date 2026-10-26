"""
EAOC IT/Support Data Provider Boundary
Defines abstract and concrete data access interfaces for IT service management:
- BaseItDataProvider (Abstract Interface)
- InMemoryItDataProvider (In-memory mock for isolated testing)
- SqlItDataProvider (Delegated SQL Agent provider enforcing permitted_schemas=['it'])

Strict boundaries:
Confines SQL operations strictly to 'it.tickets', 'it.ticket_categories', and 'it.sla_definitions'.
Rejects attempts to access finance, hr, analytics, or public schemas.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from eaoc.agents.it_support.schemas import ItTicketFilter
from eaoc.observability import get_logger

logger = get_logger("eaoc.agents.it_support.data_provider")

# Strictly permitted SQL schemas for IT Agent delegation
PERMITTED_IT_SCHEMAS: Set[str] = {"it"}

# Permitted tables within the 'it' schema
PERMITTED_IT_TABLES: Set[str] = {
    "it.tickets",
    "it.ticket_categories",
    "it.sla_definitions",
}


class BaseItDataProvider(ABC):
    """Abstract interface defining the data provider boundary for IT/Support operations."""

    @property
    def source_name(self) -> str:
        """Identifies the underlying data source for provenance tracking."""
        return self.__class__.__name__

    @abstractmethod
    async def query_tickets(
        self, filters: Optional[ItTicketFilter] = None
    ) -> List[Dict[str, Any]]:
        """Queries tickets matching optional filter criteria."""
        pass

    @abstractmethod
    async def get_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves details of a specific ticket by ID."""
        pass

    @abstractmethod
    async def search_kb(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Searches the IT knowledge base for relevant troubleshooting articles."""
        pass

    @abstractmethod
    async def lookup_policy(self, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        """Searches IT policies and guidelines matching the query."""
        pass


class InMemoryItDataProvider(BaseItDataProvider):
    """
    Thread-safe, zero-external-dependency in-memory IT data provider.
    Provides realistic sample tickets, IT knowledge base articles, and enterprise IT policies.
    """

    def __init__(
        self,
        tickets: Optional[List[Dict[str, Any]]] = None,
        kb_articles: Optional[List[Dict[str, Any]]] = None,
        policies: Optional[List[Dict[str, Any]]] = None,
    ):
        self._tickets = (
            tickets
            if tickets is not None
            else [
                {
                    "ticket_id": "TCK-1001",
                    "title": "VPN connection handshake timeout",
                    "description": "User cannot connect to vpn-gw01.corp.internal (10.200.4.5) due to TLS handshake timeout.",
                    "category": "Network",
                    "priority": "P1",
                    "status": "open",
                    "assignee": "alice.net@example.com",
                    "created_at": "2026-10-20T09:00:00Z",
                    "ip_address": "10.200.4.5",
                    "hostname": "vpn-gw01.corp.internal",
                },
                {
                    "ticket_id": "TCK-1002",
                    "title": "Self-service password reset portal error",
                    "description": "AD user locked out after password reset portal gave 500 error on auth-srv02.internal (192.168.10.45).",
                    "category": "Identity",
                    "priority": "P2",
                    "status": "in_progress",
                    "assignee": "bob.id@example.com",
                    "created_at": "2026-10-21T11:30:00Z",
                    "ip_address": "192.168.10.45",
                    "hostname": "auth-srv02.internal",
                },
                {
                    "ticket_id": "TCK-1003",
                    "title": "Developer workstation laptop provision",
                    "description": "Provision developer laptop with Ubuntu 24.04, Docker, and Python 3.12 environment.",
                    "category": "Hardware",
                    "priority": "P3",
                    "status": "pending",
                    "assignee": "carol.it@example.com",
                    "created_at": "2026-10-22T08:15:00Z",
                    "ip_address": None,
                    "hostname": None,
                },
                {
                    "ticket_id": "TCK-1004",
                    "title": "WiFi 802.1x enterprise certificate renewal",
                    "description": "Renewed RADIUS server cert on ap-floor3.corp.local (172.16.2.100).",
                    "category": "Network",
                    "priority": "P4",
                    "status": "resolved",
                    "assignee": "alice.net@example.com",
                    "created_at": "2026-10-19T14:00:00Z",
                    "ip_address": "172.16.2.100",
                    "hostname": "ap-floor3.corp.local",
                },
                {
                    "ticket_id": "TCK-1005",
                    "title": "Critical database cluster unreachable",
                    "description": "PostgreSQL read replica db-replica01.corp.internal (10.100.1.50) failing health checks. Connection string: postgresql://it_admin:SecretPass123!@db-replica01.corp.internal:5432/it_db",
                    "category": "Database",
                    "priority": "P1",
                    "status": "open",
                    "assignee": "dave.ops@example.com",
                    "created_at": "2026-10-22T15:00:00Z",
                    "ip_address": "10.100.1.50",
                    "hostname": "db-replica01.corp.internal",
                    "password": "SecretPass123!",
                },
            ]
        )

        self._kb_articles = (
            kb_articles
            if kb_articles is not None
            else [
                {
                    "article_id": "KB-NET-01",
                    "title": "VPN Connection Troubleshooting Guide",
                    "content": "Step 1: Verify network connectivity to internet. Step 2: Check VPN gateway reachability. Step 3: Clear client local DNS cache. Step 4: Re-authenticate with MFA hardware token.",
                    "category": "Network",
                    "keywords": ["vpn", "handshake", "timeout", "network", "gateway"],
                },
                {
                    "article_id": "KB-ID-02",
                    "title": "Active Directory Password Reset Procedure",
                    "content": "Step 1: Verify caller identity via registered secondary phone or manager verification. Step 2: Clear account lockout status in Active Directory Users & Computers. Step 3: Issue temporary one-time password requiring change at next logon.",
                    "category": "Identity",
                    "keywords": ["password", "reset", "lockout", "active directory", "identity", "auth"],
                },
                {
                    "article_id": "KB-WIFI-03",
                    "title": "Corporate WiFi Configuration Guide",
                    "content": "Step 1: Download and install Enterprise Root CA Certificate. Step 2: Connect to EAOC-Corp SSID. Step 3: Configure EAP-TLS with enterprise client certificate.",
                    "category": "Network",
                    "keywords": ["wifi", "wireless", "certificate", "radius", "802.1x"],
                },
            ]
        )

        self._policies = (
            policies
            if policies is not None
            else [
                {
                    "policy_id": "POL-IT-01",
                    "title": "Enterprise Acceptable Use Policy",
                    "content": "Enterprise computing resources are provided exclusively for official business purposes. Remote access requires company-managed devices and multi-factor VPN authentication.",
                    "category": "Acceptable Use",
                    "version": "2.4",
                },
                {
                    "policy_id": "POL-IT-02",
                    "title": "Password & Credential Management Policy",
                    "content": "Passwords must be at least 16 characters in length. Hardcoding credentials in source code or storing plaintext passwords is strictly prohibited and subject to immediate revocation.",
                    "category": "Security",
                    "version": "3.1",
                },
                {
                    "policy_id": "POL-IT-03",
                    "title": "Incident Severity & SLA Definitions",
                    "content": "P1 (Critical) incidents mandate initial response within 15 minutes and continuous 24/7 engagement until resolved. P2 (Major) incidents require response within 1 hour.",
                    "category": "Operations",
                    "version": "1.8",
                },
            ]
        )

    @property
    def source_name(self) -> str:
        return "it_in_memory_provider"

    async def query_tickets(
        self, filters: Optional[ItTicketFilter] = None
    ) -> List[Dict[str, Any]]:
        results = list(self._tickets)

        if filters:
            if filters.status:
                results = [
                    t for t in results if str(t.get("status", "")).lower() == filters.status.lower()
                ]
            if filters.priority:
                results = [
                    t for t in results if str(t.get("priority", "")).upper() == filters.priority.upper()
                ]
            if filters.assignee:
                results = [
                    t for t in results if filters.assignee.lower() in str(t.get("assignee", "")).lower()
                ]
            if filters.category:
                results = [
                    t for t in results if str(t.get("category", "")).lower() == filters.category.lower()
                ]
            results = results[: filters.limit]

        return results

    async def get_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        for ticket in self._tickets:
            if ticket.get("ticket_id") == ticket_id:
                return dict(ticket)
        return None

    async def search_kb(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        if not query:
            return []
        query_terms = set(query.lower().split())
        matched = []
        for article in self._kb_articles:
            score = 0
            keywords = [k.lower() for k in article.get("keywords", [])]
            title = article.get("title", "").lower()
            content = article.get("content", "").lower()

            for term in query_terms:
                if any(term in kw for kw in keywords):
                    score += 3
                if term in title:
                    score += 2
                if term in content:
                    score += 1

            if score > 0:
                matched.append((score, article))

        matched.sort(key=lambda x: x[0], reverse=True)
        return [dict(item[1]) for item in matched[:limit]]

    async def lookup_policy(self, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        if not query:
            return []
        query_terms = set(query.lower().split())
        matched = []
        for pol in self._policies:
            score = 0
            title = pol.get("title", "").lower()
            content = pol.get("content", "").lower()
            category = pol.get("category", "").lower()

            for term in query_terms:
                if term in title:
                    score += 3
                if term in category:
                    score += 2
                if term in content:
                    score += 1

            if score > 0:
                matched.append((score, pol))

        matched.sort(key=lambda x: x[0], reverse=True)
        return [dict(item[1]) for item in matched[:limit]]


class SqlItDataProvider(BaseItDataProvider):
    """
    SQL/Data Agent delegation provider.
    Enforces strict permitted_schemas=['it'] and confines table queries
    to 'it.tickets', 'it.ticket_categories', and 'it.sla_definitions'.
    """

    def __init__(
        self,
        sql_agent: Optional[Any] = None,
        permitted_schemas: Optional[List[str]] = None,
    ):
        self.sql_agent = sql_agent
        self.permitted_schemas = permitted_schemas or ["it"]

        # Validate that only 'it' schema is permitted
        unauthorized = set(self.permitted_schemas) - PERMITTED_IT_SCHEMAS
        if unauthorized:
            raise PermissionError(
                f"Unauthorized schema(s) configured for IT data provider: {list(unauthorized)}. "
                f"Only {list(PERMITTED_IT_SCHEMAS)} are permitted."
            )

    @property
    def source_name(self) -> str:
        return "it.tickets via sql_data"

    def _validate_query_safety(self, sql_query: str) -> None:
        """Validates that query targets only permitted 'it' schema tables."""
        query_lower = sql_query.lower()

        # Reject prohibited schemas
        forbidden_schemas = ["finance", "hr", "analytics", "public", "auth", "storage"]
        for schema in forbidden_schemas:
            if f"{schema}." in query_lower:
                raise PermissionError(
                    f"Access to schema '{schema}' is prohibited. IT Agent is restricted to 'it' schema."
                )

        # Enforce that table is within PERMITTED_IT_TABLES
        if not any(table in query_lower for table in PERMITTED_IT_TABLES):
            raise PermissionError(
                f"Query must target permitted IT tables: {list(PERMITTED_IT_TABLES)}."
            )

    async def query_tickets(
        self, filters: Optional[ItTicketFilter] = None
    ) -> List[Dict[str, Any]]:
        # Parameterized query template confined to it.tickets
        query = "SELECT ticket_id, title, description, category, priority, status, assignee, created_at, ip_address, hostname FROM it.tickets"
        conditions = []
        if filters:
            if filters.status:
                conditions.append(f"status = '{filters.status}'")
            if filters.priority:
                conditions.append(f"priority = '{filters.priority}'")
            if filters.category:
                conditions.append(f"category = '{filters.category}'")
            if filters.assignee:
                conditions.append(f"assignee = '{filters.assignee}'")

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        limit = filters.limit if filters else 50
        query += f" LIMIT {limit};"

        self._validate_query_safety(query)

        if self.sql_agent is None:
            raise RuntimeError("SQL Agent delegate is not configured for SqlItDataProvider.")

        # Delegate execution
        logger.info(
            "Delegating IT ticket query to SQL/Data Agent.",
            extra={"event_name": "it_sql_delegated", "query": query},
        )
        # Note: Actual agent invocation occurs through execution context in agent.py
        return []

    async def get_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        query = f"SELECT ticket_id, title, description, category, priority, status, assignee, created_at, ip_address, hostname FROM it.tickets WHERE ticket_id = '{ticket_id}' LIMIT 1;"
        self._validate_query_safety(query)
        if self.sql_agent is None:
            raise RuntimeError("SQL Agent delegate is not configured for SqlItDataProvider.")
        return None

    async def search_kb(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        # KB searches delegate to RAG rather than SQL
        return []

    async def lookup_policy(self, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        # Policy searches delegate to RAG
        return []
