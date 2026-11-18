"""Exact quantiles whose memory does not grow with the rows (0.8.9).

DuckDB's quantile_cont / median hold every value of the aggregate in memory
and cannot spill, so one exact quantile over the whole latest spine grows
with the store (~3.7 GB per copy at 306 M rows; past a 6-8 GB budget near a
billion). The same numbers from COUNTS per distinct value — a GROUP BY and a
window over the distinct values (both can spill; distinct rates are a few
million at most) — then the value at each rank.

`exact_quantiles_sql` returns, bit for bit, what `quantile_cont(x, qs)`
returns: position h = (n-1)*q; the values at ranks floor(h) and ceil(h);
`lo*(1-d) + hi*d` with d = h - floor(h) — the form DuckDB uses (lo + d*(hi-lo)
differs in the last bit and flips a half-cent; tests/mrfx/test_quantiles.py
compares both on random data with ties).
"""
from __future__ import annotations


def exact_quantiles_sql(values_sql: str, qs: list[float], round_to: int | None = None) -> str:
    """SQL with ONE row and ONE column: the list of quantiles `qs` (in that
    order) of column `x` of `values_sql`, NULL when it has no rows. NULL x
    are ignored, like quantile_cont. `round_to` rounds each with DuckDB's
    round (half away from zero), as the callers did."""
    qlist = "[" + ", ".join(repr(float(q)) for q in qs) + "]"
    v = f"round(v, {int(round_to)})" if round_to is not None else "v"
    return f"""
    WITH __c AS (SELECT x, count(*) AS k FROM ({values_sql}) __src WHERE x IS NOT NULL GROUP BY x),
         __o AS (SELECT x, sum(k) OVER (ORDER BY x ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                        AS cum FROM __c),
         __n AS (SELECT sum(k) AS n FROM __c),
         __p AS (SELECT __i, q, (__n.n - 1) * q AS h FROM __n,
                        unnest({qlist}) WITH ORDINALITY AS __t(q, __i) WHERE __n.n > 0)
    SELECT CASE WHEN count(*) = 0 THEN NULL ELSE list({v} ORDER BY __i) END FROM (
      SELECT __i, lo_v * (1 - (h - floor(h))) + hi_v * (h - floor(h)) AS v FROM (
        SELECT __i, h,
               (SELECT min(x) FROM __o WHERE cum > floor(__p.h)) AS lo_v,
               (SELECT min(x) FROM __o WHERE cum > ceil(__p.h)) AS hi_v
        FROM __p))
    """
