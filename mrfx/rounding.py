"""ONE rounding rule for every number the app prints (round 7 P1.13).

Python's built-in round() rounds half to EVEN, and rounds the binary double
(2.675 is stored as 2.67499999…). DuckDB's round() rounds half UP. Both were
in use, so one fact printed two ways: $45.00 against an MPFS $40.00 is
112.5 %, which the SQL path and a person with a calculator call 113 % and
round() called 112; positions 0 and 25 have a median of 12.5 — the headline
said p12 while the per-code SQL said p13. half_up() rounds the DECIMAL a
person reads (str(2.675) = "2.675" -> 2.68), half away from zero, with
round()'s signature and return types: no ndigits -> int, ndigits -> float.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def half_up(x, ndigits: int | None = None):
    if x is None:
        raise TypeError("half_up() of None")   # same contract as round()
    if isinstance(x, bool) or not isinstance(x, (int, float, Decimal)):
        return round(x, ndigits) if ndigits is not None else round(x)
    if isinstance(x, float) and (x != x or x in (float("inf"), float("-inf"))):
        return round(x, ndigits) if ndigits is not None else round(x)
    try:
        d = Decimal(str(x))
        q = Decimal(1).scaleb(-(ndigits or 0))
        r = d.quantize(q, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return round(x, ndigits) if ndigits is not None else round(x)
    if ndigits is None:
        return int(r)
    return float(r)
