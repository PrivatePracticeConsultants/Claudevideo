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
from .benchmark import (BenchmarkError, _market_where, normalize_market,
                        resolve_subject_tins)
from .catalog import code_info
from .store import Store, defuse_csv, mask_tin

# a rate-change list beyond this is unusable in a browser and a memory risk to
# drain whole; cap it, keeping the biggest moves, and flag truncation honestly
_CHANGES_CAP = 5000


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
    # rate changes keep their own strict two-month validation instead of
    # normalize_market, so they need the same boundary guards it grew: a
    # non-dict market and a numeric month both came straight off the wire
    # in fuzzing and 500'd here
    if market is not None and not isinstance(market, dict):
        raise BenchmarkError("market must be an object of filters")
    market = {**(market or {})}
    for k in ("month", "prev_month"):
        if market.get(k) is not None:
            market[k] = str(market[k]).strip()
    new_month = market.get("month")
    if not new_month:
        raise BenchmarkError("an as-of month is required (7A.5) — pass market.month")
    if str(new_month).strip().lower() == "latest" or \
       str(market.get("prev_month") or "").strip().lower() == "latest":
        # rate CHANGES compare two specific publication months — "latest" names
        # no single month, and treating it as a literal string fabricated a
        # comparison (lexically > every YYYY-MM, so prev auto-picked the newest
        # real month against a phantom new one)
        raise BenchmarkError(
            "rate changes compare two specific months — pick a real month "
            "(e.g. 2026-07), not 'latest'")
    months = available_months(store)
    if new_month not in months:
        # "0 changes for 2027-01" when 2027-01 has no rows is fabrication —
        # there is nothing to compare, and the CSV header would present the
        # absence of data as the absence of change
        raise BenchmarkError(
            f"no data for {new_month} in the store — months present: "
            f"{', '.join(months) or 'none'}")
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

    # LEFT JOIN the display name PER RESULT ROW — never fetch the whole
    # tin_directory into a Python dict (one entry per TIN = millions at 300M
    # rows → OOM). The change set is bounded by month-over-month rate moves.
    # min_pct is pushed into SQL and the result is CAPPED: at book scale an
    # unscoped two-month diff can move millions of contract lines, and draining
    # that whole set into Python (then copying it twice more) would OOM the
    # single user's process / wedge their browser. Keep the biggest moves.
    min_pct_sql = float(min_pct) if min_pct is not None else 0.0
    sql = f"""
    WITH cur AS ({side(where_cur)}), prev AS ({side(where_prev)}),
    moves AS (
        SELECT c.payer, c.tin_value, c.billing_code, c.modifier_set,
               round(p.rate, 2) AS old_rate, round(c.rate, 2) AS new_rate,
               round(c.rate - p.rate, 2) AS delta,
               round(100.0 * (c.rate - p.rate) / p.rate, 1) AS pct_change,
               td.display_name AS display_name
        FROM cur c JOIN prev p USING
            (payer, tin_value, billing_code, modifier_set, billing_class, service_code_set)
        LEFT JOIN tin_directory td ON td.tin_value = c.tin_value
        WHERE p.rate > 0 AND abs(c.rate - p.rate) >= 0.01
    )
    SELECT * FROM moves
    WHERE abs(pct_change) >= ?
    ORDER BY abs(pct_change) DESC, pct_change ASC
    LIMIT ?
    """
    params = [*p_cur, *subj_params, *p_prev, *subj_params, min_pct_sql, _CHANGES_CAP + 1]
    with store.connect() as con:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        raw = [dict(zip(cols, r)) for r in cur.fetchall()]
    truncated = len(raw) > _CHANGES_CAP
    raw = raw[:_CHANGES_CAP]
    # display order: most-negative (biggest cut) first, like before the cap
    raw.sort(key=lambda r: r["pct_change"])

    rows, cuts, increases, pct_moves = [], 0, 0, []
    for r in raw:
        desc, _, _ = code_info(r["billing_code"])
        direction = "cut" if r["delta"] < 0 else "increase"
        cuts += direction == "cut"
        increases += direction == "increase"
        pct_moves.append(r["pct_change"])
        rows.append({
            "payer": r["payer"],
            "tin_value": mask_tin(r["tin_value"]),
            "display_name": r["display_name"],
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
    # BY-PAYER direction: is a payer systematically cutting PT rates across the
    # market, or is this one-off? Median move + net cut/increase count per payer,
    # most-cutting first — the strategic read a flat per-line list can't give.
    import statistics
    by_payer_map: dict[str, list] = {}
    for row in rows:
        by_payer_map.setdefault(row["payer"], []).append(row)
    by_payer = sorted((
        {
            "payer": payer,
            "n": len(rs),
            "n_cuts": sum(1 for x in rs if x["direction"] == "cut"),
            "n_increases": sum(1 for x in rs if x["direction"] == "increase"),
            "median_pct_change": round(statistics.median(x["pct_change"] for x in rs), 1),
        } for payer, rs in by_payer_map.items()),
        key=lambda d: d["median_pct_change"])
    return {
        "market": {k: v for k, v in market.items() if v not in (None, [], "")},
        "new_month": new_month,
        "prev_month": old_month,
        "subject": subject,
        "count": len(rows),
        "truncated": truncated,
        "cap": _CHANGES_CAP,
        "n_cuts": cuts,
        "n_increases": increases,
        "biggest_cut_pct": biggest_cut,
        "biggest_increase_pct": biggest_increase,
        "by_payer": by_payer,
        "changes": rows,
    }


def compute_payer_trajectory(store: Store, market: dict, *, min_months: int = 2) -> dict:
    """Each payer's median therapy-rate trajectory across ALL loaded months —
    the strategic read a two-month diff can't give: is a payer eroding rates
    over time (sign a multi-year deal against), and how long is the cut streak?

    The panel of practices priced can shift month to month, so this is the
    market's median each month (directional), not a matched-line diff — that's
    what compute_rate_changes is for. Payers ranked most-eroding first.
    """
    months = available_months(store)
    if len(months) < min_months:
        raise BenchmarkError(
            "rate-trajectory needs at least two months in the store — re-ingest "
            "the payers' newer MRFs (a later file_month) and try again.")
    # trajectory spans every month, so DON'T pin one: 'latest' makes
    # _market_where skip the file_month clause; we then read rates_by_tin
    # directly (NOT the 'latest' supersession relation, which would collapse
    # history to one row per contract).
    m = normalize_market({**(market or {}), "month": "latest"})
    include_assistant = bool(m.get("include_assistant", False))
    include_non_dollar = bool(m.get("include_non_dollar", False))
    where, params = _market_where(m, include_assistant, include_non_dollar)
    sql = f"""
    WITH base AS (
        SELECT t.payer, t.file_month, t.tin_value, median(t.negotiated_rate) AS rate
        FROM rates_by_tin t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where}
        GROUP BY t.payer, t.file_month, t.tin_value
    )
    SELECT payer, file_month,
           round(median(rate), 2)    AS median_rate,
           count(DISTINCT tin_value) AS n_practices
    FROM base GROUP BY payer, file_month
    ORDER BY payer, file_month
    """
    with store.connect() as con:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        raw = [dict(zip(cols, r)) for r in cur.fetchall()]

    series: dict[str, list] = {}
    for r in raw:
        series.setdefault(r["payer"], []).append(r)
    payers = []
    for payer, pts in series.items():
        pts.sort(key=lambda x: x["file_month"])
        if len(pts) < min_months:
            continue  # need ≥2 months for THIS payer to trace a trajectory
        first, last = pts[0], pts[-1]
        cumulative = (round(100.0 * (last["median_rate"] - first["median_rate"])
                            / first["median_rate"], 1)
                      if first["median_rate"] else None)
        # consecutive months of decline counting back from the newest point
        streak = 0
        for i in range(len(pts) - 1, 0, -1):
            if pts[i]["median_rate"] < pts[i - 1]["median_rate"]:
                streak += 1
            else:
                break
        payers.append({
            "payer": payer,
            "n_months": len(pts),
            "first_month": first["file_month"], "last_month": last["file_month"],
            "first_rate": first["median_rate"], "last_rate": last["median_rate"],
            "cumulative_pct": cumulative,
            "cut_streak": streak,
            "direction": ("down" if cumulative is not None and cumulative < 0
                          else "up" if cumulative is not None and cumulative > 0
                          else "flat"),
            "series": [{"month": p["file_month"], "median_rate": p["median_rate"],
                        "n_practices": p["n_practices"]} for p in pts],
        })
    # most-eroding first (most negative cumulative change); flats/ups after
    payers.sort(key=lambda d: (d["cumulative_pct"] is None,
                               d["cumulative_pct"] if d["cumulative_pct"] is not None else 0))
    return {
        "market": {k: v for k, v in market.items() if v not in (None, [], "")},
        "months": months,
        "count": len(payers),
        "payers": payers,
        "note": ("Median rate is over the practices priced EACH month; the panel "
                 "can shift between months, so a trajectory is directional market "
                 "movement, not a matched-contract diff (use the change monitor "
                 "for line-level moves). Published rates, not collections."),
    }


def rate_changes_csv(result: dict) -> str:
    """Change list as CSV with a methodology header block."""
    import csv
    import io
    out = io.StringIO()
    for line in [
        f"Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by MRF Explorer v{__version__}.",
        f"Rate changes from {result['prev_month']} to {result['new_month']} "
        f"({result['n_cuts']} cuts, {result['n_increases']} increases)."
        + (f" NOTE: capped at the {result['cap']} largest moves — the counts "
           "above cover only these rows; narrow the market "
           "(payer/state/discipline) to see the rest."
           if result.get("truncated") else ""),
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
                    x["billing_code"], defuse_csv(x["description"]) or "",
                    defuse_csv(x["modifier_set"]) or "",
                    x["old_rate"], x["new_rate"], x["delta"], x["pct_change"], x["direction"]])
    return out.getvalue()
