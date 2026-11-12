"""Outreach export: one row per entity, keyed by org name + geography, shaped
for contact-list cross-referencing and cold-email mail merge (e.g. Brevo).

Columns are Brevo-attribute-friendly (UPPER_SNAKE, letter-first). Geography
(city / state / zip / address / phone) comes from the NPPES enrichment of each
entity's NPIs — the join keys for matching against a user's own contact list.
WEBSITE carries a website you verified for any of the org's tax IDs (neither
MRFs nor NPPES publish one); WEBSITE_LOOKUP is a search link to find one.

Per-code merge fields let a template say "your 97110 sits at the 32nd
percentile in this market": C{code}_RATE (entity median), C{code}_MKT_MEDIAN
(the median of the OTHER organizations), C{code}_PCTL (mid-rank among the
other organizations), C{code}_GAP_TO_MEDIAN.
"""

from __future__ import annotations

import bisect

import csv
import io
import logging

from . import progress
from .store import Store, mask_tin
from .rounding import half_up as _half_up

log = logging.getLogger(__name__)

MAX_CODE_COLUMNS = 10


class _Rounder:
    """half_up, remembered per value: fee schedules repeat a handful of rates
    across thousands of practices, and the whole-store export called the
    Decimal-based half_up 11,461,266 times (40 s of a 128 s profile — review 13
    item 3.3). The SAME function decides every value; only repeats are looked
    up, so the bytes are unchanged."""

    def __init__(self):
        self._memo: dict = {}

    def __call__(self, x, nd=None):
        k = (x, nd)
        r = self._memo.get(k)
        if r is None:
            r = self._memo[k] = _half_up(x, nd) if nd is not None else _half_up(x)
        return r


half_up = _half_up

BASE_HEADERS = [
    "ORG_NAME", "ENTITY_ID", "TINS", "NPI_COUNT", "ENTITY_KIND",
    "PRIMARY_DISCIPLINE", "CITY", "STATE", "ZIP", "ADDRESS", "PHONE",
    # WEBSITE = the URL you hand-verified and saved (blank until you do).
    # WEBSITE_LOOKUP = a ready-made SEARCH link to find/verify one — a starting
    # point, NOT a claimed official site (NPPES publishes no website field).
    "WEBSITE", "WEBSITE_LOOKUP", "PAYERS", "MONTHS",
]


def _lookup_url(name, city, state):
    from urllib.parse import quote_plus
    if not name:
        return ""
    terms = " ".join(t for t in (f'"{name}"', city or "", state or "",
                                 "physical therapy") if t).strip()
    return "https://www.google.com/search?q=" + quote_plus(terms)


def _geo_by_tin(store: Store, scope_tins: list[str]) -> dict[str, dict]:
    """TIN -> most common practice location among its NPIs (NPPES-enriched),
    SCOPED to the export's in-filter TINs — the whole-store version built one
    Python dict entry per TIN (millions at 300M rows → OOM)."""
    if not scope_tins:
        return {}
    import pyarrow as pa
    scope = pa.table({"tin_value": pa.array(scope_tins, type=pa.string())})
    pairs = store.pairs_relation()      # the kept (TIN, NPI) pairs, not a raw scan
    with store.connect() as con:
        con.register("_scope", scope)
        try:
            rows = con.execute(
                f"""
                WITH tin_npi AS (
                    SELECT DISTINCT r.tin_value, r.npi FROM {pairs} r JOIN _scope s USING (tin_value)
                    WHERE r.tin_value IS NOT NULL AND NOT r.tin_is_really_npi
                    UNION
                    -- tin.type='npi' rows: the "TIN" IS an NPI — locate through it
                    SELECT DISTINCT r.tin_value, r.tin_value AS npi FROM {pairs} r JOIN _scope s USING (tin_value)
                    WHERE r.tin_value IS NOT NULL AND r.tin_is_really_npi
                )
                SELECT tin_value,
                       mode(n.city ORDER BY n.city)    FILTER (n.city IS NOT NULL)    AS city,
                       mode(n.state ORDER BY n.state)   FILTER (n.state IS NOT NULL)   AS state,
                       mode(n.zip ORDER BY n.zip)     FILTER (n.zip IS NOT NULL)     AS zip,
                       mode(n.address ORDER BY n.address) FILTER (n.address IS NOT NULL) AS address,
                       mode(n.phone ORDER BY n.phone)   FILTER (n.phone IS NOT NULL)   AS phone
                FROM tin_npi JOIN npi_directory n USING (npi)
                GROUP BY tin_value
                """
            ).fetchall()
        finally:
            con.unregister("_scope")
    return {r[0]: {"city": r[1], "state": r[2], "zip": r[3], "address": r[4], "phone": r[5]} for r in rows}


def _tin_meta(store: Store, scope_tins: list[str]) -> dict[str, dict]:
    if not scope_tins:
        return {}
    import pyarrow as pa
    scope = pa.table({"tin_value": pa.array(scope_tins, type=pa.string())})
    with store.connect() as con:
        con.register("_scope", scope)
        try:
            rows = con.execute(
                "SELECT td.tin_value, td.entity_kind, td.primary_discipline "
                "FROM tin_directory td JOIN _scope s USING (tin_value)"
            ).fetchall()
        finally:
            con.unregister("_scope")
    return {r[0]: {"entity_kind": r[1], "primary_discipline": r[2]} for r in rows}


OUTREACH_COLUMNS_NOTE = (
    "Per-code columns (C<code>_*), on the house basis — the as-of month (latest "
    "by default), dollar rates, base modifiers (no CQ/CO, KX, 59...), "
    "professional class, $0/$0.01 placeholders excluded — the same basis as "
    "Benchmark, Leads and Markets:\n"
    "  C<code>_RATE: the organization's rate = median across its tax IDs of each "
    "tax ID's median rate.\n"
    "  C<code>_MKT_MEDIAN: the median of that rate over the OTHER organizations in "
    "this export priced on the code (Benchmark's and Leads' market median).\n"
    "  C<code>_PCTL: the organization's mid-rank among the OTHER organizations "
    "in this export (those below, plus half of those paid the same) — paid the "
    "same as everyone = 50; blank when no other organization is priced on the code.\n"
    "  C<code>_GAP_TO_MEDIAN: MKT_MEDIAN minus RATE (positive = paid under the median).\n"
    "Rounded half-up to the cent, like every number the app prints.")


def build_outreach_rows(store: Store, rel_sql: str, params: list,
                        codes: list[str] | None = None,
                        market: dict | None = None) -> tuple[list[str], list[dict]]:
    """`rel_sql` is a filtered grain relation (entity or tin) from the API's
    shared filter machinery: it chooses WHICH organizations are listed (the
    dashboard's rows). The per-code numbers are on the house basis
    (`market`, default the latest month) through practice_base_sql — the
    same per-practice medians Benchmark and Leads read. They used to pool
    every month, modifier and class of the dashboard rows, and rank
    at-or-below: one practice read $42 / p0 on Benchmark and $41 / PCTL 20
    in its mail merge; a flat code put everyone at PCTL 100 while three tabs
    said p50 (audit 8 P1-6)."""
    from .benchmark import normalize_market, practice_base_sql
    half_up = _Rounder()
    prog = progress.current()
    prog.stage("Listing the organizations in this view")
    with store.connect() as con:
        units = con.execute(
            f"""
            SELECT unit_id,
                   min(display_name)                             AS display_name,
                   string_agg(DISTINCT tin_value, '; ' ORDER BY tin_value) AS tins,
                   max(npi_count)                                AS npi_count,
                   string_agg(DISTINCT payer, '; ' ORDER BY payer)  AS payers,
                   string_agg(DISTINCT file_month, '; ' ORDER BY file_month) AS months
            FROM ({rel_sql}) WHERE is_dollar_rate AND negotiated_rate > 0.01
            GROUP BY unit_id
            ORDER BY display_name, unit_id
            """,
            params,
        ).fetchall()

    # only the TINs actually in this export's filtered result — geo/meta used to
    # scan the WHOLE store (one Python dict entry per TIN → OOM at 300M rows).
    # units[2] is the per-unit `string_agg(DISTINCT tin_value)`.
    tins_of = {u[0]: sorted({t.strip() for t in (u[2] or "").split(";") if t.strip()})
               for u in units}
    scope_tins = sorted({t for ts in tins_of.values() for t in ts})
    prog.stage("Looking up addresses and contacts")
    geo = _geo_by_tin(store, scope_tins)
    meta = _tin_meta(store, scope_tins)
    prog.stage("One rate per organization and code")

    # per-code entity rates on the house basis: per-TIN medians (the
    # materialised practice medians when the basis allows), then the median
    # across an organization's TINs — IN SQL, for the wanted codes only. The
    # (TIN, code) pairs used to come back to Python as a dict (4.85M entries
    # for a no-filter TIN-grain export, RSS near 6 GB) with all 194,000 TINs
    # passed as one parameter (review 9 item 4.6)
    m = normalize_market(market or {"month": "latest"})
    by_code: dict[str, dict[str, float]] = {}
    n_codes_total = 0
    wanted: list[str] = []
    if tins_of:
        import uuid
        tag = uuid.uuid4().hex[:10]
        tu, tr = f"_outreach_units_{tag}", f"_outreach_rates_{tag}"
        q, qp = practice_base_sql(
            store, m, extra=f" AND t.tin_value IN (SELECT tin_value FROM {tu})")
        with store.connect() as con:
            con.execute(
                f"CREATE OR REPLACE TEMP TABLE {tu} AS "
                f"SELECT DISTINCT unit_id, tin_value FROM ({rel_sql}) "
                "WHERE is_dollar_rate AND negotiated_rate > 0.01", params)
            con.execute(
                f"CREATE OR REPLACE TEMP TABLE {tr} AS "
                "SELECT u.unit_id, p.billing_code, median(p.rate) AS rate "
                f"FROM ({q}) p JOIN {tu} u USING (tin_value) "
                "GROUP BY u.unit_id, p.billing_code", qp)
            coverage = con.execute(
                f"SELECT billing_code, count(*) AS n FROM {tr} "
                "GROUP BY billing_code ORDER BY n DESC, billing_code").fetchall()
            n_codes_total = len(coverage)
            have = {c for c, _n in coverage}
            if codes:
                wanted = [c for c in codes if c in have]
            else:
                # most-covered codes, ties to the lower code (a stable cut)
                wanted = sorted(c for c, _n in coverage[:MAX_CODE_COLUMNS])
            if wanted:
                for unit_id, code, rate in con.execute(
                        f"SELECT unit_id, billing_code, rate FROM {tr} "
                        "WHERE billing_code IN (SELECT unnest(?::VARCHAR[])) "
                        "ORDER BY billing_code, unit_id", [wanted]).fetchall():
                    by_code.setdefault(code, {})[unit_id] = rate
            con.execute(f"DROP TABLE IF EXISTS {tr}")
            con.execute(f"DROP TABLE IF EXISTS {tu}")
    if not codes and n_codes_total > MAX_CODE_COLUMNS:
        log.info("outreach export: limiting to the %d most-covered codes of %d "
                 "(pass explicit codes to choose)", MAX_CODE_COLUMNS, n_codes_total)

    # each practice's market median is the median of the OTHER practices
    # (leads.median_of_others — Benchmark's and Leads' rule; review 10 item
    # 3.9: one median for the column included the practice itself)
    from .leads import median_of_others
    raw_sorted = {c: sorted(by_code.get(c, {}).values()) for c in wanted}
    # each code's rates (to the cent, as leads.MIDRANK_PCT compares them)
    # sorted ONCE: the mid-rank is two binary searches. Counting per practice
    # per code was quadratic — one state's export (5,700 practices) spent
    # 11.6 s in 244M generator steps.
    sorted_vals = {c: sorted(half_up(v, 2) for v in by_code.get(c, {}).values())
                   for c in wanted}
    verified_sites = store.org_websites()

    headers = list(BASE_HEADERS)
    for c in wanted:
        headers += [f"C{c}_RATE", f"C{c}_MKT_MEDIAN", f"C{c}_PCTL", f"C{c}_GAP_TO_MEDIAN"]

    prog.stage("Writing the CSV and methodology")
    out_rows: list[dict] = []
    n_units = max(1, len(units))
    for i_unit, (unit_id, display_name, tins, npi_count, payers, months) in enumerate(units):
        if i_unit % 5000 == 0:
            prog.tick(i_unit / n_units)
        # entity-grain rows already carry aggregated 'tin1; tin2' strings whose
        # order varies — split, dedupe, and sort for a stable output
        tin_list = sorted({t.strip() for t in (tins or "").split(";") if t.strip()})
        g = next((geo[t] for t in tin_list if t in geo and geo[t].get("state")), None) \
            or next((geo[t] for t in tin_list if t in geo), {}) or {}
        m = next((meta[t] for t in tin_list if t in meta), {}) or {}
        row = {
            "ORG_NAME": display_name or "",
            "ENTITY_ID": mask_tin(unit_id) or "",
            "TINS": "; ".join(mask_tin(t) or "" for t in tin_list),
            "NPI_COUNT": npi_count or 0,
            "ENTITY_KIND": m.get("entity_kind") or "",
            "PRIMARY_DISCIPLINE": m.get("primary_discipline") or "",
            "CITY": g.get("city") or "",
            "STATE": g.get("state") or "",
            "ZIP": g.get("zip") or "",
            "ADDRESS": g.get("address") or "",
            "PHONE": g.get("phone") or "",
            # verified URL if you saved one for any of this org's tax ids;
            # otherwise a search link to go find + verify one
            "WEBSITE": next((verified_sites[t] for t in tin_list if t in verified_sites), ""),
            "WEBSITE_LOOKUP": _lookup_url(display_name, g.get("city"), g.get("state")),
            "PAYERS": payers or "",
            "MONTHS": months or "",
        }
        for c in wanted:
            rate = by_code.get(c, {}).get(unit_id)
            vals = sorted_vals[c]
            if rate is None or not vals:
                row[f"C{c}_RATE"] = row[f"C{c}_MKT_MEDIAN"] = row[f"C{c}_PCTL"] = row[f"C{c}_GAP_TO_MEDIAN"] = ""
                continue
            r2 = half_up(rate, 2)
            lo, hi = bisect.bisect_left(vals, r2), bisect.bisect_right(vals, r2)
            # mid-rank among the OTHER organizations (leads.MIDRANK_PCT)
            pctl = (half_up(100.0 * (lo + 0.5 * (hi - lo - 1)) / (len(vals) - 1))
                    if len(vals) > 1 else "")
            om = median_of_others(raw_sorted[c], rate)
            med = half_up(om, 2) if om is not None else None
            row[f"C{c}_RATE"] = half_up(rate, 2)
            row[f"C{c}_MKT_MEDIAN"] = med if med is not None else ""
            row[f"C{c}_PCTL"] = pctl
            row[f"C{c}_GAP_TO_MEDIAN"] = half_up(med - rate, 2) if med is not None else ""
        out_rows.append(row)
    return headers, out_rows


def _defuse(v):
    """Excel/Sheets execute cells starting with = + - @ (and tab/CR variants)
    as formulas. Org names and addresses come from third-party MRF/NPPES
    data, and this CSV is built to be opened in Excel/Brevo — prefix risky
    leading characters with a quote so they render as text, never execute."""
    if isinstance(v, str) and v and v[0] in "=+-@\t\r":
        return "'" + v
    return v


def outreach_csv(headers: list[str], rows: list[dict]) -> str:
    # csv.writer over the header order — exactly what DictWriter writes
    # (restval "", extras ignored) without a dict per row (review 13 item 3.3)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows([[_defuse(r.get(h, "")) for h in headers] for r in rows])
    return "﻿" + buf.getvalue()  # BOM for Excel / Brevo import
