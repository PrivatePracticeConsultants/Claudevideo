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
from .benchmark import BenchmarkError, _market_where, resolve_subject_tins
from .store import Store, mask_tin

PERCENTILES = (10, 25, 40, 50, 75, 90)


def compute_leads(store: Store, market: dict, *, threshold_percentile: int = 25,
                  min_codes: int = 3, limit: int = 100,
                  exclude_subject: str | None = None) -> dict:
    """Underpaid entities in the market, most-underpaid first (§7D.1)."""
    if threshold_percentile <= 0 or threshold_percentile >= 100:
        raise BenchmarkError("threshold_percentile must be between 1 and 99")
    if min_codes < 1:
        raise BenchmarkError("min_codes must be at least 1")
    limit = max(1, min(int(limit), 1000))
    include_assistant = bool(market.get("include_assistant", False))
    include_non_dollar = bool(market.get("include_non_dollar", False))
    where, params = _market_where(market, include_assistant, include_non_dollar)

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
        FROM rates_by_tin t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where}
        GROUP BY t.billing_code, t.tin_value
    ),
    ranked AS (
        SELECT billing_code, tin_value, rate,
               100 * cume_dist() OVER (PARTITION BY billing_code ORDER BY rate) AS pct,
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
    with store.connect() as con:
        cur = con.execute(sql, [*params, min_codes, threshold_percentile, *excl_params, limit])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    sites = store.org_websites()
    leads = []
    for r in rows:
        states = r.get("states") or []
        cities = r.get("cities") or []
        leads.append({
            "tin_value": mask_tin(r["tin_value"]),
            "display_name": r["display_name"],
            "entity_kind": r["entity_kind"],
            "npi_count": r["npi_count"],
            "state": states[0] if states else None,
            "city": cities[0] if cities else None,
            "n_codes": r["n_codes"],
            "median_percentile": r["median_pct"],
            "avg_gap_to_median": r["avg_gap_to_median"],
            "website": sites.get(r["tin_value"]),
        })
    return {
        "market": {k: v for k, v in market.items() if v not in (None, [], "")},
        "threshold_percentile": threshold_percentile,
        "min_codes": min_codes,
        "count": len(leads),
        "leads": leads,
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
    w.writerow(["display_name", "tin", "entity_kind", "state", "city", "npi_count",
                "n_codes", "median_percentile", "avg_gap_to_median", "website"])
    for x in result["leads"]:
        w.writerow([x["display_name"], x["tin_value"], x["entity_kind"], x["state"] or "",
                    x["city"] or "", x["npi_count"], x["n_codes"], x["median_percentile"],
                    x["avg_gap_to_median"], x["website"] or ""])
    return out.getvalue()
