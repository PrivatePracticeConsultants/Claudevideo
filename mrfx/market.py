"""Market-intelligence analytics for a single billing code (the Markets tab).

Four consulting views over the same code + market scope, all on the house basis
(dollar, base-modifier rows, per-TIN-then-across-TIN medians, an explicit as-of
month) so every number lines up with the benchmark and rate-card tabs:

- geographic_rates    (H3): where the code pays best, state by state
- negotiability       (H1): which payers actually negotiate — the rate SPREAD
                            across practices, so an underpaid one has a case
- medicare_index      (H2): the market's and each payer's rate as % of Medicare
- assistant_pos_diff  (M2): does a payer cut for PTA/OTA assistants (CQ/CO) or
                            for telehealth place-of-service — a therapy-niche
                            question no generic rate tool answers

Every view suppresses thin cells (< _MIN_CELL backing practices) so one or two
contracts can't masquerade as "the market", and carries the honesty rails the
rest of the app uses: NPPES geography is provider location (not the rate's), a
published rate is not proof of collection, and absence in an MRF is not proof of
non-coverage.
"""

from __future__ import annotations

from .benchmark import (BASIS_NOTE, BenchmarkError, _market_where,
                        _rates_relation, month_label, normalize_market)
from .catalog import code_info
from .store import Store

# a payer/state must back a statistic with at least this many DISTINCT practices,
# else a "market median" is really one or two contracts wearing a market's label
_MIN_CELL = 5

_GEO_NOTE = ("Geography is the provider's NPPES practice-location state, not the "
             "geography the contract's rate legally applies to; a multi-state "
             "billing group contributes its rate to every state it operates in. "
             "Coverage is only as complete as NPPES enrichment — a partial state "
             "is still being identified, never truly zero.")


def _dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _code_scope(code, market):
    """Normalize (code, market) into (code, market, where, params, relation),
    scoping the market to this single code so the billing_code clustering prunes
    the scan at book scale."""
    code = str(code or "").strip()
    if not code:
        raise BenchmarkError("pick a billing code")
    m = normalize_market({**(market or {}), "codes": [code]})
    where, params = _market_where(m, bool(m.get("include_assistant")),
                                  bool(m.get("include_non_dollar")))
    return code, m, where, params, _rates_relation(m)


# ---------------------------------------------------------------------------
# H3 — geographic rate map
# ---------------------------------------------------------------------------

def geographic_rates(store: Store, code: str, market: dict, limit: int = 60) -> dict:
    """Median negotiated rate for one code, ranked across states — territory
    intelligence for prospecting and for advising a multi-site client."""
    code, m, where, params, rel = _code_scope(code, market)
    sql = f"""
    WITH base AS (
        SELECT t.tin_value, any_value(td.states) AS states,
               median(t.negotiated_rate) AS rate
        FROM {rel} t JOIN tin_directory td USING (tin_value)
        WHERE {where} AND td.states IS NOT NULL AND len(td.states) > 0
        GROUP BY t.tin_value
    ),
    exploded AS (SELECT unnest(states) AS state, tin_value, rate FROM base)
    SELECT state,
           count(DISTINCT tin_value)            AS n_practices,
           round(median(rate), 2)               AS median_rate,
           round(quantile_cont(rate, .25), 2)   AS p25,
           round(quantile_cont(rate, .75), 2)   AS p75
    FROM exploded
    GROUP BY state
    HAVING count(DISTINCT tin_value) >= ?
    ORDER BY median_rate DESC
    LIMIT ?
    """
    with store.connect() as con:
        rows = _dicts(con.execute(sql, [*params, _MIN_CELL, limit]))
        # one national reference median (same per-TIN grain, ignoring geography)
        nat = con.execute(
            f"""WITH base AS (SELECT t.tin_value, median(t.negotiated_rate) AS rate
                    FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
                    WHERE {where} GROUP BY t.tin_value)
                SELECT round(median(rate), 2), count(*) FROM base""", params).fetchone()
    return {
        "code": code, "description": code_info(code)[0],
        "as_of": month_label(m["month"]), "min_practices": _MIN_CELL,
        "national_median": nat[0], "national_practices": nat[1],
        "states": rows,
        "basis_note": BASIS_NOTE, "geo_note": _GEO_NOTE,
    }


# ---------------------------------------------------------------------------
# H1 — payer negotiability (within-payer rate dispersion)
# ---------------------------------------------------------------------------

def negotiability(store: Store, code: str, market: dict) -> dict:
    """Per payer, the SPREAD of a code's rate across practices. A tight spread =
    a fixed fee schedule (little to negotiate); a wide spread = the payer clearly
    negotiates, so a low-paid practice has room. `spread_pct` is (p90−p10) as a
    percent of the payer's median; payers ranked most-negotiable first."""
    code, m, where, params, rel = _code_scope(code, market)
    sql = f"""
    WITH base AS (
        SELECT t.payer, t.tin_value, median(t.negotiated_rate) AS rate
        FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where}
        GROUP BY t.payer, t.tin_value
    )
    SELECT payer,
           count(DISTINCT tin_value)            AS n_practices,
           round(quantile_cont(rate, .10), 2)   AS p10,
           round(quantile_cont(rate, .50), 2)   AS p50,
           round(quantile_cont(rate, .90), 2)   AS p90,
           round(min(rate), 2)                  AS min_rate,
           round(max(rate), 2)                  AS max_rate
    FROM base GROUP BY payer
    HAVING count(DISTINCT tin_value) >= ?
    ORDER BY (quantile_cont(rate, .90) - quantile_cont(rate, .10)) DESC, payer
    """
    with store.connect() as con:
        rows = _dicts(con.execute(sql, [*params, _MIN_CELL]))
    for r in rows:
        p10, p50, p90 = r["p10"], r["p50"], r["p90"]
        r["spread_pct"] = round(100 * (p90 - p10) / p50) if p50 else None
        r["spread_ratio"] = round(p90 / p10, 2) if p10 else None
        sp = r["spread_pct"]
        r["negotiability"] = (
            "wide" if sp is not None and sp >= 25 else
            "moderate" if sp is not None and sp >= 10 else "tight")
    return {
        "code": code, "description": code_info(code)[0],
        "as_of": month_label(m["month"]), "min_practices": _MIN_CELL,
        "payers": rows, "basis_note": BASIS_NOTE,
    }


# ---------------------------------------------------------------------------
# H2 — % of Medicare (market + per payer)
# ---------------------------------------------------------------------------

def medicare_index(store: Store, code: str, market: dict) -> dict:
    """The market's and each payer's median rate for one code, as a percent of
    Medicare (the median non-facility MPFS rate across loaded localities). The
    domain's headline anchor. Absent an MPFS load, returns the dollar rates with
    pct_medicare = null and mpfs_loaded = null."""
    code, m, where, params, rel = _code_scope(code, market)
    per_payer = f"""
    WITH base AS (
        SELECT t.payer, t.tin_value, median(t.negotiated_rate) AS rate
        FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where}
        GROUP BY t.payer, t.tin_value
    )
    SELECT payer, count(DISTINCT tin_value) AS n_practices,
           round(median(rate), 2) AS median_rate
    FROM base GROUP BY payer
    HAVING count(DISTINCT tin_value) >= ?
    ORDER BY median_rate DESC
    """
    market_sql = f"""
    WITH base AS (SELECT t.tin_value, median(t.negotiated_rate) AS rate
        FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where} GROUP BY t.tin_value)
    SELECT round(median(rate), 2), round(quantile_cont(rate, .25), 2),
           round(quantile_cont(rate, .75), 2), count(*) FROM base
    """
    with store.connect() as con:
        rows = _dicts(con.execute(per_payer, [*params, _MIN_CELL]))
        mkt = con.execute(market_sql, params).fetchone()
        # median across whatever localities were loaded — a single national
        # anchor, NOT locality/GPCI-matched to the practice (stated in the note)
        base = con.execute(
            "SELECT median(non_facility_rate) FROM mpfs WHERE code = ?", [code]).fetchone()[0]

    def pct(v):
        return round(100 * v / base) if base and v else None

    for r in rows:
        r["pct_medicare"] = pct(r["median_rate"])
    return {
        "code": code, "description": code_info(code)[0],
        "as_of": month_label(m["month"]), "min_practices": _MIN_CELL,
        "mpfs_rate": round(base, 2) if base else None,
        "mpfs_loaded": store.mpfs_loaded(),
        "market_median": mkt[0], "market_p25": mkt[1], "market_p75": mkt[2],
        # surface the backing count so a "market median" from 1-4 practices reads
        # as thin, not authoritative (the per-payer rows already gate on _MIN_CELL)
        "market_practices": mkt[3],
        "market_pct_medicare": pct(mkt[0]),
        "market_p25_pct_medicare": pct(mkt[1]),
        "market_p75_pct_medicare": pct(mkt[2]),
        "payers": rows, "basis_note": BASIS_NOTE,
        "medicare_note": (
            "% of Medicare uses the median non-facility MPFS rate across the "
            "localities you loaded — one national anchor applied to every rate, "
            "NOT matched to each practice's GPCI locality. Load a single-locality "
            "MPFS file to anchor to that region."),
    }


# ---------------------------------------------------------------------------
# M2 — assistant-modifier (CQ/CO) and telehealth-POS differentials
# ---------------------------------------------------------------------------

def assistant_pos_diff(store: Store, code: str, market: dict) -> dict:
    """Per payer, does the rate drop when an assistant modifier (CQ/CO, the 85%
    rule) or a telehealth place-of-service is attached — compared WITHIN the same
    payer×practice so it's a real differential, not a cross-payer mix. A payer
    that simply doesn't publish an assistant/telehealth line shows n_pairs = 0
    ('not published') rather than a fabricated parity."""
    code = str(code or "").strip()
    if not code:
        raise BenchmarkError("pick a billing code")
    # include assistant rows (they're excluded by the default basis); keep
    # base_only so KX/59/X{EPSU} noise stays out but GP/GO/GN/CQ/CO are in.
    m = normalize_market({**(market or {}), "codes": [code], "include_assistant": True})
    where, params = _market_where(m, True, bool(m.get("include_non_dollar")))
    rel = _rates_relation(m)

    # Each differential pairs LIKE WITH LIKE within the same payer×practice at
    # office place-of-service (POS 11): office-base (no CQ/CO) is the reference;
    # office-assistant (CQ/CO) and telehealth-base are compared against it.
    # `tele` requires a telehealth POS AND no office POS, so a line that lists
    # both (e.g. "02|11") counts as office only and never double-feeds both
    # buckets. Lines with NO POS at all match neither and are excluded — the
    # differential only covers POS-tagged rows (stated in the note).
    office = "('|'||service_code_set||'|') LIKE '%|11|%'"
    tele = ("((('|'||service_code_set||'|') LIKE '%|02|%' "
            "OR ('|'||service_code_set||'|') LIKE '%|10|%') "
            "AND ('|'||service_code_set||'|') NOT LIKE '%|11|%')")
    base = "modifier_set NOT LIKE '%CQ%' AND modifier_set NOT LIKE '%CO%'"
    asst = "(modifier_set LIKE '%CQ%' OR modifier_set LIKE '%CO%')"
    sql = f"""
    WITH lines AS (
        SELECT t.payer, t.tin_value, t.modifier_set, t.service_code_set,
               median(t.negotiated_rate) AS rate
        FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where}
        GROUP BY t.payer, t.tin_value, t.modifier_set, t.service_code_set
    ),
    per_practice AS (
        SELECT payer, tin_value,
            median(rate) FILTER (WHERE {office} AND {base}) AS office_base,
            median(rate) FILTER (WHERE {office} AND {asst}) AS office_asst,
            median(rate) FILTER (WHERE {tele} AND {base})   AS tele_rate
        FROM lines GROUP BY payer, tin_value
    )
    SELECT payer,
        count(*) FILTER (WHERE office_base IS NOT NULL AND office_asst IS NOT NULL) AS asst_pairs,
        round(median(office_base) FILTER (WHERE office_base IS NOT NULL AND office_asst IS NOT NULL), 2) AS asst_base_med,
        round(median(office_asst) FILTER (WHERE office_base IS NOT NULL AND office_asst IS NOT NULL), 2) AS asst_med,
        round(100 * median(office_asst / office_base) FILTER (
            WHERE office_base > 0 AND office_asst IS NOT NULL), 0)                   AS asst_pct_of_base,
        count(*) FILTER (WHERE office_base IS NOT NULL AND tele_rate IS NOT NULL)   AS tele_pairs,
        round(median(office_base) FILTER (WHERE office_base IS NOT NULL AND tele_rate IS NOT NULL), 2) AS office_med,
        round(median(tele_rate) FILTER (WHERE office_base IS NOT NULL AND tele_rate IS NOT NULL), 2)   AS tele_med,
        round(100 * median(tele_rate / office_base) FILTER (
            WHERE office_base > 0 AND tele_rate IS NOT NULL), 0)                     AS tele_pct_of_office
    FROM per_practice
    GROUP BY payer
    HAVING count(*) FILTER (WHERE office_base IS NOT NULL AND office_asst IS NOT NULL) >= 1
        OR count(*) FILTER (WHERE office_base IS NOT NULL AND tele_rate IS NOT NULL) >= 1
    ORDER BY asst_pct_of_base NULLS LAST, tele_pct_of_office NULLS LAST, payer
    """
    with store.connect() as con:
        rows = _dicts(con.execute(sql, params))
    return {
        "code": code, "description": code_info(code)[0],
        "as_of": month_label(m["month"]),
        "payers": rows,
        "basis_note": BASIS_NOTE + " Assistant (CQ/CO) rows are INCLUDED here by design.",
        "diff_note": (
            "Each % is the median per-practice ratio (assistant vs base, "
            "telehealth vs office) within the SAME payer and practice; the two "
            "medians shown are references, not that percentage's exact numerator/"
            "denominator. Only office-POS (11) rows anchor the base, so a payer "
            "that publishes no POS-tagged line shows 0 pairs — reported as 'not "
            "published', never as parity. Telehealth lines are sparse; treat "
            "small pair counts as directional."),
    }
