"""
EAOC Finance Agent Implementation
Authoritative specialized agent for financial queries (invoices, budgets, expenses, and reports).
Enforces trusted-context RBAC, deterministic Decimal monetary calculations, ISO currency
uniformity, provenance wrapping, and separation of LLM narrative from immutable figures.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from eaoc.agents.base import BaseAgent
from eaoc.agents.finance.calculator import FinanceCalculator
from eaoc.agents.finance.data_provider import BaseFinanceDataProvider
from eaoc.agents.finance.schemas import (
    FinanceEntityType,
    FinanceInput,
    FinanceOutput,
    FinanceProvenance,
    FinanceTotals,
    VarianceSummary,
)
from eaoc.models.base import ModelProviderWrapper
from eaoc.observability import get_logger
from eaoc.schemas.domain import AgentExecutionContext, AgentID

logger = get_logger("eaoc.agents.finance")

# Permitted roles per Phase 0 specification (Agent 7, AGT-FIN)
AUTHORIZED_FINANCE_ROLES: Set[str] = {"finance_user", "admin"}

FINANCE_NARRATIVE_PROMPT = (
    "You are the Enterprise AI Operations Center (EAOC) Finance Agent.\n"
    "Your responsibility is to provide a concise, professional financial summary.\n"
    "Rules:\n"
    "1. Base your summary EXCLUSIVELY on the provided financial records and calculations.\n"
    "2. Never alter, contradict, or extrapolate monetary values.\n"
    "3. Never generate speculative forecasts or predictions.\n"
    "4. Maintain strict objectivity and precision."
)


class FinanceAgent(BaseAgent[FinanceInput, FinanceOutput]):
    """
    Finance specialized agent.
    
    Guarantees:
    1. Canonical AgentID.FINANCE ('finance').
    2. Trusted-context RBAC: only 'finance_user' and 'admin' roles permitted.
    3. Fails closed / denies by default when trusted user role is missing.
    4. Deterministic Decimal arithmetic for variance and totals.
    5. Rejects mixed-currency datasets without exchange rate provenance.
    6. Complete provenance block attached to every response.
    7. No real financial side effects (read-only queries).
    """

    agent_id: AgentID = AgentID.FINANCE
    input_schema = FinanceInput
    output_schema = FinanceOutput

    def __init__(
        self,
        data_provider: Optional[BaseFinanceDataProvider] = None,
        model_provider: Optional[ModelProviderWrapper] = None,
        default_timeout_seconds: Optional[float] = 30.0,
        **kwargs,
    ):
        super().__init__(
            agent_id=AgentID.FINANCE,
            input_schema=FinanceInput,
            output_schema=FinanceOutput,
            default_timeout_seconds=default_timeout_seconds,
            **kwargs,
        )
        self.data_provider = data_provider
        self.model_provider = model_provider

    def _verify_trusted_role(self, context: AgentExecutionContext) -> str:
        """
        Validates user authorization against trusted context metadata.
        Strictly ignores validated_input and context.input_payload to prevent role spoofing.
        Fails closed (denies by default) if role or identity is missing.
        """
        if not context or not context.metadata:
            logger.warning(
                "Finance access denied: Execution context metadata is missing.",
                extra={"event_name": "finance_access_denied", "execution_id": getattr(context, "execution_id", "unknown")},
            )
            raise PermissionError("Access denied: Trusted execution context metadata is missing.")

        # Explicitly deny if context is marked unauthenticated or untrusted
        if (
            context.metadata.get("is_authenticated") is False
            or context.metadata.get("authenticated") is False
            or context.metadata.get("is_trusted") is False
        ):
            logger.warning(
                "Finance access denied: Execution context is unauthenticated or untrusted.",
                extra={"event_name": "finance_access_denied", "execution_id": getattr(context, "execution_id", "unknown")},
            )
            raise PermissionError("Access denied: Execution context is not authenticated or trusted.")

        # Check trusted user_context or metadata fields populated by platform interceptor
        # Untrusted caller metadata containers (e.g. untrusted_metadata, caller_metadata) are strictly ignored.
        user_context = context.metadata.get("user_context")
        raw_role = None
        if isinstance(user_context, dict):
            raw_role = user_context.get("role") or user_context.get("roles")
        if not raw_role:
            raw_role = (
                context.metadata.get("user_role")
                or context.metadata.get("role")
                or context.metadata.get("roles")
            )

        if isinstance(raw_role, list):
            clean_roles = {str(r).strip().lower() for r in raw_role}
            matched = clean_roles.intersection(AUTHORIZED_FINANCE_ROLES)
            if not matched:
                logger.warning(
                    f"Finance access denied: User roles {raw_role} not in {AUTHORIZED_FINANCE_ROLES}.",
                    extra={"event_name": "finance_access_denied", "execution_id": context.execution_id},
                )
                raise PermissionError(
                    f"Access denied: User roles {sorted(list(clean_roles))} are not authorized for Finance Agent. "
                    f"Permitted roles: {sorted(list(AUTHORIZED_FINANCE_ROLES))}."
                )
            return sorted(list(matched))[0]

        if not raw_role or not isinstance(raw_role, str):
            logger.warning(
                "Finance access denied: No trusted user role found in execution context.",
                extra={"event_name": "finance_access_denied", "execution_id": context.execution_id},
            )
            raise PermissionError("Access denied: No trusted user role found in execution context.")

        clean_role = raw_role.strip().lower()
        if clean_role not in AUTHORIZED_FINANCE_ROLES:
            logger.warning(
                f"Finance access denied: Role '{clean_role}' is not authorized.",
                extra={"event_name": "finance_access_denied", "execution_id": context.execution_id},
            )
            raise PermissionError(
                f"Access denied: Role '{clean_role}' is not authorized for Finance Agent. "
                f"Permitted roles: {sorted(list(AUTHORIZED_FINANCE_ROLES))}."
            )

        return clean_role

    async def _execute(
        self, validated_input: FinanceInput, context: AgentExecutionContext
    ) -> FinanceOutput:
        """
        Executes financial query with strict RBAC, bounded retrieval, Decimal arithmetic,
        and mandatory provenance.
        """
        # 1. Enforce Trusted RBAC
        trusted_role = self._verify_trusted_role(context)

        logger.info(
            f"Authorized user with role '{trusted_role}' executing finance query for entity '{validated_input.entity_type.value}'.",
            extra={
                "event_name": "finance_query_authorized",
                "execution_id": context.execution_id,
                "entity_type": validated_input.entity_type.value,
                "role": trusted_role,
            },
        )

        # 2. Resolve financial records and data source origin
        records, data_source = await self._resolve_records(
            validated_input=validated_input, context=context
        )

        # 3. Enforce maximum records bound
        if len(records) > validated_input.max_records:
            raise ValueError(
                f"Record count ({len(records)}) exceeds maximum allowed limit of "
                f"{validated_input.max_records} records."
            )

        # 4. Handle empty result set
        if not records:
            logger.info(
                f"No records found for finance query on entity '{validated_input.entity_type.value}'.",
                extra={
                    "event_name": "finance_no_records_found",
                    "execution_id": context.execution_id,
                },
            )
            provenance = FinanceProvenance(
                agent_id="AGT-FIN",
                entity_type=validated_input.entity_type.value,
                filters_applied=validated_input.filters,
                data_source=data_source,
                timestamp=datetime.now(timezone.utc).isoformat(),
                notes="No matching financial records found for criteria.",
            )
            return FinanceOutput(
                status="empty_result",
                entity_type=validated_input.entity_type,
                finance_query=validated_input.finance_query,
                records=[],
                record_count=0,
                currency=validated_input.currency,
                summary="No matching financial records found.",
                totals=None,
                variance=None,
                narrative=None,
                provenance=provenance,
            )

        # 5. Validate Currency Uniformity
        resolved_currency = FinanceCalculator.validate_currency_uniformity(
            records=records, expected_currency=validated_input.currency
        )
        if not resolved_currency:
            resolved_currency = "USD"

        # 6. Perform Deterministic Calculations
        totals: Optional[FinanceTotals] = None
        variance: Optional[VarianceSummary] = None
        summary_text: str = ""

        if validated_input.entity_type == FinanceEntityType.BUDGET:
            # Check for budget variance fields in the records or filters
            allocated_val = None
            actual_val = None

            # Check if first record contains allocated and actual
            if "allocated" in records[0] and "actual" in records[0]:
                allocated_val = records[0]["allocated"]
                actual_val = records[0]["actual"]
            elif "allocated" in validated_input.filters and "actual" in validated_input.filters:
                allocated_val = validated_input.filters["allocated"]
                actual_val = validated_input.filters["actual"]

            if allocated_val is not None and actual_val is not None:
                variance = FinanceCalculator.compute_variance(
                    allocated=allocated_val,
                    actual=actual_val,
                    currency=resolved_currency,
                )
                summary_text = (
                    f"Budget variance for period: allocated {variance.allocated} {resolved_currency}, "
                    f"actual {variance.actual} {resolved_currency}, variance {variance.variance_amount} {resolved_currency} "
                    f"({variance.variance_percentage}% - {'favorable' if variance.favorable else 'unfavorable'})."
                    if variance.variance_percentage is not None
                    else f"Budget variance for period: allocated 0.00 {resolved_currency}, actual {variance.actual} {resolved_currency} (undefined percentage)."
                )
            else:
                summary_text = f"Retrieved {len(records)} budget record(s)."

        elif validated_input.entity_type in (
            FinanceEntityType.INVOICE,
            FinanceEntityType.EXPENSE,
            FinanceEntityType.REPORT,
        ):
            # Attempt to compute totals if an amount field exists
            amount_field = "amount"
            if records and "amount" not in records[0]:
                for candidate in ("total_amount", "invoice_amount", "expense_amount"):
                    if candidate in records[0]:
                        amount_field = candidate
                        break

            if records and amount_field in records[0]:
                totals = FinanceCalculator.compute_totals(
                    records=records,
                    amount_field=amount_field,
                    expected_currency=resolved_currency,
                )
                summary_text = (
                    f"Processed {totals.record_count} {validated_input.entity_type.value} record(s). "
                    f"Total: {totals.total_amount} {totals.currency}."
                )
            else:
                summary_text = (
                    f"Retrieved {len(records)} {validated_input.entity_type.value} record(s)."
                )

        # 7. Generate Separated LLM Narrative (Optional)
        narrative = await self._generate_narrative(
            query=validated_input.finance_query,
            entity_type=validated_input.entity_type.value,
            records=records,
            totals=totals,
            variance=variance,
            currency=resolved_currency,
        )

        # 8. Construct Mandatory Provenance
        provenance = FinanceProvenance(
            agent_id="AGT-FIN",
            entity_type=validated_input.entity_type.value,
            filters_applied=validated_input.filters,
            data_source=data_source,
            timestamp=datetime.now(timezone.utc).isoformat(),
            notes=f"Processed by Finance Agent with role authorization '{trusted_role}'.",
        )

        return FinanceOutput(
            status="completed",
            entity_type=validated_input.entity_type,
            finance_query=validated_input.finance_query,
            records=records,
            record_count=len(records),
            currency=resolved_currency,
            summary=summary_text,
            totals=totals,
            variance=variance,
            narrative=narrative,
            provenance=provenance,
        )

    async def _resolve_records(
        self, validated_input: FinanceInput, context: AgentExecutionContext
    ) -> Tuple[List[Dict[str, Any]], str]:
        """
        Resolves financial records from direct input or the injected data provider.
        Strictly prevents accessing non-finance schemas (e.g., hr, it, analytics).
        """
        # Enforce that no non-finance schema can be requested in filters
        requested_schema = validated_input.filters.get("schema") or validated_input.filters.get("target_schema")
        if requested_schema is not None and str(requested_schema).lower() != "finance":
            raise PermissionError(
                f"Access to schema '{requested_schema}' is prohibited. "
                "Finance Agent operations are strictly restricted to the 'finance' schema."
            )

        if validated_input.records is not None:
            return validated_input.records, "direct_request_payload"

        if self.data_provider is not None:
            records = await self.data_provider.query_finance_data(
                entity_type=validated_input.entity_type,
                filters=validated_input.filters,
                context=context,
            )
            return records, self.data_provider.source_name

        raise RuntimeError(
            "No financial data provider configured and no records provided in input."
        )

    async def _generate_narrative(
        self,
        query: str,
        entity_type: str,
        records: List[Dict[str, Any]],
        totals: Optional[FinanceTotals],
        variance: Optional[VarianceSummary],
        currency: str,
    ) -> Optional[str]:
        """
        Generates optional narrative interpretation grounded exclusively in deterministic figures.
        """
        if self.model_provider is None or self.model_provider.chat_model is None:
            return None

        # Build grounded numerical summary
        grounding_data = [
            f"Query: {query}",
            f"Entity Type: {entity_type}",
            f"Record Count: {len(records)}",
            f"Currency: {currency}",
        ]
        if totals:
            grounding_data.append(f"Total Amount: {totals.total_amount} {totals.currency}")
            if totals.average_amount:
                grounding_data.append(f"Average Amount: {totals.average_amount} {totals.currency}")
        if variance:
            grounding_data.append(f"Allocated Budget: {variance.allocated} {variance.currency}")
            grounding_data.append(f"Actual Expenditure: {variance.actual} {variance.currency}")
            grounding_data.append(f"Variance Amount: {variance.variance_amount} {variance.currency}")
            grounding_data.append(
                f"Variance Percentage: {variance.variance_percentage}% ({variance.percentage_status})"
            )
            grounding_data.append(f"Expense Favorable: {variance.favorable}")

        grounding_context = "\n".join(grounding_data)

        messages = [
            {"role": "system", "content": FINANCE_NARRATIVE_PROMPT},
            {
                "role": "user",
                "content": f"Summarize these verified financial results:\n{grounding_context}",
            },
        ]

        try:
            response = await self.model_provider.generate_chat_completion(
                messages=messages, temperature=0.0
            )
            return response.strip()
        except Exception as e:
            logger.warning(
                f"LLM financial narrative generation failed: {e}. Gracefully returning deterministic results.",
                extra={"event_name": "finance_narrative_generation_failed"},
            )
            return None
