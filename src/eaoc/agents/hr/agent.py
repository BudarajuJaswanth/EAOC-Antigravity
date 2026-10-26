"""
EAOC HR Agent Implementation
Specialized agent for employee and organizational data operations:
headcount aggregations, org chart structures, policy retrieval, and employee profile queries.
Enforces trusted-context RBAC, conservative PII minimization by default,
strict 'hr' schema confinement, and mandatory AGT-HR provenance.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from eaoc.agents.base import BaseAgent
from eaoc.agents.hr.data_provider import BaseHrDataProvider
from eaoc.agents.hr.pii_redactor import HrPiiRedactor
from eaoc.agents.hr.schemas import (
    HeadcountGroup,
    HeadcountResult,
    HrInput,
    HrOutput,
    HrProvenance,
    HrQueryType,
)
from eaoc.models.base import ModelProviderWrapper
from eaoc.observability import get_logger
from eaoc.schemas.domain import AgentExecutionContext, AgentID

logger = get_logger("eaoc.agents.hr")

# Permitted roles per Phase 0 specification (Agent 8, AGT-HR)
AUTHORIZED_HR_ROLES: Set[str] = {"hr_user", "admin"}

HR_NARRATIVE_PROMPT = (
    "You are the Enterprise AI Operations Center (EAOC) HR Agent.\n"
    "Your responsibility is to provide a concise, professional summary of HR results.\n"
    "Rules:\n"
    "1. Base your summary EXCLUSIVELY on the provided aggregated results and policy text.\n"
    "2. Never extrapolate, hallucinate, or expose personal employee information.\n"
    "3. Never generate employee performance evaluations or appraisal opinions.\n"
    "4. Maintain strict confidentiality and objectivity."
)


class HrAgent(BaseAgent[HrInput, HrOutput]):
    """
    HR specialized agent.
    
    Guarantees:
    1. Canonical AgentID.HR ('hr').
    2. Trusted-context RBAC: permits only 'hr_user' and 'admin' roles.
    3. Fails closed / denies by default when trusted identity is missing.
    4. Conservative PII minimization: requires explicit trusted PII permission claim;
       defaults to redaction otherwise even for admin.
    5. Headcount queries return aggregates only, never individual employee rows.
    6. Org-chart queries enforce non-PII structural projections.
    7. Schema access locked strictly to the 'hr' schema.
    8. Mandatory AGT-HR provenance block attached to every response.
    """

    agent_id: AgentID = AgentID.HR
    input_schema = HrInput
    output_schema = HrOutput

    def __init__(
        self,
        data_provider: Optional[BaseHrDataProvider] = None,
        model_provider: Optional[ModelProviderWrapper] = None,
        default_timeout_seconds: Optional[float] = 30.0,
        **kwargs,
    ):
        super().__init__(
            agent_id=AgentID.HR,
            input_schema=HrInput,
            output_schema=HrOutput,
            default_timeout_seconds=default_timeout_seconds,
            **kwargs,
        )
        self.data_provider = data_provider
        self.model_provider = model_provider

    def _verify_trusted_identity(
        self, context: AgentExecutionContext
    ) -> Tuple[str, bool]:
        """
        Validates user authorization against trusted context metadata and resolves PII permission.
        Strictly ignores validated_input and context.input_payload to prevent role/permission spoofing.
        Fails closed (denies by default) if role or identity is missing.
        
        Conservative PII Rule:
        Requires an explicit trusted PII permission claim (e.g. user_context.pii_permission == True
        or context.metadata.pii_authorized == True). Admin role does NOT grant automatic PII access
        unless the explicit permission claim is present.
        """
        if not context or not context.metadata:
            logger.warning(
                "HR access denied: Execution context metadata is missing.",
                extra={"event_name": "hr_access_denied", "execution_id": getattr(context, "execution_id", "unknown")},
            )
            raise PermissionError("Access denied: Trusted execution context metadata is missing.")

        # Explicitly deny if context is marked unauthenticated or untrusted
        if (
            context.metadata.get("is_authenticated") is False
            or context.metadata.get("authenticated") is False
            or context.metadata.get("is_trusted") is False
        ):
            logger.warning(
                "HR access denied: Execution context is unauthenticated or untrusted.",
                extra={"event_name": "hr_access_denied", "execution_id": getattr(context, "execution_id", "unknown")},
            )
            raise PermissionError("Access denied: Execution context is not authenticated or trusted.")

        # Check trusted user_context or metadata fields populated by platform interceptor
        # Untrusted caller metadata containers (e.g. untrusted_metadata, caller_metadata) are strictly ignored.
        user_context = context.metadata.get("user_context")
        raw_role = None
        has_pii_permission = False

        if isinstance(user_context, dict):
            raw_role = user_context.get("role") or user_context.get("roles")
            has_pii_permission = bool(
                user_context.get("pii_permission")
                or user_context.get("pii_authorized")
                or "pii_read" in user_context.get("permissions", [])
            )
        if not raw_role:
            raw_role = (
                context.metadata.get("user_role")
                or context.metadata.get("role")
                or context.metadata.get("roles")
            )
            if not has_pii_permission:
                has_pii_permission = bool(
                    context.metadata.get("pii_authorized")
                    or context.metadata.get("pii_permission")
                    or "pii_read" in context.metadata.get("trusted_permissions", [])
                )

        if isinstance(raw_role, list):
            clean_roles = {str(r).strip().lower() for r in raw_role}
            matched = clean_roles.intersection(AUTHORIZED_HR_ROLES)
            if not matched:
                logger.warning(
                    f"HR access denied: User roles {raw_role} not in {AUTHORIZED_HR_ROLES}.",
                    extra={"event_name": "hr_access_denied", "execution_id": context.execution_id},
                )
                raise PermissionError(
                    f"Access denied: User roles {sorted(list(clean_roles))} are not authorized for HR Agent. "
                    f"Permitted roles: {sorted(list(AUTHORIZED_HR_ROLES))}."
                )
            return sorted(list(matched))[0], has_pii_permission

        if not raw_role or not isinstance(raw_role, str):
            logger.warning(
                "HR access denied: No trusted user role found in execution context.",
                extra={"event_name": "hr_access_denied", "execution_id": context.execution_id},
            )
            raise PermissionError("Access denied: No trusted user role found in execution context.")

        clean_role = raw_role.strip().lower()
        if clean_role not in AUTHORIZED_HR_ROLES:
            logger.warning(
                f"HR access denied: Role '{clean_role}' is not authorized.",
                extra={"event_name": "hr_access_denied", "execution_id": context.execution_id},
            )
            raise PermissionError(
                f"Access denied: Role '{clean_role}' is not authorized for HR Agent. "
                f"Permitted roles: {sorted(list(AUTHORIZED_HR_ROLES))}."
            )

        return clean_role, has_pii_permission

    async def _execute(
        self, validated_input: HrInput, context: AgentExecutionContext
    ) -> HrOutput:
        """
        Executes HR query with strict RBAC, data minimization, schema lockdown,
        and mandatory provenance.
        """
        # 1. Enforce Trusted RBAC and Resolve PII Permission
        trusted_role, has_pii_permission = self._verify_trusted_identity(context)

        logger.info(
            f"Authorized user with role '{trusted_role}' executing HR query for operation '{validated_input.query_type.value}'.",
            extra={
                "event_name": "hr_query_authorized",
                "execution_id": context.execution_id,
                "query_type": validated_input.query_type.value,
                "role": trusted_role,
                "pii_permission": has_pii_permission,
            },
        )

        # 2. Schema Boundary Validation
        requested_schema = validated_input.filters.get("schema") or validated_input.filters.get("target_schema")
        if requested_schema is not None and str(requested_schema).lower() != "hr":
            raise PermissionError(
                f"Access to schema '{requested_schema}' is prohibited. "
                "HR Agent operations are strictly restricted to the 'hr' schema."
            )

        # 3. Handle Policy Queries via RAG / Policy Interface
        if validated_input.query_type == HrQueryType.POLICY:
            return await self._handle_policy_query(
                validated_input=validated_input,
                context=context,
                trusted_role=trusted_role,
            )

        # 4. Resolve Structured HR Records
        records, data_source = await self._resolve_records(
            validated_input=validated_input, context=context
        )

        # 5. Enforce Maximum Records Bound
        if len(records) > validated_input.max_records:
            raise ValueError(
                f"Record count ({len(records)}) exceeds maximum allowed limit of "
                f"{validated_input.max_records} records."
            )

        # 6. Handle Empty Result Set
        if not records:
            logger.info(
                f"No records found for HR query on operation '{validated_input.query_type.value}'.",
                extra={"event_name": "hr_no_records_found", "execution_id": context.execution_id},
            )
            provenance = HrProvenance(
                agent_id="AGT-HR",
                query_type=validated_input.query_type.value,
                department_filter=validated_input.department,
                pii_redacted=False,
                data_source=data_source,
                timestamp=datetime.now(timezone.utc).isoformat(),
                notes="No matching HR records found for criteria.",
            )
            return HrOutput(
                status="empty_result",
                query_type=validated_input.query_type,
                hr_query=validated_input.hr_query,
                result={},
                pii_redacted=False,
                record_count=0,
                summary="No matching HR records found.",
                narrative=None,
                provenance=provenance,
            )

        # 7. Execute Operation-Specific Logic
        result_payload: Dict[str, Any] = {}
        pii_was_redacted: bool = False
        summary_text: str = ""

        if validated_input.query_type == HrQueryType.HEADCOUNT:
            # Aggregate headcount query: must return groups and total, NEVER individual rows
            group_field = validated_input.group_by or "department"
            counts: Dict[str, int] = {}
            for rec in records:
                label_val = str(rec.get(group_field, "Unknown")).strip()
                counts[label_val] = counts.get(label_val, 0) + 1

            groups = [
                {"label": label, "count": count}
                for label, count in sorted(counts.items())
            ]
            total_count = sum(counts.values())
            as_of_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            result_payload = {
                "group_by": group_field,
                "groups": groups,
                "total": total_count,
                "as_of_date": as_of_date,
            }
            # Headcount queries contain zero individual employee rows (aggregate only)
            pii_was_redacted = False
            summary_text = (
                f"Headcount by {group_field}: {total_count} total employee(s) across "
                f"{len(groups)} group(s)."
            )

        elif validated_input.query_type == HrQueryType.ORG_CHART:
            # Org-chart query: strictly projects non-PII structural fields
            filtered_nodes = HrPiiRedactor.filter_org_chart(records)
            result_payload = {
                "org_chart": filtered_nodes,
                "node_count": len(filtered_nodes),
            }
            pii_was_redacted = False
            summary_text = f"Retrieved org chart hierarchy containing {len(filtered_nodes)} position(s)."

        elif validated_input.query_type == HrQueryType.EMPLOYEE_INFO:
            # Employee-info query: enforces conservative PII minimization
            redacted_records, pii_was_redacted = HrPiiRedactor.redact_records(
                records=records, has_pii_permission=has_pii_permission
            )
            result_payload = {
                "employees": redacted_records,
                "count": len(redacted_records),
                "pii_minimized": pii_was_redacted,
            }
            summary_text = (
                f"Retrieved {len(redacted_records)} employee profile(s) "
                f"({'PII redacted' if pii_was_redacted else 'full authorized PII access'})."
            )

        # 8. Generate Optional Narrative (LLM)
        narrative = await self._generate_narrative(
            query=validated_input.hr_query,
            query_type=validated_input.query_type.value,
            result=result_payload,
        )

        # 9. Construct Mandatory Provenance Block
        provenance = HrProvenance(
            agent_id="AGT-HR",
            query_type=validated_input.query_type.value,
            department_filter=validated_input.department,
            pii_redacted=pii_was_redacted,
            data_source=data_source,
            timestamp=datetime.now(timezone.utc).isoformat(),
            notes=f"Processed by HR Agent with role authorization '{trusted_role}'.",
        )

        return HrOutput(
            status="completed",
            query_type=validated_input.query_type,
            hr_query=validated_input.hr_query,
            result=result_payload,
            pii_redacted=pii_was_redacted,
            record_count=len(records),
            summary=summary_text,
            narrative=narrative,
            provenance=provenance,
        )

    async def _handle_policy_query(
        self,
        validated_input: HrInput,
        context: AgentExecutionContext,
        trusted_role: str,
    ) -> HrOutput:
        """
        Executes HR policy lookups via the injected data provider.
        """
        if self.data_provider is None:
            raise RuntimeError("No HR data provider configured for policy lookups.")

        policy_res = await self.data_provider.query_policy(
            topic=validated_input.hr_query,
            context=context,
        )

        provenance = HrProvenance(
            agent_id="AGT-HR",
            query_type=HrQueryType.POLICY.value,
            department_filter=validated_input.department,
            pii_redacted=False,
            data_source=self.data_provider.source_name,
            timestamp=datetime.now(timezone.utc).isoformat(),
            notes=f"Policy retrieved via HR provider; authorized by role '{trusted_role}'.",
        )

        return HrOutput(
            status="completed",
            query_type=HrQueryType.POLICY,
            hr_query=validated_input.hr_query,
            result=policy_res,
            pii_redacted=False,
            record_count=1 if policy_res.get("policy_found", True) else 0,
            summary=f"HR policy query for '{validated_input.hr_query}'.",
            narrative=None,
            provenance=provenance,
        )

    async def _resolve_records(
        self, validated_input: HrInput, context: AgentExecutionContext
    ) -> Tuple[List[Dict[str, Any]], str]:
        """
        Resolves HR records from direct input or the injected data provider.
        """
        if validated_input.records is not None:
            # Department filter applied if present
            records = validated_input.records
            if validated_input.department:
                clean_dept = validated_input.department.strip().lower()
                records = [
                    r for r in records
                    if str(r.get("department", "")).strip().lower() == clean_dept
                ]
            return records, "direct_request_payload"

        if self.data_provider is not None:
            filters = dict(validated_input.filters)
            if validated_input.department and "department" not in filters:
                filters["department"] = validated_input.department

            records = await self.data_provider.query_hr_data(
                query_type=validated_input.query_type,
                filters=filters,
                context=context,
            )
            return records, self.data_provider.source_name

        raise RuntimeError(
            "No HR data provider configured and no records provided in input."
        )

    async def _generate_narrative(
        self,
        query: str,
        query_type: str,
        result: Dict[str, Any],
    ) -> Optional[str]:
        """
        Generates optional narrative interpretation without exposing raw personal PII.
        """
        if self.model_provider is None or self.model_provider.chat_model is None:
            return None

        # Build non-PII summary context for LLM
        grounding_data = [
            f"Query: {query}",
            f"Query Type: {query_type}",
        ]
        if query_type == HrQueryType.HEADCOUNT.value:
            grounding_data.append(f"Total Headcount: {result.get('total')}")
            grounding_data.append(f"Groups: {result.get('groups')}")
        elif query_type == HrQueryType.ORG_CHART.value:
            grounding_data.append(f"Position Count: {result.get('node_count')}")
        elif query_type == HrQueryType.EMPLOYEE_INFO.value:
            grounding_data.append(f"Employees Count: {result.get('count')}")
            grounding_data.append(f"PII Minimized: {result.get('pii_minimized')}")

        grounding_context = "\n".join(grounding_data)

        messages = [
            {"role": "system", "content": HR_NARRATIVE_PROMPT},
            {
                "role": "user",
                "content": f"Summarize these verified HR results:\n{grounding_context}",
            },
        ]

        try:
            response = await self.model_provider.generate_chat_completion(
                messages=messages, temperature=0.0
            )
            return response.strip()
        except Exception as e:
            logger.warning(
                f"LLM HR narrative generation failed: {e}. Gracefully returning deterministic results.",
                extra={"event_name": "hr_narrative_generation_failed"},
            )
            return None
