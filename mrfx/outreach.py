"""Outreach export: one row per entity, keyed by org name + geography, shaped
for contact-list cross-referencing and cold-email mail merge (e.g. Brevo).

Columns are Brevo-attribute-friendly (UPPER_SNAKE, letter-first). Geography
(city / state / zip / address / phone) comes from the NPPES enrichment of each
entity's NPIs — the join keys for matching against a user's own contact list.
WEBSITE ships as an always-empty column for template consistency: neither MRFs
nor NPPES publish websites, so it is left for the user's own list to fill.

Per-code merge fields let a template say "your 97110 sits at the 32nd
percentile in this market": C{code}_RATE (entity median), C{code}_MKT_MEDIAN,
C{code}_PCTL (share of entities at or below), C{code}_GAP_TO_MEDIAN.
"""

from __future__ import annotations

import csv
import io
import logging

from .store import Store, mask_tin

log = logging.getLogger(__name__)

MAX_CODE_COLUMNS = 10

BASE_HEADERS = [
    "ORG_NAME", "ENTITY_ID", "TINS", "NPI_COUNT", "ENTITY_KIND",
    "PRIMARY_DISCIPLINE", "CITY", "STATE", "ZIP", "ADDRESS", "PHONE",
    "WEBSITE", "PAYERS", "MONTHS",
]


def _geo_by_tin(store: Store) -> dict[str, dict]:
    """TIN -> most common practice location among its NPIs (NPPES-enriched)."""
    with store.connect() as con:
        rows = con.execute(
            """
            WITH tin_npi AS (
                SELECT DISTINCT tin_value, npi FROM rates
                WHERE tin_value IS NOT NULL AND NOT tin_is_really_npi
                UNION
                -- tin.type='npi' rows: the "TIN" IS an NPI — locate through it
                SELECT DISTINCT tin_value, tin_value AS npi FROM rates
                WHERE tin_value IS NOT NULL AND tin_is_really_npi
            )
            SELECT tin_value,
                   mode(n.city)    FILTER (n.city IS NOT NULL)    AS city,
                   mode(n.state)   FILTER (n.state IS NOT NULL)   AS state,
                   mode(n.zip)     FILTER (n.zip IS NOT NULL)     AS zip,
                   mode(n.address) FILTER (n.address IS NOT NULL) AS address,
                   mode(n.phone)   FILTER (n.phone IS NOT NULL)   AS phone
            FROM tin_npi JOIN npi_directory n USING (npi)
            GROUP BY tin_value
            """
        ).fetchall()
    return {r[0]: {"city": r[1], "state": r[2], "zip": r[3], "address": r[4], "phone": r[5]} for r in rows}


def _tin_meta(store: Store) -> dict[str, dict]:
    with store.connect() as con:
        rows = con.execute(
            "SELECT tin_value, entity_kind, primary_discipline FROM tin_directory"
        ).fetchall()
    return {r[0]: {"entity_kind": r[1], "primary_discipline": r[2]} for r in rows}


def build_outreach_rows(store: Store, rel_sql: str, params: list,
                        codes: list[str] | None = None) -> tuple[list[str], list[dict]]:
    """`rel_sql` is a filtered grain relation (entity or tin) from the API's
    shared filter machinery — outreach numbers therefore match the dashboard."""
    with store.connect() as con:
        units = con.execute(
            f"""
            SELECT unit_id,
                   any_value(display_name)                       AS display_name,
                   string_agg(DISTINCT tin_value, '; ')          AS tins,
                   max(npi_count)                                AS npi_count,
                   string_agg(DISTINCT payer, '; ' ORDER BY payer)  AS payers,
                   string_agg(DISTINCT file_month, '; ' ORDER BY file_month) AS months
            FROM ({rel_sql}) WHERE is_dollar_rate
            GROUP BY unit_id
            ORDER BY display_name
            """,
            params,
        ).fetchall()
        unit_code = con.execute(
            f"""
            SELECT unit_id, billing_code, median(negotiated_rate) AS rate
            FROM ({rel_sql}) WHERE is_dollar_rate
            GROUP BY unit_id, billing_code
            """,
            params,
        ).fetchall()

    geo = _geo_by_tin(store)
    meta = _tin_meta(store)

    # per-code entity medians -> market stats
    by_code: dict[str, dict[str, float]] = {}
    for unit_id, code, rate in unit_code:
        by_code.setdefault(code, {})[unit_id] = rate
    if codes:
        wanted = [c for c in codes if c in by_code]
    else:
        wanted = sorted(by_code, key=lambda c: -len(by_code[c]))[:MAX_CODE_COLUMNS]
        wanted.sort()
    if not codes and len(by_code) > MAX_CODE_COLUMNS:
        log.info("outreach export: limiting to the %d most-covered codes of %d "
                 "(pass explicit codes to choose)", MAX_CODE_COLUMNS, len(by_code))

    def market_median(code: str) -> float | None:
        vals = sorted(by_code.get(code, {}).values())
        if not vals:
            return None
        mid = len(vals) // 2
        return round((vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2), 2)

    medians = {c: market_median(c) for c in wanted}

    headers = list(BASE_HEADERS)
    for c in wanted:
        headers += [f"C{c}_RATE", f"C{c}_MKT_MEDIAN", f"C{c}_PCTL", f"C{c}_GAP_TO_MEDIAN"]

    out_rows: list[dict] = []
    for unit_id, display_name, tins, npi_count, payers, months in units:
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
            "WEBSITE": "",  # not published in MRF/NPPES data — fill from your own list
            "PAYERS": payers or "",
            "MONTHS": months or "",
        }
        for c in wanted:
            rate = by_code.get(c, {}).get(unit_id)
            vals = list(by_code.get(c, {}).values())
            if rate is None or not vals:
                row[f"C{c}_RATE"] = row[f"C{c}_MKT_MEDIAN"] = row[f"C{c}_PCTL"] = row[f"C{c}_GAP_TO_MEDIAN"] = ""
                continue
            pctl = round(100 * sum(1 for v in vals if v <= rate) / len(vals))
            med = medians[c]
            row[f"C{c}_RATE"] = round(rate, 2)
            row[f"C{c}_MKT_MEDIAN"] = med if med is not None else ""
            row[f"C{c}_PCTL"] = pctl
            row[f"C{c}_GAP_TO_MEDIAN"] = round(med - rate, 2) if med is not None else ""
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
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    w.writeheader()
    w.writerows([{k: _defuse(v) for k, v in r.items()} for r in rows])
    return "﻿" + buf.getvalue()  # BOM for Excel / Brevo import
