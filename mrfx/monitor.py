"""Rate-change monitoring (§7E).

When a payer refreshes its MRF, compare the new month against a prior month and
surface the rate changes — quiet cuts and increases — on the codes that matter.
Requires at least two months of a payer in the store (re-ingest monthly).

Matches on the full dedup grain (payer, TIN, code, modifier-set, billing class,
place-of-service set) so a change is a real rate move on the SAME contract line,
not an artifact of a modifier or class differing between months. Dollar,
base-modifier, professional basis by default — same as the benchmark.
"""

from __future__ import annotations

import datetime as dt
import json

from . import __version__
from .benchmark import BenchmarkError, _market_where, resolve_subject_tins
from .catalog import code_info
from .store import Store, defuse_csv, mask_tin


def available_months(store: Store) -> list[str]:
    with store.connect() as con:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT file_month FROM rates_by_tin "
            "WHERE file_month IS NOT NULL ORDER BY file_month DESC"
        ).fetchall()]


def compute_rate_changes(store: Store, market: dict, *, subject: str | None = None,
                         min_pct: float | None = None) -> dict:
    """Rate moves between two months on matching contract lines (§7E.1).

    `market.month` is the NEW month (required). `market.prev_month` is the
    baseline; if absent, the most recent month strictly before the new one that
    exists in the store is used. Optionally scope to a subject practice.
    """
    new_month = market.get("month")
    if not new_month:
        raise BenchmarkError("an as-of month is required (7A.5) — pass market.month")
    months = available_months(store)
    old_month = market.get("prev_month")
    if not old_month:
        earlier = [m for m in months if m < new_month]
        old_month = earlier[0] if earlier else None
    if not old_month:
        raise BenchmarkError(
            "rate-change monitoring needs two months — only one is in the store. "
            "Re-ingest the payer's newer MRF (a later file_month) and try again.")
    if old_month >= new_month:
        raise BenchmarkError("prev_month must be earlier than month")

    include_assistant = bool(market.get("include_assistant", False))
    include_non_dollar = bool(market.get("include_non_dollar", False))
    where_cur, p_cur = _market_where({**market, "month": new_month},
                                     include_assistant, include_non_dollar)
    where_prev, p_prev = _market_where({**market, "month": old_month},
                                       include_assistant, include_non_dollar)

    subject_tins = resolve_subject_tins(store, subject) if subject else None
    subj_clause, subj_params = "", []
    if subject_tins:
        subj_clause = "AND t.tin_value IN (SELECT unnest(?::VARCHAR[]))"
        subj_params = [subject_tins]

    # median per (payer, tin, code, modifier, class, POS) within each month, so
    # a TIN's rate variants collapse the same way the rest of the app dedups
    def side(where: str) -> str:
        return f"""
            SELECT t.payer, t.tin_value, t.billing_code, t.modifier_set,
                   t.billing_class, t.service_code_set,
                   median(t.negotiated_rate) AS rate
            FROM rates_by_tin t LEFT JOIN tin_directory td USING (tin_value)
            WHERE {where} {subj_clause}
            GROUP BY t.payer, t.tin_value, t.billing_code, t.modifier_set,
                     t.billing_class, t.service_code_set
        """

    sql = f"""
    WITH cur AS ({side(where_cur)}), prev AS ({side(where_prev)})
    SELECT c.payer, c.tin_value, c.billing_code, c.modifier_set,
           round(p.rate, 2) AS old_rate, round(c.rate, 2) AS new_rate,
           round(c.rate - p.rate, 2) AS delta,
           round(100.0 * (c.rate - p.rate) / p.rate, 1) AS pct_change
    FROM cur c JOIN prev p USING
        (payer, tin_value, billing_code, modifier_set, billing_class, service_code_set)
    WHERE p.rate > 0 AND abs(c.rate - p.rate) >= 0.01
    ORDER BY pct_change ASC
    """
    params = [*p_cur, *subj_params, *p_prev, *subj_params]
    with store.connect() as con:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        raw = [dict(zip(cols, r)) for r in cur.fetchall()]
        names = dict(con.execute("SELECT tin_value, display_name FROM tin_directory").fetchall())

    rows, cuts, increases, pct_moves = [], 0, 0, []
    for r in raw:
        if min_pct is not None and abs(r["pct_change"]) < min_pct:
            continue
        desc, _, _ = code_info(r["billing_code"])
        direction = "cut" if r["delta"] < 0 else "increase"
        cuts += direction == "cut"
        increases += direction == "increase"
        pct_moves.append(r["pct_change"])
        rows.append({
            "payer": r["payer"],
            "tin_value": mask_tin(r["tin_value"]),
            "display_name": names.get(r["tin_value"]),
            "billing_code": r["billing_code"],
            "description": desc,
            "modifier_set": r["modifier_set"],
            "old_rate": r["old_rate"],
            "new_rate": r["new_rate"],
            "delta": r["delta"],
            "pct_change": r["pct_change"],
            "direction": direction,
        })
    # only negative moves are cuts / only positive are increases — taking the
    # min/max over ALL rows would report the smallest increase as a "cut" when
    # nothing was actually cut this month
    biggest_cut = min((p for p in pct_moves if p < 0), default=None)
    biggest_increase = max((p for p in pct_moves if p > 0), default=None)
    return {
        "market": {k: v for k, v in market.items() if v not in (None, [], "")},
        "new_month": new_month,
        "prev_month": old_month,
        "subject": subject,
        "count": len(rows),
        "n_cuts": cuts,
        "n_increases": increases,
        "biggest_cut_pct": biggest_cut,
        "biggest_increase_pct": biggest_increase,
        "changes": rows,
    }


def rate_changes_csv(result: dict) -> str:
    """Change list as CSV with a methodology header block."""
    import csv
    import io
    out = io.StringIO()
    for line in [
        f"Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by MRF Explorer v{__version__}.",
        f"Rate changes from {result['prev_month']} to {result['new_month']} "
        f"({result['n_cuts']} cuts, {result['n_increases']} increases).",
        f"Market definition: {json.dumps(result['market'])}.",
        "A change is a rate move on the SAME contract line (payer, TIN, code, "
        "modifier-set, billing class, place-of-service set) present in BOTH "
        "months. Published negotiated rates, not collections.",
    ]:
        out.write(f"# {line}\n")
    w = csv.writer(out)
    w.writerow(["payer", "display_name", "tin", "billing_code", "description",
                "modifier_set", "old_rate", "new_rate", "delta", "pct_change", "direction"])
    for x in result["changes"]:
        w.writerow([defuse_csv(x["payer"]), defuse_csv(x["display_name"]) or "", x["tin_value"],
                    x["billing_code"], defuse_csv(x["description"]) or "", x["modifier_set"] or "",
                    x["old_rate"], x["new_rate"], x["delta"], x["pct_change"], x["direction"]])
    return out.getvalue()
