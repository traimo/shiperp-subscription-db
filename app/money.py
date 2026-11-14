"""Money as Decimal.

Stored amounts are Numeric(14, 2) and come back as Decimal. Every value assigned to a money column is converted to
Decimal and rounded to the cent (half-up) by the model hook in models.py, so the importer, the workflows and the forms
all land in the same place. Derived figures (ARR = TCV / years, monthly revenue, deferred) are computed in Decimal at
full precision and rounded only for display or when stored.

Rule: never mix float and Decimal arithmetic - convert with D() first (comparisons between the two are fine).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal("0")
CENT = Decimal("0.01")


def D(value) -> Decimal | None:
    """Any number (int, float, Decimal, '1,234.50') -> Decimal; None / '' -> None. Floats go through str() so 0.1
    becomes Decimal('0.1'), not its binary expansion."""
    if value is None or isinstance(value, bool):
        return None if value is None else Decimal(int(value))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"Not a number: {value!r}") from None


def D0(value) -> Decimal:
    """D() with None -> 0."""
    d = D(value)
    return ZERO if d is None else d


def cents(value) -> Decimal | None:
    """Round to the cent, half-up (accounting rounding, not Python's banker's rounding)."""
    d = D(value)
    return None if d is None else d.quantize(CENT, rounding=ROUND_HALF_UP)
