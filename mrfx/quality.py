"""Three questions about a rate that the rate itself doesn't answer.

Every number in this app has so far been treated as equally solid. It isn't:

- **What KIND of rate is it?** The TiC schema's `negotiated_type` separates a
  real contracted amount ("negotiated", "fee schedule") from one the payer
  DERIVED — an estimate it publishes where no contracted dollar amount exists.
  A market median built partly from derived rates is a weaker number than one
  built from contracts, and a client's rate being "derived" changes what the
  negotiation is even about.
- **How OLD is it?** A payer that last refreshed its file 14 months ago is
  describing contracts that may have moved. Freshness is published per file
  (`last_updated_on`), and until now nothing compared payers on it.
- **Does SIZE explain the gap?** The most common objection to a benchmark is
  "the practices above me are bigger". That is testable: bucket practices by
  how many providers they bill under and compare rates within the same payer
  and code. Sometimes there is a size premium and the client should hear it;
  sometimes there isn't, and then the objection is answered with evidence.

HONESTY. All three describe the DATA, not the practice. A derived rate is not
a worse contract, it is a less certain reading of one. A stale file is not a
stale contract. And a size premium is an association across published rates —
never a claim that growing to N providers would earn a particular rate.
"""

from __future__ import annotations

import datetime as dt
import logging

from .catalog import code_info
from .store import Store

log = logging.getLogger(__name__)

# TiC negotiated_type values that mean "a real contracted amount". Anything
# else — chiefly 'derived' — is the payer's own construction where no
# contracted dollar figure exists for that provider.
CONTRACTED_TYPES = ("negotiated", "fee schedule", "fee_schedule")

TYPE_NOTE = (
    "negotiated_type comes from the payer's own file. 'negotiated' and 'fee "
    "schedule' are contracted amounts; 'derived' is a figure the payer "
    "constructed where it has no contracted dollar amount for that provider, "
    "and 'percentage' / 'per diem' are not dollar rates at all (already "
    "excluded from every dollar median). A derived rate is not a worse "
    "contract — it is a less certain reading of one, and it belongs in a "
    "negotiation conversation as exactly that."
)

FRESHNESS_NOTE = (
    "Freshness is the last_updated_on the PAYER published in its file, and the "
    "file month this store recorded. An old file does not mean an old "
    "contract: payers refresh on their own cadences and some restate the same "
    "date for months. It means the rates you are quoting from that payer have "
    "not been re-published recently — worth saying out loud before a client "
    "acts on them."
)

SIZE_NOTE = (
    "Size is the number of distinct providers (NPIs) a practice bills under in "
    "this store's data, which is a floor: a provider who published no rate "
    "with that payer is not counted. Comparisons are within the same payer and "
    "code, so a payer's overall generosity cannot masquerade as a size effect. "
    "This is an association across published rates — never a prediction that "
    "growing to a given size would earn a given rate."
)

# Provider-count bands. Deliberately coarse: the question is
# "solo / small / group / large", and finer bands split thin markets into
# cells too small to read.
SIZE_BANDS = ((1, 1, "solo (1 provider)"), (2, 4, "small (2-4)"),
              (5, 14, "group (5-14)"), (15, 10 ** 9, "large (15+)"))


def _band(n: int | None) -> str | None:
    if not n:
        return None
    for lo, hi, label in SIZE_BANDS:
        if lo <= n <= hi:
            return label
    return None


def rate_type_mix(store: Store, market: dict | None = None) -> dict:
    """What KINDS of rate the scoped market is built from.

    Reported, never silently filtered: excluding derived rates by default would
    change every number the user has already seen. The composition is the
    finding; acting on it is the user's call.
    """
    from .benchmark import (_market_where, _rates_relation, normalize_market,
                            resolve_plan_scope)

    m = resolve_plan_scope(store, normalize_market(market or {}))
    where, params = _market_where(m, bool(m.get("include_assistant")),
                                  bool(m.get("include_non_dollar")))
    rel = _rates_relation(m)
    sql = f"""
        SELECT coalesce(nullif(trim(lower(t.negotiated_type)), ''), '(unstated)') AS rate_type,
               count(*)                     AS n_rows,
               count(DISTINCT t.tin_value)  AS n_practices,
               count(DISTINCT t.payer)      AS n_payers,
               round(median(t.negotiated_rate), 2) AS median_rate
        FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where}
        GROUP BY 1 ORDER BY n_rows DESC, rate_type
    """
    with store.connect() as con:
        rows = [dict(zip([d[0] for d in cur.description], r))
                for cur in [con.execute(sql, params)] for r in cur.fetchall()]
    total = sum(r["n_rows"] for r in rows) or 0
    for r in rows:
        r["pct"] = round(100.0 * r["n_rows"] / total, 1) if total else None
        r["contracted"] = r["rate_type"] in CONTRACTED_TYPES
    derived = [r for r in rows if not r["contracted"] and r["rate_type"] != "(unstated)"]
    derived_pct = round(sum(r["pct"] or 0 for r in derived), 1)

    # per payer, because the mix is a PAYER's publishing choice
    psql = f"""
        SELECT t.payer,
               count(*) AS n_rows,
               sum(CASE WHEN lower(trim(t.negotiated_type)) IN
                   ({', '.join("'" + x + "'" for x in CONTRACTED_TYPES)})
                   THEN 1 ELSE 0 END) AS n_contracted
        FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where}
        GROUP BY 1 ORDER BY 2 DESC, t.payer
    """
    with store.connect() as con:
        payers = [{"payer": p, "n_rows": n,
                   "pct_contracted": round(100.0 * c / n, 1) if n else None}
                  for p, n, c in con.execute(psql, params).fetchall()]
    return {
        "rows": rows, "total_rows": total, "by_payer": payers,
        "derived_pct": derived_pct,
        "flag": derived_pct >= 10.0,
        "note": TYPE_NOTE,
        "headline": (
            f"{derived_pct:g}% of the rates in this market are not contracted "
            "amounts — mostly payer-derived figures. Say so before a client "
            "quotes them." if derived_pct >= 10.0
            else "Nearly all rates in this market are contracted amounts."
            if total else "No rates match this market."),
    }


def payer_freshness(store: Store, *, stale_days: int = 270) -> dict:
    """How recently each payer re-published, by its own last_updated_on."""
    with store.connect() as con:
        rows = con.execute("""
            SELECT payer,
                   max(last_updated_on)            AS published,
                   count(*)                        AS n_files,
                   sum(coalesce(rows_emitted, 0))  AS rows_emitted,
                   max(finished_at)                AS last_ingested
            FROM files
            WHERE status = 'done' AND file_type = 'in_network' AND payer IS NOT NULL
            GROUP BY payer ORDER BY published DESC NULLS LAST, payer
        """).fetchall()
        months = dict(con.execute(
            "SELECT payer, max(file_month) FROM rates_by_tin "
            "WHERE payer IS NOT NULL GROUP BY payer").fetchall())
    today = dt.date.today()
    out = []
    for payer, published, n_files, n_rows, ingested in rows:
        age = None
        d = _as_date(published)
        if d:
            age = (today - d).days
        out.append({
            "payer": payer,
            "published": published,
            "age_days": age,
            # a date we cannot read must not pass as fresh, and must not be
            # called stale either — it is unknown, and says so
            "stale": (age is not None and age > stale_days),
            "date_unreadable": bool(published) and d is None,
            "newest_file_month": months.get(payer),
            "n_files": n_files,
            "rows": int(n_rows or 0),
            "last_ingested": str(ingested)[:10] if ingested else None,
        })
    stale = [r for r in out if r["stale"]]
    unknown = [r for r in out if r["published"] is None or r["date_unreadable"]]
    return {
        "payers": out, "count": len(out),
        "n_stale": len(stale), "stale_days": stale_days,
        "n_unknown": len(unknown),
        "note": FRESHNESS_NOTE,
        "headline": (
            f"{len(stale)} payer(s) have not re-published in over "
            f"{stale_days} days: {', '.join(r['payer'] for r in stale[:5])}."
            if stale else
            f"Every payer has re-published within {stale_days} days."
            if out else "No in-network files ingested yet."),
    }


def _as_date(s) -> dt.date | None:
    t = str(s or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def size_premium(store: Store, market: dict | None = None, *,
                 subject: str | None = None, min_practices: int = 5) -> dict:
    """Does a bigger practice get paid more, in this market?

    Compared WITHIN (payer, code) so a payer's overall generosity cannot show
    up as a size effect: each practice's rate is scored against the other
    practices priced by the same payer for the same code, and the bands are
    then compared on those scores.
    """
    from .benchmark import (BenchmarkError, _market_where, _rates_relation,
                            normalize_market, resolve_plan_scope,
                            resolve_subject_tins)

    # a band below ONE practice cannot exist; a hostile 0/negative walked
    # straight into indexing an empty percentile list (found by audit)
    min_practices = max(1, int(min_practices))
    m = resolve_plan_scope(store, normalize_market(market or {}))
    where, params = _market_where(m, bool(m.get("include_assistant")),
                                  bool(m.get("include_non_dollar")))
    rel = _rates_relation(m)
    subject_tins = resolve_subject_tins(store, subject) if subject else []
    if subject and not subject_tins:
        raise BenchmarkError(f"no practice matches {subject!r}")

    # percent_rank within (payer, code): strictly-below share, so the metric is
    # a POSITION and comparable across codes with very different dollar levels
    sql = f"""
        WITH per_tin AS (
            SELECT t.payer, t.billing_code, t.tin_value,
                   median(t.negotiated_rate) AS rate
            FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
            WHERE {where}
            GROUP BY t.payer, t.billing_code, t.tin_value
        ),
        cells AS (
            SELECT *, count(*) OVER (PARTITION BY payer, billing_code) AS n_in_cell,
                   percent_rank() OVER (PARTITION BY payer, billing_code
                                        ORDER BY rate) AS pr
            FROM per_tin
        )
        SELECT c.tin_value,
               round(100.0 * avg(c.pr), 1)  AS avg_percentile,
               count(*)                     AS n_cells,
               round(median(c.rate), 2)     AS median_rate,
               any_value(d.npi_count)       AS npi_count
        FROM cells c LEFT JOIN tin_directory d USING (tin_value)
        -- a one- or two-practice cell has no meaningful percentile
        WHERE c.n_in_cell >= 3
        GROUP BY c.tin_value
    """
    with store.connect() as con:
        rows = [dict(zip([d[0] for d in cur.description], r))
                for cur in [con.execute(sql, params)] for r in cur.fetchall()]

    bands: dict[str, list] = {}
    for r in rows:
        b = _band(r.get("npi_count"))
        if b:
            bands.setdefault(b, []).append(r)

    out = []
    for _lo, _hi, label in SIZE_BANDS:
        group = bands.get(label, [])
        if len(group) < min_practices:
            # a band with a handful of practices is noise, and printing it as a
            # finding would be the same sin as a 2-practice "market"
            out.append({"band": label, "n_practices": len(group),
                        "avg_percentile": None, "median_rate": None,
                        "thin": True})
            continue
        pct = sorted(r["avg_percentile"] for r in group)
        rates = sorted(r["median_rate"] for r in group if r["median_rate"])
        mid = len(pct) // 2
        out.append({
            "band": label, "n_practices": len(group),
            "avg_percentile": round(
                pct[mid] if len(pct) % 2 else (pct[mid - 1] + pct[mid]) / 2, 1),
            "median_rate": (round(rates[len(rates) // 2], 2) if rates else None),
            "thin": False,
        })

    usable = [b for b in out if not b["thin"]]
    spread = None
    if len(usable) >= 2:
        spread = round(usable[-1]["avg_percentile"] - usable[0]["avg_percentile"], 1)

    subj = None
    if subject_tins:
        mine = [r for r in rows if r["tin_value"] in set(subject_tins)]
        if not mine:
            # Two very different reasons for no rows, and they must not be
            # conflated. resolve_subject_tins falls through to the RAW STRING
            # for an unknown subject, so a typo lands here — and silently
            # dropping the subject sentence would let the user read a
            # market-wide answer as one about their client. But a real practice
            # whose every (payer, code) cell is too thin to rank also lands
            # here, and that is a legitimate "can't say", not a bad name.
            with store.connect() as con:
                present = con.execute(
                    f"SELECT 1 FROM {rel} t LEFT JOIN tin_directory td USING (tin_value) "
                    f"WHERE {where} AND t.tin_value IN (SELECT unnest(?::VARCHAR[])) "
                    "LIMIT 1", [*params, subject_tins]).fetchone()
            if not present:
                raise BenchmarkError(
                    f"no rates under this market scope for {subject!r} — check "
                    "the practice name, tax ID or NPI, or widen the month/state "
                    "filters")
            subj = {"subject": subject, "npi_count": None, "band": None,
                    "avg_percentile": None, "band_avg_percentile": None,
                    "vs_band": None,
                    "reason": ("this practice has rates here, but every payer+code "
                               "it is priced on has fewer than 3 practices to rank "
                               "against — too thin to place it by size")}
        if mine:
            n = max((r.get("npi_count") or 0) for r in mine)
            pcts = sorted(r["avg_percentile"] for r in mine)
            mid = len(pcts) // 2
            band = _band(n)
            peer = next((b for b in usable if b["band"] == band), None)
            mypct = round(pcts[mid] if len(pcts) % 2
                          else (pcts[mid - 1] + pcts[mid]) / 2, 1)
            subj = {
                "subject": subject, "npi_count": n, "band": band,
                "avg_percentile": mypct,
                "band_avg_percentile": peer["avg_percentile"] if peer else None,
                "vs_band": (round(mypct - peer["avg_percentile"], 1)
                            if peer else None),
            }

    headline = "Not enough practices in more than one size band to compare."
    if spread is not None:
        if abs(spread) < 5:
            headline = (
                f"No size premium in this market: the largest practices sit "
                f"{abs(spread):g} percentile points from the smallest — within "
                "noise. 'They're bigger than us' does not explain a rate gap here.")
        else:
            direction = "above" if spread > 0 else "below"
            headline = (
                f"Larger practices sit {abs(spread):g} percentile points "
                f"{direction} the smallest in this market.")
    if subj and subj["vs_band"] is not None:
        headline += (
            f" This practice ({subj['npi_count']} provider(s), {subj['band']}) "
            f"is at p{subj['avg_percentile']:g} — {abs(subj['vs_band']):g} points "
            f"{'above' if subj['vs_band'] > 0 else 'below'} others its size.")

    return {
        "bands": out, "n_practices": len(rows), "spread_points": spread,
        "subject": subj, "min_practices": min_practices,
        "market": {k: v for k, v in m.items() if not k.startswith("_")},
        "headline": headline, "note": SIZE_NOTE,
    }


def code_type_flag(store: Store, market: dict | None, subject: str) -> dict:
    """What kind of rate the SUBJECT's own rows are — the version of the
    rate-type question that belongs in a client's own report."""
    from .benchmark import (_market_where, _rates_relation, normalize_market,
                            resolve_plan_scope, resolve_subject_tins)

    m = resolve_plan_scope(store, normalize_market(market or {}))
    tins = resolve_subject_tins(store, subject)
    if not tins:
        return {"rows": [], "derived_pct": None, "flag": False, "note": TYPE_NOTE}
    where, params = _market_where(m, bool(m.get("include_assistant")),
                                  bool(m.get("include_non_dollar")))
    rel = _rates_relation(m)
    sql = f"""
        SELECT t.payer, t.billing_code,
               coalesce(nullif(trim(lower(t.negotiated_type)), ''), '(unstated)') AS rate_type,
               count(*) AS n
        FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where} AND t.tin_value IN (SELECT unnest(?::VARCHAR[]))
        GROUP BY 1, 2, 3 ORDER BY 1, 2
    """
    with store.connect() as con:
        raw = [dict(zip(["payer", "billing_code", "rate_type", "n"], r))
               for r in con.execute(sql, [*params, tins]).fetchall()]
    total = sum(r["n"] for r in raw) or 0
    not_contracted = [r for r in raw if r["rate_type"] not in CONTRACTED_TYPES
                      and r["rate_type"] != "(unstated)"]
    for r in raw:
        r["description"] = code_info(r["billing_code"])[0]
        r["contracted"] = r["rate_type"] in CONTRACTED_TYPES
    pct = round(100.0 * sum(r["n"] for r in not_contracted) / total, 1) if total else None
    return {
        "rows": [r for r in raw if not r["contracted"]][:200],
        "all_rows": len(raw), "derived_pct": pct,
        "flag": bool(pct and pct >= 10.0),
        "note": TYPE_NOTE,
        "headline": (
            f"{pct:g}% of this practice's own published rates are not "
            "contracted amounts — the payer derived them. That changes what "
            "the conversation is about: ask for a contracted schedule first."
            if pct and pct >= 10.0 else
            "This practice's published rates are contracted amounts."),
    }


# The two contracted types are NOT interchangeable, and collapsing them loses
# the most actionable fact in the column. A payer publishing 'fee schedule' for
# every provider is running one book take-it-or-leave-it; a payer publishing
# 'negotiated' is doing deals, provider by provider. That is the difference
# between a negotiation worth opening and one that will go nowhere.
NEGOTIATED_TYPES = ("negotiated",)
FEE_SCHEDULE_TYPES = ("fee schedule", "fee_schedule")

POSTURE_NOTE = (
    "Posture reads the payer's own negotiated_type field, then checks it "
    "against the evidence: whether that payer actually pays different "
    "practices different amounts for the same code. The field is the payer's "
    "CLAIM and the dispersion is the OBSERVATION, so when they disagree the "
    "answer says so rather than picking one. A payer with a single published "
    "rate per code is running one schedule for everyone — which does not prove "
    "it will refuse to negotiate, only that it has not published evidence of "
    "doing so."
)


def payer_posture(store: Store, market: dict | None = None,
                  *, min_codes: int = 3) -> dict:
    """Per payer: does it negotiate, or publish one schedule for everyone?

    Two independent signals, reported side by side:
      - the payer's own `negotiated_type` mix ('negotiated' vs 'fee schedule')
      - the OBSERVED dispersion: the share of that payer's codes on which
        different practices are paid different amounts
    """
    from .benchmark import (_market_where, _rates_relation, normalize_market,
                            resolve_plan_scope)

    # below 1, a payer with ZERO rankable codes would be judged "one rate
    # for everyone" — a fabricated verdict about evidence that does not exist
    min_codes = max(1, int(min_codes))
    m = resolve_plan_scope(store, normalize_market(market or {}))
    where, params = _market_where(m, bool(m.get("include_assistant")),
                                  bool(m.get("include_non_dollar")))
    rel = _rates_relation(m)
    neg = ", ".join("'" + t + "'" for t in NEGOTIATED_TYPES)
    fee = ", ".join("'" + t + "'" for t in FEE_SCHEDULE_TYPES)
    sql = f"""
        WITH rows AS (
            SELECT t.payer, t.billing_code, t.tin_value,
                   lower(trim(coalesce(t.negotiated_type, ''))) AS ntype,
                   median(t.negotiated_rate) AS rate
            FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
            WHERE {where}
            GROUP BY t.payer, t.billing_code, t.tin_value, 4
        ),
        -- dispersion is measured per (payer, code) across PRACTICES: a code
        -- priced by one practice cannot show dispersion either way, so it is
        -- excluded rather than counted as "no spread"
        percode AS (
            SELECT payer, billing_code,
                   count(DISTINCT tin_value)        AS n_practices,
                   count(DISTINCT round(rate, 2))   AS n_distinct_rates
            FROM rows GROUP BY payer, billing_code
        )
        SELECT r.payer,
               count(*)                                                  AS n_rows,
               sum(CASE WHEN r.ntype IN ({neg}) THEN 1 ELSE 0 END)       AS n_negotiated,
               sum(CASE WHEN r.ntype IN ({fee}) THEN 1 ELSE 0 END)       AS n_fee_schedule,
               count(DISTINCT r.billing_code)                            AS n_codes,
               (SELECT count(*) FROM percode p
                 WHERE p.payer = r.payer AND p.n_practices >= 2)         AS n_codes_rankable,
               (SELECT count(*) FROM percode p
                 WHERE p.payer = r.payer AND p.n_practices >= 2
                   AND p.n_distinct_rates > 1)                           AS n_codes_varying
        FROM rows r
        GROUP BY r.payer
        ORDER BY n_rows DESC, r.payer
    """
    with store.connect() as con:
        cur = con.execute(sql, params)
        raw = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

    out = []
    for r in raw:
        n = r["n_rows"] or 0
        pct_neg = round(100.0 * r["n_negotiated"] / n, 1) if n else None
        pct_fee = round(100.0 * r["n_fee_schedule"] / n, 1) if n else None
        rankable = r["n_codes_rankable"] or 0
        pct_var = (round(100.0 * r["n_codes_varying"] / rankable, 1)
                   if rankable else None)
        # the payer's CLAIM
        claim = ("negotiates" if (pct_neg or 0) >= 60
                 else "standard fee schedule" if (pct_fee or 0) >= 60
                 else "mixed" if n else None)
        # the OBSERVATION, only when there is enough to observe
        thin = rankable < min_codes
        observed = (None if thin else
                    "prices practices differently" if (pct_var or 0) >= 25
                    else "one rate for everyone")
        # do they agree? disagreement is a finding, not a thing to resolve
        conflict = bool(
            observed and claim and (
                (claim == "standard fee schedule" and observed == "prices practices differently")
                or (claim == "negotiates" and observed == "one rate for everyone")))
        if thin:
            verdict = f"{claim or 'unstated'} (claimed; too few shared codes to check)"
        elif conflict and claim == "standard fee schedule":
            verdict = ("calls it a fee schedule, but pays practices differently "
                       "— it does make exceptions")
        elif conflict:
            verdict = ("calls it negotiated, but publishes one rate for everyone "
                       "— no evidence of deals here")
        elif observed == "prices practices differently":
            verdict = "negotiates — practices are priced differently"
        elif observed == "one rate for everyone":
            verdict = "one schedule for everyone — expect little movement"
        else:
            verdict = claim or "unstated"
        out.append({
            "payer": r["payer"], "n_rows": n, "n_codes": r["n_codes"],
            "pct_negotiated": pct_neg, "pct_fee_schedule": pct_fee,
            "claimed": claim,
            "codes_rankable": rankable, "codes_varying": r["n_codes_varying"],
            "pct_codes_varying": pct_var,
            "observed": observed, "thin": thin, "conflict": conflict,
            "verdict": verdict,
            # the practical read: is a negotiation here worth opening?
            "winnable": (None if thin or not observed
                         else observed == "prices practices differently"),
        })

    winnable = [p for p in out if p["winnable"]]
    return {
        "payers": out, "count": len(out), "min_codes": min_codes,
        "n_winnable": len(winnable),
        "headline": (
            f"{len(winnable)} of {len(out)} payer(s) demonstrably price "
            "practices differently — those are the negotiations with room. "
            + (f"{', '.join(p['payer'] for p in winnable[:4])}."
               if winnable else "")
            if out else "No rates match this market."),
        "note": POSTURE_NOTE,
    }
