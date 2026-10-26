"""
EAOC Analytics Agent - Deterministic Calculation Engine
Performs purely mathematical, reproducible metric computations, aggregations,
and group-by breakdowns with defensive handling of nulls, empty sets, and division by zero.
"""

import math
from typing import Any, Dict, List, Optional, Tuple


class AnalyticsCalculator:
    """
    Deterministic calculation engine for quantitative enterprise analysis.
    """

    @staticmethod
    def extract_numeric_series(
        rows: List[Dict[str, Any]], field: str
    ) -> Tuple[List[float], int]:
        """
        Extracts valid float numbers from the specified field across all rows.
        Returns:
            Tuple[List[float], int]: (clean_numeric_values, missing_or_invalid_count)
        """
        values: List[float] = []
        invalid_count = 0

        for r in rows:
            raw = r.get(field)
            if raw is None:
                invalid_count += 1
                continue
            try:
                # Handle numeric types and clean numeric strings
                if isinstance(raw, (int, float)):
                    if math.isnan(raw) or math.isinf(raw):
                        invalid_count += 1
                    else:
                        values.append(float(raw))
                elif isinstance(raw, str):
                    clean_str = raw.strip().replace("$", "").replace(",", "")
                    values.append(float(clean_str))
                else:
                    invalid_count += 1
            except (ValueError, TypeError):
                invalid_count += 1

        return values, invalid_count

    @staticmethod
    def compute_aggregation(values: List[float], op: str) -> Optional[float]:
        """
        Computes standard aggregation operator on a list of clean float numbers.
        Supported ops: sum, mean, count, min, max, std.
        Returns None if dataset is empty for operations requiring elements.
        """
        operator = op.lower().strip()
        n = len(values)

        if operator == "count":
            return float(n)

        if n == 0:
            return None

        if operator == "sum":
            return round(sum(values), 4)
        elif operator in ("mean", "avg", "average"):
            return round(sum(values) / n, 4)
        elif operator == "min":
            return round(min(values), 4)
        elif operator == "max":
            return round(max(values), 4)
        elif operator in ("std", "stddev"):
            if n < 2:
                return 0.0
            mean = sum(values) / n
            variance = sum((x - mean) ** 2 for x in values) / (n - 1)
            return round(math.sqrt(variance), 4)
        else:
            raise ValueError(f"Unsupported aggregation operation '{op}'.")

    @staticmethod
    def compute_ratio(numerator: float, denominator: float) -> Tuple[Optional[float], Optional[str]]:
        """
        Safely computes ratio or percentage with division-by-zero guard.
        Returns:
            Tuple[Optional[float], Optional[str]]: (ratio_value, note)
        """
        if denominator == 0.0:
            return None, "Division by zero avoided; denominator is 0.0."
        return round(numerator / denominator, 4), None

    @staticmethod
    def compute_grouped_aggregations(
        rows: List[Dict[str, Any]],
        group_by: List[str],
        field: str,
        op: str,
    ) -> Dict[str, Optional[float]]:
        """
        Groups rows by specified key(s) and computes aggregation per group.
        """
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            key_parts = [str(r.get(g, "UNKNOWN")) for g in group_by]
            group_key = " / ".join(key_parts)
            if group_key not in groups:
                groups[group_key] = []
            groups[group_key].append(r)

        results: Dict[str, Optional[float]] = {}
        for group_key, group_rows in groups.items():
            vals, _ = AnalyticsCalculator.extract_numeric_series(group_rows, field)
            results[group_key] = AnalyticsCalculator.compute_aggregation(vals, op)

        return results
