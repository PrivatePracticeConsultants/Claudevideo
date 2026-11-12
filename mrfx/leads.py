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
from decimal import ROUND_HALF_UP, Decimal

from . import __version__
from .benchmark import (PRACTICE_RATE_RULE, BenchmarkError, _market_where, _rates_relation, is_latest, market_in_words,
                        normalize_market, practice_base_sql, resolve_subject_tins)
from .store import Store, defuse_csv, generation_cached, mask_tin

PERCENTILES = (10, 25, 40, 50, 75, 90)
LEADS_LIMIT = 1000
# The sweep tests the position AS SHOWN: the median of the per-code
# percentiles, rounded to a whole percentile. A practice listed at "p25" is
# therefore always inside a p25 sweep, whatever its unrounded median.
LEADS_RULE = (
    "A practice's position on a code is its MID-RANK among the OTHER practices "
    "in the market (those paid less, plus half of those paid the same), so a "
    "practice paid exactly what everyone else is paid sits at p50. A lead is a "
    "practice whose median position across its priced codes, rounded to a "
    "whole percentile as shown, is at or below the threshold; only codes with "
    "at least 2 practices and at least 2 different rates count (a code where "
    "every practice is paid the same says nothing about underpayment). The "
    "gap is to the code's market median, this practice included.")

# MID-RANK position among the OTHER practices on the code (strictly below +
# half the ties, self excluded) — the Benchmark tab's convention. It was
# percent_rank (strictly below, ties scored 0): a payer's flat fee schedule
# put EVERY practice at p0 with a $0.00 gap, i.e. the whole market was an
# "underpaid lead" (round 7 P0.4; BCBS Alabama publishes one 97110 rate to
# 6,190 practices). Rates compared at the cent, as the Benchmark does.
# Identical to percent_rank whenever no two practices tie.
def midrank_pct_sql(partition: str = "billing_code") -> str:
    """MIDRANK_PCT over any partition (the size premium ranks within
    (payer, billing_code) — audit 8 P1-5)."""
    return (
        f"100.0 * ((rank() OVER (PARTITION BY {partition} ORDER BY round(rate, 2))) - 1"
        f" + 0.5 * ((count(*) OVER (PARTITION BY {partition}, round(rate, 2))) - 1))"
        f" / nullif((count(*) OVER (PARTITION BY {partition})) - 1, 0)")


MIDRANK_PCT = midrank_pct_sql("billing_code")


def others_median_sql(base: str) -> str:
    """SELECT billing_code, tin_value, others_median FROM `base` (one `rate`
    per (billing_code, tin_value)): each practice's market median is the
    median of the OTHER practices — Benchmark's rule (review 10 item 3.9: a
    6-practice market read $47.50 on Leads and $48.00 on Benchmark). Exact,
    without a per-row list: removing the practice at sorted position rk
    leaves a median made of at most two of the code's order statistics."""
    return f"""
    WITH _o AS (
        SELECT billing_code, tin_value, rate,
               row_number() OVER (PARTITION BY billing_code ORDER BY rate, tin_value) AS rk,
               count(*) OVER (PARTITION BY billing_code) AS n
        FROM {base}),
    _s AS (
        SELECT billing_code,
               max(rate) FILTER (WHERE rk = (n - 1) // 2) AS v_lo,
               max(rate) FILTER (WHERE rk = (n + 1) // 2) AS v_mid,
               max(rate) FILTER (WHERE rk = (n + 3) // 2) AS v_hi,
               max(rate) FILTER (WHERE rk = n // 2)       AS e_lo,
               max(rate) FILTER (WHERE rk = n // 2 + 1)   AS e_hi
        FROM _o GROUP BY billing_code)
    SELECT o.billing_code, o.tin_value,
           CASE WHEN o.n < 2 THEN NULL
                WHEN o.n % 2 = 0 THEN CASE WHEN o.rk <= o.n // 2 THEN s.e_hi ELSE s.e_lo END
                WHEN o.rk <= (o.n - 1) // 2 THEN (s.v_mid + s.v_hi) / 2
                WHEN o.rk = (o.n + 1) // 2  THEN (s.v_lo + s.v_hi) / 2
                ELSE (s.v_lo + s.v_mid) / 2 END AS others_median
    FROM _o o JOIN _s s USING (billing_code)"""


def median_of_others(sorted_vals: list, x) -> float | None:
    """Python twin of others_median_sql: the median of `sorted_vals` with ONE
    occurrence of `x` removed (x must be in the list)."""
    import bisect
    n = len(sorted_vals)
    if n < 2:
        return None
    rk = bisect.bisect_left(sorted_vals, x) + 1      # 1-based; ties are equal values
    v = sorted_vals
    if n % 2 == 0:
        return v[n // 2] if rk <= n // 2 else v[n // 2 - 1]
    lo, mid, hi = v[(n - 1) // 2 - 1], v[(n + 1) // 2 - 1], v[(n + 3) // 2 - 1]
    if rk <= (n - 1) // 2:
        return (mid + hi) / 2
    if rk == (n + 1) // 2:
        return (lo + hi) / 2
    return (lo + mid) / 2


def _worst_payer(by_payer: dict) -> dict | None:
    """The payer that underpays a practice most, compared on the codes ALL its
    payers price (a like-for-like basket — the rule visit economics uses).
    Each payer's average over ITS OWN codes named Beta ($6 under on 97110)
    over Alpha ($8 under on 97110, $2 on 97140), although Alpha pays less on
    the only code both price (review 9 item 4.3). With no code every payer
    prices, the payers are not ranked against each other: the one with the
    biggest average gap is named with its code count, so the reader sees what
    it rests on. None when no payer is under the market."""
    payers = sorted(by_payer)
    if not payers:
        return None
    shared = set.intersection(*(set(v) for v in by_payer.values()))
    basis = "shared" if shared and len(payers) > 1 else ("own" if len(payers) > 1 else "only")
    scored = []
    for p in payers:
        codes = sorted(shared) if basis == "shared" else sorted(by_payer[p])
        gaps = [by_payer[p][c] for c in codes]
        # exact decimals, half-up to the cent: a DOUBLE sum depends on order
        avg = (float((sum(gaps, Decimal(0)) / len(gaps)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)) if gaps else 0.0)
        scored.append((-avg, p, avg, len(codes)))
    scored.sort()
    _neg, payer, avg, n = scored[0]
    if avg <= 0:
        return None
    return {"payer": payer, "avg_gap": avg, "n_codes": n, "basis": basis}


@generation_cached('leads.compute_leads')
def compute_leads(store: Store, market: dict, *, threshold_percentile: int = 25,
                  min_codes: int = 3, limit: int = 100,
                  exclude_subject: str | None = None) -> dict:
    """Underpaid entities in the market, most-underpaid first (§7D.1)."""
    market = normalize_market(market)
    if threshold_percentile <= 0 or threshold_percentile >= 100:
        raise BenchmarkError("threshold_percentile must be between 1 and 99")
    if min_codes < 1:
        raise BenchmarkError("min_codes must be at least 1")
    limit = max(1, min(int(limit), LEADS_LIMIT))
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

    # The market base -- one median per (code, practice) -- is built ONCE into a
    # connection-local temp table: the ranking and the worst-payer step both
    # read it. The worst-payer step used to rebuild the whole market a second
    # time just to get each code's median.
    base_sql, base_params = practice_base_sql(store, market)
    sql = f"""
    WITH base AS (SELECT * FROM _lead_base),
    ranked AS (
        -- MID-RANK among the other practices (MIDRANK_PCT): the cheapest of
        -- a thin market still sits at 0, so the finder still surfaces it,
        -- and a flat fee schedule no longer makes everyone a p0 lead. The
        -- gap is to the median of the OTHER practices (others_median_sql)
        SELECT b.billing_code, b.tin_value, b.rate,
               {midrank_pct_sql("b.billing_code")} AS pct,
               om.others_median  AS code_median,
               count(*)          OVER (PARTITION BY b.billing_code) AS n_in_code,
               count(DISTINCT round(b.rate, 2)) OVER (PARTITION BY b.billing_code) AS n_rates
        FROM base b JOIN _lead_om om USING (billing_code, tin_value)
    ),
    per_tin AS (
        -- only codes with a real market (≥2 TINs) count toward a position
        SELECT tin_value,
               count(*)                                   AS n_codes,
               round(median(pct), 0)                      AS median_pct,
               -- summed as exact decimals: a DOUBLE sum depends on the threads'
               -- order, and the rounded cent flipped between calls (16.45 / 16.44)
               round(avg(greatest(0, code_median - rate)::DECIMAL(18, 6)), 2) AS avg_gap_to_median
        FROM ranked WHERE n_in_code >= 2 AND n_rates >= 2
        GROUP BY tin_value
    )
    SELECT p.tin_value, p.n_codes, p.median_pct, p.avg_gap_to_median,
           td.display_name, td.entity_kind, td.npi_count, td.states, td.cities
    FROM per_tin p JOIN tin_directory td USING (tin_value)
    WHERE p.n_codes >= ? AND p.median_pct <= ? {excl_clause}
    ORDER BY p.median_pct ASC, td.npi_count DESC NULLS LAST, p.avg_gap_to_median DESC,
             p.tin_value   -- unique: equal leads came back in thread order (invariant 7)
    LIMIT ?
    """
    # the state the user filtered on (if any) — a matched TIN is guaranteed to
    # carry it, so it's the state to SHOW even for a multi-state billing entity
    filter_state = (market.get("state") or "").upper() or None
    with store.connect() as con:
        con.execute(f"CREATE OR REPLACE TEMP TABLE _lead_base AS {base_sql}", base_params)
        con.execute(f"CREATE OR REPLACE TEMP TABLE _lead_om AS {others_median_sql('_lead_base')}")
        # one row past the cap says whether the list was CUT: a 1,000-row
        # list used to look complete when the market held more leads
        cur = con.execute(sql, [min_codes, threshold_percentile, *excl_params, limit + 1])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        truncated = len(rows) > limit
        rows = rows[:limit]
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
                -- the SAME code set the position counts: ≥2 practices AND ≥2
                -- distinct rates. An all-tied code has no "under market" —
                -- counting its 0 gap diluted the payer that underpays on the
                -- codes that DO rank (audit 8 P1-3: Sigma $16 named over Rho $20).
                -- The market is the median of the OTHER practices (3.9)
                SELECT om.billing_code, om.tin_value, om.others_median AS code_median,
                       c.n_in_code
                FROM _lead_om om JOIN (
                    SELECT billing_code, count(*) AS n_in_code
                    FROM _lead_base GROUP BY billing_code
                    HAVING count(DISTINCT round(rate, 2)) >= 2) c USING (billing_code)
                WHERE om.tin_value IN (SELECT unnest(?::VARCHAR[]))
            ),
            gap AS (
                -- the SAME codes the position counts: a code only this practice
                -- prices has no market to be under. Averaging its 0 in made a
                -- payer pricing one such extra code look less stingy — a payer
                -- $20 under on every shared code lost "worst payer" to one $16
                -- under on one code (tests/mrfx/test_known_answer_leads.py)
                SELECT tp.tin_value, tp.payer, tp.billing_code,
                       greatest(0, m.code_median - tp.rate)::DECIMAL(18, 6) AS gap
                FROM tp JOIN mkt m USING (billing_code, tin_value)
                WHERE m.n_in_code >= 2
            )
            SELECT tin_value, payer, billing_code, gap FROM gap
            ORDER BY tin_value, payer, billing_code
            """
            per: dict[str, dict] = {}
            for tv, payer, code, gap in con.execute(
                    wsql, [*params, lead_tins, lead_tins]).fetchall():
                per.setdefault(tv, {}).setdefault(payer, {})[code] = gap
            for tv, by_payer in per.items():
                w = _worst_payer(by_payer)
                if w:
                    worst_payer[tv] = w
        # A contract line the payer's NEWER files no longer carry stays the
        # practice's "latest" line (one row per contract, newest file it
        # appears in), so a practice the payer dropped can still top the list
        # (review 10 item 3.12). Flag it: for each lead, the payers whose
        # newest loaded month does not include it. Flag, not drop — a payer's
        # newest month may be only partly loaded.
        dropped: dict[str, list[dict]] = {}
        if lead_tins and is_latest(market):
            for tv, payer, seen, newest in con.execute("""
                WITH pm AS (SELECT payer, max(file_month) AS newest
                            FROM rates_by_tin GROUP BY payer)
                SELECT t.tin_value, t.payer, max(t.file_month) AS seen, any_value(pm.newest)
                FROM rates_by_tin t JOIN pm USING (payer)
                WHERE t.tin_value IN (SELECT unnest(?::VARCHAR[]))
                GROUP BY t.tin_value, t.payer
                HAVING max(t.file_month) < any_value(pm.newest)
                ORDER BY t.tin_value, t.payer""", [lead_tins]).fetchall():
                if market.get("payers") and payer not in market["payers"]:
                    continue
                dropped.setdefault(tv, []).append(
                    {"payer": payer, "last_seen": seen, "payer_newest": newest})
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
            # how many codes the gap rests on, and whether the payers were
            # compared on a common basket ("shared") or each on its own
            "worst_payer_codes": (worst_payer.get(r["tin_value"]) or {}).get("n_codes"),
            "worst_payer_basis": (worst_payer.get(r["tin_value"]) or {}).get("basis"),
            # payers whose newest loaded month no longer lists this practice
            "not_in_newest": dropped.get(r["tin_value"], []),
        })
    return {
        "market": {k: v for k, v in market.items() if v not in (None, [], "")},
        "threshold_percentile": threshold_percentile,
        "min_codes": min_codes,
        "count": len(leads),
        "limit": limit,
        "truncated": truncated,
        "leads": leads,
        "note": LEADS_RULE,
    }


@generation_cached('leads.compute_leaderboard')
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
    order = {
        # biggest groups first; then best-paid; then widest footprint
        "size": "td.npi_count DESC NULLS LAST, p.median_pct DESC",
        "paid": "p.median_pct DESC, td.npi_count DESC NULLS LAST",
        "footprint": "len(td.states) DESC NULLS LAST, td.npi_count DESC NULLS LAST",
    }[sort]
    base_q, base_params = practice_base_sql(store, market)
    sql = f"""
    WITH base AS ({base_q}),
    ranked AS (
        SELECT billing_code, tin_value, rate,
               {MIDRANK_PCT} AS pct,
               count(*) OVER (PARTITION BY billing_code) AS n_in_code,
               count(DISTINCT round(rate, 2)) OVER (PARTITION BY billing_code) AS n_rates
        FROM base
    ),
    per_tin AS (
        -- POSITION over the lead finder's code set (≥2 distinct rates): one
        -- practice read p0 on Leads and p25 here because a code every
        -- practice is paid the same on was averaged in (audit 8 P1-3). A
        -- practice whose codes are ALL paid the same as everyone sits at
        -- p50 — it stays on the board (size / footprint sorts need it).
        -- n_codes counts the codes IN the position (Leads' count: the same
        -- practice read 2 codes on Leads and 3 here — review 9 item 4.3);
        -- the min_codes gate counts every compared code
        SELECT tin_value, count(*) FILTER (WHERE n_rates >= 2) AS n_codes,
               count(*) AS n_compared,
               coalesce(round(median(pct) FILTER (WHERE n_rates >= 2), 0), 50.0)
                                      AS median_pct,
               round(median(rate), 2) AS median_rate
        FROM ranked WHERE n_in_code >= 2 GROUP BY tin_value
    )
    SELECT p.tin_value, p.n_codes, p.median_pct, p.median_rate,
           td.display_name, td.entity_kind, td.npi_count, td.states, td.cities
    FROM per_tin p JOIN tin_directory td USING (tin_value)
    WHERE p.n_compared >= ?
    ORDER BY {order}, p.tin_value
    LIMIT ?
    """
    filter_state = (market.get("state") or "").upper() or None
    with store.connect() as con:
        cur = con.execute(sql, [*base_params, min_codes, limit])
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
                 "into one TIN). Position is the practice's mid-rank among the "
                 "other practices on each code, median across the codes where the "
                 "market's rates differ (a code every practice is paid the same on "
                 "ranks no one — the lead finder's rule; a practice with only such "
                 "codes is p50); it describes published rates, not collections."),
    }


@generation_cached('leads.compute_payer_roster')
def compute_payer_roster(store: Store, payer: str, market: dict, *,
                         min_codes: int = 1, limit: int = 500,
                         sort: str = "name") -> dict:
    """Every organization contracted with ONE payer — the payer's book of
    therapy practices. 'Contracted' means the payer published negotiated rates
    for that practice in its MRF; that is the best available signal, not proof
    of an active, billed contract. One row per billing TIN, joined to its NPPES
    name/geography, with how many codes it prices with the payer, its median
    rate, and where it sits among the payer's OTHER practices (percentile).

    Same house basis (dollar, base-modifier, professional, latest) and market
    scope (state/discipline/therapy_only) as every other view — the payer is
    just pinned. Scoping to the one payer keeps the scan bounded."""
    payer = str(payer or "").strip()
    if not payer:
        raise BenchmarkError("pick a payer")
    if min_codes < 1:
        raise BenchmarkError("min_codes must be at least 1")
    limit = max(1, min(int(limit), 2000))
    sort = sort if sort in ("name", "size", "paid", "codes") else "name"
    market = normalize_market({**(market or {}), "payers": [payer]})
    order = {
        "name": "td.display_name NULLS LAST, td.npi_count DESC NULLS LAST",
        "size": "td.npi_count DESC NULLS LAST, td.display_name NULLS LAST",
        "paid": "p.median_pct DESC NULLS LAST, td.npi_count DESC NULLS LAST",
        "codes": "p.n_codes DESC, td.npi_count DESC NULLS LAST",
    }[sort]
    base_q, base_params = practice_base_sql(store, market)
    sql = f"""
    WITH base AS ({base_q}),
    ranked AS (
        SELECT billing_code, tin_value, rate,
               {MIDRANK_PCT} AS pct,
               count(*) OVER (PARTITION BY billing_code) AS n_in_code,
               count(DISTINCT round(rate, 2)) OVER (PARTITION BY billing_code) AS n_rates
        FROM base
    ),
    per_tin AS (
        -- n_codes counts EVERY code the practice prices with the payer (book
        -- size); median_pct is the one overall-position rule (Leads, the
        -- leaderboard, benchmark.overall_position): codes the payer prices
        -- for ≥2 practices at ≥2 different rates; p50 when every such code is
        -- all-tied (an all-tied code read p50 here and was skipped on Leads:
        -- p50 vs p31 for one practice — review 9 item 4.3); null with none
        SELECT tin_value, count(*) AS n_codes,
               round(median(rate), 2) AS median_rate,
               CASE WHEN count(*) FILTER (WHERE n_in_code >= 2) = 0 THEN NULL
                    ELSE coalesce(round(median(pct) FILTER (
                             WHERE n_in_code >= 2 AND n_rates >= 2), 0), 50.0)
               END AS median_pct
        FROM ranked GROUP BY tin_value
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
        cur = con.execute(sql, [*base_params, min_codes, limit + 1])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    truncated = len(rows) > limit
    rows = rows[:limit]
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
    # How fresh is the book being described. A roster of a payer that last
    # re-published 14 months ago is a roster of what WAS true, and the reader
    # is about to act on it — so the age travels with the list, not on some
    # other tab (cosmetic tier: never let it cost the roster).
    freshness = None
    try:
        from .quality import payer_freshness
        freshness = next(
            (f for f in payer_freshness(store)["payers"] if f["payer"] == payer),
            None)
    except Exception:  # noqa: BLE001
        freshness = None
    return {
        "payer": payer,
        "market": {k: v for k, v in market.items()
                   if k != "payers" and v not in (None, [], "")},
        "sort": sort, "min_codes": min_codes,
        "count": len(out), "truncated": truncated, "limit": limit,
        "organizations": out,
        "freshness": freshness,
        "note": ("'Contracted' = the payer published negotiated rates for the "
                 "practice; a published rate is not proof of an active or billed "
                 "contract. Percentile is the practice's mid-rank among THIS "
                 "payer's other practices (paid less, plus half of those paid the "
                 "same; codes it prices for ≥2 practices); "
                 "size is distinct NPIs under the billing TIN. Names/geography "
                 "from NPPES enrichment — practices still being identified show "
                 "a masked TIN and no location."),
    }


def payer_roster_csv(result: dict) -> str:
    """Payer roster as CSV with a methodology header block."""
    import csv
    import io
    out = io.StringIO()
    for line in [
        f"Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by MRF Explorer v{__version__}.",
        f"Organizations contracted with {result['payer']} "
        f"({result['count']}{'+' if result.get('truncated') else ''}, "
        f"pricing at least {result['min_codes']} code(s)).",
        f"Market definition: {market_in_words(result['market'])}.",
        "'Contracted' = the payer published negotiated rates for the practice — "
        "not proof of an active/billed contract. Percentile is the practice's "
        "position among this payer's other practices; not collections. "
        "Names/geography from NPPES enrichment.",
    ]:
        out.write(f"# {line}\n")
    w = csv.writer(out)
    w.writerow(["display_name", "tin", "entity_kind", "state", "all_states",
                "city", "npi_count", "n_codes", "median_percentile",
                "median_rate", "website"])
    for x in result["organizations"]:
        w.writerow([defuse_csv(x["display_name"]) or "", x["tin_value"],
                    x["entity_kind"] or "", x["state"] or "",
                    "; ".join(x.get("states") or []),
                    defuse_csv(x["city"]) or "", x["npi_count"],
                    x["n_codes"], x["median_percentile"] if x["median_percentile"] is not None else "",
                    x["median_rate"] if x["median_rate"] is not None else "",
                    defuse_csv(x["website"]) or ""])
    return out.getvalue()


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
        f"Market definition: {market_in_words(m)}.",
        LEADS_RULE,
        PRACTICE_RATE_RULE,
        *([f"List CUT at {result['limit']:,} leads; the market holds more. Narrow it "
           "(state, payer, discipline) or raise the minimum code count to see the rest."]
          if result.get("truncated") else []),
        "Percentile position comes from published negotiated rates, not "
        "collections; avg_gap_to_median is a rate-level magnitude, NOT annual "
        "dollars (that needs the practice's own volumes — run the benchmark's "
        "opportunity model per lead). Geography/name from NPPES enrichment.",
        "worst_payer is compared on the codes every one of the practice's "
        "payers prices (worst_payer_codes of them); when they share none, each "
        "payer's own codes are averaged. avg_gap_to_median is to the median of "
        "the OTHER practices (Benchmark's market median).",
        "not_in_payers_newest_file names a payer whose newest loaded month no "
        "longer lists the practice — its rate here is from an older file; the "
        "payer may have dropped it (or that month is only partly loaded).",
    ]:
        out.write(f"# {line}\n")
    w = csv.writer(out)
    # `state` is the matched/primary state; `all_states` is the full list so a
    # multi-state billing entity is never silently presented as single-state
    # the columns 0.7.2 had keep their places (a mail merge mapped by position
    # shifted when 0.7.3 put worst_payer_codes before website); new ones go last
    w.writerow(["display_name", "tin", "entity_kind", "state", "all_states",
                "city", "npi_count", "n_codes", "median_percentile",
                "avg_gap_to_median", "worst_payer", "worst_payer_gap",
                "website", "worst_payer_codes", "not_in_payers_newest_file"])
    for x in result["leads"]:
        w.writerow([defuse_csv(x["display_name"]), x["tin_value"], x["entity_kind"],
                    x["state"] or "", "; ".join(x.get("states") or []),
                    defuse_csv(x["city"]) or "", x["npi_count"],
                    x["n_codes"], x["median_percentile"], x["avg_gap_to_median"],
                    defuse_csv(x.get("worst_payer")) or "", x.get("worst_payer_gap") or "",
                    defuse_csv(x["website"]) or "",
                    x.get("worst_payer_codes") or "",
                    "; ".join(defuse_csv(f"{d['payer']} (last in {d['last_seen']}, newest "
                                         f"{d['payer_newest']})")
                              for d in x.get("not_in_newest") or [])])
    return out.getvalue()
