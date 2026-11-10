"""ONE rounding rule for every number the app prints (round 7 P1.13).

Python's built-in round() rounds half to EVEN, and rounds the binary double
(2.675 is stored as 2.67499999…). DuckDB's round() rounds half UP. Both were
in use, so one fact printed two ways: $45.00 against an MPFS $40.00 is
112.5 %, which the SQL path and a person with a calculator call 113 % and
round() called 112; positions 0 and 25 have a median of 12.5 — the headline
said p12 while the per-code SQL said p13. half_up() rounds the DECIMAL a
DuckDB itself
rounds: half away from zero on the DOUBLE product x * 10**d (audit 8 P2-9 —
the decimal-string rule disagreed with SQL on 1,012 of 18,000 half-cent
medians: 20.025 is stored as 20.02499…, SQL prints 20.02, str() said 20.03,
so Overview's 45.12 sat beside Markets' 45.13). Checked against DuckDB's
round() on 30,000 values at 0, 1 and 2 dp: identical. Same signature and
return types as round(): no ndigits -> int, ndigits -> float.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def signed_pct(x, ndigits: int = 1) -> str:
    """A signed percentage the way every report prints one: half-up (half_up)
    and never "-0.0%" — $40.00 -> $39.99 is -0.025 %, which Python's format
    printed as "-0.0% in dollars … but -2.9% in real terms" (review 9 4.9)."""
    v = half_up(float(x), ndigits)
    if v == 0:
        v = 0.0
    return f"{v:+.{ndigits}f}%"


def half_up(x, ndigits: int | None = None):
    if x is None:
        raise TypeError("half_up() of None")   # same contract as round()
    if isinstance(x, bool) or not isinstance(x, (int, float, Decimal)):
        return round(x, ndigits) if ndigits is not None else round(x)
    if isinstance(x, float) and (x != x or x in (float("inf"), float("-inf"))):
        return round(x, ndigits) if ndigits is not None else round(x)
    nd = ndigits or 0
    try:
        if isinstance(x, float):
            # DuckDB's rule: scale in floating point, round the scaled double
            # half away from zero (exactly — Decimal(float) is the binary
            # value), scale back
            scale = 10.0 ** nd
            r = Decimal(x * scale).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            if ndigits is None:
                return int(r)
            return float(r) / scale
        r = Decimal(x).quantize(Decimal(1).scaleb(-nd), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, OverflowError):
        return round(x, ndigits) if ndigits is not None else round(x)
    if ndigits is None:
        return int(r)
    return float(r)
