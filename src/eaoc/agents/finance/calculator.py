"""
EAOC Finance Agent Calculator
Implements deterministic, exact monetary arithmetic using Python Decimal with
ROUND_HALF_UP rounding, currency validation, mixed-currency rejection, and zero-allocation
budget variance safeguards.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from eaoc.agents.finance.schemas import FinanceTotals, VarianceSummary

TWO_PLACES = Decimal("0.01")
CURRENCY_REGEX = re.compile(r"^[A-Z]{3}$")


class FinanceCalculator:
    """
    Deterministic monetary calculation engine.
    
    Guarantees:
    1. Uses Decimal for all calculations to eliminate floating-point inaccuracies.
    2. Quantizes all currency amounts to 2 decimal places using ROUND_HALF_UP.
    3. Strictly enforces currency uniformity (rejects mixed currencies without exchange rates).
    4. Guards against division by zero when calculating budget variance percentages.
    5. Defines favorable/unfavorable semantics strictly in the context of expense budgets.
    """

    @staticmethod
    def to_decimal(val: Any) -> Decimal:
        """
        Converts input value to Decimal and quantizes to 2 decimal places.
        Raises ValueError if val is non-finite, NaN, or non-numeric.
        """
        if val is None:
            raise ValueError("Monetary amount cannot be None.")
        if isinstance(val, (bool)):
            raise ValueError(f"Boolean value {val!r} is not a valid monetary amount.")
        try:
            d = Decimal(str(val))
        except (InvalidOperation, TypeError, ValueError) as e:
            raise ValueError(f"Invalid monetary value: {val!r}") from e

        if not d.is_finite():
            raise ValueError(f"Monetary value must be finite; received {val!r}.")

        return d.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    @classmethod
    def validate_currency_uniformity(
        cls,
        records: List[Dict[str, Any]],
        expected_currency: Optional[str] = None,
        currency_field: str = "currency",
    ) -> Optional[str]:
        """
        Validates that all records in the collection have consistent ISO 4217 currency.
        Raises ValueError if multiple different currencies are present or if a currency
        mismatches expected_currency.
        """
        found_currencies: Set[str] = set()

        if expected_currency is not None:
            clean_expected = expected_currency.strip().upper()
            if not CURRENCY_REGEX.match(clean_expected):
                raise ValueError(
                    f"Expected currency '{expected_currency}' is not a valid ISO 4217 code."
                )
            found_currencies.add(clean_expected)

        for idx, rec in enumerate(records):
            raw_curr = rec.get(currency_field)
            if raw_curr is not None:
                curr_clean = str(raw_curr).strip().upper()
                if not CURRENCY_REGEX.match(curr_clean):
                    raise ValueError(
                        f"Record {idx} has invalid currency code '{raw_curr}'. Must be 3 uppercase letters."
                    )
                found_currencies.add(curr_clean)

        if len(found_currencies) > 1:
            sorted_currs = sorted(list(found_currencies))
            raise ValueError(
                f"Mixed currencies detected: {sorted_currs}. Currency conversion is strictly "
                "prohibited without validated exchange rates."
            )

        if found_currencies:
            return next(iter(found_currencies))
        return expected_currency

    @classmethod
    def compute_variance(
        cls,
        allocated: Any,
        actual: Any,
        currency: str,
    ) -> VarianceSummary:
        """
        Computes deterministic budget variance.
        
        Formula:
          variance_amount = allocated - actual
          variance_percentage = (allocated - actual) / allocated * 100  (when allocated != 0)
        
        Semantics:
          In expense budgets, favorable=True when actual <= allocated (under or on budget).
          When allocated is 0: variance_percentage is None, percentage_status='undefined_zero_allocation'.
        """
        curr_clean = currency.strip().upper()
        if not CURRENCY_REGEX.match(curr_clean):
            raise ValueError(f"Invalid currency code '{currency}' in compute_variance.")

        allocated_dec = cls.to_decimal(allocated)
        actual_dec = cls.to_decimal(actual)

        variance_amount = (allocated_dec - actual_dec).quantize(
            TWO_PLACES, rounding=ROUND_HALF_UP
        )

        # Favorable in expense budgets means actual spend did not exceed allocation
        favorable = actual_dec <= allocated_dec

        if allocated_dec == Decimal("0.00"):
            return VarianceSummary(
                currency=curr_clean,
                allocated=allocated_dec,
                actual=actual_dec,
                variance_amount=variance_amount,
                variance_percentage=None,
                percentage_status="undefined_zero_allocation",
                favorable=favorable,
                notes="Allocated budget is 0.00; percentage variance is undefined.",
            )

        # Compute percentage variance
        pct = (
            (allocated_dec - actual_dec)
            / allocated_dec
            * Decimal("100")
        ).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

        return VarianceSummary(
            currency=curr_clean,
            allocated=allocated_dec,
            actual=actual_dec,
            variance_amount=variance_amount,
            variance_percentage=pct,
            percentage_status="computed",
            favorable=favorable,
            notes=f"Expense budget variance: {'favorable (under budget)' if favorable else 'unfavorable (over budget)'}.",
        )

    @classmethod
    def compute_totals(
        cls,
        records: List[Dict[str, Any]],
        amount_field: str = "amount",
        expected_currency: Optional[str] = None,
    ) -> FinanceTotals:
        """
        Aggregates total monetary amount across records and validates currency consistency.
        Computes exact average amount per record.
        """
        resolved_curr = cls.validate_currency_uniformity(
            records=records, expected_currency=expected_currency
        )
        if not resolved_curr:
            resolved_curr = "USD"  # Default fallback if no currency specified anywhere

        total = Decimal("0.00")
        count = len(records)

        for idx, rec in enumerate(records):
            val = rec.get(amount_field)
            if val is None:
                raise ValueError(
                    f"Record at index {idx} is missing required monetary field '{amount_field}'."
                )
            amount_dec = cls.to_decimal(val)
            total += amount_dec

        total = total.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        avg = (
            (total / Decimal(str(count))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            if count > 0
            else None
        )

        return FinanceTotals(
            currency=resolved_curr,
            total_amount=total,
            record_count=count,
            average_amount=avg,
        )
