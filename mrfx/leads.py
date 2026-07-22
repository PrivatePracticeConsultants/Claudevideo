"""Underpaid-practice lead finder (§7D).

Sweep every billing entity (TIN) in a market and rank the ones sitting furthest
BELOW the market — the biggest gap is the strongest consulting lead. Joins the
contact info already in the store (org name, geography, verified website) so the
output is a ready-to-work prospect list, not just numbers.

Position is each TIN's median percentile across the codes it prices (only codes
at least two TINs price count — a code with a single provider has no market to
be under). "Below market" means that median percentile ≤ a threshold the caller
sets (default p25). No volumes and no dollars are invented; the average gap to
the code median is shown as a rate-level magnitude, and the consultant runs the
full benchmark/opportunity per lead once they pick one.
"""

from __future__ import annotations

import datetime as dt
import json

from . import __version__
from .benchmark import (BenchmarkError, _market_where, _rates_relation,
                        normalize_market, resolve_subject_tins)
from .store import Store, defuse_csv, mask_tin

PERCENTILES = (10, 25, 40, 50, 75, 90)


def compute_leads(store: Store, market: dict, *, threshold_percentile: int = 25,
                  min_codes: int = 3, limit: int = 100,
                  exclude_subject: str | None = None) -> dict:
    """Underpaid entities in the market, most-underpaid first (§7D.1)."""
    market = normalize_market(market)
    if threshold_percentile <= 0 or threshold_percentile >= 100:
        raise BenchmarkError("threshold_percentile must be between 1 and 99")
    if min_codes < 1:
        raise BenchmarkError("min_codes must be at least 1")
    limit = max(1, min(int(limit), 1000))
    include_assistant = bool(market.get("include_assistant", False))
    include_non_dollar = bool(market.get("include_non_dollar", False))
    where, params = _market_where(market, include_assistant, include_non_dollar)
    rel = _rates_relation(market)

    # optionally drop a known practice from its own lead list (a consultant
    # sweeping for prospects excludes the client they already have). Excluded
    # from the OUTPUT only — it stays in the market so everyone else's
    # percentile is unchanged.
    excl = resolve_subject_tins(store, exclude_subject) if exclude_subject else []
    excl_clause = ""
    excl_params: list = []
    if excl:
        excl_clause = "AND p.tin_value NOT IN (SELECT unnest(?::VARCHAR[]))"
        excl_params = [excl]

    sql = f"""
    WITH base AS (
        SELECT t.billing_code, t.tin_value, median(t.negotiated_rate) AS rate
        FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where}
        GROUP BY t.billing_code, t.tin_value
    ),
    ranked AS (
        -- percent_rank (0 at the cheapest, = fraction strictly below) matches
        -- the benchmark's subject-excluded position metric; cume_dist put the
        -- cheapest at 1/n, so in a thin 2-3 provider market the most-underpaid
        -- practice could never reach the default p25 sweep — the very practice
        -- the finder exists to surface.
        SELECT billing_code, tin_value, rate,
               100 * percent_rank() OVER (PARTITION BY billing_code ORDER BY rate) AS pct,
               median(rate)      OVER (PARTITION BY billing_code) AS code_median,
               count(*)          OVER (PARTITION BY billing_code) AS n_in_code
        FROM base
    ),
    per_tin AS (
        -- only codes with a real market (≥2 TINs) count toward a position
        SELECT tin_value,
               count(*)                                   AS n_codes,
               round(median(pct), 0)                      AS median_pct,
               round(avg(greatest(0, code_median - rate)), 2) AS avg_gap_to_median
        FROM ranked WHERE n_in_code >= 2
        GROUP BY tin_value
    )
    SELECT p.tin_value, p.n_codes, p.median_pct, p.avg_gap_to_median,
           td.display_name, td.entity_kind, td.npi_count, td.states, td.cities
    FROM per_tin p JOIN tin_directory td USING (tin_value)
    WHERE p.n_codes >= ? AND p.median_pct <= ? {excl_clause}
    ORDER BY p.median_pct ASC, td.npi_count DESC NULLS LAST, p.avg_gap_to_median DESC
    LIMIT ?
    """
    # the state the user filtered on (if any) — a matched TIN is guaranteed to
    # carry it, so it's the state to SHOW even for a multi-state billing entity
    filter_state = (market.get("state") or "").upper() or None
    with store.connect() as con:
        cur = con.execute(sql, [*params, min_codes, threshold_percentile, *excl_params, limit])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        # OUTREACH HOOK: for each prospect, WHICH payer underpays them most —
        # the payer with the biggest average shortfall vs the code's market
        # median. Turns "this practice is at p18" into "…because <payer> pays
        # them $X under market." Scoped to the returned TINs (small set).
        worst_payer: dict[str, dict] = {}
        lead_tins = [r["tin_value"] for r in rows]
        if lead_tins:
            wsql = f"""
            WITH tp AS (
                SELECT t.tin_value, t.payer, t.billing_code,
                       median(t.negotiated_rate) AS rate
                FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
                WHERE {where} AND t.tin_value IN (SELECT unnest(?::VARCHAR[]))
                GROUP BY t.tin_value, t.payer, t.billing_code
            ),
            mkt AS (
                SELECT billing_code, median(rate) AS code_median FROM (
                    SELECT t.billing_code, t.tin_value,
                           median(t.negotiated_rate) AS rate
                    FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
                    WHERE {where} GROUP BY t.billing_code, t.tin_value
                ) GROUP BY billing_code
            ),
            gap AS (
                SELECT tp.tin_value, tp.payer,
                       round(avg(greatest(0, m.code_median - tp.rate)), 2) AS avg_gap,
                       count(*) AS n_codes
                FROM tp JOIN mkt m USING (billing_code)
                GROUP BY tp.tin_value, tp.payer
            )
            SELECT tin_value, payer, avg_gap FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY tin_value ORDER BY avg_gap DESC, payer) AS rn
                FROM gap) WHERE rn = 1 AND avg_gap > 0
            """
            for tv, payer, gap in con.execute(
                    wsql, [*params, lead_tins, *params]).fetchall():
                worst_payer[tv] = {"payer": payer, "avg_gap": gap}
    sites = store.org_websites()
    leads = []
    for r in rows:
        states = r.get("states") or []
        cities = r.get("cities") or []
        # A billing TIN (one tax ID) can span several states — a multi-location
        # group, a management company, a metro straddling a state line. Such a
        # TIN MATCHES a state filter because it has a location there, but its
        # alphabetically-first state was being shown, so it looked out-of-state.
        # Show the MATCHED state when a filter is applied (it's guaranteed
        # present); expose the full list + a flag so the UI/CSV can be honest.
        if filter_state and filter_state in states:
            primary_state = filter_state
        else:
            primary_state = states[0] if states else None
        leads.append({
            "tin_value": mask_tin(r["tin_value"]),
            "display_name": r["display_name"],
            "entity_kind": r["entity_kind"],
            "npi_count": r["npi_count"],
            "state": primary_state,
            "states": states,                 # full list — never hide multi-state
            "multi_state": len(states) > 1,
            "city": cities[0] if cities else None,
            "n_codes": r["n_codes"],
            "median_percentile": r["median_pct"],
            "avg_gap_to_median": r["avg_gap_to_median"],
            "website": sites.get(r["tin_value"]),
            # the payer driving the underpayment — the outreach angle
            "worst_payer": (worst_payer.get(r["tin_value"]) or {}).get("payer"),
            "worst_payer_gap": (worst_payer.get(r["tin_value"]) or {}).get("avg_gap"),
        })
    return {
        "market": {k: v for k, v in market.items() if v not in (None, [], "")},
        "threshold_percentile": threshold_percentile,
        "min_codes": min_codes,
        "count": len(leads),
        "leads": leads,
    }


def compute_leaderboard(store: Store, market: dict, *, min_codes: int = 3,
                        limit: int = 50, sort: str = "size") -> dict:
    """The inverse of the lead finder: rank the market's practices by SIZE
    (distinct NPIs under the TIN), by how HIGH they're paid (median percentile),
    or by geographic FOOTPRINT — to surface the anchor practices, consolidators,
    and named comparables a consultant benchmarks against or approaches. Same
    market basis and two-TIN-real-market rule as the lead finder."""
    market = normalize_market(market)
    if min_codes < 1:
        raise BenchmarkError("min_codes must be at least 1")
    limit = max(1, min(int(limit), 1000))
    sort = sort if sort in ("size", "paid", "footprint") else "size"
    include_assistant = bool(market.get("include_assistant", False))
    include_non_dollar = bool(market.get("include_non_dollar", False))
    where, params = _market_where(market, include_assistant, include_non_dollar)
    rel = _rates_relation(market)
    order = {
        # biggest groups first; then best-paid; then widest footprint
        "size": "td.npi_count DESC NULLS LAST, p.median_pct DESC",
        "paid": "p.median_pct DESC, td.npi_count DESC NULLS LAST",
        "footprint": "len(td.states) DESC NULLS LAST, td.npi_count DESC NULLS LAST",
    }[sort]
    sql = f"""
    WITH base AS (
        SELECT t.billing_code, t.tin_value, median(t.negotiated_rate) AS rate
        FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where}
        GROUP BY t.billing_code, t.tin_value
    ),
    ranked AS (
        SELECT billing_code, tin_value, rate,
               100 * percent_rank() OVER (PARTITION BY billing_code ORDER BY rate) AS pct,
               count(*) OVER (PARTITION BY billing_code) AS n_in_code
        FROM base
    ),
    per_tin AS (
        SELECT tin_value, count(*) AS n_codes,
               round(median(pct), 0)  AS median_pct,
               round(median(rate), 2) AS median_rate
        FROM ranked WHERE n_in_code >= 2 GROUP BY tin_value
    )
    SELECT p.tin_value, p.n_codes, p.median_pct, p.median_rate,
           td.display_name, td.entity_kind, td.npi_count, td.states, td.cities
    FROM per_tin p JOIN tin_directory td USING (tin_value)
    WHERE p.n_codes >= ?
    ORDER BY {order}, p.tin_value
    LIMIT ?
    """
    filter_state = (market.get("state") or "").upper() or None
    with store.connect() as con:
        cur = con.execute(sql, [*params, min_codes, limit])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    sites = store.org_websites()
    out = []
    for r in rows:
        states = r.get("states") or []
        cities = r.get("cities") or []
        primary_state = (filter_state if filter_state and filter_state in states
                         else (states[0] if states else None))
        out.append({
            "tin_value": mask_tin(r["tin_value"]),
            "display_name": r["display_name"],
            "entity_kind": r["entity_kind"],
            "npi_count": r["npi_count"],
            "state": primary_state,
            "states": states,
            "multi_state": len(states) > 1,
            "city": cities[0] if cities else None,
            "n_codes": r["n_codes"],
            "median_percentile": r["median_pct"],
            "median_rate": r["median_rate"],
            "website": sites.get(r["tin_value"]),
        })
    return {
        "market": {k: v for k, v in market.items() if v not in (None, [], "")},
        "sort": sort, "min_codes": min_codes, "count": len(out),
        "practices": out,
        "note": ("Ranked over published negotiated rates; size is distinct NPIs "
                 "under the billing TIN (a hospital system collapses many NPIs "
                 "into one TIN). Percentile position is not collections."),
    }


def leads_csv(store: Store, result: dict) -> str:
    """Prospect list as CSV with a methodology header block (honesty invariant:
    exports carry their methodology)."""
    import csv
    import io
    out = io.StringIO()
    m = result["market"]
    for line in [
        f"Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by MRF Explorer v{__version__}.",
        f"Underpaid-practice leads: entities whose median percentile across "
        f"priced codes is at or below p{result['threshold_percentile']}, pricing "
        f"at least {result['min_codes']} codes with a real market (≥2 providers).",
        f"Market definition: {json.dumps(m)}.",
        "Percentile position comes from published negotiated rates, not "
        "collections; avg_gap_to_median is a rate-level magnitude, NOT annual "
        "dollars (that needs the practice's own volumes — run the benchmark's "
        "opportunity model per lead). Geography/name from NPPES enrichment.",
    ]:
        out.write(f"# {line}\n")
    w = csv.writer(out)
    # `state` is the matched/primary state; `all_states` is the full list so a
    # multi-state billing entity is never silently presented as single-state
    w.writerow(["display_name", "tin", "entity_kind", "state", "all_states",
                "city", "npi_count", "n_codes", "median_percentile",
                "avg_gap_to_median", "worst_payer", "worst_payer_gap", "website"])
    for x in result["leads"]:
        w.writerow([defuse_csv(x["display_name"]), x["tin_value"], x["entity_kind"],
                    x["state"] or "", "; ".join(x.get("states") or []),
                    defuse_csv(x["city"]) or "", x["npi_count"],
                    x["n_codes"], x["median_percentile"], x["avg_gap_to_median"],
                    defuse_csv(x.get("worst_payer")) or "", x.get("worst_payer_gap") or "",
                    defuse_csv(x["website"]) or ""])
    return out.getvalue()
