"""
EAOC IT/Support Agent Implementation
Specialized agent for IT service management operations:
- Querying open and filtered IT tickets
- Checking single ticket statuses and details
- Synthesizing resolution recommendations from verified IT knowledge base articles
- Looking up enterprise IT policies

Enforces trusted-context RBAC (it_user, admin), defensive infrastructure and secret redaction,
strict 'it' schema boundaries, prompt-injection defense on untrusted content,
and mandatory AGT-IT provenance.
"""

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from eaoc.agents.base import BaseAgent
from eaoc.agents.it_support.data_provider import (
    BaseItDataProvider,
    InMemoryItDataProvider,
    SqlItDataProvider,
)
from eaoc.agents.it_support.infra_redactor import ItInfraRedactor
from eaoc.agents.it_support.schemas import (
    ItProvenance,
    ItQueryType,
    ItResolutionStep,
    ItSupportInput,
    ItSupportOutput,
    ItTicketFilter,
)
from eaoc.models.base import ModelProviderWrapper
from eaoc.observability import get_logger, redact_sensitive_data
from eaoc.schemas.domain import AgentExecutionContext, AgentID

logger = get_logger("eaoc.agents.it_support")

# Permitted roles per Phase 0 specification (Agent 9, AGT-IT)
AUTHORIZED_IT_ROLES: Set[str] = {"it_user", "admin"}

# Prompt injection signatures to neutralize from untrusted user/ticket text
PROMPT_INJECTION_PATTERN = re.compile(
    r"(ignore\s+all\s+previous\s+instructions|system\s+override|reveal\s+(?:all\s+)?passwords|"
    r"grant\s+(?:me\s+)?admin|you\s+are\s+now\s+in\s+admin\s+mode|bypass\s+(?:all\s+)?security|"
    r"disable\s+redaction|drop\s+table)",
    re.IGNORECASE,
)


class ItSupportAgent(BaseAgent[ItSupportInput, ItSupportOutput]):
    """
    IT / Support specialized agent.

    Guarantees:
    1. Canonical AgentID.IT_SUPPORT ('it_support') and logical ID 'AGT-IT'.
    2. Trusted-context RBAC: permits only 'it_user' and 'admin' roles.
    3. Fails closed / denies by default when trusted identity is missing or unauthenticated.
    4. Anti-spoofing: ignores caller-supplied role claims in input payloads.
    5. Defensive infrastructure & secret redaction: IP addresses and hostnames masked for non-authorized roles;
       credentials and secrets ALWAYS redacted for all roles (including admin).
    6. Prompt injection defense: treats ticket text and queries as untrusted data.
    7. Strict schema boundaries: SQL delegation restricted strictly to 'it' schema.
    8. Grounded recommendations: attached strictly to genuine KB evidence; no hallucinatory citations.
    9. Mandatory AGT-IT provenance block on all outputs.
    """

    agent_id: AgentID = AgentID.IT_SUPPORT
    input_schema = ItSupportInput
    output_schema = ItSupportOutput

    def __init__(
        self,
        data_provider: Optional[BaseItDataProvider] = None,
        model_provider: Optional[ModelProviderWrapper] = None,
        default_timeout_seconds: Optional[float] = 30.0,
        **kwargs,
    ):
        super().__init__(
            agent_id=AgentID.IT_SUPPORT,
            input_schema=ItSupportInput,
            output_schema=ItSupportOutput,
            default_timeout_seconds=default_timeout_seconds,
            **kwargs,
        )
        self.data_provider = data_provider or InMemoryItDataProvider()
        self.model_provider = model_provider

    def _verify_trusted_identity(
        self, context: AgentExecutionContext
    ) -> Tuple[str, bool]:
        """
        Validates user authorization against trusted context metadata.
        Strictly ignores validated_input and context.input_payload to prevent role spoofing.
        Fails closed (denies by default) if role or identity is missing or untrusted.

        Returns:
            (role, is_admin_or_infra_authorized)
        """
        if not context or not context.metadata:
            logger.warning(
                "IT access denied: Execution context metadata is missing.",
                extra={
                    "event_name": "it_access_denied",
                    "execution_id": getattr(context, "execution_id", "unknown"),
                    "reason": "missing_metadata",
                },
            )
            raise PermissionError("Access denied: Trusted execution context metadata is missing.")

        # Explicitly deny if context is marked unauthenticated or untrusted
        if (
            context.metadata.get("is_authenticated") is False
            or context.metadata.get("authenticated") is False
            or context.metadata.get("is_trusted") is False
        ):
            logger.warning(
                "IT access denied: Execution context is unauthenticated or untrusted.",
                extra={
                    "event_name": "it_access_denied",
                    "execution_id": getattr(context, "execution_id", "unknown"),
                    "reason": "unauthenticated",
                },
            )
            raise PermissionError("Access denied: Execution context is not authenticated or trusted.")

        # Resolve role strictly from trusted metadata
        user_context = context.metadata.get("user_context", {})
        if not isinstance(user_context, dict):
            user_context = {}

        role = user_context.get("role") or context.metadata.get("role")

        if not role or role not in AUTHORIZED_IT_ROLES:
            logger.warning(
                f"IT access denied: Role '{role}' is not authorized.",
                extra={
                    "event_name": "it_access_denied",
                    "role": role,
                    "permitted_roles": list(AUTHORIZED_IT_ROLES),
                    "execution_id": getattr(context, "execution_id", "unknown"),
                    "reason": "unauthorized_role",
                },
            )
            raise PermissionError(
                f"Access denied: Role '{role}' is not authorized for IT Agent. "
                f"Permitted roles: {sorted(list(AUTHORIZED_IT_ROLES))}."
            )

        # Check infrastructure topology visibility authorization
        is_admin_or_infra_authorized = bool(
            role == "admin"
            or user_context.get("infra_read") is True
            or user_context.get("infra_authorized") is True
            or context.metadata.get("infra_authorized") is True
            or "infra_read" in context.metadata.get("trusted_permissions", [])
            or "infra_topology_read" in context.metadata.get("trusted_permissions", [])
        )

        return str(role), is_admin_or_infra_authorized

    def _sanitize_untrusted_input(self, text: Optional[str]) -> Optional[str]:
        """
        Neutralizes potential prompt-injection signatures in untrusted query text,
        ensuring it is processed strictly as data without executing embedded instructions.
        """
        if not text:
            return text
        sanitized = PROMPT_INJECTION_PATTERN.sub("[UNTRUSTED_INSTRUCTION_REMOVED]", text)
        return sanitized

    async def _execute(
        self, validated_input: ItSupportInput, context: AgentExecutionContext
    ) -> ItSupportOutput:
        """
        Core IT/Support Agent execution pipeline.
        Dispatches to query_tickets, ticket_status, recommend_resolution, or policy_lookup.
        """
        role, is_infra_authorized = self._verify_trusted_identity(context)

        logger.info(
            f"Authorized user with role '{role}' executing IT query for operation '{validated_input.operation.value}'.",
            extra={
                "event_name": "it_query_received",
                "operation": validated_input.operation.value,
                "role": role,
                "task_id": validated_input.task_id or context.task_id,
                "execution_id": context.execution_id,
            },
        )

        op = validated_input.operation

        try:
            if op == ItQueryType.QUERY_TICKETS:
                return await self._handle_query_tickets(
                    validated_input, context, is_infra_authorized
                )
            elif op == ItQueryType.TICKET_STATUS:
                return await self._handle_ticket_status(
                    validated_input, context, is_infra_authorized
                )
            elif op == ItQueryType.RECOMMEND_RESOLUTION:
                return await self._handle_recommend_resolution(
                    validated_input, context, is_infra_authorized
                )
            elif op == ItQueryType.POLICY_LOOKUP:
                return await self._handle_policy_lookup(
                    validated_input, context, is_infra_authorized
                )
            else:
                raise ValueError(f"Unsupported IT operation: {op}")

        except Exception as exc:
            # Defensive secret redaction on error messages
            redacted_msg = redact_sensitive_data(str(exc))
            logger.error(
                f"IT Agent execution error during operation '{op.value}': {redacted_msg}",
                extra={
                    "event_name": "it_execution_error",
                    "operation": op.value,
                    "error_type": type(exc).__name__,
                    "execution_id": context.execution_id,
                },
            )
            raise type(exc)(redacted_msg) from exc

    async def _handle_query_tickets(
        self,
        validated_input: ItSupportInput,
        context: AgentExecutionContext,
        is_infra_authorized: bool,
    ) -> ItSupportOutput:
        """Handles query_tickets: filters and retrieves ticket records with defensive redaction."""
        filters = validated_input.filters or ItTicketFilter()
        raw_tickets = await self.data_provider.query_tickets(filters)

        # Redact infrastructure details and credentials
        redacted_tickets, was_infra_redacted = ItInfraRedactor.redact_record(
            raw_tickets, is_admin_or_infra_authorized=is_infra_authorized
        )

        if was_infra_redacted:
            logger.info(
                "Infrastructure details redacted from ticket query results.",
                extra={"event_name": "it_sensitive_field_redacted", "execution_id": context.execution_id},
            )

        logger.info(
            f"Returned {len(redacted_tickets)} tickets for query.",
            extra={
                "event_name": "it_result_returned",
                "count": len(redacted_tickets),
                "operation": ItQueryType.QUERY_TICKETS.value,
                "execution_id": context.execution_id,
            },
        )

        is_sql = isinstance(self.data_provider, SqlItDataProvider)

        provenance = ItProvenance(
            agent_id="AGT-IT",
            operation=ItQueryType.QUERY_TICKETS,
            ticket_id=None,
            sql_agent_ref="sql_data" if is_sql else None,
            rag_agent_ref=None,
            infra_redacted=was_infra_redacted,
            data_source=self.data_provider.source_name,
            timestamp=datetime.now(timezone.utc),
        )

        return ItSupportOutput(
            operation=ItQueryType.QUERY_TICKETS,
            tickets=redacted_tickets,
            count=len(redacted_tickets),
            filters_applied=filters.model_dump(exclude_none=True),
            provenance=provenance,
        )

    async def _handle_ticket_status(
        self,
        validated_input: ItSupportInput,
        context: AgentExecutionContext,
        is_infra_authorized: bool,
    ) -> ItSupportOutput:
        """Handles ticket_status: retrieves a specific ticket by ticket_id."""
        if not validated_input.ticket_id:
            raise ValueError("Operation 'ticket_status' requires 'ticket_id'.")

        ticket = await self.data_provider.get_ticket(validated_input.ticket_id)

        if ticket is None:
            # Ticket not found - return structured empty result
            provenance = ItProvenance(
                agent_id="AGT-IT",
                operation=ItQueryType.TICKET_STATUS,
                ticket_id=validated_input.ticket_id,
                sql_agent_ref=None,
                rag_agent_ref=None,
                infra_redacted=False,
                data_source=self.data_provider.source_name,
                timestamp=datetime.now(timezone.utc),
            )
            return ItSupportOutput(
                operation=ItQueryType.TICKET_STATUS,
                ticket=None,
                count=0,
                recommendation=f"Ticket '{validated_input.ticket_id}' was not found.",
                provenance=provenance,
            )

        redacted_ticket, was_infra_redacted = ItInfraRedactor.redact_record(
            ticket, is_admin_or_infra_authorized=is_infra_authorized
        )

        if was_infra_redacted:
            logger.info(
                "Infrastructure details redacted from ticket record.",
                extra={"event_name": "it_sensitive_field_redacted", "execution_id": context.execution_id},
            )

        is_sql = isinstance(self.data_provider, SqlItDataProvider)

        provenance = ItProvenance(
            agent_id="AGT-IT",
            operation=ItQueryType.TICKET_STATUS,
            ticket_id=validated_input.ticket_id,
            sql_agent_ref="sql_data" if is_sql else None,
            rag_agent_ref=None,
            infra_redacted=was_infra_redacted,
            data_source=self.data_provider.source_name,
            timestamp=datetime.now(timezone.utc),
        )

        return ItSupportOutput(
            operation=ItQueryType.TICKET_STATUS,
            ticket=redacted_ticket,
            count=1,
            provenance=provenance,
        )

    async def _handle_recommend_resolution(
        self,
        validated_input: ItSupportInput,
        context: AgentExecutionContext,
        is_infra_authorized: bool,
    ) -> ItSupportOutput:
        """
        Handles recommend_resolution: combines ticket detail with KB search,
        synthesizes grounded resolution steps, and attaches real source references.
        """
        target_ticket: Optional[Dict[str, Any]] = None
        search_terms: List[str] = []

        if validated_input.ticket_id:
            target_ticket = await self.data_provider.get_ticket(validated_input.ticket_id)
            if target_ticket:
                search_terms.append(target_ticket.get("title", ""))
                search_terms.append(target_ticket.get("description", ""))
                search_terms.append(target_ticket.get("category", ""))

        if validated_input.it_query:
            clean_query = self._sanitize_untrusted_input(validated_input.it_query)
            if clean_query:
                search_terms.append(clean_query)

        search_query = " ".join([t for t in search_terms if t]).strip()
        if not search_query:
            search_query = "troubleshooting guide"

        logger.info(
            f"Delegating knowledge base lookup for query: '{search_query[:100]}...'",
            extra={"event_name": "it_kb_delegated", "execution_id": context.execution_id},
        )

        kb_articles = await self.data_provider.search_kb(search_query, limit=3)

        was_infra_redacted = False
        redacted_ticket = None
        if target_ticket:
            redacted_ticket, infra_mod = ItInfraRedactor.redact_record(
                target_ticket, is_admin_or_infra_authorized=is_infra_authorized
            )
            if infra_mod:
                was_infra_redacted = True

        # Synthesize recommendations strictly from retrieved evidence
        if not kb_articles:
            # No matching evidence exists
            recommendation = (
                "Insufficient evidence: No relevant IT knowledge base articles found matching this ticket."
            )
            steps: List[ItResolutionStep] = []
            sources: List[Dict[str, Any]] = []
        else:
            steps = []
            sources = []
            # Extract actionable steps from the top KB article
            top_article = kb_articles[0]
            content = top_article.get("content", "")

            # Parse "Step X: ..." from content if formatted
            step_matches = re.findall(r"Step\s+(\d+):\s*([^.]+)\.", content, re.IGNORECASE)
            if step_matches:
                for idx, instr in step_matches:
                    steps.append(
                        ItResolutionStep(
                            step_number=int(idx),
                            instruction=instr.strip(),
                            target_component=top_article.get("category"),
                        )
                    )
            else:
                steps.append(
                    ItResolutionStep(
                        step_number=1,
                        instruction=content[:200].strip(),
                        target_component=top_article.get("category"),
                    )
                )

            recommendation = (
                f"Recommended resolution based on '{top_article.get('title')}': "
                f"Follow the {len(steps)} verified procedure step(s)."
            )

            for art in kb_articles:
                sources.append(
                    {
                        "article_id": art.get("article_id"),
                        "title": art.get("title"),
                        "category": art.get("category"),
                        "excerpt": art.get("content", "")[:150],
                    }
                )

        # Apply defensive redaction to steps, recommendation, and sources
        redacted_rec, r_infra = ItInfraRedactor.redact_record(
            recommendation, is_admin_or_infra_authorized=is_infra_authorized
        )
        redacted_steps_dict, s_infra = ItInfraRedactor.redact_record(
            [s.model_dump() for s in steps], is_admin_or_infra_authorized=is_infra_authorized
        )
        redacted_sources, src_infra = ItInfraRedactor.redact_record(
            sources, is_admin_or_infra_authorized=is_infra_authorized
        )

        if r_infra or s_infra or src_infra:
            was_infra_redacted = True

        final_steps = [ItResolutionStep.model_validate(sd) for sd in redacted_steps_dict]

        provenance = ItProvenance(
            agent_id="AGT-IT",
            operation=ItQueryType.RECOMMEND_RESOLUTION,
            ticket_id=validated_input.ticket_id,
            sql_agent_ref=None,
            rag_agent_ref="research_rag" if kb_articles else None,
            infra_redacted=was_infra_redacted,
            data_source=self.data_provider.source_name,
            timestamp=datetime.now(timezone.utc),
        )

        return ItSupportOutput(
            operation=ItQueryType.RECOMMEND_RESOLUTION,
            ticket=redacted_ticket,
            count=1 if target_ticket else 0,
            recommendation=redacted_rec,
            steps=final_steps,
            sources=redacted_sources,
            provenance=provenance,
        )

    async def _handle_policy_lookup(
        self,
        validated_input: ItSupportInput,
        context: AgentExecutionContext,
        is_infra_authorized: bool,
    ) -> ItSupportOutput:
        """Handles policy_lookup: searches IT policies and returns excerpts with citations."""
        clean_query = self._sanitize_untrusted_input(validated_input.it_query or "")
        logger.info(
            f"Delegating IT policy lookup for query: '{clean_query}'",
            extra={"event_name": "it_kb_delegated", "execution_id": context.execution_id},
        )

        raw_policies = await self.data_provider.lookup_policy(clean_query, limit=3)

        redacted_policies, was_infra_redacted = ItInfraRedactor.redact_record(
            raw_policies, is_admin_or_infra_authorized=is_infra_authorized
        )

        provenance = ItProvenance(
            agent_id="AGT-IT",
            operation=ItQueryType.POLICY_LOOKUP,
            ticket_id=None,
            sql_agent_ref=None,
            rag_agent_ref="research_rag" if redacted_policies else None,
            infra_redacted=was_infra_redacted,
            data_source=self.data_provider.source_name,
            timestamp=datetime.now(timezone.utc),
        )

        return ItSupportOutput(
            operation=ItQueryType.POLICY_LOOKUP,
            policy_excerpts=redacted_policies,
            count=len(redacted_policies),
            provenance=provenance,
        )
