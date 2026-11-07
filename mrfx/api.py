"""FastAPI JSON API over the DuckDB store + static dashboard hosting (V4).

Every list endpoint filters/sorts/paginates SERVER-SIDE (§8.3); CSV export
reuses the exact same WHERE/ORDER SQL as the view that produced it (§8.7) and
ships with a methodology sidecar (§7A.6). The default analytical grain is
(billing_code × TIN); entity and NPI grains are explicit toggles.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .benchmark import (
    BenchmarkError,
    clean_volumes,
    compute_benchmark,
    compute_opportunity,
    compute_payer_comparison,
    compute_payer_negotiation,
    contract_gaps,
    render_negotiation_report,
    render_payer_compare_report,
    render_pitch_report,
)
from .catalog import (THERAPY_MIN_SHARE_PCT, THERAPY_SMALL_PRACTICE_NPIS,
                      catalog_json, therapy_taxonomy_sql)
from .config import MrfxConfig
from .enrich import use_bulk_enrichment
from .leads import (compute_leaderboard, compute_leads, compute_payer_roster,
                    leads_csv, payer_roster_csv)
from .market import (
    assistant_pos_diff,
    geographic_rates,
    medicare_index,
    negotiability,
)
from .modifiers import BASE_RULE_TEXT, MODIFIER_INGEST_NOTE, base_sql
from .monitor import compute_payer_trajectory, compute_rate_changes, rate_changes_csv
from .schedule import (
    compute_fee_schedule,
    fee_schedule_csv,
    payer_scorecard,
    render_rate_card,
)
from .entities import sync_entity_map, update_entity
from .ingest import ingest_file, scan_inbox, thread_stacks_text
from .registry import Registry
from .store import Store, mask_tin, sql_path

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


class NoCacheStaticFiles(StaticFiles):
    """Serve the dashboard with `Cache-Control: no-cache`.

    Starlette's StaticFiles sends only ETag/Last-Modified, so a browser is free
    to *heuristically* cache app.js/index.html and skip revalidation. When the
    user drops in a new build (replaces the `mrfx` folder), the browser can keep
    running the OLD app.js against the NEW markup — which showed up as the
    "As-of month" dropdown rendering blank after an update. `no-cache` (NOT
    no-store) forces a conditional revalidation on every load: unchanged files
    still 304 cheaply, but an updated file is always re-fetched, so a build swap
    takes effect on the next page load without a manual hard-refresh.
    """

    def file_response(self, *args, **kwargs):  # noqa: ANN002, ANN003
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

SORTABLE = {
    "display_name", "unit_id", "payer", "billing_code", "discipline", "modifier_set",
    "billing_class", "negotiated_rate", "negotiated_type", "source_count",
    "npi_count", "tin_count", "rate_variants", "file_month", "last_updated_on",
}

UPLOAD_LIMIT_BYTES = 1 << 30  # browser uploads capped at 1 GB (§5.4)

OUTLIER_RULE = "hide rows >5x or <0.2x of the code's median within the current filter"

_SSN_PREFIXES = ("('00','07','08','09','17','18','19','28','29','49',"
                 "'69','70','78','79','89','96','97')")

# the no-name fallback label must never surface a raw SSN-pattern TIN — it is
# rendered as display_name in the UI, CSV exports, and outreach ORG_NAME
_TIN_LABEL_SQL = (
    "'TIN ' || CASE WHEN regexp_full_match(t.tin_value, '[0-9]{9}') "
    f"AND substr(t.tin_value, 1, 2) IN {_SSN_PREFIXES} "
    "THEN 'MASKED-SSN' ELSE t.tin_value END"
)

# ---------------------------------------------------------------------------
# grain relations — uniform column set across entity / tin / npi
# ---------------------------------------------------------------------------

# therapy-provider tests for the different NPI columns each grain exposes
_THERAPY_NN = therapy_taxonomy_sql("nn.taxonomy_code")   # NPI-in-TIN-slot row
_THERAPY_N = therapy_taxonomy_sql("n.taxonomy_code")     # NPI grain

# The name/geo columns come from three LEFT JOINs (directory, entity map, NPI
# directory). Split into projection + joins so the table endpoint can attach them
# AFTER sorting+limiting the base rows to one page — joining 100 rows instead of
# the whole store (see api.rates()'s late-join path). `t` is the base relation.
# The PRACTICE-level columns: each reads only t.tin_value / t.tin_is_really_npi
# and the three dimension tables, so it is constant across a practice's rows.
# One definition, used by the full projection and by the unit lookup that
# filters on them (_tin_rows_by_unit) — the two must never disagree.
_UNIT_COLS = {
    "display_name": f"coalesce(em.entity_name, td.display_name, nn.org_name, {_TIN_LABEL_SQL})",
    "states": "coalesce(td.states, CASE WHEN nn.state IS NULL THEN [] ELSE [nn.state] END)",
    "cities": "coalesce(td.cities, CASE WHEN nn.city IS NULL THEN [] ELSE [nn.city] END)",
    # PT/OT/SLP practice? from tin_directory (EIN TINs) or, for an NPI-in-the-
    # TIN-slot row, that NPI's own taxonomy.
    "is_therapy": f"coalesce(td.is_therapy, {_THERAPY_NN}, FALSE)",
}
_TIN_PROJECTION = f"""
    {_UNIT_COLS["display_name"]} AS display_name,
    t.tin_value AS unit_id, t.tin_value,
    em.entity_name IS NOT NULL AS is_mapped_entity,
    t.tin_is_really_npi,
    1 AS tin_count, t.npi_count, t.rate_variants, t.rate_min, t.rate_max,
    t.payer, t.billing_code, t.billing_code_type, t.discipline, t.is_timed,
    t.modifier_set, t.billing_class, t.service_code_set, t.file_month,
    t.negotiated_rate, t.negotiated_type, t.is_dollar_rate,
    t.source_count, t.source_files, t.schema_version, t.last_updated_on,
    {_UNIT_COLS["states"]} AS states,
    {_UNIT_COLS["cities"]} AS cities,
    {_UNIT_COLS["is_therapy"]} AS is_therapy,
    -- for the entity key only (see _ENTITY_KEY_SQL): is the display name an
    -- ORGANIZATION's (an NPI-2), and the practice's main state
    coalesce(td.entity_kind = 'org', nn.entity_type = 'NPI-2', FALSE) AS name_is_org,
    coalesce(td.primary_state, nn.state) AS primary_state
"""
_TIN_JOINS = """
    LEFT JOIN tin_directory td USING (tin_value)
    LEFT JOIN entity_map em USING (tin_value)
    -- payers that set tin.type='npi' publish an NPI in the TIN slot: name and
    -- locate those rows from the NPI directory instead of leaving raw numbers.
    -- The key is an EXPRESSION equality on purpose: written as
    -- `ON t.tin_is_really_npi AND nn.npi = t.tin_value`, the one-sided
    -- predicate made DuckDB fall back to a nested-loop join (BLOCKWISE_NL_JOIN:
    -- every spine row against every named provider — 1.41M x 93k on the real
    -- store, 529 thread-seconds; the Explorer's table took 158 s and the Code
    -- page 10 s once practice names were loaded, 0.9 s / 0.1 s before). The
    -- CASE yields NULL for an EIN row, which matches nothing — the same rows,
    -- hash-joined.
    LEFT JOIN npi_directory nn ON nn.npi = CASE WHEN t.tin_is_really_npi THEN t.tin_value END
"""
_TIN_REL = f"SELECT {_TIN_PROJECTION} FROM rates_by_tin t {_TIN_JOINS}"


def _tin_rows_by_unit(where_key: str, where_unit: str, latest: bool) -> str:
    """The TIN relation's rows whose contract passes `where_key` and whose
    PRACTICE passes `where_unit` — the same rows as
    `SELECT * FROM (_TIN_REL[_LATEST]) WHERE key AND unit`, found the other way
    round. Joining the three dimension tables onto every spine row and only
    then testing state / city / therapy / a name was most of a filtered
    Explorer view's cost (round 5, P1.4: a state filter 2.9 s and a name
    search 3.9 s at 99M rates). Here the practice columns are computed once
    per (tin_value, tin_is_really_npi) — from _UNIT_COLS, the projection's own
    expressions — the passing practices are found, and only their rows are
    read and joined. Binds the key params, then the unit params (bucket order)."""
    spine = "rates_by_tin WHERE " + ("is_latest AND " if latest else "") + where_key
    unit_cols = ", ".join(f"{sql} AS {name}" for name, sql in _UNIT_COLS.items())
    return f"""
        WITH k AS NOT MATERIALIZED (SELECT * FROM {spine}),
        u AS (
            SELECT tin_value, tin_is_really_npi FROM (
                SELECT t.tin_value, t.tin_value AS unit_id, t.tin_is_really_npi,
                       {unit_cols}
                FROM (SELECT DISTINCT tin_value, tin_is_really_npi FROM k) t {_TIN_JOINS})
            WHERE {where_unit})
        SELECT {_TIN_PROJECTION}
        FROM k t SEMI JOIN u ON u.tin_value = t.tin_value
                            AND u.tin_is_really_npi = t.tin_is_really_npi
        {_TIN_JOINS}"""
# each contract's newest month only: the spine's stored flag (store.SPINE_QUERY)
# — a filter DuckDB applies in the scan, under the joins
_TIN_REL_LATEST = _TIN_REL + " WHERE t.is_latest"

# The entity a TIN rolls up to. A manual entity_map name wins. Otherwise an
# ORGANIZATION name (from an NPI-2) plus the practice's main state: two TINs of
# "GREATER BOSTON WELLNESS PHYSICIANS" in MA are one entity, an unrelated
# "ACTIVE PHYSICAL THERAPY" in KS is another. A TIN with no organization NPI is
# named after one of its clinicians — and that clinician often bills under
# several unrelated TINs (real store: 462 person-names shared by 1,008 TINs),
# so such a TIN is its OWN entity (key = tin_value), as is a no-name TIN.
_ENTITY_KEY_SQL = """coalesce(
    s2.entity_name,
    CASE WHEN NOT s.name_is_org OR s.display_name LIKE 'TIN %' THEN s.tin_value
         ELSE s.display_name || coalesce(' · ' || s.primary_state, '') END)"""


def _entity_keyed(tin_rows: str) -> str:
    """TIN rows (the _TIN_REL column set) + their entity_key."""
    return (f"SELECT s.*, {_ENTITY_KEY_SQL} AS entity_key FROM ({tin_rows}) s "
            "LEFT JOIN entity_map s2 ON s2.tin_value = s.tin_value")


def _resolve_entity_key(con, key: str) -> str:
    """An entity drawer link may carry a bare organization name (a bookmark,
    an older build's link, a hand-typed URL) where the key is now "NAME · ST".
    Exactly one entity of that name -> its key; several -> say which (never
    pick one); none -> the key as given (the caller 404s)."""
    if con.execute(f"SELECT 1 FROM ({_entity_keyed(_TIN_REL)}) WHERE entity_key = ? LIMIT 1",
                   [key]).fetchone():
        return key
    keys = [r[0] for r in con.execute(
        f"SELECT DISTINCT entity_key FROM ({_entity_keyed(_TIN_REL)}) "
        "WHERE display_name = ? AND entity_key <> tin_value ORDER BY 1", [key]).fetchall()]
    if len(keys) > 1:
        raise HTTPException(409, f"several practices are named {key!r}: "
                                 + "; ".join(keys) + " — open the one you mean")
    return keys[0] if keys else key


def _entity_rel(tin_rows: str, latest: bool = False) -> str:
    """Entity grain over the given TIN rows: one row per entity x payer x code x
    modifier set x class x POS set (x month, unless `latest`). Under `latest`
    each member TIN has ALREADY been reduced to its own newest month (rel_sql),
    so the entity rate is the median over every member's current rate — the
    Benchmark's rule. Grouping by month first and then keeping the entity's
    newest month dropped a member whose payer hadn't republished it yet
    (A: July $55, B: June $40 -> $55 from A alone, vs the Benchmark's $47.50)."""
    month_col = "max(file_month) AS file_month" if latest else "file_month"
    month_key = "" if latest else " file_month,"
    # The DISTINCT counts and lists are taken from ONE list() per column
    # (list_distinct + len / list_sort + array_to_string — the same values as
    # count(DISTINCT x) / string_agg(DISTINCT x ORDER BY x), NULLs ignored
    # alike). A DISTINCT aggregate builds its own hash table over every group
    # key plus the value, per thread: four of them over an unfiltered spine
    # (1.4M groups of long string keys) ran the whole-store entity view out
    # of memory once "latest" stopped being a window that happened to feed
    # the aggregate pre-clustered rows (old plan 19 s; this one: see
    # tests/mrfx/test_known_answer_entity.py and the round-4 report).
    return f"""
    SELECT display_name, unit_id,
           CASE WHEN len(tins) > 0 THEN array_to_string(tins, '; ') END AS tin_value,
           is_mapped_entity, tin_is_really_npi,
           len(tins) AS tin_count,
           npi_count,
           -- ONE meaning: how many different member-TIN rates the shown
           -- median is taken over (rate_min/rate_max still span every
           -- published value of every member)
           len(list_distinct(rates)) AS rate_variants,
           rate_min, rate_max,
           payer, billing_code, billing_code_type,
           discipline, is_timed,
           modifier_set, billing_class, service_code_set, file_month,
           negotiated_rate, negotiated_type, is_dollar_rate,
           -- files behind the line: members of one payer usually share ONE
           -- file, so summing per-member counts multiplied it (2 TINs from one
           -- file read "2 files"). Distinct representative files, never less
           -- than any single member's count — exact whenever each member's
           -- line comes from one file (the export methodology says so)
           greatest(max_source_count, len(files)) AS source_count,
           CASE WHEN len(files) > 0 THEN array_to_string(files, ';') END AS source_files,
           schema_version, last_updated_on, states, cities, is_therapy
    FROM (
    SELECT min(display_name) AS display_name,
           entity_key AS unit_id,
           list_sort(list_distinct(list(tin_value))) AS tins,
           bool_or(is_mapped_entity) AS is_mapped_entity,
           bool_or(tin_is_really_npi) AS tin_is_really_npi,
           -- member TINs' NPI counts summed: an NPI listed under two member
           -- TINs counts twice (the rollup keeps per-TIN counts, not the NPIs;
           -- stated in the export methodology)
           sum(npi_count) AS npi_count,
           list(negotiated_rate) AS rates,
           min(rate_min) AS rate_min, max(rate_max) AS rate_max,
           payer, billing_code, min(billing_code_type) AS billing_code_type,
           discipline, min(is_timed) AS is_timed,
           modifier_set, billing_class, service_code_set, {month_col},
           -- Re-median across the entity's member TINs — but a placeholder-ONLY
           -- TIN carries its 0.01 as its per-TIN "median" (store.BY_TIN_QUERY's
           -- coalesce fallback), so folding it in would drag a multi-TIN entity
           -- to a bogus midpoint (median of 0.01 and 80 = 40) and disagree with
           -- the benchmark, which strips placeholders first. Mirror the TIN-level
           -- FILTER exactly: drop placeholder members from a dollar group, but
           -- keep a placeholder-only entity showing its value (coalesce) instead
           -- of NULL. is_dollar_rate is a GROUP BY key, so this no-ops for
           -- non-dollar (percentage) groups.
           round(coalesce(
               median(negotiated_rate) FILTER (NOT is_dollar_rate OR negotiated_rate > 0.01),
               median(negotiated_rate)
           ), 4) AS negotiated_rate,
           min(negotiated_type) AS negotiated_type, is_dollar_rate,
           max(source_count) AS max_source_count,
           list_sort(list_distinct(list(source_files))) AS files,
           min(schema_version) AS schema_version,
           max(last_updated_on) AS last_updated_on,
           list_sort(list_distinct(flatten(list(states)))) AS states,
           list_sort(list_distinct(flatten(list(cities)))) AS cities,
           bool_or(is_therapy) AS is_therapy   -- entity is therapy if any TIN is
    FROM ({_entity_keyed(tin_rows)})
    GROUP BY entity_key, payer, billing_code, discipline, modifier_set,
             billing_class, service_code_set,{month_key} is_dollar_rate
    )
"""


_ENTITY_REL = _entity_rel(_TIN_REL)

_NPI_REL = f"""
    SELECT coalesce(n.org_name, 'NPI ' || d.npi) AS display_name,
           d.npi AS unit_id, d.tin_value,
           FALSE AS is_mapped_entity,
           d.tin_is_really_npi,
           1 AS tin_count, 1 AS npi_count, d.rate_variants,
           d.negotiated_rate AS rate_min, d.negotiated_rate AS rate_max,
           d.payer, d.billing_code, d.billing_code_type, d.discipline, d.is_timed,
           d.modifier_set, d.billing_class, d.service_code_set, d.file_month,
           d.negotiated_rate, d.negotiated_type, d.is_dollar_rate,
           d.source_count, d.source_files, d.schema_version, d.last_updated_on,
           CASE WHEN n.state IS NULL THEN [] ELSE [n.state] END AS states,
           CASE WHEN n.city IS NULL THEN [] ELSE [n.city] END AS cities,
           coalesce({_THERAPY_N}, FALSE) AS is_therapy
    FROM rates_dedup d LEFT JOIN npi_directory n USING (npi)
"""

GRAIN_REL = {"tin": _TIN_REL, "entity": _ENTITY_REL, "npi": _NPI_REL}


# Store size (done in-network rates) above which the NPI / entity views need a
# narrowing filter — see create_app's _require_narrowing for the measurements.
NARROW_REQUIRED_ROWS = 40_000_000

# Placeholder for an exact NPI/TIN search clause; rel_sql substitutes the
# grain's single-column predicate (see FilterSet's q handling).
_IDENT_TOKEN = "(__IDENT_MATCH__)"


def _ident_clause(grain: str, kind: str) -> str:
    """One-parameter exact match for an NPI (kind 'npi') or TIN ('tin')."""
    if grain == "npi":
        return "unit_id = ?" if kind == "npi" else "tin_value = ?"
    if grain == "tin":
        return "tin_value = ?"          # a 10-digit value = an NPI in the TIN slot
    # entity grain: tin_value is the '; '-joined list of member TINs
    return "list_contains(string_split(tin_value, '; '), ?)"


class FilterSet:
    """Query params -> parameterized WHERE over the uniform grain relation.
    Shared by table, summary, and CSV export so the numbers always agree."""

    def __init__(self, qp: dict):
        split = lambda s: [x.strip() for x in s.split(",") if x.strip()] if s else []  # noqa: E731

        def multi(key):
            # Payer names are FREE TEXT and very frequently contain commas
            # ("Blue Cross and Blue Shield of Illinois, a Division of HCSC"), so
            # web values must NOT be comma-split — that shattered one payer into
            # two non-matching names and the filter returned nothing. They
            # arrive from the dashboard as repeated params (?payer=a&payer=b),
            # preserved as a LIST by _qp: lists are read verbatim. A bare
            # STRING (the CLI's --payer flag, older callers) keeps its
            # historical comma-split so `--payer "Aetna,Cigna"` still means
            # two payers — CLI users pick names from `mrfx status`, which
            # shows them exactly as stored.
            if hasattr(qp, "getlist"):
                return [v.strip() for v in qp.getlist(key) if v and v.strip()]
            v = qp.get(key)
            if not v:
                return []
            if isinstance(v, list):
                return [x.strip() for x in v if x and x.strip()]
            return split(v)

        # Every clause lands in ONE bucket; rel_sql needs to know which:
        #  key   — payer / code / pinned month: pick whole contracts
        #  unit  — state, city, therapy-practice, hide-TIN=NPI: attributes of
        #          the practice (constant across its rows)
        #  ident — the search box: picks units (NPI / TIN / entity)
        #  row   — everything else (modifier, class, POS, rate bounds,
        #          dollar-only, discipline): picks rows WITHIN a contract
        # "latest" must be decided over key/unit/ident-filtered rows ONLY: a
        # row filter applied first removes the newest row and promotes a
        # superseded month (rate_max=41 showed May's $40 instead of June's $42).
        # Params are kept in bucket order key, unit, ident, row — the order
        # every query assembles them in (see rel_sql / where_of).
        self._buckets: dict[str, tuple[list, list]] = {
            k: ([], []) for k in ("key", "unit", "ident", "row")}

        def put(kind, clause, *ps):
            self._buckets[kind][0].append(clause)
            self._buckets[kind][1].extend(ps)
        self.described: dict = {}
        self.ident_kind: str | None = None   # set by an exact NPI/TIN search
        # True once a filter references a column that only exists AFTER the
        # name/geo joins (states, cities, display_name). The table endpoint can
        # take its fast late-join path only when this stays False.
        self.uses_dim_cols = False
        # a practice attribute that usually keeps a SMALL share of practices
        # (state, city, a name or partial-ID search): rel_sql then finds those
        # practices first (_tin_rows_by_unit). "Therapy practices only" alone
        # keeps most of a therapy store — listing practices first only added
        # a scan there (99M rates: 0.77 -> 1.41 s), so it stays a join filter.
        self.selective_dims = False

        def add(desc_key, desc_val):
            self.described[desc_key] = desc_val

        payers = multi("payer")
        if payers:
            put("key", f"payer IN ({', '.join('?' for _ in payers)})", *payers)
            add("payers", payers)
        codes = split(qp.get("cpt") or qp.get("code"))
        if codes:
            put("key", f"billing_code IN ({', '.join('?' for _ in codes)})", *codes)
            add("codes", codes)
        disciplines = split(qp.get("discipline"))
        if disciplines:
            # + 'unspecified', the Benchmark's rule (_market_where): a shared
            # code (97110, 97530...) the payer prices without GP/GO/GN is ONE
            # rate for every discipline — excluding it showed "PT 97110" as
            # only the few GP-tagged lines (PT $70 + untagged $65: Explorer 70,
            # Benchmark 67.5)
            put("row", f"(discipline IN ({', '.join('?' for _ in disciplines)}) "
                "OR discipline = 'unspecified')", *disciplines)
            add("disciplines", f"{', '.join(disciplines)} (plus lines the payer did not tag "
                "with a discipline: a shared code priced without GP/GO/GN applies to every "
                "discipline)")
        modifier = qp.get("modifier")
        if modifier == "base":
            put("row", base_sql("modifier_set"))
            add("modifier", BASE_RULE_TEXT)
        elif modifier == "assistant":
            put("row", "(modifier_set LIKE '%CQ%' OR modifier_set LIKE '%CO%')")
            add("modifier", "assistant-provided only (CQ/CO)")
        elif modifier:
            put("row", "('|' || modifier_set || '|') LIKE ('%|' || ? || '|%')", modifier)
            add("modifier", f"contains {modifier}")
        for m in split(qp.get("mod_has")):
            put("row", "('|' || modifier_set || '|') LIKE ('%|' || ? || '|%')", m)
            add(f"mod_has:{m}", True)
        for m in split(qp.get("mod_not")):
            put("row", "('|' || modifier_set || '|') NOT LIKE ('%|' || ? || '|%')", m)
            add(f"mod_not:{m}", True)
        if qp.get("billing_class"):
            put("row", "billing_class = ?", qp["billing_class"])
            add("billing_class", qp["billing_class"])
        if qp.get("pos"):
            put("row", "('|' || service_code_set || '|') LIKE ('%|' || ? || '|%')", qp["pos"])
            add("place_of_service", qp["pos"])
        if qp.get("state"):
            put("unit", "list_contains(states, ?)", qp["state"].upper())
            add("state", qp["state"].upper())
            self.uses_dim_cols = True  # `states` is a joined column
            self.selective_dims = True
        if qp.get("city"):
            put("unit", "len(list_filter(cities, c -> upper(c) = upper(?))) > 0", qp["city"])
            add("city", qp["city"])
            self.uses_dim_cols = True  # `cities` is a joined column
            self.selective_dims = True
        if qp.get("month"):
            if str(qp["month"]).strip().lower() == "latest":
                # the report tabs' "latest" sentinel is not a real file_month —
                # matching it literally returned 0 rows while the methodology
                # sidecar claimed as_of_month "latest" was applied
                raise ValueError(
                    "the explorer month filter takes a real month like 2026-07 "
                    "(leave it empty to see all months)")
            put("key", "file_month = ?", qp["month"])
            add("as_of_month", qp["month"])
        q = qp.get("q")
        ident = re.sub(r"[\s-]", "", q) if q else ""
        if q and ident.isdigit():
            # An NPI / TIN search (the product's core lookup) matches the ID
            # columns ONLY. Both are grouping keys of every grain relation, so
            # DuckDB applies the filter to the raw rows BEFORE the NPI dedup
            # aggregation. ORing in display_name (a joined column) blocked that
            # pushdown: at 306M rows the NPI search aggregated the whole store
            # first and died out of temp disk after 30 s. Digits never occur in
            # a real name; a nameless TIN's label "TIN 123…" is its tin_value.
            if len(ident) in (9, 10):
                # a complete TIN (9) / NPI (10): an exact match on ONE column,
                # which DuckDB pushes into the parquet scan itself (measured at
                # 306M rows: 0.5 s, vs 9.8 s for `npi = ? OR tin_value = ?` —
                # an OR across columns is evaluated only after reading every
                # row). Which column depends on the grain, so rel_sql swaps
                # this token for the grain's clause (exactly one parameter).
                put("ident", _IDENT_TOKEN, ident)
                self.ident_kind = "npi" if len(ident) == 10 else "tin"
            else:   # a partial number while typing
                put("ident", "(unit_id LIKE ? OR tin_value LIKE ?)", f"%{ident}%", f"%{ident}%")
                self.selective_dims = True
            add("search", q)
            # unit_id is a projected name (tin_value / npi / entity), not a
            # base-table column: keep the TIN grain off the late-join path
            self.uses_dim_cols = True
        elif q:
            put("ident", "(unit_id LIKE ? OR upper(display_name) LIKE upper(?) OR tin_value LIKE ?)",
                f"%{q}%", f"%{q}%", f"%{q}%")
            add("search", q)
            self.uses_dim_cols = True  # searches display_name (a joined column)
            self.selective_dims = True
        dollar_only = qp.get("dollar_only", "1") not in ("0", "false")
        if dollar_only:
            # exclude $0/$0.01/negative DOLLAR placeholders here too, so the
            # explorer / code-comparison / CSV-export medians and mins agree with
            # the benchmark and rate-card (which strip them via _market_where).
            # Without this the same store shows a lower median on one screen than
            # another. The per-file zero_rates QA count still reports how many
            # placeholders a payer published (transparency preserved there).
            put("row", "is_dollar_rate AND negotiated_rate > 0.01")
        add("dollar_rates_only", dollar_only)
        if qp.get("hide_tin_npi", "0") in ("1", "true"):
            put("unit", "NOT tin_is_really_npi")
            add("hide_tin_is_really_npi", True)
        if qp.get("therapy_only", "0") in ("1", "true"):
            # STRICT outpatient-practice test (tin_directory.is_therapy) — the
            # description below ships verbatim in every export's methodology
            # sidecar, so it must state the rule the query ACTUALLY applied.
            put("unit", "is_therapy")
            add("therapy_practices_only",
                f"outpatient PT/OT/SLP practices only: >= {THERAPY_MIN_SHARE_PCT}% of the "
                "practice's identified CLINICIANS are PT/OT/SLP (incl. PTA/OTA/SLPA); "
                "organization NPIs are counted on neither side, being billing "
                "entities rather than people. A practice carrying a definite "
                "therapy-clinic org NPI (Clinic/Center - Physical Therapy, "
                "Hearing and Speech, Developmental Disabilities, or CORF) qualifies "
                "on half its clinicians, and a "
                f"<= {THERAPY_SMALL_PRACTICE_NPIS}-NPI practice known only by such an "
                "NPI (or by an incorporated sole proprietor's own practitioner NPI) "
                "qualifies while its clinicians are still being identified. "
                "Otherwise at least half the practice's clinician NPIs must be "
                "identified. Any hospital, skilled-nursing, home-health, hospice, "
                "residential, school/agency, pharmacy or ambulance NPI disqualifies "
                "the practice outright.")
            self.uses_dim_cols = True  # is_therapy is a joined/computed column
        for bound, op in (("rate_min", ">="), ("rate_max", "<=")):
            raw = qp.get(bound)
            if raw in (None, ""):
                continue
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue  # junk numeric input is ignored, not a 500
            put("row", f"negotiated_rate {op} ?", val)
            add(bound, val)
        # "newest file per contract" — the report tabs' LATEST rule, for the
        # explorer-shaped views: payers republish monthly, so with no month
        # pinned every contract appears once per ingested month. A pinned
        # month wins (it is already single-vintage).
        self.latest_only = (not qp.get("month")
                            and str(qp.get("latest", "0")).lower() in ("1", "true"))
        if self.latest_only:
            add("as_of_month", "latest")
        self.hide_outliers = qp.get("hide_outliers", "0") in ("1", "true")
        add("hide_outliers", f"ON — {OUTLIER_RULE}" if self.hide_outliers else "off")
        self.where = self.where_of("key", "unit", "ident", "row")
        self.params = [p for k in ("key", "unit", "ident", "row") for p in self._buckets[k][1]]

    def has(self, kind: str) -> bool:
        """Whether any clause landed in bucket `kind`."""
        return bool(self._buckets[kind][0])

    def where_of(self, *kinds: str) -> str:
        """AND of the named buckets' clauses, in the canonical bucket order
        (so a query assembling buckets in that order binds fs.params as-is)."""
        cl = [c for k in ("key", "unit", "ident", "row") if k in kinds
              for c in self._buckets[k][0]]
        return " AND ".join(cl) if cl else "1=1"


def _qp(request: "Request") -> dict:
    """Query params as a MUTABLE plain dict (handlers add/remove keys), but with
    the free-text multi-value `payer` filter preserved as the FULL list of
    repeated values. Payer names contain commas, so they arrive as repeated
    params (?payer=a&payer=b) rather than one comma-joined value that would
    shatter a name into non-matching pieces."""
    qp = dict(request.query_params)
    payers = request.query_params.getlist("payer")
    if payers:
        qp["payer"] = payers
    return qp


def website_lookup_url(name: str | None, city: str | None, state: str | None) -> str:
    """A ready-made web SEARCH link for a practice — org name + city + state.
    NPPES has no website field, so this is a starting point to FIND and verify
    the real site, never a claimed official URL. The verified URL is whatever
    the user hand-checks and saves (store.org_websites)."""
    from urllib.parse import quote_plus
    terms = " ".join(t for t in (f'"{name}"' if name else "", city or "",
                                  state or "", "physical therapy") if t).strip()
    return "https://www.google.com/search?q=" + quote_plus(terms) if name else ""


def grain_of(qp: dict, cfg: MrfxConfig, store: Store) -> str:
    # Entity grain rolls TINs up by the organization name their NPIs resolve to
    # in NPPES — this works with OR without a manual entity_map.yaml; the map
    # only ADDS explicit TIN->name overrides on top. (Historically entity grain
    # degraded to tin when no map was loaded, from back when _ENTITY_REL grouped
    # ONLY by the map. It now also groups by the NPPES display_name, so an
    # explicit entity request must be honored or the automatic org rollup is
    # invisible — a TIN with no resolved name is its own entity, so unenriched
    # data simply looks like tin grain until names arrive.)
    grain = qp.get("grain") or cfg.default_grain or (
        "entity" if store.entity_map() else "tin"
    )
    return grain if grain in GRAIN_REL else "tin"


# "latest" = each CONTRACT's newest month — a contract is a payer x provider x
# code line, the Benchmark's rule (benchmark._rates_relation). tin_is_really_npi
# is part of the identity: an EIN and a same-digits npi-typed TIN are different
# providers and must never supersede each other. The TIN and entity grains read
# it from the spine's stored is_latest (store.LATEST_PARTITION, the same key).
# An NPI billing under two TINs holds two contracts, and an NPI's newest month
# under a TIN can be older than the TIN's (it left the roster), so the NPI
# grain — live over the raw rows, always narrowed by an exact ID or payer+code
# — keeps its own window.
_NPI_LATEST_QUALIFY = ("QUALIFY file_month = max(file_month) OVER "
                       "(PARTITION BY payer, unit_id, tin_value, tin_is_really_npi, billing_code)")


def rel_sql(grain: str, fs: FilterSet) -> str:
    """The filtered grain relation. Binds fs.params in bucket order (key, unit,
    ident, row) — every branch below assembles the WHERE in that order.

    Under "latest" the newest month is decided over the WHOLE contract, never
    over the rows a filter left: a row filter (rate bounds, modifiers, POS,
    class, dollar-only, discipline) applied first removed a contract's newest
    row and promoted a superseded month — rate_max=41 showed May's $40 under
    'latest' while June's $42 was the payer's current rate. At the TIN and
    entity grains the spine's stored is_latest IS that whole-contract answer
    (key/unit/ident filters select whole contracts, so it equals the window
    over the filtered rows); the NPI grain filters by key/unit/ident first and
    windows, then applies row filters."""
    def sub(w: str) -> str:
        if fs.ident_kind:
            return w.replace(_IDENT_TOKEN, f"({_ident_clause(grain, fs.ident_kind)})")
        return w
    # a selective practice-attribute filter (state, city, a name or partial-ID
    # search) finds its practices first, then reads only their rows; an exact
    # ID alone stays a plain spine predicate (pushed into the scan already),
    # and so does "therapy only" alone (see FilterSet.selective_dims)
    by_unit = fs.selective_dims and grain in ("tin", "entity")
    if grain == "entity":
        # member TINs are filtered by contract and practice attributes FIRST —
        # a state=MO filter keeps the entity's MO TINs, not every TIN of an
        # entity that has one MO TIN (that showed a MO+KS blend under "MO") —
        # and, under latest, each member is reduced to ITS newest month before
        # the member rates are combined
        if by_unit and fs.has("unit"):
            inner = _tin_rows_by_unit(fs.where_of("key"), fs.where_of("unit"), fs.latest_only)
        else:
            tin_rel = _TIN_REL_LATEST if fs.latest_only else _TIN_REL
            inner = f"SELECT * FROM ({tin_rel}) WHERE {fs.where_of('key', 'unit')}"
        base = (f"SELECT * FROM ({_entity_rel(inner, fs.latest_only)}) "
                f"WHERE {sub(fs.where_of('ident', 'row'))}")
    elif by_unit and (fs.has("unit") or not fs.ident_kind):
        rows = _tin_rows_by_unit(fs.where_of("key"), sub(fs.where_of("unit", "ident")),
                                 fs.latest_only)
        base = f"WITH base AS ({rows}) SELECT * FROM base WHERE {fs.where_of('row')}"
    elif fs.latest_only and grain == "tin":
        base = f"WITH base AS ({_TIN_REL_LATEST}) SELECT * FROM base WHERE {sub(fs.where)}"
    elif fs.latest_only:
        pre = sub(fs.where_of("key", "unit", "ident"))
        base = (f"SELECT * FROM (WITH base AS ({GRAIN_REL[grain]}) SELECT * FROM base "
                f"WHERE {pre} {_NPI_LATEST_QUALIFY}) "
                f"WHERE {fs.where_of('row')}")
    else:
        base = f"WITH base AS ({GRAIN_REL[grain]}) SELECT * FROM base WHERE {sub(fs.where)}"
    return _outlier_band(base) if fs.hide_outliers else base


# how long a JOINED view's summary strip is reused (a spine-only strip lives
# for its data generation — see the summary endpoint)
SUMMARY_TTL_SECONDS = 30.0


def spine_where(fs: FilterSet) -> str:
    """WHERE over the BARE spine (rates_by_tin, no joins) equal to the TIN
    relation's filter — valid only when no filter touches a joined column
    (not fs.uses_dim_cols). Binds fs.params."""
    return f"is_latest AND {fs.where}" if fs.latest_only else fs.where


def stat_rel(grain: str, fs: FilterSet) -> str:
    """The relation the AGGREGATE views read — the summary strip and its
    by-code rows, the Code page's histogram and payer leaderboard, the trend:
    rel_sql's rows. At the TIN grain with no name/geo/search filter they come
    straight off the spine: the three LEFT JOINs match at most one row each,
    so they never add or drop a row, and none of these aggregates reads a
    joined column — joining names onto every spine row only to count and
    median the rates was most of their cost. Same rows, same columns used."""
    if grain != "tin" or fs.uses_dim_cols:
        return rel_sql(grain, fs)
    base = f"SELECT *, tin_value AS unit_id FROM rates_by_tin WHERE {spine_where(fs)}"
    return _outlier_band(base) if fs.hide_outliers else base


def _outlier_band(base: str) -> str:
    """`base` without rows >5x or <0.2x of their code's median within it.

    The reference median must exclude $0/$0.01 placeholders: with "dollar
    rates only" OFF, the filter no longer strips them, and a placeholder-heavy
    code's median collapsed toward $0.01 — the 0.2x-5x band then hid most REAL
    rates. And the band is denominated in dollars, so percentage/per-diem rows
    (rate ~1.5) must be exempt from it — hiding them was exactly what
    unchecking dollar-only asked to undo."""
    return f"""
        WITH filtered AS ({base}),
        med AS (
            SELECT billing_code, median(negotiated_rate) AS m
            FROM filtered WHERE is_dollar_rate AND negotiated_rate > 0.01
            GROUP BY billing_code
        )
        SELECT filtered.* FROM filtered LEFT JOIN med USING (billing_code)
        WHERE NOT is_dollar_rate OR m IS NULL
           OR (negotiated_rate <= 5 * m AND negotiated_rate >= 0.2 * m)
        """


def practice_basis(fs: FilterSet) -> tuple[str, str]:
    """(SQL predicate, plain-words label) for "a practice's rate" in the
    Code page's payer leaderboard and the summary's by-code strip — the
    Benchmark's basis (benchmark._market_where), so the same store and month
    give the same median on every screen: dollar rates without $0/$0.01
    placeholders, no NPI-in-the-TIN-slot rows, professional class and base
    modifiers — unless the user's own filter picked a class or a modifier,
    which then wins (the relation is already restricted to it)."""
    preds = ["is_dollar_rate", "negotiated_rate > 0.01", "NOT tin_is_really_npi"]
    notes = ["dollar rates (no $0/$0.01 placeholders)", "no NPI-in-the-TIN-slot rows"]
    if "billing_class" not in fs.described:
        preds.append("billing_class = 'professional'")
        notes.append("professional class")
    if not any(k == "modifier" or k.startswith(("mod_has:", "mod_not:")) for k in fs.described):
        preds.append(base_sql("modifier_set"))
        notes.append(BASE_RULE_TEXT)
    return " AND ".join(preds), "; ".join(notes)


# The tiebreaker must be a TOTAL order over the grain's row identity:
# LIMIT/OFFSET pages are independent queries, and DuckDB (with
# preserve_insertion_order off) may order still-tied rows differently per page —
# rows silently duplicated on one page and MISSING from another. Multi-month
# stores are tie-dense (a TIN's rate for a code is usually unchanged month over
# month), so every identity column joins the order. tin_value + negotiated_rate
# complete it: at NPI grain an NPI under two TINs ties on every other key (an
# export came back ...13 before ...12 on one run, and a paged view could show
# one twice and drop the other).
_ORDER_TIEBREAK = ("unit_id", "billing_code", "modifier_set", "payer", "file_month",
                   "billing_class", "service_code_set", "is_dollar_rate", "tin_value",
                   "tin_is_really_npi", "negotiated_rate")


def order_sql(sort: str, direction: str, src: str = "") -> str:
    """`src` qualifies every column ("src."): the export's select list re-uses
    the names unit_id / tin_value / payer for MASKED or formula-defused values,
    and an unqualified ORDER BY binds to those aliases — every SSN-pattern TIN
    then tied as 'MASKED-SSN' and the export came out in thread order, a
    different file on each download (and not the screen's order)."""
    if sort not in SORTABLE:
        sort = "negotiated_rate"
    direction = "ASC" if direction.lower() == "asc" else "DESC"
    return (f"ORDER BY {src}{sort} {direction} NULLS LAST, "
            + ", ".join(f"{src}{c} ASC" for c in _ORDER_TIEBREAK))


def _mask_tin_sql(col: str) -> str:
    """SQL twin of store.mask_tin, applied PER ELEMENT of a single TIN or a
    '; '-joined list. (An earlier version checked only the first element's
    prefix, so an entity of [EIN, SSN-pattern] exported the SSN raw.)
    Lengths must be exactly 9 digits: NPIs are 10 and must never mask."""
    return (
        f"CASE WHEN {col} IS NOT NULL THEN "
        f"array_to_string(list_transform(string_split(CAST({col} AS VARCHAR), '; '), "
        f"t -> CASE WHEN regexp_full_match(t, '[0-9]{{9}}') "
        f"AND substr(t, 1, 2) IN {_SSN_PREFIXES} "
        f"THEN 'MASKED-SSN' ELSE t END), '; ') "
        f"ELSE NULL END"
    )


def _defuse_sql(expr: str) -> str:
    """SQL twin of outreach._defuse: a leading =, +, -, @, tab, or CR would
    execute as a formula when the CSV opens in Excel/Sheets — payer and org
    names come from third-party files, so prefix a quote. CSV quoting alone
    does not stop formula execution."""
    return (f"CASE WHEN regexp_matches(CAST({expr} AS VARCHAR), '^[=+\\-@\\t\\r]') "
            f"THEN chr(39) || CAST({expr} AS VARCHAR) "
            f"ELSE CAST({expr} AS VARCHAR) END")


def _as_int(v, default: int) -> int:
    """Coerce a JSON body value to int; None/'' -> default. Any other junk
    (a list, a non-numeric string) raises BenchmarkError so the handler returns
    a clean 422 instead of a 500 on int(None)/int([])."""
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        raise BenchmarkError(f"expected a whole number, got {v!r}")


def _bounded_int(v, default, lo, hi):
    """Wire-boundary int with hard bounds. Negative LIMITs reach DuckDB as a
    binder error (a 500), and a negative min_practices walked size_premium
    into indexing an empty list — found by the parameter-abuse audit. Every
    count-like wire param comes through here so the class cannot recur."""
    return max(lo, min(_as_int(v, default), hi))


def _as_float(v, default):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        raise BenchmarkError(f"expected a number, got {v!r}")


def _comparables_arg(v) -> list[str] | None:
    """Comparable practices off the wire: a LIST of names. A bare string used
    to be iterated letter by letter ("Acme" -> columns A, c, m, e) and a
    number raised TypeError (HTTP 500) in the proposal endpoints."""
    if v in (None, "", []):
        return None
    if not isinstance(v, list):
        raise BenchmarkError("comparables must be a list of practice names, "
                             'e.g. ["Acme PT", "431234567"]')
    out = [str(c).strip() for c in v if c is not None and str(c).strip()]
    return out or None


def _state_arg(v):
    """A state off the wire, handed to the compute layer unmangled.

    Routes used to do `str(v).strip()`, which turns a JSON object into a Python
    repr — and `mrfx.states` then quoted that repr back at the reader
    ("could not read \"{'a': 1}\" as a state"). A container is named by its type
    instead; a string is passed through for `state_code` to accept or refuse."""
    if v is None:
        return None
    if isinstance(v, (dict, list, tuple, set, bool)):
        raise BenchmarkError("state must be a two-letter state code or a state "
                             "name, e.g. MO or Missouri")
    return str(v).strip() or None


def export_select(grain: str, fs: FilterSet, sort: str, direction: str) -> tuple[str, list]:
    """The one export query (provenance columns per §7A.6). Every text column
    that can carry third-party strings is formula-defused; TIN columns are
    masked per element first."""
    d = _defuse_sql
    sql = f"""
        SELECT {d('payer')} AS payer,
               {d(_mask_tin_sql('unit_id'))} AS unit_id,
               {d('display_name')} AS display_name,
               {d(_mask_tin_sql('tin_value'))} AS tin_value,
               npi_count, tin_count,
               {d('billing_code')} AS billing_code,
               {d('billing_code_type')} AS billing_code_type,
               discipline, is_timed,
               {d("replace(modifier_set, '|', ';')")} AS modifiers,
               negotiated_rate, rate_min, rate_max, rate_variants,
               {d('negotiated_type')} AS negotiated_type,
               is_dollar_rate,
               {d('billing_class')} AS billing_class,
               {d("replace(service_code_set, '|', ';')")} AS service_codes,
               file_month,
               {d('last_updated_on')} AS last_updated_on,
               {d('schema_version')} AS schema_version,
               source_count,
               {d('source_files')} AS source_files
        FROM ({rel_sql(grain, fs)}) src {order_sql(sort, direction, "src.")}
    """
    return sql, fs.params


def methodology_text(cfg: MrfxConfig, store: Store, grain: str, fs: FilterSet,
                     sort: str, direction: str, view: str) -> str:
    with store.connect() as con:
        files = con.execute(
            "SELECT filename, payer, substr(coalesce(last_updated_on, ''), 1, 7), "
            "last_updated_on FROM files WHERE status = 'done' "
            "AND file_type = 'in_network' ORDER BY filename"
        ).fetchall()
    return "\n".join([
        f"MRF Explorer v{__version__} export methodology — view: {view}",
        f"Generated: {dt.datetime.now(dt.timezone.utc).isoformat()}",
        f"Grain: {grain} (one row per payer x billing_code x {grain} x modifier-set x class x POS-set x month)",
        "Month (file_month): the payer's own date in the file (header last_updated_on), "
        "else the date in the file name, else the month the file was read. A header "
        "dated AFTER the file was read is not trusted: the file is filed under its "
        "name's month (or the month read) and its Files-tab line says so. Payers' "
        "clocks differ — one payer's October files can carry an August date — so a "
        "pinned month compares payers' own statements of vintage.",
        f"Filters: {json.dumps(fs.described, default=str)}",
        f"Sort: {sort} {direction}",
        "Dedup rule: distinct negotiated facts per grain tuple. A TIN rate is the "
        "median of its distinct published values EXCLUDING $0/$0.01 dollar "
        "placeholders (rate_variants still counts them, and rate_min/rate_max "
        "still show them); an ENTITY rate is the median of its member TINs' "
        "rates (under 'latest', each member TIN at ITS newest month); an entity "
        "is an organization (NPI-2) name plus the practice's main state (where "
        "most of its NPIs are), a TIN named only after a clinician is its own "
        "entity, and entity_map.yaml overrides both; entity npi_count sums members (an NPI listed under two member TINs "
        "counts twice); entity source_count counts distinct files (exact when "
        "each member's line comes from one file, otherwise a minimum); entity "
        "rate_variants = how many different member-TIN rates the median is over.",
        f"Outlier handling: {fs.described.get('hide_outliers')}",
        # honesty: with no month filter the grain keeps each file_month as its
        # own row, so a rate republished monthly appears once per month and the
        # medians/counts pool every vintage. Say so, or an exported median reads
        # as one number when it blends several months.
        ("As-of month: latest — each contract's NEWEST ingested month only "
         "(an older month's rate a payer has since republished is superseded)."
         if fs.described.get("as_of_month") == "latest" else
         "As-of month: " + str(fs.described["as_of_month"]) + " (only this month's rates)."
         if "as_of_month" in fs.described else
         "As-of month: ALL months pooled (no month filter was set) — a rate "
         "republished across months is counted once per month, so these "
         "medians/counts blend every vintage. Set a month filter, or use the "
         "report tabs' 'latest' view, for a single-vintage number."),
        "Non-dollar negotiated_type rows (percentage, per diem) are excluded when "
        f"dollar_rates_only is true (currently: {fs.described.get('dollar_rates_only')}).",
        "SSN-pattern TINs are masked in every export.",
        MODIFIER_INGEST_NOTE,
        "Per-row provenance: source_files names ONE representative file per row; "
        "source_count is the number of distinct files behind that row.",
        "Rate files in the store at export time (the export above draws on the "
        "subset matching its filters, NOT necessarily all of these): "
        f"{'; '.join(f'{f[0]} ({f[1]}, month {f[2]}, updated {f[3]})' for f in files) or 'none'}",
        "Caveats: a negotiated rate is not per-visit revenue (timed 15-min units, MPPR, "
        "CQ/CO reductions, sequestration, cost-share); published rates include "
        "ghost rates (contracted-but-never-billed codes); a published rate is not "
        "proof of collection.",
    ])


def _mask_row_tins(rows: list[dict]) -> list[dict]:
    for r in rows:
        if r.get("tin_value"):
            r["tin_value"] = "; ".join(
                mask_tin(t.strip()) or "" for t in str(r["tin_value"]).split(";")
            )
        # unit_id stays raw in JSON — it is the drill-down key, never displayed;
        # the UI renders display_name + the masked tin_value column instead.
    return rows


def _bg_safe(fn, *args) -> None:
    """Daemon-thread wrapper for long background work (inbox scans, confirmed
    ingests): log failures instead of dying silently. Deliberately NOT
    BackgroundTasks — those share the request threadpool's tokens, and an
    hours-long ingest parked there starves every other request."""
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 — background work must never die silently
        log.exception("background task %s failed", getattr(fn, "__name__", fn))


def _fs(qp: dict) -> FilterSet:
    """FilterSet for a request: an unusable filter value (e.g. month=latest on
    the explorer) is the CLIENT's error — 422 with the message, never a 500."""
    try:
        return FilterSet(qp)
    except ValueError as e:
        raise HTTPException(422, str(e))


def create_app(cfg: MrfxConfig, store: Store) -> FastAPI:
    # Startup work is collected here and run by the lifespan handler below.
    # `@app.on_event("startup")` is deprecated and slated for removal, but the
    # handlers are defined far below (they close over locals built on the way
    # down), so they register into this list instead of decorating.
    startup_tasks: list = []

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        for task in startup_tasks:
            try:
                await task()
            except Exception:  # noqa: BLE001 — boot tuning must never block serve
                log.exception("startup task %s failed", getattr(task, "__name__", task))
        yield

    app = FastAPI(title="MRF Explorer", docs_url="/api/docs", lifespan=lifespan)
    registry = Registry(cfg)
    sync_entity_map(cfg, store)
    if cfg.mpfs_path and Path(cfg.mpfs_path).exists():
        try:
            _load_mpfs_csv(store, Path(cfg.mpfs_path).read_bytes(), str(cfg.mpfs_path))
        except Exception:  # noqa: BLE001 — a bad anchor CSV must not stop serve
            logging.getLogger(__name__).exception(
                "mpfs_path %s could not be loaded — %% -of-Medicare anchors are "
                "off until the file is fixed (code,locality,non_facility_rate)",
                cfg.mpfs_path)

    # -- rates table ---------------------------------------------------------

    # Row-count cache for the table's pagination total. The count is the same
    # for every page and every sort of one filter set, and only changes when the
    # data does — but it was recomputed (a full scan of the filtered relation)
    # on every keystroke, page turn, and sort click, which dominates latency on
    # a large store. Key on (grain, filter, data_generation) so paging/sorting
    # reuse it instantly and any ingest/rebuild (which bumps data_generation)
    # invalidates it; a short TTL backstops the live NPI-grain view, whose parts
    # can change without a rebuild. Bounded so it can't grow without limit.
    _count_cache: dict[tuple, tuple[float, int]] = {}
    _count_lock = threading.Lock()  # single-flight for the heavy non-late-join count
    _COUNT_TTL = 30.0

    def _tin_late_join_ok(grain: str, fs: FilterSet, sort: str) -> bool:
        """The TIN-grain table can attach names/geo AFTER paging (join 100 rows,
        not the whole store) only when nothing needs those joined columns first:
        no filter on state/city/search, no per-code outlier median (needs the
        full set), and no sort by a column the base table lacks: display_name is
        joined, and tin_count exists only as the projection's synthesized
        `1 AS tin_count` (sorting the base table by it was a BinderException →
        500). Every other ORDER BY tiebreaker is a base rates_by_tin column, so
        the page — and its order — is byte-identical to the full-join query,
        just far cheaper. "Latest" qualifies too: it is the spine's own stored
        is_latest column (store.SPINE_QUERY), see spine_where."""
        return (grain == "tin" and not fs.hide_outliers
                and not fs.uses_dim_cols and sort not in ("display_name", "tin_count"))

    def _filtered_count(con, grain: str, fs: FilterSet) -> int:
        key = (grain, fs.where, tuple(fs.params), fs.hide_outliers, fs.latest_only,
               store.data_generation)
        hit = _count_cache.get(key)
        now = time.monotonic()
        if hit is not None and now - hit[0] < _COUNT_TTL:
            return hit[1]
        # When the late-join path applies, the LEFT joins can't change the row
        # count (≤1 match each), so count the base table directly and skip
        # building three hash tables over the whole store. That path is cheap
        # (indexed on the materialized table) and needs no single-flight.
        if _tin_late_join_ok(grain, fs, "negotiated_rate"):
            total = con.execute(
                f"SELECT count(*) FROM rates_by_tin WHERE {spine_where(fs)}",
                fs.params).fetchone()[0]
            _count_cache[key] = (now, total)
            return total
        # The heavy path (entity/NPI grain, or dim-filtered) is a full-store
        # aggregation. Single-flight it so N concurrent page/sort clicks on a
        # cold cache don't each launch a whole-store scan (a stampede that
        # thrashes the HDD at 300M rows). One computes; the rest serve the last
        # value (or wait) rather than piling on.
        if not _count_lock.acquire(blocking=False):
            if hit is not None:
                return hit[1]
            with _count_lock:  # no prior value — block for the first computation
                hit = _count_cache.get(key)
                if hit is not None and time.monotonic() - hit[0] < _COUNT_TTL:
                    return hit[1]
                total = con.execute(
                    f"SELECT count(*) FROM ({rel_sql(grain, fs)})", fs.params).fetchone()[0]
                _count_cache[key] = (time.monotonic(), total)
                return total
        try:
            hit = _count_cache.get(key)  # re-check under the lock
            if hit is not None and time.monotonic() - hit[0] < _COUNT_TTL:
                return hit[1]
            total = con.execute(
                f"SELECT count(*) FROM ({rel_sql(grain, fs)})", fs.params).fetchone()[0]
            if len(_count_cache) > 512:
                _count_cache.clear()  # crude but fine: keys churn as filters change
            _count_cache[key] = (time.monotonic(), total)
            return total
        finally:
            _count_lock.release()

    # NPI and entity grains are LIVE aggregations over the whole store (the NPI
    # dedup, the entity rollup): ORDER BY … LIMIT 100 still aggregates every
    # row a filter can't remove first. Measured on a 306M-row store:
    #   NPI view    exact NPI/TIN search 0.6-1.2 s · payer+code 2-3 s ·
    #               code only 30-41 s · payer only 51 s · name search / no
    #               filter: out of temp disk after ~30 s
    #   Entity view payer+code 3.5 s · code only / TIN search / no filter:
    #               out of temp disk (membership resolves after aggregation)
    # On a big store, ask for a filter that keeps it fast instead of a long
    # hang that ends in a 500. Small stores are unaffected (NARROW_REQUIRED_ROWS).
    def _require_narrowing(grain: str, fs: FilterSet) -> None:
        if grain not in ("npi", "entity"):
            return
        d = fs.described
        payer_and_code = bool(d.get("payers")) and bool(d.get("codes"))
        if payer_and_code or (grain == "npi" and fs.ident_kind):
            return
        try:
            with store.connect() as con:
                n = con.execute("SELECT coalesce(sum(rows_emitted), 0) FROM files "
                                "WHERE status = 'done' AND file_type = 'in_network'"
                                ).fetchone()[0]
        except Exception:  # noqa: BLE001 — the guard is advisory
            return
        if n < NARROW_REQUIRED_ROWS:
            return
        if grain == "npi":
            msg = (f"Your store is large ({n:,} rates), so the NPI view needs a "
                   "narrower question: type a full 10-digit NPI (or 9-digit TIN) "
                   "in the search box, or pick BOTH a payer and a code. To find a "
                   "practice by NAME, use the TIN view's search, then click it.")
        else:
            msg = (f"Your store is large ({n:,} rates), so the Entity view needs "
                   "BOTH a payer and a code picked. To look up one practice, use "
                   "the TIN view's search (name or TIN), then click it.")
        raise HTTPException(422, msg)

    @app.get("/api/rates")
    def rates(
        request: Request,
        sort: str = "negotiated_rate",
        dir: str = "desc",
        page: int = Query(1, ge=1),
        page_size: int = Query(100, ge=1, le=1000),
    ):
        qp = _qp(request)
        grain = grain_of(qp, cfg, store)
        fs = _fs(qp)
        _require_narrowing(grain, fs)
        order = order_sql(sort, dir)
        limit_params = [*fs.params, page_size, (page - 1) * page_size]
        if _tin_late_join_ok(grain, fs, sort):
            # sort + LIMIT the base rows to one page FIRST (no joins), THEN attach
            # names/geo to just those rows. On a broad view this turns three
            # whole-store hash joins into a 100-row join — seconds -> milliseconds.
            # Order the PROJECTED page (unique output names) — ordering the raw
            # join would be ambiguous for a sort column that also exists in a
            # dimension table (e.g. npi_count lives in tin_directory too).
            sql = (f"WITH page AS (SELECT *, tin_value AS unit_id FROM rates_by_tin "
                   f"WHERE {spine_where(fs)} {order} LIMIT ? OFFSET ?) "
                   f"SELECT * FROM (SELECT {_TIN_PROJECTION} FROM page t {_TIN_JOINS}) {order}")
        else:
            sql = f"{rel_sql(grain, fs)} {order} LIMIT ? OFFSET ?"
        with store.connect() as con:
            rows = _dicts(con.execute(sql, limit_params))
            total = _filtered_count(con, grain, fs)
        return {"rows": _mask_row_tins(rows), "total": total, "page": page,
                "page_size": page_size, "grain": grain}

    # The summary strip is a heavy whole-store aggregation (5 quantiles + 2
    # count-distinct). Cache it on (grain, filter, data_generation) with a
    # short TTL + single-flight, exactly like _filtered_count/store_stats: it
    # was seen running CONCURRENTLY with a rollup delta and the enrichment
    # scan, three heavy scans thrashing the 8 GB pool. Paging never reloads it;
    # this collapses repeated identical loads and stops stacking.
    _summary_cache: dict[tuple, tuple[float, dict]] = {}
    _summary_lock = threading.Lock()
    # the market overview does several whole-spine GROUP BYs — heavy at book
    # scale — so cache per (state, data_generation); it changes only on a rebuild.
    _overview_cache: dict[tuple, dict] = {}
    _overview_lock = threading.Lock()

    @app.get("/api/summary")
    def summary(request: Request):
        qp = _qp(request)
        grain = grain_of(qp, cfg, store)
        fs = _fs(qp)
        _require_narrowing(grain, fs)
        # hide_outliers is applied inside rel_sql but is NOT part of fs.where/
        # params — omitting it from the key served the unfiltered stats to the
        # outlier-hidden view (and vice versa) for a TTL: the strip disagreed
        # with the table it sits above (caught by the verification audit,
        # reproduced live: max=$9000 shown with the outlier "hidden")
        key = (grain, fs.where, tuple(fs.params), fs.hide_outliers, fs.latest_only,
               store.data_generation)
        # Off the bare spine (stat_rel's own test) the strip is a pure function
        # of the key: the spine changes only with data_generation, which every
        # rollup write bumps after its commit. So it lives for the generation,
        # not 30 s — the Explorer opens on the pooled, unfiltered strip (9.1 s
        # cold at 99M rates) and recomputed it on every visit to the tab more
        # than 30 s after the last (round 5, P1.3). A joined view (names, state,
        # therapy) reads tables enrichment updates between generations: TTL.
        per_generation = grain == "tin" and not fs.uses_dim_cols

        def fresh(h):
            return h is not None and (per_generation
                                      or time.monotonic() - h[0] < SUMMARY_TTL_SECONDS)

        hit = _summary_cache.get(key)
        if fresh(hit):
            return _with_anchor(hit[1], fs)
        if not _summary_lock.acquire(blocking=False):
            # a heavy scan is already running — serve the last value for THIS
            # key, else say "busy" honestly (serving another filter's numbers
            # under this view's label would be a wrong number; and iterating
            # the cache dict here races the winner's insert/clear)
            if hit is not None:
                return _with_anchor(hit[1], fs)
            raise HTTPException(503, "summary is busy — it will load on the next refresh")
        try:
            hit = _summary_cache.get(key)  # re-check under the lock
            if fresh(hit):
                return _with_anchor(hit[1], fs)
            # the strip and the by-code rows read only spine columns: off the
            # bare spine when no name/geo filter needs the joins (stat_rel)
            srel = stat_rel(grain, fs)
            with store.connect() as con:
                row = con.execute(
                    f"""
                    -- ONE list-form quantile, not median + two quantile_cont:
                    -- each exact quantile aggregate holds its own copy of every
                    -- value in memory (not spillable). At 306M rows the three
                    -- copies peaked the server at 11.2 GB RSS; one state gives
                    -- the identical exact values (median == quantile_cont .5).
                    SELECT n, entities, codes, mn, q[1], q[2], q[3], mx FROM (
                    SELECT count(*) AS n,
                           count(DISTINCT unit_id) AS entities,
                           count(DISTINCT billing_code) AS codes,
                           min(negotiated_rate) FILTER (is_dollar_rate AND negotiated_rate > 0.01) AS mn,
                           quantile_cont(negotiated_rate, [0.25, 0.5, 0.75])
                               FILTER (is_dollar_rate AND negotiated_rate > 0.01) AS q,
                           max(negotiated_rate) FILTER (is_dollar_rate AND negotiated_rate > 0.01) AS mx
                    FROM ({srel}))
                    """,
                    fs.params,
                ).fetchone()
                # PER-CODE breakdown: one blended median across 97110+97530+…
                # is analytically meaningless (different services), so give a row
                # per code — median, spread (IQR), practice count, and % of
                # Medicare / below-Medicare flag when an MPFS anchor is loaded.
                # Practice basis = the Benchmark's (practice_basis): each
                # practice's own median first, then the spread ACROSS
                # practices — a raw-row quantile let a TIN publishing many
                # variants outvote the rest, and blended professional with
                # institutional and NPI-slot rows the Benchmark excludes.
                basis, basis_text = practice_basis(fs)
                by_code = _dicts(con.execute(
                    f"""
                    WITH r AS (SELECT billing_code, unit_id, median(negotiated_rate) AS negotiated_rate
                               FROM ({srel})
                               WHERE {basis}
                               GROUP BY billing_code, unit_id)
                    SELECT billing_code, q[2] AS median, q[1] AS p25, q[3] AS p75,
                           entities FROM (
                    SELECT r.billing_code,
                           quantile_cont(r.negotiated_rate, [0.25, 0.5, 0.75]) AS q,
                           count(DISTINCT r.unit_id)             AS entities
                    FROM r GROUP BY r.billing_code)
                    ORDER BY billing_code
                    """,
                    fs.params,
                ))
            keys = ["n", "entities", "codes", "min", "p25", "median", "p75", "max"]
            scanned = {**dict(zip(keys, row)), "grain": grain, "by_code": by_code,
                       "by_code_basis": basis_text}
            if len(_summary_cache) > 256:
                _summary_cache.clear()
            _summary_cache[key] = (time.monotonic(), scanned)
            return _with_anchor(scanned, fs)
        finally:
            _summary_lock.release()

    def _with_anchor(scanned: dict, fs: FilterSet) -> dict:
        """The cached scan + the Medicare comparison, applied per request: an
        MPFS import changes the anchor without a new data generation, and a
        per-generation strip must not keep the old percentages."""
        # the Medicare anchor for THIS view's state (store.medicare_anchor):
        # one locality applied to another state's practices misstates the %
        anchor = store.medicare_anchor(fs.described.get("state"))
        by_code = []
        for c0 in scanned["by_code"]:
            c = dict(c0)
            mc = anchor["rates"].get(c["billing_code"])
            c["pct_medicare"] = (round(100 * c["median"] / mc)
                                 if mc and c.get("median") else None)
            c["below_medicare"] = bool(mc and c.get("median") is not None
                                       and c["median"] < mc)
            by_code.append(c)
        return {**scanned, "by_code": by_code,
                "medicare_anchor": anchor["label"], "medicare_refusal": anchor["refusal"],
                "mpfs_loaded": any(c["pct_medicare"] is not None for c in by_code)}

    # -- detail views -----------------------------------------------------------

    @app.get("/api/entity/{grain}/{unit_id:path}")
    def entity_detail(grain: str, unit_id: str):
        if grain not in GRAIN_REL:
            raise HTTPException(404, "grain must be entity|tin|npi")
        with store.connect() as con:
            if grain == "entity":
                unit_id = _resolve_entity_key(con, unit_id)
            rows = _dicts(con.execute(
                f"SELECT * FROM ({GRAIN_REL[grain]}) WHERE unit_id = ? "
                "ORDER BY billing_code, payer, modifier_set, file_month, billing_class, "
                "service_code_set, is_dollar_rate, tin_value, negotiated_rate",
                [unit_id],
            ))
            if not rows:
                raise HTTPException(404, f"no rates for {grain} {unit_id}")
            if grain == "npi":
                member_npis = _dicts(con.execute(
                    "SELECT * FROM npi_directory WHERE npi = ?", [unit_id]))
                tins = []
                tin_list = []
            else:
                if grain == "tin":
                    tin_list = [unit_id]  # unit_id IS the tax id
                else:
                    # Entity: resolve EVERY tax id that rolls up to this entity
                    # through the SAME key the grain groups on (_ENTITY_KEY_SQL:
                    # manual map name, else NPPES name + state(s), else the raw
                    # TIN for a no-name SSN-pattern TIN) — so the drawer lists
                    # exactly the members the entity row aggregated.
                    tin_list = [r[0] for r in con.execute(
                        f"SELECT DISTINCT tin_value FROM ({_entity_keyed(_TIN_REL)}) "
                        "WHERE entity_key = ? ORDER BY tin_value",
                        [unit_id],
                    ).fetchall()]
                tins = _dicts(con.execute(
                    f"SELECT * FROM tin_directory WHERE tin_value IN ({', '.join('?' for _ in tin_list)}) "
                    "ORDER BY npi_count DESC NULLS LAST, tin_value",
                    tin_list,
                )) if tin_list else []
                for _t in tins:  # SSN-pattern TINs masked on every surface
                    _t["tin_value_masked"] = mask_tin(_t["tin_value"])
                member_npis = _dicts(con.execute(
                    f"""
                    SELECT DISTINCT r.npi, n.org_name, n.entity_type, n.city, n.state, n.taxonomy_desc
                    FROM rates r LEFT JOIN npi_directory n USING (npi)
                    WHERE r.tin_value IN ({', '.join('?' for _ in tin_list)})
                      AND r.npi IS NOT NULL  -- TIN-only rows have no NPI to list
                    ORDER BY r.npi
                    """,
                    tin_list,
                )) if tin_list else []
            chart = _dicts(con.execute(
                f"""
                SELECT billing_code, payer, median(negotiated_rate) AS median_rate
                FROM ({GRAIN_REL[grain]})
                WHERE unit_id = ? AND is_dollar_rate AND negotiated_rate > 0.01
                GROUP BY billing_code, payer ORDER BY billing_code, payer
                """,
                [unit_id],
            ))
        variance = [r for r in rows if (r.get("rate_variants") or 1) > 1]
        # website: the user-verified URL (if any of this org's TINs has one) +
        # a lookup link to find/verify one. Compute BEFORE masking tin_value.
        sites = store.org_websites()
        website = next((sites[t] for t in tin_list if t in sites), None) if grain != "npi" else None
        # geo for the lookup link: tin_directory rows carry LIST columns
        # (states/cities), npi_directory rows carry scalars (state/city) —
        # reading .get("city") off a directory row was always None, so the
        # lookup degraded to a name-only search for every TIN/entity drawer
        if tins:
            t0 = tins[0]
            geo_city = (t0.get("cities") or [None])[0]
            geo_state = (t0.get("states") or [None])[0]
        elif member_npis:
            geo_city = member_npis[0].get("city")
            geo_state = member_npis[0].get("state")
        else:
            geo_city = geo_state = None
        website_lookup = website_lookup_url(rows[0]["display_name"], geo_city, geo_state)
        for t in tins:
            t["tin_value"] = mask_tin(t["tin_value"])
        return {
            "unit_id": unit_id, "grain": grain,
            "display_name": rows[0]["display_name"],
            "rates": _mask_row_tins(rows), "tins": tins, "npis": member_npis,
            "chart": chart, "variants": len(variance),
            "website": website, "website_lookup": website_lookup,
            "website_tins": tin_list if grain != "npi" else [],
            # The Medicare layers travel WITH the practice rather than living
            # only on their own tab: whoever opens a practice anywhere in the
            # app sees at a glance that it has referral data and whether any
            # of its referrers just lost standing. Absent (None) unless the
            # layers have been imported — never an empty box implying "none".
            "medicare": _medicare_glance(
                [r["npi"] for r in member_npis] if member_npis
                else ([unit_id] if grain == "npi" else [])),
        }

    def _medicare_glance(npis: list[str]) -> dict | None:
        """Two numbers and a warning for the entity drawer. Best-effort and
        never fatal: a drawer must still open when the Medicare layers are
        absent, half-imported, or mid-import."""
        if not npis:
            return None
        try:
            from .medicare import org_referrals, recent_losses
            ref = org_referrals(store, npis, "in", limit=500)
            if not ref["rows"]:
                return None
            lost = recent_losses(store, [r["npi"] for r in ref["rows"]])
            return {
                # exact totals from the aggregate, NOT sums over the fetched
                # rows — a practice with more partners than the row limit
                # would otherwise be silently understated on every drawer
                "sources": ref["total_partners"],
                "patients": ref["total_patients"],
                "lost_standing": len(lost),
                "dataset": ref["dataset"], "data_year": ref["data_year"],
            }
        except Exception:  # noqa: BLE001 — a cosmetic panel never breaks a drawer
            log.debug("medicare glance unavailable", exc_info=True)
            return None

    @app.get("/api/code/{code}")
    def code_detail(code: str, request: Request):
        qp = _qp(request)
        qp["code"] = code
        # Payers republish every month, so with no month pinned the relation
        # holds one row per contract PER INGESTED MONTH. Pooling them blended
        # May's $40 and June's $42 into a "$41" median nobody published, and
        # counted the same NPIs once per month. Default to each contract's
        # NEWEST month (FilterSet.latest_only) unless a month is pinned; the
        # page's export button sends the same latest=1, so export == screen.
        qp.setdefault("latest", "1")
        grain = grain_of(qp, cfg, store)
        fs = _fs(qp)
        # the same guard as the table: an entity/NPI-grain code view on a big
        # store is a whole-store live aggregation (temp-disk 500 at 306M rows)
        _require_narrowing(grain, fs)
        rel = rel_sql(grain, fs)
        # histogram + payer leaderboard read only spine columns (stat_rel);
        # the ranking needs the practice names, so it reads `rel`
        srel = stat_rel(grain, fs)
        as_of = qp.get("month") or ("latest" if fs.latest_only else "all")
        with store.connect() as con:
            ranked = _dicts(con.execute(
                f"""
                SELECT unit_id, min(display_name) AS display_name,
                       payer, modifier_set, billing_class,
                       min(discipline) AS discipline,
                       -- is_dollar_rate in the GROUP BY: under dollar_only=0 a
                       -- unit holding $85 AND a 150% percentage row must not
                       -- blend them into one median that matches no published
                       -- number (dollars and percentages are incommensurable)
                       is_dollar_rate,
                       median(negotiated_rate) AS median_rate,
                       min(negotiated_rate) AS min_rate, max(negotiated_rate) AS max_rate,
                       -- MAX, not SUM: the grouped rows are the same
                       -- practice's place-of-service variants, which share
                       -- one roster — summing counted each NPI per variant
                       max(npi_count) AS npi_count, count(*) AS n
                FROM ({rel})
                GROUP BY unit_id, payer, modifier_set, billing_class, is_dollar_rate
                ORDER BY median_rate DESC, unit_id, payer, modifier_set, billing_class,
                         is_dollar_rate LIMIT 500
                """,
                fs.params,
            ))
            # EVERY bucket between the lowest and highest, empty ones as 0: the
            # chart draws one evenly spaced bar per entry, so leaving the empty
            # ones out squeezed the axis ($85.72 next to $300.03) and drew a
            # different distribution than the data's. Same buckets, same counts.
            hist = _dicts(con.execute(
                f"""
                WITH r AS (SELECT negotiated_rate FROM ({srel}) WHERE is_dollar_rate AND negotiated_rate > 0.01),
                g AS (SELECT w, floor(lo / w)::BIGINT AS k0, floor(hi / w)::BIGINT AS k1 FROM (
                        SELECT greatest((max(negotiated_rate) - min(negotiated_rate)) / 20, 0.01) AS w,
                               min(negotiated_rate) AS lo, max(negotiated_rate) AS hi FROM r)),
                c AS (SELECT floor(negotiated_rate / g.w)::BIGINT AS k, count(*) AS n FROM r, g GROUP BY 1)
                SELECT t.k * g.w AS bucket, coalesce(c.n, 0) AS n
                FROM g, range(g.k0, g.k1 + 1) t(k) LEFT JOIN c ON c.k = t.k
                ORDER BY t.k
                """,
                fs.params,
            ))
            # PAYER LEADERBOARD: which payers pay best for THIS code in the
            # current filter (state/discipline/etc). One row per payer — the
            # median across the distinct PRACTICES that payer prices (each
            # practice collapsed to its own median first, so a TIN publishing
            # many modifier/POS/month variants counts once, matching the
            # Benchmark/Markets/report grain — not a raw-row median that
            # over-weights variant-heavy TINs). Placeholders ($0/$0.01) excluded
            # even when the user turns dollar-only off.
            # basis = the Benchmark's (practice_basis): a practice billing
            # professional $50 and institutional $100 was one $75 "practice
            # rate" here while the Benchmark said $50
            basis, basis_text = practice_basis(fs)
            payer_rank = _dicts(con.execute(
                f"""
                WITH per_unit AS (
                    SELECT payer, unit_id, median(negotiated_rate) AS rate
                    FROM ({srel})
                    WHERE {basis}
                    GROUP BY payer, unit_id
                )
                SELECT payer,
                       median(rate)               AS median_rate,
                       quantile_cont(rate, .25)   AS p25,
                       quantile_cont(rate, .75)   AS p75,
                       min(rate)                  AS min_rate,
                       max(rate)                  AS max_rate,
                       count(DISTINCT unit_id)    AS n_entities
                FROM per_unit
                GROUP BY payer ORDER BY median_rate DESC, payer
                """,
                fs.params,
            ))
            anchor = store.medicare_anchor(fs.described.get("state"))
            base = anchor["rates"].get(code)
        # % of Medicare per payer when an MPFS anchor is loaded; the key is
        # always present (None without an anchor) so the frontend never reads
        # undefined — matching /api/summary by_code.
        for p in payer_rank:
            p["pct_medicare"] = (round(100 * p["median_rate"] / base)
                                 if base and p.get("median_rate") else None)
        info = catalog_json().get(code, {})
        return {"billing_code": code, "grain": grain, "as_of": as_of, **info, "ranked": ranked,
                "histogram": hist, "payer_rank": payer_rank, "payer_rank_basis": basis_text,
                "medicare_anchor": anchor["label"], "medicare_refusal": anchor["refusal"],
                "mpfs_rate": round(base, 2) if base else None}

    @app.get("/api/trend")
    def trend(request: Request):
        qp = _qp(request)
        qp.pop("month", None)  # trend spans months by definition
        grain = grain_of(qp, cfg, store)
        fs = _fs(qp)
        _require_narrowing(grain, fs)
        with store.connect() as con:
            rows = _dicts(con.execute(
                f"""
                SELECT billing_code, payer, file_month,
                       median(negotiated_rate) AS median_rate,
                       count(DISTINCT unit_id) AS entities
                FROM ({stat_rel(grain, fs)}) WHERE is_dollar_rate AND negotiated_rate > 0.01
                GROUP BY billing_code, payer, file_month
                ORDER BY billing_code, payer, file_month
                """,
                fs.params,
            ))
        return {"rows": rows}

    @app.get("/api/months")
    def months():
        with store.connect() as con:
            rows = con.execute(
                "SELECT DISTINCT file_month FROM rates_by_tin "
                "WHERE file_month IS NOT NULL AND file_month <> '' "
                "ORDER BY file_month DESC"
            ).fetchall()
        return {"months": [r[0] for r in rows]}

    # -- validation cross-check (§7A.10) -----------------------------------------

    # EVERY endpoint below is a sync `def` ON PURPOSE: FastAPI runs sync
    # endpoints in a threadpool, keeping the event loop free. An `async def`
    # that computes a benchmark (seconds-to-minutes on a big store) or waits on
    # the store's write lock (held for minutes during a rollup) BLOCKS the
    # loop — the server answers nothing, and the dashboard's 15s poll reports
    # "API unreachable" until the work finishes. Enforced by a test that walks
    # every route and rejects coroutine endpoints.
    @app.post("/api/validate")
    def validate(body: dict = Body(...)):
        ident = str(body.get("id", "")).replace("-", "").strip()
        code = str(body.get("code", "")).strip()
        expected = body.get("expected_rate")
        if not ident or not code:
            raise HTTPException(422, "id (TIN or NPI) and code are required")
        if expected is not None and str(expected).strip() != "":
            try:
                expected = float(expected)  # a non-numeric expected_rate is a 422, not a 500
            except (TypeError, ValueError):
                raise HTTPException(422, "expected_rate must be a number")
        else:
            expected = None
        with store.connect() as con:
            rows = _dicts(con.execute(
                """
                SELECT payer, tin_value, npi, billing_code,
                       coalesce(array_to_string(billing_code_modifier, '|'), '') AS modifiers,
                       negotiated_rate, negotiated_type, billing_class,
                       file_month, source_file
                FROM rates
                WHERE billing_code = ? AND (tin_value = ? OR npi = ?)
                -- unique (invariant 7): payer+month+rate tied across TINs,
                -- NPIs, modifiers, classes and files; two identical requests
                -- came back in different orders on the 99M store
                ORDER BY payer, file_month, negotiated_rate, tin_value, npi, modifiers,
                         billing_class, negotiated_type, source_file
                """,
                [code, ident, ident],
            ))
        for r in rows:
            r["tin_value"] = mask_tin(r["tin_value"])
            if expected is not None and r["negotiated_rate"]:
                r["delta_vs_expected"] = round(r["negotiated_rate"] - float(expected), 2)
        return {"rows": rows, "expected_rate": expected,
                "match": any(abs(r.get("delta_vs_expected", 1)) < 0.005 for r in rows)
                if expected is not None else None}

    # -- entity map -----------------------------------------------------------------

    @app.get("/api/entities/map")
    def entities_map():
        mapping = store.entity_map()
        by_name: dict[str, list[str]] = {}
        for tin, name in mapping.items():
            by_name.setdefault(name, []).append(mask_tin(tin))
        return {"entities": [{"name": n, "tins": sorted(t)} for n, t in sorted(by_name.items())]}

    @app.post("/api/entities/update")
    def entities_update(body: dict = Body(...)):
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(422, "entity name required")
        update_entity(cfg, store, name, body.get("add_tins") or [], body.get("remove_tins") or [])
        return entities_map()

    @app.post("/api/org-website")
    def org_website(body: dict = Body(...)):
        """Save (or clear, when url is empty) the hand-verified website for an
        org — applied to all the tax ids passed (an entity's constituent TINs),
        so it shows on the org however a later view resolves it. Accepts only
        http(s) URLs; anything else is rejected rather than stored as a bad
        link."""
        tins = [str(t).strip() for t in (body.get("tins") or []) if str(t).strip()]
        url = str(body.get("url") or "").strip()
        if not tins:
            raise HTTPException(422, "at least one tax id (tins) is required")
        if url and not url.lower().startswith(("http://", "https://")):
            raise HTTPException(422, "website must start with http:// or https:// (or be empty to clear)")
        store.set_org_website(tins, url or None)
        return {"saved": bool(url), "tins": len(tins), "url": url}

    # -- files -----------------------------------------------------------------------

    @app.get("/api/files")
    def files():
        with store.connect() as con:
            rows = _dicts(con.execute(
                "SELECT * FROM files ORDER BY coalesce(finished_at, started_at) DESC NULLS LAST"
            ))
        for r in rows:
            for k in ("started_at", "finished_at"):
                if r.get(k) is not None:
                    r[k] = str(r[k])
            if r.get("qa"):
                try:
                    r["qa"] = json.loads(r["qa"])
                except json.JSONDecodeError:
                    pass
        return {"files": rows}

    @app.post("/api/files/scan")
    def files_scan():
        # a plain daemon thread, NOT BackgroundTasks: background tasks share
        # the request threadpool's 40 tokens, and an hours-long inbox scan
        # parked there starves every other request (audit F3)
        threading.Thread(target=_bg_safe, args=(scan_inbox, cfg, store),
                         name="mrfx-scan", daemon=True).start()
        return {"status": "scanning"}

    @app.delete("/api/files/{filename}")
    def files_forget(filename: str):
        # sync def on purpose: takes the store write lock + a rollup rebuild,
        # so it runs in the threadpool instead of blocking the event loop
        from .ingest import forget_file

        st = store.file_status(filename)
        if not st:
            raise HTTPException(404, "no such file in the store")
        if st.get("status") in ("processing", "queued"):
            # forgetting mid-parse would silently lose the race: the ingest
            # keeps reading its open file handle and re-creates every record
            # minutes after this endpoint returned "forgotten"
            raise HTTPException(409, "this file is being processed right now — "
                                     "wait for it to finish, then remove it")
        if store.url_inflight_for_filename(filename):
            # a retried queue row is re-downloading this file — the files row
            # still shows its OLD terminal status for the whole download
            raise HTTPException(409, "this file's link is being retried right now — "
                                     "wait for it (or skip the link), then remove it")
        try:
            info = forget_file(cfg, store, filename)
        except ValueError:
            raise HTTPException(400, "not a plain filename")
        except RuntimeError as e:  # ingest claimed the file between check and act
            raise HTTPException(409, str(e))
        return {"status": "forgotten", **info}

    # -- URL-drop queue (paste links, the app does the rest) -------------------

    @app.post("/api/urls")
    def urls_add(body: dict):
        # sync def on purpose: enqueueing takes the store's write lock, and a
        # sync route runs in the threadpool instead of blocking the event loop
        from .fetch import add_urls

        raw = body.get("urls") or []
        if isinstance(raw, str):
            # one link per line — commas are legal INSIDE URLs (signed query
            # strings), so never split on them
            raw = raw.splitlines()
        counts = add_urls(store, [str(u) for u in raw])
        return counts

    @app.post("/api/urls/retry-failed")
    def urls_retry_failed():
        return {"requeued": store.requeue_failed()}

    @app.post("/api/urls/clear-queued")
    def urls_clear_queued():
        # bulk-cancel the not-yet-started backlog (e.g. a whole national portal
        # queued 2,000 at a time) so the parsers can catch up. Marked 'skipped'
        # (retryable), nothing destroyed.
        return {"cleared": store.clear_queued()}

    @app.post("/api/urls/resume-stopped")
    def urls_resume_stopped():
        # bulk undo of "Clear queue" / "Stop downloads" (rows parked as skipped)
        return {"resumed": store.resume_canceled()}

    @app.post("/api/urls/stop-downloads")
    def urls_stop_downloads():
        # abort in-flight downloads mid-stream to free the slots; partial files
        # are kept so a later retry resumes them.
        return {"stopped": store.cancel_all_downloading()}

    @app.get("/api/urls")
    def urls_list():
        return {"urls": store.list_urls(), "counts": store.url_queue_counts()}

    @app.get("/api/known-sources")
    def known_sources():
        from .known_sources import load_known_sources

        return {"sources": load_known_sources(cfg.known_sources_path)}

    @app.post("/api/urls/known")
    def urls_add_known(body: dict | None = Body(None)):
        from .fetch import add_urls
        from .known_sources import for_states, load_known_sources

        # {"states": ["MO", "KS"]} queues only those states' payers plus the
        # national ones — not all 104 indexes (hundreds of thousands of files)
        states = (body or {}).get("states") or []
        if isinstance(states, str):
            states = [x for x in states.replace(",", " ").split() if x]
        sources = for_states(load_known_sources(cfg.known_sources_path), states)
        counts = add_urls(store, [s["url"] for s in sources if s["queueable"]])
        counts["portals"] = sum(1 for s in sources if not s["queueable"])
        return counts

    @app.post("/api/urls/{url_id}/retry")
    def urls_retry(url_id: int):
        if not store.set_url_status_by_id(url_id, "queued"):
            raise HTTPException(409, "only failed, skipped, or too-big links can be retried "
                                     "(this row may have just started or already finished)")
        return {"status": "queued"}

    @app.post("/api/urls/{url_id}/cancel")
    def urls_cancel(url_id: int):
        if not store.set_url_status_by_id(url_id, "skipped"):
            raise HTTPException(409, "only queued, failed, or too-big links can be skipped "
                                     "(this row may have just started or already finished)")
        return {"status": "skipped"}

    @app.post("/api/urls/{url_id}/stop")
    def urls_stop(url_id: int):
        """Stop a link whose file is being PROCESSED (the download is kept;
        retry re-processes it without downloading again)."""
        from .fetch import filename_for
        url = store.url_of(url_id)
        if not url or not store.request_parse_stop(filename_for(url)):
            raise HTTPException(409, "this link's file isn't being processed right now")
        return {"status": "stopping"}

    @app.post("/api/files/{filename}/stop")
    def files_stop(filename: str):
        if not store.request_parse_stop(filename):
            raise HTTPException(409, "this file isn't being processed right now")
        return {"status": "stopping"}

    @app.post("/api/urls/{url_id}/force-size")
    def urls_force_size(url_id: int):
        # "download anyway" past confirm_over_gb, for THIS file only. The
        # disk-space guard still applies, so it can't fill the drive.
        if not store.force_size_requeue(url_id):
            raise HTTPException(409, "only a failed or skipped link can be forced past "
                                     "the size limit (this row may be in flight or done)")
        return {"status": "queued"}

    @app.post("/api/files/{filename}/confirm")
    def confirm_file(filename: str):
        st = store.file_status(filename)
        if not st or st.get("status") != "pending_confirmation":
            raise HTTPException(404, "file is not awaiting confirmation")
        path = cfg.inbox_dir / filename
        if not path.exists():
            raise HTTPException(404, "file no longer in inbox")
        threading.Thread(target=_bg_safe, args=(ingest_file, cfg, store, path),
                         name="mrfx-confirm-ingest", daemon=True).start()
        store.upsert_file(filename, status="queued")
        return {"status": "queued"}

    @app.post("/api/upload")
    def upload(file: UploadFile):
        # Path(...).name strips every traversal ("../x"→"x"), but ".", "..", ""
        # collapse to "" → dest would be the inbox dir itself; fall back to a
        # safe name so the temp/rename never targets a directory
        safe = Path(file.filename or "upload.json").name or "upload.json"
        dest = cfg.inbox_dir / safe
        # stream to a name the inbox scanner ignores, rename when COMPLETE —
        # the watcher fires on creation and would otherwise preflight (and
        # quarantine/move!) a half-written file out from under this handler
        import uuid as _uuid
        # pid+uuid keeps two simultaneous uploads OF THE SAME NAME from
        # truncating each other's in-progress temp (audit F7)
        tmp = dest.with_name(f"{dest.name}.{os.getpid()}-{_uuid.uuid4().hex[:8]}.uploading")
        size = 0
        try:
            with open(tmp, "wb") as out:
                while chunk := file.file.read(1 << 20):
                    size += len(chunk)
                    if size > UPLOAD_LIMIT_BYTES:
                        raise HTTPException(413, "over 1 GB — drop the file into data/inbox/ instead")
                    out.write(chunk)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
        threading.Thread(target=_bg_safe, args=(scan_inbox, cfg, store),
                         name="mrfx-upload-scan", daemon=True).start()
        return {"status": "queued", "filename": dest.name, "bytes": size}

    # -- export (CSV + methodology sidecar, §7A.6) ---------------------------------


    def _mktemp(suffix: str) -> Path:
        fd, p = tempfile.mkstemp(suffix=suffix)
        os.close(fd)  # mkstemp returns an OPEN fd — dropping it leaks one per export
        return Path(p)

    def _export_payload(request: Request, view: str, sort: str, dir: str, full: bool):
        """Build the export as a temp FILE (BOM-prefixed for Excel) and return
        its path — never the CSV as a Python string. `?full=true` on a 64M-row
        store is multiple GB; DuckDB streams the COPY to disk under its own
        memory cap, and everything after must stay disk-to-disk so the export
        can't balloon the process RSS with data-sized strings."""
        qp = {} if full else _qp(request)
        grain = grain_of(qp, cfg, store)
        fs = _fs(qp if not full else {"dollar_only": "0"})
        if not full:
            _require_narrowing(grain, fs)
        select, params = export_select(grain, fs, sort, dir)
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
        tmp = _mktemp(".csv")
        with store.connect() as con:
            con.execute(f"COPY ({select}) TO '{sql_path(tmp)}' (FORMAT CSV, HEADER)", params)
        out = _mktemp(".csv")
        import shutil as _shutil
        with open(out, "wb") as dst, open(tmp, "rb") as src:
            dst.write(b"\xef\xbb\xbf")           # BOM for Excel
            _shutil.copyfileobj(src, dst, 1 << 20)
        tmp.unlink()
        method = methodology_text(cfg, store, grain, fs, sort, dir, view)
        return stamp, out, method

    @app.get("/api/export.csv")
    def export_csv(
        request: Request,
        background: BackgroundTasks,
        view: str = "explorer",
        sort: str = "negotiated_rate",
        dir: str = "desc",
        full: bool = False,
    ):
        stamp, out, _method = _export_payload(request, view, sort, dir, full)
        background.add_task(out.unlink, missing_ok=True)  # or it orphans per export
        return FileResponse(out, filename=f"mrfx_{view}_{stamp}.csv", media_type="text/csv")

    @app.get("/api/export.zip")
    def export_zip(
        request: Request,
        background: BackgroundTasks,
        view: str = "explorer",
        sort: str = "negotiated_rate",
        dir: str = "desc",
        full: bool = False,
    ):
        stamp, csv_path, method = _export_payload(request, view, sort, dir, full)
        out = _mktemp(".zip")
        # z.write streams the CSV from disk; ZipFile writes to the file as it
        # goes — no data-sized BytesIO copy of a multi-GB export in RAM
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(csv_path, f"mrfx_{view}_{stamp}.csv")
            z.writestr(f"mrfx_{view}_{stamp}_methodology.txt", method)
        csv_path.unlink(missing_ok=True)
        background.add_task(out.unlink, missing_ok=True)
        return FileResponse(out, filename=f"mrfx_{view}_{stamp}.zip", media_type="application/zip")

    @app.post("/api/report/org-bundle.zip")
    def org_bundle(background: BackgroundTasks, body: dict = Body(...)):
        """One organization, packaged to be combined with the Medicare Order &
        Referring Tracker: a paste-ready NPI list (the tracker is NPI-native,
        this app is TIN-grained — the NPI is the join) plus the rate profile
        the tracker has no way to know."""
        from .orgprofile import compute_org_profile, org_bundle_files
        try:
            profile = compute_org_profile(
                store, str(body.get("subject", "")), body.get("market") or {})
            files = org_bundle_files(store, profile)
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))
        safe = re.sub(r"[^A-Za-z0-9]+", "_", profile["display_name"]).strip("_")[:48]
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
        out = _mktemp(".zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for fname, text in files.items():
                z.writestr(fname, text)
        background.add_task(out.unlink, missing_ok=True)
        return FileResponse(out, filename=f"mrfx_org_{safe or 'practice'}_{stamp}.zip",
                            media_type="application/zip")

    # -- Medicare layers (eligibility + referral structure) ----------------------------

    # State of the most recent tracker import, polled by the dashboard. The
    # import runs on a daemon thread — a Hop Teaming year is 7-11 GB and takes
    # minutes, which must not hold a request open or block the event loop.
    # ONE at a time: the store has a single writer, and two concurrent imports
    # would queue on the write lock anyway with no way to report which is which.
    tracker_job: dict = {"state": "idle", "message": "", "items": [], "done": []}
    tracker_job_lock = threading.Lock()

    def _run_tracker_import(items: list[dict]) -> None:
        from .medicare import import_orf_roster, import_shared_patients

        done: list[dict] = []
        try:
            for i, item in enumerate(items, 1):
                label = item.get("label") or item.get("path")
                with tracker_job_lock:
                    tracker_job["message"] = f"importing {label} ({i} of {len(items)})…"
                try:
                    if item.get("kind") == "eligibility":
                        r = import_orf_roster(store, item["path"])
                        # the label already names the release — repeating it
                        # here rendered "release X — N providers (release X)"
                        done.append({"label": label, "ok": True,
                                     "detail": f"{r['providers']:,} providers",
                                     "diff": r.get("diff")})
                    else:
                        r = import_shared_patients(
                            store, item["path"], year=item.get("year") or None,
                            interval=item.get("interval") or None)
                        done.append({"label": label, "ok": True,
                                     "detail": (r["warning"] or
                                                f"{r['pairs']:,} pairs touching your providers")})
                except Exception as e:  # noqa: BLE001 — one bad file must not
                    # abort the others; fault isolation, same as the ingest path
                    log.warning("tracker import failed for %s: %s", label, e)
                    done.append({"label": label, "ok": False, "detail": str(e)})
                with tracker_job_lock:
                    tracker_job["done"] = list(done)
        finally:
            with tracker_job_lock:
                tracker_job["state"] = "done"
                tracker_job["done"] = list(done)
                ok = sum(1 for d in done if d["ok"])
                tracker_job["message"] = (
                    f"imported {ok} of {len(items)}" if done else "nothing to import")

    def _tracker_discover() -> dict:
        from .tracker import discover
        return discover(store, getattr(cfg, "tracker_dir", None))

    @app.get("/api/medicare/tracker")
    def api_tracker():
        """Where the Order & Referring Tracker is, what it has downloaded, and
        what of that is not yet imported here. Read-only: the tracker's files
        belong to the tracker, which has its own retention rules."""
        d = _tracker_discover()
        with tracker_job_lock:
            d["job"] = dict(tracker_job)
        return d

    @app.post("/api/medicare/tracker/import")
    def api_tracker_import(body: dict = Body(...)):
        """Import selected tracker files INTO THIS RUNNING SERVER. The CLI has
        to refuse while `mrfx serve` holds the database; the server importing
        its own store is the one process allowed to, so the user never has to
        stop anything."""
        # Reserve the job slot ATOMICALLY with the busy-check: discovery below
        # takes real time (it stats files and queries the store), and checking
        # in one lock acquisition then setting "running" in another would let
        # two concurrent clicks both pass the check and spawn two import
        # threads. Anything that fails between reserve and thread-start must
        # release the slot, or the button would be stuck "running" forever.
        with tracker_job_lock:
            if tracker_job["state"] == "running":
                raise HTTPException(409, "an import is already running — "
                                    f"{tracker_job['message']}")
            tracker_job.update({"state": "running", "done": [], "items": [],
                                "message": "finding the tracker's files…"})
        try:
            found = {p["path"]: p for p in _tracker_discover()["pending"]}
            want = body.get("paths")
            # Import only what discovery just offered: the request names files
            # by path, and echoing an arbitrary path back into a reader would
            # let the dashboard read anywhere on disk.
            items = ([found[p] for p in want if p in found] if want
                     else list(found.values()))
            if not items:
                raise HTTPException(422, "nothing to import — either the tracker "
                                    "has nothing new, or those files are no longer "
                                    "there. Refresh and try again.")
        except BaseException:
            with tracker_job_lock:
                tracker_job.update({"state": "idle", "message": ""})
            raise
        with tracker_job_lock:
            tracker_job.update({"items": [i["label"] for i in items],
                                "message": f"starting {len(items)} import(s)…"})
        threading.Thread(target=_bg_safe, args=(_run_tracker_import, items),
                         name="mrfx-tracker-import", daemon=True).start()
        return {"started": [i["label"] for i in items]}

    # -- client watchlist ("my book") ---------------------------------------------------

    @app.get("/api/clients")
    def clients_list():
        from .clients import watchlist
        return {"clients": watchlist(store)}

    @app.post("/api/clients")
    def clients_add(body: dict = Body(...)):
        from .clients import add_client
        try:
            return {"clients": add_client(store, str(body.get("subject", "")))}
        except BenchmarkError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/clients/remove")
    def clients_remove(body: dict = Body(...)):
        from .clients import remove_client
        return {"clients": remove_client(store, str(body.get("subject", "")))}

    @app.post("/api/contracts/renewals")
    def api_renewals(body: dict = Body(...)):
        """Contracts with a REAL published end date, soonest first — with the
        coverage figure attached, because payers publish this field
        inconsistently and a sparse radar must never read as a schedule."""
        from .contracts import renewal_radar
        try:
            return renewal_radar(store, body.get("subjects") or None,
                                 body.get("market") or {},
                                 within_days=_as_int(body.get("within_days"), 365))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.get("/api/contracts/expiry-coverage")
    def api_expiry_coverage():
        from .contracts import expiration_coverage
        return expiration_coverage(store, {})

    @app.post("/api/contracts/new-to-network")
    def api_new_to_network(body: dict = Body(...)):
        """Practices that appear in a payer's book this month and were absent
        from the same payer's previous published month."""
        from .contracts import new_to_network
        try:
            return new_to_network(
                store, body.get("market") or {},
                zip_code=str(body.get("zip") or "").strip() or None,
                radius_miles=float(body["radius_miles"]) if body.get("radius_miles") else None,
                therapy_only=bool(body.get("therapy_only", True)))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.get("/api/plans")
    def api_plans(payer: str = ""):
        """Which plans each payer's ingested files serve — and which payers are
        therefore BLENDING several networks into one median."""
        from .benchmark import plan_coverage
        return plan_coverage(store, payer or None)

    # -- reference datasets: utilization, demographics, new clinics --------------------

    @app.get("/api/utilization/status")
    def api_utilization_status():
        from .utilization import utilization_status
        return utilization_status(store)

    @app.post("/api/utilization/volumes")
    def api_utilization_volumes(body: dict = Body(...)):
        """Annual units per code for a practice, from Medicare claims — what
        pre-fills a volumes box. Medicare-only, so it is labeled a floor."""
        from .utilization import suggested_volumes
        try:
            return suggested_volumes(store, str(body.get("subject", "")),
                                     body.get("year") or None,
                                     multiplier=_as_float(body.get("multiplier"), 1.0))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/utilization/import")
    def api_utilization_import(body: dict = Body(...)):
        """Import by PATH, not upload: the national PUF is a multi-gigabyte CSV
        and pushing it through the browser would be slower and far more
        fragile than DuckDB streaming it off disk (same reason the Medicare
        tab imports the tracker's files by path)."""
        from .utilization import UtilizationImportError, import_utilization
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(422, "give the path to the CSV you downloaded")
        try:
            return import_utilization(store, path, str(body.get("year") or "") or None)
        except UtilizationImportError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/demographics/status")
    def api_demographics_status():
        from .demographics import demographics_status
        return demographics_status(store)

    @app.post("/api/demographics/import")
    def api_demographics_import(body: dict = Body(...)):
        from .demographics import DemographicsImportError, import_demographics
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(422, "give the path to the ACS CSV you downloaded")
        try:
            return import_demographics(store, path,
                                       str(body.get("vintage") or "") or None)
        except DemographicsImportError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/market/sizing")
    def api_market_sizing(body: dict = Body(...)):
        """Population, seniors and therapy practices inside a radius — is this
        market underserved or saturated?"""
        from .demographics import DemographicsImportError, market_sizing
        try:
            return market_sizing(store, str(body.get("zip") or ""),
                                 _as_float(body.get("radius_miles"), 25),
                                 therapy_only=bool(body.get("therapy_only", True)))
        except DemographicsImportError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.get("/api/newclinics/status")
    def api_newclinics_status():
        from .nppes import feed_status
        return feed_status(store)

    @app.post("/api/newclinics")
    def api_newclinics(body: dict = Body(...)):
        """Therapy NPIs issued recently — practices that have no payer contract
        yet, which is the earliest a consultant can reach them."""
        from .nppes import NppesFeedError, new_enumerations
        try:
            return new_enumerations(
                store, zip_code=str(body.get("zip") or "").strip() or None,
                radius_miles=float(body["radius_miles"]) if body.get("radius_miles") else None,
                days=_as_int(body.get("days"), 180),
                therapy_only=bool(body.get("therapy_only", True)),
                state=_state_arg(body.get("state")))
        except NppesFeedError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.get("/api/closures/status")
    def api_closures_status():
        from .nppes import closure_status
        return closure_status(store)

    @app.post("/api/closures")
    def api_closures(body: dict = Body(...)):
        """Therapy NPIs deactivated recently — a closed referral source is a
        hole in a client's inbound volume; a closed competitor is room."""
        from .nppes import NppesFeedError, closures
        try:
            return closures(
                store, zip_code=str(body.get("zip") or "").strip() or None,
                radius_miles=float(body["radius_miles"]) if body.get("radius_miles") else None,
                days=_as_int(body.get("days"), 365),
                therapy_only=bool(body.get("therapy_only", True)),
                state=_state_arg(body.get("state")))
        except NppesFeedError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    # -- Medicaid / workers-comp floor schedules ---------------------------------------

    @app.get("/api/floors/status")
    def api_floors_status():
        from .floors import floor_status
        return floor_status(store)

    @app.post("/api/floors/import")
    def api_floors_import(file: UploadFile, kind: str = "medicaid", state: str = "",
                          label: str = "", year: str = "",
                          code_col: str = "", rate_col: str = ""):
        """Load one state Medicaid / workers'-comp schedule (a code,rate CSV)."""
        import uuid as _uuid

        from .floors import FloorImportError, floor_status, import_fee_schedule
        dest = (Path(cfg.store_dir) / "uploads" /
                f"{os.getpid()}-{_uuid.uuid4().hex[:8]}")
        dest.mkdir(parents=True, exist_ok=True)
        p = dest / (Path(file.filename or "schedule.csv").name or "schedule.csv")
        try:
            with open(p, "wb") as out:
                while chunk := file.file.read(1 << 20):
                    out.write(chunk)
            res = import_fee_schedule(
                store, p, kind=kind, state=state,
                label=label.strip() or None, year=year.strip() or None,
                code_col=code_col.strip() or None, rate_col=rate_col.strip() or None)
        except FloorImportError as e:
            raise HTTPException(422, str(e))
        except (OSError, ValueError) as e:
            raise HTTPException(422, f"could not read that file: {e}")
        finally:
            p.unlink(missing_ok=True)
            try:
                dest.rmdir()
            except OSError:
                pass
        return {**res, "status": floor_status(store)}

    @app.post("/api/floors/compare")
    def api_floors_compare(body: dict = Body(...)):
        """Commercial rates against the loaded Medicaid / WC schedules."""
        from .floors import floor_comparison
        try:
            return floor_comparison(
                store, body.get("market") or {},
                subject=str(body.get("subject") or "").strip() or None,
                state=_state_arg(body.get("state")),
                payer=str(body.get("payer") or "").strip() or None)
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.delete("/api/floors/{kind}/{state}")
    def api_floors_forget(kind: str, state: str, year: str = ""):
        from .floors import FloorImportError, floor_status, forget_schedule
        try:
            removed = forget_schedule(store, kind, state, year.strip() or None)
        except FloorImportError as e:      # an unreadable state is a refusal
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")
        return {**removed, "status": floor_status(store)}

    # -- prospect dossier + market report -------------------------------------------------

    @app.post("/api/report/dossier")
    def api_dossier(body: dict = Body(...)):
        """One branded page about a practice you have never spoken to."""
        from .dossier import build_prospect_dossier
        try:
            res = build_prospect_dossier(
                cfg, store, str(body.get("subject") or "").strip(),
                body.get("market") or {},
                radius_miles=_as_float(body.get("radius_miles"), 25.0))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")
        return HTMLResponse(res["html"])

    @app.post("/api/report/dossier.json")
    def api_dossier_json(body: dict = Body(...)):
        """The dossier's facts and omissions, without the rendered page."""
        from .dossier import build_prospect_dossier
        try:
            res = build_prospect_dossier(
                cfg, store, str(body.get("subject") or "").strip(),
                body.get("market") or {},
                radius_miles=_as_float(body.get("radius_miles"), 25.0))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")
        return {k: v for k, v in res.items() if k != "html"}

    @app.post("/api/report/market")
    def api_market_report(body: dict = Body(...)):
        """The metro-level story as one sellable branded document."""
        from .dossier import build_market_report
        try:
            res = build_market_report(
                cfg, store, state=_state_arg(body.get("state")),
                code=str(body.get("code") or "97110").strip(),
                zip_code=str(body.get("zip") or "").strip() or None,
                radius_miles=_as_float(body.get("radius_miles"), 25.0),
                market=body.get("market") or {})
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")
        return HTMLResponse(res["html"])

    # -- practice growth + service-line gaps ----------------------------------------------

    @app.get("/api/utilization/years")
    def api_utilization_years():
        from .utilization import available_years
        return {"years": available_years(store)}

    @app.post("/api/utilization/growth")
    def api_practice_growth(body: dict = Body(...)):
        """A practice's Medicare volume between two PUF years."""
        from .utilization import practice_growth
        try:
            return practice_growth(
                store, str(body.get("subject") or "").strip(),
                from_year=str(body.get("from_year") or "").strip() or None,
                to_year=str(body.get("to_year") or "").strip() or None)
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.post("/api/utilization/service-lines")
    def api_service_lines(body: dict = Body(...)):
        """Codes nearby practices bill Medicare that this one does not."""
        from .utilization import service_line_gaps
        try:
            return service_line_gaps(
                store, str(body.get("subject") or "").strip(),
                zip_code=str(body.get("zip") or "").strip() or None,
                radius_miles=_as_float(body.get("radius_miles"), 25.0),
                year=str(body.get("year") or "").strip() or None,
                min_peers=_bounded_int(body.get("min_peers"), 3, 1, 1000))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    # -- per-visit economics + volume-weighted position -----------------------------------

    @app.post("/api/economics/visit")
    def api_visit_economics(body: dict = Body(...)):
        """What one payer's visit is worth on this practice's own code mix."""
        from .economics import visit_economics
        try:
            return visit_economics(
                store, str(body.get("subject") or "").strip(),
                body.get("market") or {},
                units_per_visit=_as_float(body.get("units_per_visit"), None),
                year=str(body.get("year") or "").strip() or None)
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.post("/api/economics/weighted-position")
    def api_weighted_position(body: dict = Body(...)):
        """Market position weighted by what the practice actually bills."""
        from .economics import weighted_position
        try:
            return weighted_position(
                store, str(body.get("subject") or "").strip(),
                body.get("market") or {},
                volumes=body.get("volumes"),
                year=str(body.get("year") or "").strip() or None)
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    # -- walk-away leverage --------------------------------------------------------------

    @app.post("/api/leverage")
    def api_leverage(body: dict = Body(...)):
        """How many other local practices this payer publishes rates for."""
        from .leverage import network_leverage
        try:
            return network_leverage(
                store, str(body.get("subject") or "").strip(),
                str(body.get("payer") or "").strip(),
                body.get("market") or {},
                radius_miles=_as_float(body.get("radius_miles"), 15.0))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.post("/api/leverage/summary")
    def api_leverage_summary(body: dict = Body(...)):
        """Every payer this practice contracts with, thinnest network first."""
        from .leverage import leverage_summary
        try:
            return leverage_summary(
                store, str(body.get("subject") or "").strip(),
                body.get("market") or {},
                radius_miles=_as_float(body.get("radius_miles"), 15.0))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    # -- territory: sub-state geography, payer concentration, steal-share --------------

    @app.post("/api/territory/local")
    def api_territory_local(body: dict = Body(...)):
        """Median rate by CITY inside one state, for one code."""
        from .territory import local_rate_map
        try:
            return local_rate_map(store, str(body.get("code") or ""),
                                  body.get("market") or {},
                                  state=_state_arg(body.get("state")),
                                  limit=_bounded_int(body.get("limit"), 60, 1, 5000))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.post("/api/territory/radius")
    def api_territory_radius(body: dict = Body(...)):
        """One code's median rate by DISTANCE BAND from a ZIP."""
        from .territory import radius_rate_map
        try:
            return radius_rate_map(
                store, str(body.get("code") or ""), body.get("market") or {},
                zip_code=str(body.get("zip") or "").strip(),
                max_miles=_as_float(body.get("max_miles"), 100.0))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.post("/api/territory/concentration")
    def api_territory_concentration(body: dict = Body(...)):
        """How contracted relationships divide across payers (HHI)."""
        from .territory import payer_concentration
        try:
            return payer_concentration(store, body.get("market") or {},
                                       code=body.get("code") or None)
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.post("/api/territory/steal-share")
    def api_territory_steal(body: dict = Body(...)):
        """Referral sources feeding a client's competitors, not the client."""
        from .medicare import MedicareImportError
        from .territory import steal_share
        try:
            return steal_share(
                store, str(body.get("subject") or "").strip(),
                limit=_bounded_int(body.get("limit"), 50, 1, 5000),
                min_patients=_bounded_int(body.get("min_patients"), 11, 0, 10**9),
                dataset_id=str(body.get("dataset_id") or "").strip() or None)
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except MedicareImportError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    # -- data quality: what kind of rate, how old, does size explain it ----------------

    @app.post("/api/quality/types")
    def api_quality_types(body: dict = Body(...)):
        """Contracted vs payer-derived composition of the scoped market."""
        from .quality import rate_type_mix
        try:
            return rate_type_mix(store, body.get("market") or {})
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.post("/api/quality/posture")
    def api_quality_posture(body: dict = Body(...)):
        """Per payer: does it negotiate, or run one schedule for everyone?"""
        from .quality import payer_posture
        try:
            return payer_posture(store, body.get("market") or {},
                                 min_codes=_bounded_int(body.get("min_codes"), 3, 1, 1000))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.get("/api/quality/freshness")
    def api_quality_freshness(stale_days: int = 270):
        from .quality import payer_freshness
        return payer_freshness(store, stale_days=max(1, min(int(stale_days), 3650)))

    @app.post("/api/quality/size")
    def api_quality_size(body: dict = Body(...)):
        """Whether bigger practices are paid better in this market."""
        from .quality import size_premium
        try:
            return size_premium(store, body.get("market") or {},
                                subject=str(body.get("subject") or "").strip() or None,
                                min_practices=_bounded_int(body.get("min_practices"), 5, 1, 1000))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.post("/api/quality/subject-types")
    def api_quality_subject_types(body: dict = Body(...)):
        from .quality import code_type_flag
        try:
            return code_type_flag(store, body.get("market") or {},
                                  str(body.get("subject") or "").strip())
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    # -- hospital price transparency ---------------------------------------------------

    @app.get("/api/hospital/status")
    def api_hospital_status():
        from .hospital import hospital_status
        return hospital_status(store)

    @app.post("/api/hospital/import")
    def api_hospital_import(file: UploadFile, state: str = "", city: str = "",
                            hospital_name: str = ""):
        """Load one hospital's CMS standard-charges file (CSV or JSON)."""
        import uuid as _uuid

        from .hospital import HospitalImportError, hospital_status, import_hospital_file
        # A per-upload DIRECTORY, not a suffixed filename: two uploads of the
        # same name must not truncate each other (audit F7), and the importer
        # records the file's own name as provenance, so it has to survive.
        dest = (Path(cfg.store_dir) / "uploads" /
                f"{os.getpid()}-{_uuid.uuid4().hex[:8]}")
        dest.mkdir(parents=True, exist_ok=True)
        safe = Path(file.filename or "hospital.csv").name or "hospital.csv"
        p = dest / safe
        try:
            with open(p, "wb") as out:
                while chunk := file.file.read(1 << 20):
                    out.write(chunk)
            res = import_hospital_file(
                store, p, hospital_name=hospital_name.strip() or None,
                state=state.strip() or None, city=city.strip() or None, cfg=cfg)
        except HospitalImportError as e:
            raise HTTPException(422, str(e))
        except (OSError, ValueError) as e:
            raise HTTPException(422, f"could not read that file: {e}")
        finally:
            p.unlink(missing_ok=True)
            try:
                dest.rmdir()
            except OSError:
                pass
        return {**res, "status": hospital_status(store)}

    @app.post("/api/hospital/parity")
    def api_hospital_parity(body: dict = Body(...)):
        """Practice rates vs hospital outpatient rates for the same payer+code."""
        from .hospital import hospital_parity
        try:
            return hospital_parity(
                store, body.get("market") or {},
                payer=str(body.get("payer") or "").strip() or None,
                state=_state_arg(body.get("state")),
                subject=str(body.get("subject") or "").strip() or None,
                limit=_bounded_int(body.get("limit"), 200, 1, 5000))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.post("/api/hospital/cash")
    def api_hospital_cash(body: dict = Body(...)):
        """What local hospitals charge self-pay patients for the same codes."""
        from .hospital import cash_anchors
        try:
            return cash_anchors(
                store, state=_state_arg(body.get("state")),
                codes=body.get("codes") or None,
                limit=_bounded_int(body.get("limit"), 200, 1, 2000))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.delete("/api/hospital/{name}")
    def api_hospital_forget(name: str):
        from .hospital import forget_hospital, hospital_status
        return {**forget_hospital(store, name), "status": hospital_status(store)}

    # -- underpayment check ------------------------------------------------------------

    @app.post("/api/remits/check")
    def api_remit_check(body: dict = Body(...)):
        from .remits import check_underpayments
        try:
            return check_underpayments(
                store, str(body.get("subject", "")), str(body.get("text", "")),
                body.get("market") or {"month": "latest"},
                basis=str(body.get("basis") or "allowed"))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.post("/api/remits/check.csv", response_class=PlainTextResponse)
    def api_remit_check_csv(body: dict = Body(...)):
        from .remits import check_underpayments, underpayment_csv
        try:
            return underpayment_csv(check_underpayments(
                store, str(body.get("subject", "")), str(body.get("text", "")),
                body.get("market") or {"month": "latest"},
                basis=str(body.get("basis") or "allowed")))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    # -- engagement baselines / win tracking -------------------------------------------

    @app.get("/api/engagements")
    def api_engagements(subject: str = ""):
        from .engagements import list_baselines
        return {"baselines": list_baselines(store, subject or None)}

    @app.post("/api/engagements/baseline")
    def api_save_baseline(body: dict = Body(...)):
        from .engagements import save_baseline
        try:
            return save_baseline(store, str(body.get("subject", "")),
                                 body.get("market") or {"month": "latest"},
                                 label=str(body.get("label") or "engagement start"))
        except BenchmarkError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/engagements/delete")
    def api_delete_baseline(body: dict = Body(...)):
        from .engagements import delete_baseline, list_baselines
        delete_baseline(store, str(body.get("subject", "")),
                        str(body.get("label") or "engagement start"))
        return {"baselines": list_baselines(store)}

    @app.post("/api/engagements/compare")
    def api_compare_baseline(body: dict = Body(...)):
        from .engagements import compare_to_baseline
        try:
            return compare_to_baseline(
                store, str(body.get("subject", "")),
                str(body.get("label") or "engagement start"),
                body.get("market") or None, body.get("volumes") or None)
        except BenchmarkError as e:
            raise HTTPException(422, str(e))

    # -- monthly client packets --------------------------------------------------------

    packet_job: dict = {"state": "idle", "message": "", "result": None}
    packet_lock = threading.Lock()

    def _run_packets() -> None:
        from .packets import build_all_packets
        try:
            def progress(msg):
                with packet_lock:
                    packet_job["message"] = msg
            res = build_all_packets(cfg, store, getattr(cfg, "packets_dir", None),
                                    progress=progress)
            with packet_lock:
                packet_job.update({"state": "done", "result": res,
                                   "message": f"{res['clients']} client packet(s) "
                                              f"written to {res['dir']}"})
        except Exception as e:  # noqa: BLE001 — report, never die silently
            log.warning("packet build failed: %s", e)
            with packet_lock:
                packet_job.update({"state": "done", "result": None,
                                   "message": f"could not build packets: {e}"})

    @app.get("/api/packets")
    def api_packets_status():
        with packet_lock:
            return dict(packet_job)

    @app.post("/api/packets/build")
    def api_packets_build():
        with packet_lock:
            if packet_job["state"] == "running":
                raise HTTPException(409, "a packet build is already running")
            packet_job.update({"state": "running", "result": None,
                               "message": "starting…"})
        try:
            threading.Thread(target=_bg_safe, args=(_run_packets,),
                             name="mrfx-packets", daemon=True).start()
        except Exception:
            # release the slot — a failed thread start must not wedge the
            # button as "already running" until the server restarts
            with packet_lock:
                packet_job.update({"state": "idle",
                                   "message": "could not start the build"})
            raise
        return {"started": True}

    @app.get("/api/clients/digest")
    def clients_digest():
        """The Monday review: per saved client, what moved. Composes the same
        functions the Changes/Benchmark/Medicare tabs run, so a digest cell can
        never disagree with its drill-down."""
        from .clients import client_digest
        return client_digest(store)

    @app.post("/api/medicare/leaders")
    def api_medicare_leaders(body: dict = Body(...)):
        """Practices ranked by shared Medicare patients received, optionally
        ZIP+radius clipped. in_store rows click through to the entity drawer
        — every payer rate the MRF files priced them at."""
        from .medicare import MedicareImportError, referral_leaders
        try:
            return referral_leaders(
                store,
                zip_code=str(body.get("zip") or "").strip() or None,
                radius_miles=float(body["radius_miles"]) if body.get("radius_miles") else None,
                limit=_bounded_int(body.get("limit"), 50, 1, 5000),
                dataset_id=str(body.get("dataset_id") or "").strip() or None,
                therapy_only=bool(body.get("therapy_only", True)))
        except MedicareImportError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

    @app.get("/api/medicare/status")
    def api_medicare_status():
        """What Medicare data has been imported (`mrfx medicare`) — the
        dashboard's Medicare tab decides between data and instructions on this."""
        from .medicare import medicare_status
        return medicare_status(store)

    @app.post("/api/medicare/eligibility")
    def api_medicare_eligibility(body: dict = Body(...)):
        """Batch Order & Referring check: any pasted text in, one row per
        VALID NPI found (10 digits + NPPES prefix + Luhn check digit — pasted
        emails and referral lists are full of phone numbers, and reporting a
        phone number as 'not on the list' would be a false alarm). Absence
        from the roster is a real answer, not an error — expected for
        therapists/orgs, a denial risk for referrers."""
        from .medicare import is_valid_npi, npi_eligibility
        found = list(dict.fromkeys(re.findall(r"\b\d{10}\b", str(body.get("text", "")))))
        npis = [n for n in found if is_valid_npi(n)]
        if not npis:
            raise HTTPException(422, "no valid NPIs found in the pasted text"
                                + (f" ({len(found)} ten-digit numbers were "
                                   "skipped — they fail the NPI check digit, "
                                   "e.g. phone numbers)" if found else ""))
        if len(npis) > 2000:
            raise HTTPException(422, f"{len(npis):,} NPIs is too many for one "
                                "check — paste up to 2,000 at a time")
        rows = npi_eligibility(store, npis)
        if not rows:
            raise HTTPException(422, "no Order & Referring roster is loaded — "
                                "import one first (mrfx medicare --eligibility …)")
        return {"rows": rows, "checked": len(npis),
                "ignored_non_npi": len(found) - len(npis),
                "on_list": sum(1 for r in rows if r["on_list"])}

    @app.post("/api/medicare/org")
    def api_medicare_org(body: dict = Body(...)):
        """One practice's Medicare view: its NPIs' eligibility, plus who shares
        patients into it and where it shares onward — each referral row carrying
        whether that provider is still Part B order/refer-eligible."""
        from .medicare import (MedicareImportError, medicare_status,
                               npi_eligibility, org_referrals, recent_losses,
                               taxonomy_label)
        from .benchmark import resolve_subject_tins
        subject = str(body.get("subject", "")).strip()
        if not subject:
            raise HTTPException(422, "pick a subject practice first")
        year = str(body.get("year") or "").strip() or None
        ds_id = str(body.get("dataset_id") or "").strip() or None
        try:
            limit = min(max(_bounded_int(body.get("limit"), 100, 1, 5000), 1), 500)
        except (TypeError, ValueError):
            raise HTTPException(422, "limit must be a number")
        # resolve_subject_tins never returns empty (a raw id falls through as
        # itself), so "did we match a practice" is decided by whether any rate
        # rows carry NPIs for it — the NPI is the join this whole tab runs on.
        tins = resolve_subject_tins(store, subject)
        with store.connect() as con:
            npis = [r[0] for r in con.execute(
                "SELECT DISTINCT npi FROM rates WHERE tin_value IN "
                "(SELECT unnest(?::VARCHAR[])) AND npi IS NOT NULL ORDER BY npi",
                [tins]).fetchall()]
            name = (con.execute(
                "SELECT any_value(display_name) FROM tin_directory WHERE tin_value "
                "IN (SELECT unnest(?::VARCHAR[]))", [tins]).fetchone() or [None])[0]
        if not npis:
            raise HTTPException(
                422, f"no practice with NPIs matched '{subject}'. The Medicare "
                "layers join on NPI, and no rate rows carry NPIs for that "
                "subject — check the spelling, or pick it from the suggestions.")

        def with_eligibility(ref):
            row_npis = [r["npi"] for r in ref["rows"]]
            by = {e["npi"]: e for e in npi_eligibility(store, row_npis)}
            losses = recent_losses(store, row_npis)
            for r in ref["rows"]:
                e = by.get(r["npi"]) or {}
                r["on_orf"] = bool(e.get("on_list"))
                r["partb"] = e.get("partb")
                r["specialty"] = taxonomy_label(r.get("taxonomy"))
                # 'removed' | 'lost_partb' when this provider lost order/refer
                # standing between the two most recent roster snapshots — the
                # alert a consultant acts on the day they see it
                r["recent_change"] = losses.get(r["npi"])
            return ref

        try:
            refs_in = with_eligibility(org_referrals(store, npis, "in", limit, year, ds_id))
            refs_out = with_eligibility(org_referrals(store, npis, "out", limit, year, ds_id))
        except MedicareImportError as e:
            raise HTTPException(422, str(e))
        return {
            "display_name": name or subject,
            "tins": [mask_tin(t) for t in tins],
            "npis": npis,
            "eligibility": npi_eligibility(store, npis),
            "referrals_in": refs_in,
            "referrals_out": refs_out,
            "status": medicare_status(store),
        }

    def _outreach_parts(request: Request):
        from .outreach import build_outreach_rows, outreach_csv

        qp = _qp(request)
        grain = grain_of(qp, cfg, store)
        if grain == "npi":
            grain = "tin"  # outreach is entity-level by definition
        fs = _fs(qp)
        headers, rows = build_outreach_rows(
            store, rel_sql(grain, fs), fs.params, fs.described.get("codes")
        )
        method = methodology_text(cfg, store, grain, fs, "display_name", "asc", "outreach")
        return outreach_csv(headers, rows), method

    @app.get("/api/export/outreach.csv")
    def export_outreach(request: Request, background: BackgroundTasks):
        """One row per entity (org name + geography + per-code merge fields),
        for cross-referencing a contact list / Brevo mail merge. DATA ONLY —
        no comment header lines that would break a mail-merge import; the
        dashboard button uses outreach.zip, which carries the methodology."""
        csv_text, _method = _outreach_parts(request)
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
        out = _mktemp(".csv")
        out.write_text(csv_text, encoding="utf-8")
        background.add_task(out.unlink, missing_ok=True)
        return FileResponse(out, filename=f"mrfx_outreach_{stamp}.csv", media_type="text/csv")

    @app.get("/api/export/outreach.zip")
    def export_outreach_zip(request: Request, background: BackgroundTasks):
        """Outreach CSV + its methodology sidecar in one ZIP — the honesty
        contract says every delivered export carries its methodology, and a
        bare CSV download had nowhere to put it (comment lines would break
        the mail-merge import the CSV exists for)."""
        csv_text, method = _outreach_parts(request)
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
        out = _mktemp(".zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"mrfx_outreach_{stamp}.csv", csv_text.encode("utf-8"))
            z.writestr(f"mrfx_outreach_{stamp}_methodology.txt", method)
        background.add_task(out.unlink, missing_ok=True)
        return FileResponse(out, filename=f"mrfx_outreach_{stamp}.zip", media_type="application/zip")

    # -- benchmarks (§7B) --------------------------------------------------------------

    @app.get("/api/benchmark/subjects")
    def benchmark_subjects(q: str = "", limit: int = Query(50, ge=1, le=500)):
        """Subject typeahead. Returns at most `limit` matches so the picker stays
        fast on a national store (hundreds of thousands of TINs) — the whole
        directory used to be serialized on every tab open. `q` filters by org
        name or TIN; empty `q` returns the largest practices as a starter set."""
        emap = store.entity_map()
        ql = q.strip().lower()
        like = f"%{ql}%"
        with store.connect() as con:
            # auto-grouped orgs: an NPPES org name shared by >1 TIN is ONE
            # practice on the Explorer's entity grain, and resolve_subject_tins
            # expands the name to all its TINs — but without this it had no
            # single picker option, so a multi-location org got benchmarked as a
            # lone fragment TIN. Exclude the 'TIN …' no-name fallback labels.
            auto_sql = ("SELECT display_name FROM tin_directory "
                        "WHERE display_name NOT LIKE 'TIN %' "
                        + ("AND lower(display_name) LIKE ? " if ql else "")
                        # the biggest multi-TIN practices first; a UNIQUE
                        # order (invariant 7) — without it LIMIT kept a
                        # different 50 on each call
                        + "GROUP BY display_name HAVING count(*) > 1 "
                        "ORDER BY count(*) DESC, display_name LIMIT ?")
            auto = [r[0] for r in con.execute(
                auto_sql, ([like] if ql else []) + [limit]).fetchall()]
            if ql:
                tins = _dicts(con.execute(
                    "SELECT tin_value, display_name, npi_count, states FROM tin_directory "
                    "WHERE lower(display_name) LIKE ? OR tin_value LIKE ? "
                    "ORDER BY npi_count DESC NULLS LAST, display_name, tin_value LIMIT ?",
                    [like, f"{ql}%", limit]))
            else:
                tins = _dicts(con.execute(
                    "SELECT tin_value, display_name, npi_count, states FROM tin_directory "
                    "ORDER BY npi_count DESC NULLS LAST, display_name, tin_value LIMIT ?",
                    [limit]))
        entities = sorted(set(emap.values()) | set(auto))
        if ql:
            entities = [e for e in entities if ql in e.lower()]
        entities = entities[:limit]
        # SSN-pattern TINs are masked on EVERY surface — a subject picker that
        # displays the raw nine digits would be the one exception. They can't
        # be selectable anyway (the mask can't round-trip to a lookup key), so
        # they are excluded here rather than shown raw in the dropdown.
        tins = [t for t in tins if mask_tin(t["tin_value"]) == t["tin_value"]]
        for t in tins:
            t["entity"] = emap.get(t["tin_value"])
            t["tin_value_masked"] = mask_tin(t["tin_value"])
        return {"entities": entities, "tins": tins}

    @app.post("/api/benchmark/market")
    def benchmark_market(body: dict = Body(...)):
        try:
            return compute_benchmark(store, str(body.get("subject", "")), body.get("market") or {})
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body (int()/float()/None) — a client input error, so 422.
            raise HTTPException(422, str(e))

    @app.post("/api/benchmark/opportunity")
    def benchmark_opportunity(body: dict = Body(...)):
        try:
            bench = compute_benchmark(store, str(body.get("subject", "")), body.get("market") or {})
            volumes = clean_volumes(body.get("volumes"))
            opp = compute_opportunity(bench, volumes,
                                      int(body.get("conservative_percentile", 40)))
            return {"benchmark": bench, "opportunity": opp}
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body (int()/float()/None) — a client input error, so 422.
            raise HTTPException(422, str(e))

    @app.post("/api/report/pitch", response_class=HTMLResponse)
    def pitch_report(body: dict = Body(...)):
        try:
            bench = compute_benchmark(store, str(body.get("subject", "")), body.get("market") or {})
            opp = None
            volumes = clean_volumes(body.get("volumes"))
            if volumes:
                opp = compute_opportunity(bench, volumes,
                                          int(body.get("conservative_percentile", 40)))
            return HTMLResponse(render_pitch_report(cfg, store, bench, opp))
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body (int()/float()/None) — a client input error, so 422.
            raise HTTPException(422, str(e))

    @app.post("/api/report/negotiation", response_class=HTMLResponse)
    def negotiation_report(body: dict = Body(...)):
        try:
            volumes = clean_volumes(body.get("volumes"))
            neg = compute_payer_negotiation(
                store, str(body.get("subject", "")), body.get("market") or {},
                volumes=volumes or None,
                conservative_percentile=int(body.get("conservative_percentile", 40)),
            )
            return HTMLResponse(render_negotiation_report(cfg, store, neg))
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body (int()/float()/None) — a client input error, so 422.
            raise HTTPException(422, str(e))

    # payer-comparison workspace (Negotiate tab): one payer, named comparables
    @app.post("/api/negotiate/compare")
    def negotiate_compare(body: dict = Body(...)):
        try:
            return compute_payer_comparison(
                store, str(body.get("subject", "")), str(body.get("payer", "")),
                body.get("market") or {},
                comparables=_comparables_arg(body.get("comparables")) or [],
            )
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/report/payer-compare", response_class=HTMLResponse)
    def payer_compare_report(body: dict = Body(...)):
        try:
            comp = compute_payer_comparison(
                store, str(body.get("subject", "")), str(body.get("payer", "")),
                body.get("market") or {},
                comparables=_comparables_arg(body.get("comparables")) or [],
            )
            return HTMLResponse(render_payer_compare_report(cfg, store, comp))
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    # fee schedule / payer scorecard (§7C)
    @app.post("/api/negotiate/proposal")
    def negotiate_proposal(body: dict = Body(...)):
        from .benchmark import compute_rate_proposal
        try:
            return compute_rate_proposal(
                store, str(body.get("subject", "")), str(body.get("payer", "")),
                body.get("market") or {}, body.get("target") or {},
                volumes=body.get("volumes") or None,
                comparables=_comparables_arg(body.get("comparables")))
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/report/proposal", response_class=HTMLResponse)
    def report_proposal(body: dict = Body(...)):
        from .benchmark import compute_rate_proposal, render_proposal_report
        try:
            prop = compute_rate_proposal(
                store, str(body.get("subject", "")), str(body.get("payer", "")),
                body.get("market") or {}, body.get("target") or {},
                volumes=body.get("volumes") or None,
                comparables=_comparables_arg(body.get("comparables")))
            return render_proposal_report(cfg, store, prop)
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/schedule/fee")
    def schedule_fee(body: dict = Body(...)):
        try:
            fs = compute_fee_schedule(store, str(body.get("subject", "")), body.get("market") or {})
            return {"fee_schedule": fs, "scorecard": payer_scorecard(fs)}
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body — a client input error (422), never a 500.
            raise HTTPException(422, str(e))

    @app.get("/api/schedule/visit/presets")
    def schedule_visit_presets():
        """The standard-visit baskets the Rate card's visit calculator offers."""
        from .visit import DEFAULT_PRESET, UNITS_RULE, presets_public
        return {"presets": presets_public(), "default": DEFAULT_PRESET, "units_rule": UNITS_RULE}

    @app.post("/api/schedule/visit")
    def schedule_visit(body: dict = Body(...)):
        """One standard therapy visit, priced per payer at the practice's own
        rates — units by the CMS 8-minute rule (mrfx/visit.py)."""
        from .visit import VisitError, price_visit
        try:
            return price_visit(store, str(body.get("subject", "")), body.get("market") or {},
                               body.get("lines"), body.get("preset"))
        except (VisitError, BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/report/ratecard", response_class=HTMLResponse)
    def ratecard_report(body: dict = Body(...)):
        try:
            fs = compute_fee_schedule(store, str(body.get("subject", "")), body.get("market") or {})
            return HTMLResponse(render_rate_card(cfg, store, fs, payer_scorecard(fs)))
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body — a client input error (422), never a 500.
            raise HTTPException(422, str(e))

    @app.post("/api/schedule/fee.csv", response_class=PlainTextResponse)
    def schedule_fee_csv(body: dict = Body(...)):
        try:
            fs = compute_fee_schedule(store, str(body.get("subject", "")), body.get("market") or {})
            csv_text = fee_schedule_csv(fs, payer_scorecard(fs), store)
            return PlainTextResponse("﻿" + csv_text, headers={  # BOM for Excel
                "Content-Disposition": 'attachment; filename="rate_card.csv"'})
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body — a client input error (422), never a 500.
            raise HTTPException(422, str(e))

    # underpaid-practice leads (§7D)
    @app.post("/api/leads")
    def leads(body: dict = Body(...)):
        try:
            return compute_leads(
                store, body.get("market") or {},
                threshold_percentile=_as_int(body.get("threshold_percentile"), 25),
                min_codes=_bounded_int(body.get("min_codes"), 3, 1, 1000),
                limit=_bounded_int(body.get("limit"), 100, 1, 5000),
                exclude_subject=(str(body["exclude_subject"]) if body.get("exclude_subject") else None),
            )
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body — a client input error (422), never a 500.
            raise HTTPException(422, str(e))

    @app.post("/api/leads.csv", response_class=PlainTextResponse)
    def leads_export(body: dict = Body(...)):
        try:
            result = compute_leads(
                store, body.get("market") or {},
                threshold_percentile=_as_int(body.get("threshold_percentile"), 25),
                min_codes=_bounded_int(body.get("min_codes"), 3, 1, 1000),
                limit=_bounded_int(body.get("limit"), 1000, 1, 5000),
                exclude_subject=(str(body["exclude_subject"]) if body.get("exclude_subject") else None),
            )
            return PlainTextResponse("﻿" + leads_csv(store, result), headers={
                "Content-Disposition": 'attachment; filename="leads.csv"'})
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body — a client input error (422), never a 500.
            raise HTTPException(422, str(e))

    # rate-change monitoring (§7E)
    @app.post("/api/changes")
    def changes(body: dict = Body(...)):
        try:
            return compute_rate_changes(
                store, body.get("market") or {},
                subject=(str(body["subject"]) if body.get("subject") else None),
                min_pct=_as_float(body.get("min_pct"), None),
            )
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body — a client input error (422), never a 500.
            raise HTTPException(422, str(e))

    @app.post("/api/changes.csv", response_class=PlainTextResponse)
    def changes_export(body: dict = Body(...)):
        try:
            result = compute_rate_changes(
                store, body.get("market") or {},
                subject=(str(body["subject"]) if body.get("subject") else None),
                min_pct=_as_float(body.get("min_pct"), None),
            )
            return PlainTextResponse("﻿" + rate_changes_csv(result), headers={
                "Content-Disposition": 'attachment; filename="rate_changes.csv"'})
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body — a client input error (422), never a 500.
            raise HTTPException(422, str(e))

    # market intelligence for one code (Markets tab): geography, negotiability,
    # % of Medicare, assistant/telehealth differential — all share the house basis
    @app.post("/api/market/geography")
    def market_geography(body: dict = Body(...)):
        try:
            return geographic_rates(store, body.get("code"), body.get("market") or {},
                                    limit=_bounded_int(body.get("limit"), 60, 1, 5000))
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/market/negotiability")
    def market_negotiability(body: dict = Body(...)):
        try:
            return negotiability(store, body.get("code"), body.get("market") or {})
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/market/medicare")
    def market_medicare(body: dict = Body(...)):
        try:
            return medicare_index(store, body.get("code"), body.get("market") or {})
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/market/differential")
    def market_differential(body: dict = Body(...)):
        try:
            return assistant_pos_diff(store, body.get("code"), body.get("market") or {})
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/contract-gaps")
    def contract_gaps_ep(body: dict = Body(...)):
        try:
            return contract_gaps(
                store, str(body.get("subject") or ""), body.get("market") or {},
                min_peers=_as_int(body.get("min_peers"), 5),
                limit=_bounded_int(body.get("limit"), 100, 1, 5000))
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/leaderboard")
    def leaderboard_ep(body: dict = Body(...)):
        try:
            return compute_leaderboard(
                store, body.get("market") or {},
                min_codes=_bounded_int(body.get("min_codes"), 3, 1, 1000),
                limit=_bounded_int(body.get("limit"), 50, 1, 5000),
                sort=str(body.get("sort") or "size"))
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/trajectory")
    def trajectory_ep(body: dict = Body(...)):
        from .inflation import InflationError
        try:
            return compute_payer_trajectory(
                store, body.get("market") or {},
                # an inflation ASSUMPTION is opt-in and echoed back as one;
                # 0 is a meaningful value, so don't treat it as absent
                assume_inflation_pct=_as_float(body.get("assume_inflation_pct"), None))
        except (BenchmarkError, InflationError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.get("/api/inflation/status")
    def api_inflation_status():
        """Which years the real-terms basis covers, and how to extend it."""
        from .inflation import index_status
        return index_status(store)

    @app.post("/api/inflation")
    def api_inflation_save(body: dict = Body(...)):
        """Record published index values (year, value). Never computed here —
        the user reads them off the BLS release."""
        from .inflation import InflationError, index_status, save_index_values
        rows = body.get("values")
        if isinstance(rows, dict):
            rows = list(rows.items())
        if not isinstance(rows, list) or not rows:
            raise HTTPException(422, 'send {"values": [[2025, 322.1], ...]}')
        try:
            n = save_index_values(
                store, [tuple(r) if isinstance(r, (list, tuple)) else (r,) for r in rows],
                index_name=str(body.get("index_name") or "").strip() or None,
                source=str(body.get("source") or "").strip() or None)
        except InflationError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")
        return {"saved": n, **index_status(store)}

    @app.post("/api/roster")
    def roster_ep(body: dict = Body(...)):
        try:
            return compute_payer_roster(
                store, str(body.get("payer") or ""), body.get("market") or {},
                min_codes=_bounded_int(body.get("min_codes"), 1, 1, 1000),
                limit=_bounded_int(body.get("limit"), 500, 1, 5000),
                sort=str(body.get("sort") or "name"))
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/roster.csv", response_class=PlainTextResponse)
    def roster_csv_ep(body: dict = Body(...)):
        try:
            result = compute_payer_roster(
                store, str(body.get("payer") or ""), body.get("market") or {},
                min_codes=_bounded_int(body.get("min_codes"), 1, 1, 1000),
                limit=_bounded_int(body.get("limit"), 2000, 1, 5000),
                sort=str(body.get("sort") or "name"))
            return PlainTextResponse("﻿" + payer_roster_csv(result), headers={
                "Content-Disposition": 'attachment; filename="payer_roster.csv"'})
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    # peer sets
    @app.get("/api/peersets")
    def peersets():
        return {"peer_sets": store.peer_sets()}

    @app.post("/api/peersets")
    def peersets_save(body: dict = Body(...)):
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(422, "peer set name required")
        store.save_peer_set(name, {
            "mode": body.get("mode", "curated"),
            "tins": [str(t).replace("-", "") for t in (body.get("tins") or [])],
            "notes": body.get("notes", ""),
        })
        return {"peer_sets": store.peer_sets()}

    @app.delete("/api/peersets/{name}")
    def peersets_delete(name: str):
        store.delete_peer_set(name)
        return {"peer_sets": store.peer_sets()}

    @app.get("/api/overview")
    def overview(request: Request):
        from .overview import market_overview
        from .states import state_code
        # canonicalise BEFORE the cache key, so "MO", "mo" and "Missouri" share
        # one entry instead of computing the same whole-spine scan three times
        try:
            state = state_code((_qp(request).get("state") or "").strip() or None)
        except ValueError as e:      # refuse next to the input, like every other route
            raise HTTPException(422, str(e))
        key = (state, store.data_generation)
        hit = _overview_cache.get(key)
        if hit is not None:
            return hit
        # single-flight: the overview does several whole-spine GROUP BYs, so a
        # cold cache (esp. right after a rebuild bumps data_generation for all
        # polling tabs at once) would otherwise fan out N concurrent scans.
        if not _overview_lock.acquire(blocking=False):
            raise HTTPException(503, "overview is building — it will load on the next refresh")
        try:
            hit = _overview_cache.get(key)  # re-check under the lock
            if hit is not None:
                return hit
            result = market_overview(store, state)
            if len(_overview_cache) > 64:
                _overview_cache.clear()
            _overview_cache[key] = result
            return result
        finally:
            _overview_lock.release()

    # MPFS
    @app.get("/api/mpfs/status")
    def mpfs_status():
        return {"loaded": store.mpfs_loaded()}

    @app.post("/api/mpfs/upload")
    def mpfs_upload(file: UploadFile, cf: str = "", state: str = "",
                    locality: str = ""):
        """Two inputs accepted: the official CMS RVU bundle (PPRRVU+GPCI zip,
        needs the published conversion factor + a state to pick the GPCI
        locality) or the simple code,non_facility_rate CSV, unchanged."""
        from .mpfs import MpfsImportError, import_official, looks_official
        data = file.file.read()
        fname = file.filename or "upload.csv"
        if looks_official(data, fname):
            if not cf.strip():
                raise HTTPException(422, "official CMS RVU files need the "
                                    "published conversion factor — enter it in "
                                    "the CF box (e.g. 32.35) and reload.")
            if not state.strip():
                raise HTTPException(422, "official CMS RVU files need a state "
                                    "to pick the GPCI locality — enter it in "
                                    "the State box (e.g. MO) and reload.")
            try:
                r = import_official(store, data, fname,
                                    conversion_factor=float(cf),
                                    state=state, locality_name=locality.strip() or None)
            except MpfsImportError as e:
                raise HTTPException(422, str(e))
            except ValueError:
                raise HTTPException(422, f"CF must be a number, got {cf!r}")
            return {"loaded": store.mpfs_loaded(), "rows": r["rows"],
                    "locality": r["locality"]}
        try:
            n = _load_mpfs_csv(store, data, fname)
        except (ValueError, KeyError) as e:
            raise HTTPException(422, f"MPFS CSV must have code,locality,non_facility_rate columns: {e}")
        return {"loaded": store.mpfs_loaded(), "rows": n}

    # -- sources / registry ------------------------------------------------------------

    @app.get("/api/sources")
    def sources(state: str | None = None):
        return {
            "states": registry.states(),
            "national": registry.national(),
            "state": registry.state(state) if state else [],
            "elevance_note": registry.elevance_note(),
        }

    @app.post("/api/sources/override")
    def sources_override(body: dict = Body(...)):
        key, url = body.get("key"), (body.get("mrf_url") or "").strip()
        if not key or not url:
            raise HTTPException(422, "key and mrf_url required")
        if not url.startswith("https://"):
            raise HTTPException(422, "mrf_url must be https://")
        return registry.save_override(key, url)

    # -- misc -------------------------------------------------------------------------------

    @app.get("/api/stats")
    def stats():
        # the whole-store counts are cached + single-flight (store_stats): this
        # endpoint is on the dashboard's 15s poll, and an uncached
        # count(DISTINCT) over 86M raw rows stacked concurrent HDD scans that
        # starved the parser workers of the disk (the recurring "stall")
        counts = store.store_stats()
        with store.connect() as con:
            files_done = con.execute("SELECT count(*) FROM files WHERE status = 'done'").fetchone()[0]
            attention = _dicts(con.execute(
                """
                SELECT filename, status, ref_groups_skipped, error FROM files
                WHERE status IN ('failed', 'quarantined', 'pending_confirmation')
                   OR ref_groups_skipped > 0
                """
            ))
        return {"rates": counts["rates"], "tins": counts["tins"], "payers": counts["payers"],
                "files_done": files_done, "attention": attention,
                "enrichment": store.enrichment_progress(),
                "enrichment_mode": cfg.enrichment.mode,
                # True when the fast local NPPES bulk file is actually in use, so
                # the dashboard only nudges toward it when we're on the slow API.
                "enrichment_bulk_active": use_bulk_enrichment(cfg),
                "default_grain": grain_of({}, cfg, store)}

    @app.get("/api/states")
    def states():
        """Distinct states that actually have enriched data — populates the
        state filter so an empty pick reads as 'no data yet', not a broken box."""
        return {"states": store.available_states()}

    @app.get("/api/payers")
    def payers():
        # populate the payer-filter dropdown. Read DISTINCT payer from the
        # MATERIALIZED spine (rates_by_tin_tbl) when it exists: one small native
        # table with a low-cardinality payer column, vs. a DISTINCT scan across
        # every raw parquet part in the store (an uncached full-store scan on the
        # interactive dashboard-load path). Fall back to raw `rates` only before
        # the first rollup exists, so a brand-new store still lists its payers.
        with store.connect() as con:
            has_tbl = con.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name = 'rates_by_tin_tbl'").fetchone()[0]
            src = "rates_by_tin_tbl" if has_tbl else "rates"
            rows = con.execute(
                f"SELECT DISTINCT payer FROM {src} ORDER BY payer").fetchall()
        return {"payers": [r[0] for r in rows]}

    @app.get("/api/catalog")
    def catalog():
        return catalog_json()

    @app.get("/api/debug/stacks", response_class=PlainTextResponse)
    async def debug_stacks():
        """Live stack of every thread in the server process — the decisive
        stall diagnostic. When the app burns CPU with nothing in the log, this
        names the exact line every thread sits on (lock waits included).

        Deliberately `async` — the ONE allowed coroutine endpoint. Sync
        endpoints borrow a threadpool worker; if a stall has every worker
        wedged, a sync diagnostic queues behind the very jam it exists to
        diagnose and never answers. Running on the event loop needs no
        worker, and it's safe there: a pure in-memory frame walk, a few
        milliseconds, no store or lock access. Localhost-only app; text is
        for pasting into a bug report."""
        return PlainTextResponse(thread_stacks_text())

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("API error on %s", request.url.path)
        # DuckDB's memory-limit guard is not a bug — it means this view needs
        # more working memory than the cap. Translate the raw "failed to pin
        # block …" text into something a non-technical user can act on.
        if "OutOfMemory" in type(exc).__name__ or "Out of Memory" in str(exc):
            return JSONResponse(status_code=503, content={"error": (
                f"This view needs more memory than the current limit "
                f"({store._memory_limit_gb} GB). Narrow it with a filter (payer, "
                f"code, or state), or raise duckdb_memory_gb in config/mrfx.yaml "
                f"if your machine has spare RAM, then restart.")})
        if isinstance(exc, ValueError):
            # a filter/parameter the request supplied was unusable (e.g. the
            # explorer month filter given the report tabs' 'latest' sentinel) —
            # client input, not a server fault
            return JSONResponse(status_code=422, content={"error": str(exc)})
        return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})

    async def _widen_threadpool():
        # sync endpoints + their lock-waits share anyio's default 40-token
        # threadpool; during a minutes-long rollup a busy dashboard can park
        # enough waiters to starve even trivial requests ("API unreachable" by
        # pool exhaustion). More headroom is cheap — these threads are idle
        # waiters, not CPU burners. (audit F3)
        try:
            import anyio.to_thread
            anyio.to_thread.current_default_thread_limiter().total_tokens = 100
        except Exception:  # noqa: BLE001 — tuning must never block startup
            log.exception("could not widen the request threadpool; keeping default")

    async def _tracker_autoimport():
        """OPT-IN (tracker_auto_import). The tracker refreshes the eligibility
        roster ~twice a week; with this on, a newer snapshot is picked up at
        startup so 'is this referrer still eligible' is never answered from a
        stale roster. Eligibility only — referral datasets are 7-11 GB and
        licence-encumbered, so they always stay a deliberate click."""
        if not getattr(cfg, "tracker_auto_import", False):
            return

        def _go():
            try:
                pend = [p for p in _tracker_discover()["pending"]
                        if p["kind"] == "eligibility"]
            except Exception:  # noqa: BLE001 — discovery must never block boot
                log.exception("tracker auto-import: discovery failed")
                return
            if not pend:
                return
            log.info("tracker auto-import: %s", pend[0]["label"])
            with tracker_job_lock:
                tracker_job.update({"state": "running", "done": [],
                                    "items": [pend[0]["label"]],
                                    "message": "auto-importing the newest roster…"})
            _run_tracker_import(pend[:1])

        threading.Thread(target=_bg_safe, args=(_go,),
                         name="mrfx-tracker-autoimport", daemon=True).start()

    startup_tasks.extend([_widen_threadpool, _tracker_autoimport])

    app.mount("/", NoCacheStaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def order_export_sql(args) -> tuple[str, list]:
    """CLI adapter: argparse namespace -> the same export query the API uses."""
    qp = {
        "payer": args.payer, "cpt": args.cpt, "modifier": args.modifier,
        "billing_class": args.billing_class, "q": args.q,
        "dollar_only": "0" if args.all_types else "1",
        "rate_min": args.rate_min, "rate_max": args.rate_max,
    }
    qp = {k: v for k, v in qp.items() if v not in (None, "")}
    fs = FilterSet(qp)
    grain = getattr(args, "grain", None) or "tin"
    return export_select(grain if grain in GRAIN_REL else "tin", fs, "negotiated_rate", "desc")


def _load_mpfs_csv(store: Store, data: bytes, source: str) -> int:
    import csv as _csv

    text = data.decode("utf-8-sig", errors="replace")
    reader = _csv.DictReader(io.StringIO(text))
    fields = {(f or "").strip().lower(): f for f in (reader.fieldnames or [])}
    required = {"code", "non_facility_rate"}
    if not required <= set(fields):
        raise ValueError(f"missing columns {required - set(fields)}")
    from .parser import clean_code

    rows = []
    for i, row in enumerate(reader, start=2):
        # normalize like billing_code is normalized ('g0283' -> G0283,
        # Excel's '97110.0' -> 97110) or %-of-Medicare silently never matches
        code = clean_code(row.get(fields["code"]) or "")
        if not code:
            continue
        rate_raw = row.get(fields["non_facility_rate"])
        if rate_raw is None or str(rate_raw).strip() == "":
            # short/truncated rows give None here — float(None) would be a
            # TypeError that bypasses the caller's clean 422
            raise ValueError(f"line {i} (code {code}): missing non_facility_rate")
        try:
            rate = float(rate_raw)
        except ValueError:
            raise ValueError(f"line {i} (code {code}): non-numeric rate {rate_raw!r}")
        rows.append({
            "code": code,
            "locality": (row.get(fields.get("locality", ""), "") or "") if fields.get("locality") else "",
            # optional: which state the row prices (else "% of Medicare" says
            # the file didn't state its locality)
            "state": (row.get(fields["state"]) or "").strip().upper()[:2] if fields.get("state") else "",
            "non_facility_rate": rate,
        })
    return store.load_mpfs(rows, source)


def _dicts(cursor) -> list[dict]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]
