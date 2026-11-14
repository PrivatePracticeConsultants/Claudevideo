"""Contract-lifecycle views over the rate store.

Two questions the ingested MRF rows can answer but nothing surfaced yet:

- **Renewal radar** — MRFs carry an `expiration_date` per negotiated price.
  When a payer publishes a REAL one, it says when a contract is up, which is
  when a renegotiation has to start. Payers are wildly inconsistent here
  (blank, 9999-12-31, 2099 placeholders, or a date already in the past), so
  this module measures usable coverage FIRST and every answer states it. A
  radar built on 4%-populated dates would be worse than none.
- **New to network** — a practice that appears in a payer's published book
  this month and was absent last month has just signed its first contract
  with that payer. That is the strongest lead signal in the data: brand new
  to negotiating, probably took the opening offer.

Both reuse the house market basis, so their numbers agree with every tab.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from .benchmark import (BenchmarkError, _market_where, _rates_relation,
                        normalize_market, resolve_subject_tins)
from .catalog import therapy_taxonomy_sql
from .sqlfn import modal_zip_by_tin
from .store import Store, generation_cached, mask_tin
from .rounding import half_up

log = logging.getLogger(__name__)

# A published expiration is USABLE only if it parses and lands in a window a
# real contract could occupy. Everything else is a placeholder or a stale
# publication, and counting it would invent renewals that do not exist.
PLACEHOLDER_YEARS = (9999, 2099, 2999, 1900)
MAX_FUTURE_YEARS = 10


def _usable_expiry_sql(col: str = "expiration_date") -> str:
    """SQL boolean: TRUE when `col` looks like a real contract end date."""
    this_year = dt.date.today().year
    bad = ", ".join(str(y) for y in PLACEHOLDER_YEARS)
    return (
        f"({col} IS NOT NULL AND {col} <> '' "
        f" AND regexp_full_match({col}, '\\d{{4}}-\\d{{2}}-\\d{{2}}') "
        f" AND CAST(substr({col}, 1, 4) AS INTEGER) NOT IN ({bad}) "
        f" AND CAST(substr({col}, 1, 4) AS INTEGER) BETWEEN {this_year - 1} "
        f"     AND {this_year + MAX_FUTURE_YEARS})"
    )


@generation_cached('contracts.expiration_coverage')
def expiration_coverage(store: Store, market: dict | None = None) -> dict:
    """How often payers actually publish a usable expiration date, overall and
    per payer. This is the honesty gate for the radar: the UI shows it before
    any renewal list, so a sparse field can never masquerade as a schedule."""
    market = normalize_market(market or {})
    where, params = _market_where(market, False, False)
    rel = _rates_relation(market)
    usable = _usable_expiry_sql()
    with store.connect() as con:
        # one scan: the overall figures are the sum of the per-payer ones, and
        # for month="latest" each scan re-sorts the whole spine — no reason to
        # pay that twice
        per_payer = [
            {"payer": p, "rows": n, "with_expiry": k,
             "pct": half_up(100.0 * k / n, 1) if n else 0.0}
            for p, n, k in con.execute(
                f"SELECT t.payer, count(*), count(*) FILTER ({usable}) FROM {rel} t "
                # _market_where emits td.-qualified state predicates, so the
                # directory must be joined under exactly that alias
                f"LEFT JOIN tin_directory td ON td.tin_value = t.tin_value "
                f"WHERE {where} GROUP BY t.payer ORDER BY 2 DESC, t.payer", params).fetchall()]
    total = sum(r["rows"] for r in per_payer)
    ok = sum(r["with_expiry"] for r in per_payer)
    return {
        "rows": total, "with_expiry": ok,
        "pct": half_up(100.0 * ok / total, 1) if total else 0.0,
        "by_payer": per_payer,
        "note": ("Payers publish `expiration_date` inconsistently — blank, a "
                 "9999/2099 placeholder, or a date already past. Only dates "
                 "that parse and fall in a plausible window are counted here, "
                 "and the radar lists only those: a contract with no usable "
                 "published date is ABSENT from the radar, not 'no renewal'."),
    }


def renewal_radar(store: Store, subjects: list[str] | None = None,
                  market: dict | None = None, within_days: int = 365,
                  limit: int = 200) -> dict:
    """Contracts with a real published end date, soonest first.

    Scoped to the given practices (your clients) or, with none, the whole
    store. Every row names its payer, the codes it covers and the exact
    published date — and the result carries the coverage figure so 'only 3
    contracts' reads as 'only 3 have a usable published date', never as 'you
    only have 3 contracts'."""
    market = normalize_market(market or {})
    where, params = _market_where(market, False, False)
    rel = _rates_relation(market)
    usable = _usable_expiry_sql()
    try:
        within_days = max(1, min(int(within_days), 3650))
    except (TypeError, ValueError):
        raise BenchmarkError("within_days must be a number")
    limit = max(1, min(int(limit), 1000))

    tin_filter, tin_params, subject_map = "", [], {}
    if subjects:
        all_tins: list[str] = []
        for s in subjects:
            tins = resolve_subject_tins(store, s)
            for t in tins:
                subject_map[t] = s
            all_tins.extend(tins)
        if not all_tins:
            raise BenchmarkError("none of those practices resolved to a tax id")
        tin_filter = " AND t.tin_value IN (SELECT unnest(?::VARCHAR[]))"
        tin_params = [all_tins]

    horizon = (dt.date.today() + dt.timedelta(days=within_days)).isoformat()
    today = dt.date.today().isoformat()
    with store.connect() as con:
        cur = con.execute(f"""
            SELECT t.tin_value, coalesce(td.display_name, t.tin_value) AS practice,
                   t.payer, t.expiration_date AS expires,
                   count(DISTINCT t.billing_code) AS codes,
                   round(median(t.negotiated_rate), 2) AS median_rate,
                   date_diff('day', CAST(? AS DATE), CAST(t.expiration_date AS DATE)) AS days
            FROM {rel} t
            LEFT JOIN tin_directory td ON td.tin_value = t.tin_value
            WHERE {where} AND {usable.replace('expiration_date', 't.expiration_date')}
              AND t.expiration_date <= ?{tin_filter}
            GROUP BY 1, 2, 3, 4
            ORDER BY t.expiration_date, practice, t.payer, t.tin_value
            LIMIT {max(1, min(int(limit), 5000))}
        """, [today, *params, horizon, *tin_params])
        rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
    for r in rows:
        r["subject"] = subject_map.get(r["tin_value"])
        r["tin_value"] = mask_tin(r["tin_value"])
        r["expired"] = (r["days"] or 0) < 0
    cov = expiration_coverage(store, market)
    return {"rows": rows, "within_days": within_days, "horizon": horizon,
            "coverage": {k: cov[k] for k in ("rows", "with_expiry", "pct", "note")},
            "subjects": subjects or []}


def new_to_network(store: Store, market: dict | None = None,
                   prev_month: str | None = None, zip_code: str | None = None,
                   radius_miles: float | None = None, therapy_only: bool = True,
                   limit: int = 200, centroids_path=None) -> dict:
    """Practices in a payer's published book THIS month that were absent from
    every earlier book of the same payer — i.e. newly contracted.

    Needs two loaded vintages of the same payer, and says so plainly when it
    doesn't have them. A practice missing last month because the payer's file
    was not ingested that month is a FALSE new-signal, so payers are compared
    only across months where that payer actually published."""
    from .medicare import haversine_miles_sql, load_centroids  # local: optional dep

    market = normalize_market(market or {})
    month = market.get("month")
    latest = not month or str(month).strip().lower() == "latest"
    limit = max(1, min(int(limit), 1000))
    therapy = therapy_taxonomy_sql("n.taxonomy_code") if therapy_only else "TRUE"

    zip_code = (zip_code or "").strip()[:5] or None
    if zip_code and not re.fullmatch(r"\d{5}", zip_code):
        raise BenchmarkError(f"'{zip_code}' is not a 5-digit ZIP code")
    use_radius = bool(zip_code and radius_miles)

    # Which two books each payer is compared across. "latest" = EACH PAYER's
    # own newest month against its own previous one: the store's newest month
    # is a month most payers never published in (round 6: one payer's 76-row
    # file dated a month ahead left every other payer's "now" book empty, and
    # the panel listed 8 payers as compared while comparing none). A pinned
    # month compares only payers that published IN that month.
    from .monitor import payer_months
    scope_payers = market.get("payers") or None
    with store.connect() as con:
        if latest:
            pairs = [(p, n, o) for p, n, o in payer_months(store, scope_payers) if o]
        else:
            pclause = (f"AND payer IN ({', '.join('?' for _ in scope_payers)})"
                       if scope_payers else "")
            rows_pm = con.execute(f"""
                SELECT payer,
                       bool_or(file_month = ?) AS has_month,
                       max(file_month) FILTER (file_month < ?) AS prev
                FROM rates_by_tin WHERE file_month <= ? {pclause}
                GROUP BY payer ORDER BY payer
            """, [month, month, month, *(scope_payers or [])]).fetchall()
            pairs = [(p, month, o) for p, has, o in rows_pm if has and o]
        if prev_month:
            # a payer that did not publish in the requested prev_month has an
            # EMPTY "before" set — its whole book would read as newly signed.
            # Compare only payers that actually published then.
            published = {p for (p,) in con.execute(
                "SELECT DISTINCT payer FROM rates_by_tin WHERE file_month = ?",
                [prev_month]).fetchall()}
            pairs = [(p, n, prev_month) for p, n, _ in pairs
                     if p in published and prev_month < n]
        label = "latest" if latest else month
        if not pairs:
            return {"rows": [], "month": label, "compared": [], "compared_months": [],
                    "reason": ((f"no payer published in {prev_month}, so there is "
                                "no honest earlier book to compare against")
                               if prev_month else
                               ("no payer has two published months in the store yet — "
                                "new-to-network needs two vintages of the same payer "
                                "to compare")),
                    "zip": zip_code, "radius_miles": None, "total": 0}
        origin = None
        if use_radius:
            load_centroids(con, centroids_path)
            origin = con.execute("SELECT lat, lon FROM _zcta WHERE zip = ?",
                                 [zip_code]).fetchone()
            if origin is None:
                raise BenchmarkError(
                    f"ZIP {zip_code} is not in the Census ZCTA centroid list")
        # Both books on the HOUSE basis (dollar rates > $0.01, base modifiers,
        # professional, no NPI-in-the-TIN-slot rows) and the market's own
        # filters: the panel read the raw spine, so a practice whose only line
        # was an institutional $900 row was "newly contracted at $900".
        # therapy_only is applied below on the NPIs (it keeps unidentified
        # practices), not as the directory's strict practice flag.
        where, params = _market_where({**market, "month": "latest", "therapy_only": False},
                                      bool(market.get("include_assistant")),
                                      bool(market.get("include_non_dollar")))
        cur = con.execute(f"""
            WITH pairs AS (SELECT unnest(?::VARCHAR[]) AS payer,
                                  unnest(?::VARCHAR[]) AS m_now,
                                  unnest(?::VARCHAR[]) AS m_prev),
            b AS (
                SELECT t.payer, t.tin_value, t.billing_code, t.negotiated_rate,
                       t.file_month = pp.m_now AS is_now
                FROM rates_by_tin t LEFT JOIN tin_directory td USING (tin_value)
                -- every month up to this one, not only the previous: a
                -- practice in the payer's book two months ago that a partly
                -- loaded middle month lacks is not newly contracted (review
                -- 10 item 3.8). "New" = absent from EVERY earlier book.
                JOIN pairs pp ON pp.payer = t.payer AND t.file_month <= pp.m_now
                WHERE {where}
            ),
            -- the newcomers FIRST (in this month's book, absent from the
            -- previous one), then medians for them alone: the anti-join used
            -- to run after a median over every practice in every book
            -- (10.7 s -> 4.4 s on a 99M-rate store, identical rows)
            keys AS (
                SELECT payer, tin_value FROM b GROUP BY 1, 2
                HAVING bool_or(is_now) AND NOT bool_or(NOT is_now)
            )
            SELECT b.payer, pp.m_prev AS prev_month, pp.m_now AS month, b.tin_value,
                   coalesce(any_value(d.display_name), b.tin_value) AS practice,
                   any_value(d.cities)  AS cities,
                   any_value(d.states)  AS states,
                   count(DISTINCT b.billing_code) AS codes,
                   round(median(b.negotiated_rate), 2) AS median_rate
            FROM b SEMI JOIN keys k ON k.payer = b.payer AND k.tin_value = b.tin_value
            JOIN pairs pp ON pp.payer = b.payer
            LEFT JOIN tin_directory d ON d.tin_value = b.tin_value
            WHERE b.is_now
            GROUP BY b.payer, pp.m_prev, pp.m_now, b.tin_value
        """, [[p for p, _, _ in pairs], [n for _, n, _ in pairs], [o for _, _, o in pairs],
              *params])
        cols = [c[0] for c in cur.description]
        rows: list[dict] = [dict(zip(cols, r)) for r in cur.fetchall()]
        # therapy + geography filters need the member NPIs' directory rows.
        # LEFT JOIN, one scan: a practice whose clinicians are not yet in the
        # NPI directory must be KEPT and flagged "unidentified", never silently
        # dropped — under therapy_only only a practice whose identified
        # clinicians are all NON-therapy is excluded (same rule as the
        # Medicare leaderboard).
        if rows:
            tins = sorted({r["tin_value"] for r in rows})
            # placement: the shared rule (sqlfn — 5-digit, zero-padded, a blank
            # ZIP never wins, ties to the lowest ZIP), as every radius uses
            zips = dict(con.execute(
                modal_zip_by_tin("SELECT unnest(?::VARCHAR[])", store.placement_relation()),
                [tins]).fetchall())
            info = {t: (bool(ident), bool(ther), zips.get(t)) for t, ident, ther in
                    con.execute(f"""
                SELECT r.tin_value,
                       bool_or(n.npi IS NOT NULL),
                       coalesce(bool_or({therapy}), FALSE)
                FROM {store.pairs_relation()} r LEFT JOIN npi_directory n ON n.npi = r.npi
                WHERE r.tin_value IN (SELECT unnest(?::VARCHAR[]))
                GROUP BY 1
            """, [tins]).fetchall()}
            out = []
            for r in rows:
                ident, ther, z = info.get(r["tin_value"], (False, False, None))
                if therapy_only and ident and not ther:
                    continue
                r["unidentified"] = not ident
                r["zip"] = z
                out.append(r)
            if use_radius:
                dist = dict(con.execute(f"""
                    SELECT z.zip, {haversine_miles_sql('z.lat', 'z.lon',
                                                       origin[0], origin[1])}
                    FROM _zcta z
                    WHERE z.zip IN (SELECT unnest(?::VARCHAR[]))
                """, [sorted({r["zip"] for r in out if r["zip"]})]).fetchall())
                kept = []
                for r in out:
                    d = dist.get(r["zip"]) if r["zip"] else None
                    if d is None or d > float(radius_miles):
                        continue
                    r["miles"] = half_up(d, 1)
                    kept.append(r)
                out = kept
            rows = out
    # tin_value completes the order: two practices of one name (and code
    # count) came back in thread order (invariant 7) — sorted on the RAW tax
    # id, before masking makes every SSN-pattern one tie
    rows.sort(key=lambda r: (-(r.get("codes") or 0), r["practice"], r["payer"], r["tin_value"]))
    for r in rows:
        r["cities"] = list(r.get("cities") or [])
        r["states"] = list(r.get("states") or [])
        r["tin_value"] = mask_tin(r["tin_value"])
    return {"rows": rows[:limit], "month": label,
            # every payer actually compared, with the two months it was
            # compared across — whether or not it added anyone
            "compared": [(p, o) for p, _, o in pairs],
            "compared_months": [{"payer": p, "month": n, "prev_month": o} for p, n, o in pairs],
            "zip": zip_code, "radius_miles": radius_miles if use_radius else None,
            "total": len(rows), "reason": None,
            "note": ("A practice counts as new to a payer when it appears in that "
                     "payer's published book this month and was absent from the "
                     "same payer's previous published month. Payers are compared "
                     "only across months they actually published, so a month you "
                     "did not ingest cannot fake a wave of new contracts. "
                     "Practices whose clinicians are not yet identified in the "
                     "NPI directory are kept and flagged, never dropped."),
            }
