"""
EAOC Analytics Agent Implementation
Provides deterministic quantitative analysis on structured enterprise datasets:
aggregations, KPIs, trends, and grouped statistics, coupled with grounded narrative
summaries without exposing raw PII row records.
"""

from typing import Any, Dict, List, Optional

from eaoc.agents.analytics.calculator import AnalyticsCalculator
from eaoc.agents.analytics.data_provider import BaseDataProvider
from eaoc.agents.analytics.schemas import (
    AggregationSpec,
    AnalyticsInput,
    AnalyticsOutput,
    MetricValue,
)
from eaoc.agents.base import BaseAgent
from eaoc.models.base import ModelProviderWrapper
from eaoc.observability import get_logger
from eaoc.schemas.domain import AgentExecutionContext, AgentID

logger = get_logger("eaoc.agents.analytics")

ANALYTICS_NARRATIVE_PROMPT = (
    "You are the Enterprise AI Operations Center (EAOC) Analytics Agent.\n"
    "Your responsibility is to generate a concise, professional narrative summary of quantitative metrics.\n"
    "Rules:\n"
    "1. Base your narrative EXCLUSIVELY on the computed metrics provided below.\n"
    "2. Never alter, contradict, or extrapolate numbers not present in the metrics.\n"
    "3. Maintain strict data integrity and objectivity."
)


class AnalyticsAgent(BaseAgent[AnalyticsInput, AnalyticsOutput]):
    """
    Analytics specialized agent.
    Computes mathematical metrics, group breakdowns, and narrative summaries on structured data.
    Enforces strict PII aggregation constraint (never outputs raw row records).
    """

    agent_id: AgentID = AgentID.ANALYTICS
    input_schema = AnalyticsInput
    output_schema = AnalyticsOutput

    def __init__(
        self,
        data_provider: Optional[BaseDataProvider] = None,
        model_provider: Optional[ModelProviderWrapper] = None,
        default_timeout_seconds: Optional[float] = 30.0,
        **kwargs,
    ):
        super().__init__(
            agent_id=AgentID.ANALYTICS,
            input_schema=AnalyticsInput,
            output_schema=AnalyticsOutput,
            default_timeout_seconds=default_timeout_seconds,
            **kwargs,
        )
        self.data_provider = data_provider
        self.model_provider = model_provider

    async def _execute(
        self, validated_input: AnalyticsInput, context: AgentExecutionContext
    ) -> AnalyticsOutput:
        """
        Executes quantitative analysis on the resolved dataset.
        Guarantees:
        1. Bounded dataset row limits.
        2. Explicit handling of empty datasets ('empty_dataset').
        3. Deterministic calculation of aggregations and group breakdowns.
        4. Strict PII aggregation (raw row records are never returned in output).
        5. Separation of LLM narrative from immutable computed numeric values.
        """
        # 1. Resolve dataset rows and reference
        rows, data_ref = await self._resolve_dataset(validated_input=validated_input, context=context)

        # 2. Enforce row bounds
        if len(rows) > validated_input.max_rows:
            raise ValueError(
                f"Dataset row count ({len(rows)}) exceeds maximum allowed limit of "
                f"{validated_input.max_rows} rows."
            )

        # 3. Explicit handling of empty datasets
        if not rows:
            logger.info(
                f"Analytics received an empty dataset for goal: '{validated_input.analysis_goal[:50]}...'",
                extra={"event_name": "analytics_empty_dataset", "execution_id": context.execution_id},
            )
            return AnalyticsOutput(
                status="empty_dataset",
                analysis_goal=validated_input.analysis_goal,
                metrics={},
                grouped_metrics=None,
                computation_notes="Dataset is empty (0 rows). No metrics calculated.",
                narrative=None,
                data_reference=data_ref,
                row_count=0,
                pii_aggregated=True,
                provenance_ref=f"prov-{context.execution_id}",
            )

        logger.info(
            f"Executing analytics on {len(rows)} records for goal: '{validated_input.analysis_goal[:50]}...'",
            extra={
                "event_name": "analytics_computation_started",
                "execution_id": context.execution_id,
                "row_count": len(rows),
                "data_reference": data_ref,
            },
        )

        notes: List[str] = [f"Processed {len(rows)} record(s) from '{data_ref}'."]

        # 4. Compute primary aggregations
        metrics_dict, agg_notes = self._compute_metrics(rows=rows, aggregations=validated_input.aggregations)
        notes.extend(agg_notes)

        # 5. Compute grouped breakdowns if requested
        grouped_dict = None
        if validated_input.group_by and validated_input.aggregations:
            grouped_dict = {}
            for agg in validated_input.aggregations:
                metric_key = agg.alias or f"{agg.field}_{agg.op.value}"
                grouped_res = AnalyticsCalculator.compute_grouped_aggregations(
                    rows=rows,
                    group_by=validated_input.group_by,
                    field=agg.field,
                    op=agg.op.value,
                )
                grouped_dict[metric_key] = grouped_res
            notes.append(f"Grouped metrics by: {', '.join(validated_input.group_by)}.")

        # 6. Generate narrative summary grounded strictly in computed metrics
        narrative = await self._generate_narrative(
            analysis_goal=validated_input.analysis_goal,
            metrics_dict=metrics_dict,
            data_ref=data_ref,
            row_count=len(rows),
        )

        logger.info(
            f"Analytics completed with {len(metrics_dict)} computed metric(s).",
            extra={
                "event_name": "analytics_computation_completed",
                "execution_id": context.execution_id,
                "metric_count": len(metrics_dict),
            },
        )

        return AnalyticsOutput(
            status="completed",
            analysis_goal=validated_input.analysis_goal,
            metrics=metrics_dict,
            grouped_metrics=grouped_dict,
            computation_notes=" ".join(notes),
            narrative=narrative,
            data_reference=data_ref,
            row_count=len(rows),
            pii_aggregated=True,  # Guarantees no raw row records are exposed
            provenance_ref=f"prov-{context.execution_id}",
        )

    async def _resolve_dataset(
        self, validated_input: AnalyticsInput, context: AgentExecutionContext
    ) -> tuple[List[Dict[str, Any]], str]:
        """Resolves records from direct input payload or delegated BaseDataProvider."""
        if validated_input.dataset is not None:
            data_ref = validated_input.data_source or f"direct_payload_{context.execution_id}"
            return validated_input.dataset, data_ref

        if not validated_input.data_source:
            raise ValueError("Neither 'dataset' nor 'data_source' was provided.")

        if self.data_provider is None:
            raise RuntimeError(
                f"Data Provider is not configured for agent '{self.agent_id.value}'."
            )

        rows = await self.data_provider.get_data(
            data_source=validated_input.data_source,
            query_spec=validated_input.time_range,
        )
        return rows, validated_input.data_source

    def _compute_metrics(
        self, rows: List[Dict[str, Any]], aggregations: List[AggregationSpec]
    ) -> tuple[Dict[str, MetricValue], List[str]]:
        """Calculates metric values using AnalyticsCalculator."""
        metrics_dict: Dict[str, MetricValue] = {}
        notes: List[str] = []

        if aggregations:
            for agg in aggregations:
                metric_name = agg.alias or f"{agg.field}_{agg.op.value}"
                values, invalid_count = AnalyticsCalculator.extract_numeric_series(rows=rows, field=agg.field)
                if invalid_count > 0:
                    notes.append(f"Field '{agg.field}': skipped {invalid_count} null or non-numeric value(s).")

                computed_val = AnalyticsCalculator.compute_aggregation(values=values, op=agg.op.value)
                metrics_dict[metric_name] = MetricValue(value=computed_val, unit=agg.unit)
        else:
            # Default auto-metrics if no specific aggregations requested
            metrics_dict["record_count"] = MetricValue(value=float(len(rows)), unit="records")
            sample = rows[0] if rows else {}
            for col, val in sample.items():
                if isinstance(val, (int, float)):
                    values, _ = AnalyticsCalculator.extract_numeric_series(rows=rows, field=col)
                    if values:
                        metrics_dict[f"{col}_sum"] = MetricValue(
                            value=AnalyticsCalculator.compute_aggregation(values, "sum")
                        )
                        metrics_dict[f"{col}_mean"] = MetricValue(
                            value=AnalyticsCalculator.compute_aggregation(values, "mean")
                        )

        return metrics_dict, notes

    async def _generate_narrative(
        self,
        analysis_goal: str,
        metrics_dict: Dict[str, MetricValue],
        data_ref: str,
        row_count: int,
    ) -> str:
        """
        Synthesizes human-readable narrative.
        Crucially separates narrative generation from calculated metrics,
        guaranteeing the model cannot alter the numeric metric dictionary.
        """
        metric_lines = [
            f"- {k}: {m.value} {m.unit or ''}".strip()
            for k, m in metrics_dict.items()
        ]
        metrics_text = "\n".join(metric_lines) if metric_lines else "No metrics calculated."

        if self.model_provider is not None and self.model_provider.chat_model is not None:
            prompt = (
                f"{ANALYTICS_NARRATIVE_PROMPT}\n\n"
                f"Analysis Goal: {analysis_goal}\n"
                f"Data Source: {data_ref} ({row_count} records)\n\n"
                f"Computed Metrics:\n{metrics_text}\n\n"
                "Narrative Interpretation:"
            )

            chat_model = self.model_provider.chat_model
            if hasattr(chat_model, "ainvoke"):
                resp = await chat_model.ainvoke(prompt)
            elif hasattr(chat_model, "invoke"):
                resp = chat_model.invoke(prompt)
            else:
                resp = str(chat_model)

            return resp.content if hasattr(resp, "content") else str(resp)

        # Deterministic, zero-network fallback narrative
        parts = [f"{k} = {m.value} {m.unit or ''}".strip() for k, m in metrics_dict.items()]
        return (
            f"Analysis of '{data_ref}' ({row_count} records) for '{analysis_goal}': "
            + "; ".join(parts)
            + "."
        )
