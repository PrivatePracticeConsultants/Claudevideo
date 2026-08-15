"""FastAPI JSON API over the DuckDB store + static dashboard hosting (V4).

Every list endpoint filters/sorts/paginates SERVER-SIDE (§8.3); CSV export
reuses the exact same WHERE/ORDER SQL as the view that produced it (§8.7) and
ships with a methodology sidecar (§7A.6). The default analytical grain is
(billing_code × TIN); entity and NPI grains are explicit toggles.
"""

from __future__ import annotations

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
_TIN_PROJECTION = f"""
    coalesce(em.entity_name, td.display_name, nn.org_name,
             {_TIN_LABEL_SQL}) AS display_name,
    t.tin_value AS unit_id, t.tin_value,
    em.entity_name IS NOT NULL AS is_mapped_entity,
    t.tin_is_really_npi,
    1 AS tin_count, t.npi_count, t.rate_variants, t.rate_min, t.rate_max,
    t.payer, t.billing_code, t.billing_code_type, t.discipline, t.is_timed,
    t.modifier_set, t.billing_class, t.service_code_set, t.file_month,
    t.negotiated_rate, t.negotiated_type, t.is_dollar_rate,
    t.source_count, t.source_files, t.schema_version, t.last_updated_on,
    coalesce(td.states, CASE WHEN nn.state IS NULL THEN [] ELSE [nn.state] END) AS states,
    coalesce(td.cities, CASE WHEN nn.city IS NULL THEN [] ELSE [nn.city] END) AS cities,
    -- PT/OT/SLP practice? from tin_directory (EIN TINs) or, for an NPI-in-the-
    -- TIN-slot row, that NPI's own taxonomy.
    coalesce(td.is_therapy, {_THERAPY_NN}, FALSE) AS is_therapy
"""
_TIN_JOINS = """
    LEFT JOIN tin_directory td USING (tin_value)
    LEFT JOIN entity_map em USING (tin_value)
    -- payers that set tin.type='npi' publish an NPI in the TIN slot: name and
    -- locate those rows from the NPI directory instead of leaving raw numbers
    LEFT JOIN npi_directory nn ON t.tin_is_really_npi AND nn.npi = t.tin_value
"""
_TIN_REL = f"SELECT {_TIN_PROJECTION} FROM rates_by_tin t {_TIN_JOINS}"

_ENTITY_REL = f"""
    SELECT any_value(display_name) AS display_name,
           entity_key AS unit_id,
           string_agg(DISTINCT tin_value, '; ') AS tin_value,
           bool_or(is_mapped_entity) AS is_mapped_entity,
           bool_or(tin_is_really_npi) AS tin_is_really_npi,
           count(DISTINCT tin_value) AS tin_count,
           sum(npi_count) AS npi_count,
           CASE WHEN count(DISTINCT negotiated_rate) > 1 THEN count(DISTINCT negotiated_rate)
                ELSE max(rate_variants) END AS rate_variants,
           min(rate_min) AS rate_min, max(rate_max) AS rate_max,
           payer, billing_code, any_value(billing_code_type) AS billing_code_type,
           discipline, any_value(is_timed) AS is_timed,
           modifier_set, billing_class, service_code_set, file_month,
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
           any_value(negotiated_type) AS negotiated_type, is_dollar_rate,
           sum(source_count) AS source_count,
           string_agg(DISTINCT source_files, ';') AS source_files,
           any_value(schema_version) AS schema_version,
           max(last_updated_on) AS last_updated_on,
           list_sort(list_distinct(flatten(list(states)))) AS states,
           list_sort(list_distinct(flatten(list(cities)))) AS cities,
           bool_or(is_therapy) AS is_therapy   -- entity is therapy if any TIN is
    FROM (
        -- Group on the entity NAME, but a no-name SSN-pattern TIN falls back to
        -- the literal 'TIN MASKED-SSN' label — identical for EVERY such TIN — so
        -- grouping on it would merge unrelated sole-proprietor practices into one
        -- bogus entity. Fall back to the (unique) tin_value in that case so each
        -- stays its own entity; the masked label is still what renders.
        SELECT s.*, coalesce(
            s2.entity_name,
            CASE WHEN s.display_name = 'TIN MASKED-SSN' THEN s.tin_value
                 ELSE s.display_name END
        ) AS entity_key
        FROM ({_TIN_REL}) s LEFT JOIN entity_map s2 ON s2.tin_value = s.tin_value
    )
    GROUP BY entity_key, payer, billing_code, discipline, modifier_set,
             billing_class, service_code_set, file_month, is_dollar_rate
"""

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

        clauses, params = [], []
        self.described: dict = {}
        # True once a filter references a column that only exists AFTER the
        # name/geo joins (states, cities, display_name). The table endpoint can
        # take its fast late-join path only when this stays False.
        self.uses_dim_cols = False

        def add(desc_key, desc_val):
            self.described[desc_key] = desc_val

        payers = multi("payer")
        if payers:
            clauses.append(f"payer IN ({', '.join('?' for _ in payers)})")
            params += payers
            add("payers", payers)
        codes = split(qp.get("cpt") or qp.get("code"))
        if codes:
            clauses.append(f"billing_code IN ({', '.join('?' for _ in codes)})")
            params += codes
            add("codes", codes)
        disciplines = split(qp.get("discipline"))
        if disciplines:
            clauses.append(f"discipline IN ({', '.join('?' for _ in disciplines)})")
            params += disciplines
            add("disciplines", disciplines)
        modifier = qp.get("modifier")
        if modifier == "base":
            clauses.append("(modifier_set = '' OR modifier_set IN ('GP','GO','GN'))")
            add("modifier", "base only (no modifiers, or discipline GP/GO/GN only)")
        elif modifier == "assistant":
            clauses.append("(modifier_set LIKE '%CQ%' OR modifier_set LIKE '%CO%')")
            add("modifier", "assistant-provided only (CQ/CO)")
        elif modifier:
            clauses.append("('|' || modifier_set || '|') LIKE ('%|' || ? || '|%')")
            params.append(modifier)
            add("modifier", f"contains {modifier}")
        for m in split(qp.get("mod_has")):
            clauses.append("('|' || modifier_set || '|') LIKE ('%|' || ? || '|%')")
            params.append(m)
            add(f"mod_has:{m}", True)
        for m in split(qp.get("mod_not")):
            clauses.append("('|' || modifier_set || '|') NOT LIKE ('%|' || ? || '|%')")
            params.append(m)
            add(f"mod_not:{m}", True)
        if qp.get("billing_class"):
            clauses.append("billing_class = ?")
            params.append(qp["billing_class"])
            add("billing_class", qp["billing_class"])
        if qp.get("pos"):
            clauses.append("('|' || service_code_set || '|') LIKE ('%|' || ? || '|%')")
            params.append(qp["pos"])
            add("place_of_service", qp["pos"])
        if qp.get("state"):
            clauses.append("list_contains(states, ?)")
            params.append(qp["state"].upper())
            add("state", qp["state"].upper())
            self.uses_dim_cols = True  # `states` is a joined column
        if qp.get("city"):
            clauses.append("len(list_filter(cities, c -> upper(c) = upper(?))) > 0")
            params.append(qp["city"])
            add("city", qp["city"])
            self.uses_dim_cols = True  # `cities` is a joined column
        if qp.get("month"):
            if str(qp["month"]).strip().lower() == "latest":
                # the report tabs' "latest" sentinel is not a real file_month —
                # matching it literally returned 0 rows while the methodology
                # sidecar claimed as_of_month "latest" was applied
                raise ValueError(
                    "the explorer month filter takes a real month like 2026-07 "
                    "(leave it empty to see all months)")
            clauses.append("file_month = ?")
            params.append(qp["month"])
            add("as_of_month", qp["month"])
        q = qp.get("q")
        if q:
            clauses.append(
                "(unit_id LIKE ? OR upper(display_name) LIKE upper(?) OR tin_value LIKE ?)"
            )
            params += [f"%{q}%", f"%{q}%", f"%{q}%"]
            add("search", q)
            self.uses_dim_cols = True  # searches display_name (a joined column)
        dollar_only = qp.get("dollar_only", "1") not in ("0", "false")
        if dollar_only:
            # exclude $0/$0.01/negative DOLLAR placeholders here too, so the
            # explorer / code-comparison / CSV-export medians and mins agree with
            # the benchmark and rate-card (which strip them via _market_where).
            # Without this the same store shows a lower median on one screen than
            # another. The per-file zero_rates QA count still reports how many
            # placeholders a payer published (transparency preserved there).
            clauses.append("is_dollar_rate AND negotiated_rate > 0.01")
        add("dollar_rates_only", dollar_only)
        if qp.get("hide_tin_npi", "0") in ("1", "true"):
            clauses.append("NOT tin_is_really_npi")
            add("hide_tin_is_really_npi", True)
        if qp.get("therapy_only", "0") in ("1", "true"):
            # STRICT outpatient-practice test (tin_directory.is_therapy) — the
            # description below ships verbatim in every export's methodology
            # sidecar, so it must state the rule the query ACTUALLY applied.
            clauses.append("is_therapy")
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
            clauses.append(f"negotiated_rate {op} ?")
            params.append(val)
            add(bound, val)
        self.hide_outliers = qp.get("hide_outliers", "0") in ("1", "true")
        add("hide_outliers", f"ON — {OUTLIER_RULE}" if self.hide_outliers else "off")
        self.where = " AND ".join(clauses) if clauses else "1=1"
        self.params = params


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


def rel_sql(grain: str, fs: FilterSet) -> str:
    base = f"WITH base AS ({GRAIN_REL[grain]}) SELECT * FROM base WHERE {fs.where}"
    if fs.hide_outliers:
        # The reference median must exclude $0/$0.01 placeholders: with
        # "dollar rates only" OFF, fs.where no longer strips them, and a
        # placeholder-heavy code's median collapsed toward $0.01 — the 0.2x-5x
        # band then hid most REAL rates. And the band is denominated in
        # dollars, so percentage/per-diem rows (rate ~1.5) must be exempt from
        # it — hiding them was exactly what unchecking dollar-only asked to
        # undo.
        base = f"""
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
    return base


def order_sql(sort: str, direction: str) -> str:
    if sort not in SORTABLE:
        sort = "negotiated_rate"
    direction = "ASC" if direction.lower() == "asc" else "DESC"
    # The tiebreaker must be a TOTAL order over the grain's row identity:
    # LIMIT/OFFSET pages are independent queries, and DuckDB (with
    # preserve_insertion_order off) may order still-tied rows differently per
    # page — rows silently duplicated on one page and MISSING from another.
    # Multi-month stores are tie-dense (a TIN's rate for a code is usually
    # unchanged month over month), so every identity column joins the order.
    return (f"ORDER BY {sort} {direction} NULLS LAST, unit_id ASC, billing_code ASC, "
            "modifier_set ASC, payer ASC, file_month ASC, billing_class ASC, "
            "service_code_set ASC, is_dollar_rate ASC")


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


def _as_float(v, default):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        raise BenchmarkError(f"expected a number, got {v!r}")


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
        FROM ({rel_sql(grain, fs)}) {order_sql(sort, direction)}
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
        f"Filters: {json.dumps(fs.described, default=str)}",
        f"Sort: {sort} {direction}",
        "Dedup rule: distinct negotiated facts per grain tuple. A TIN rate is the "
        "median of its distinct published values EXCLUDING $0/$0.01 dollar "
        "placeholders (rate_variants still counts them, and rate_min/rate_max "
        "still show them); an ENTITY rate is the median of its member TINs' "
        "rates, and entity npi_count/source_count sum members (an NPI shared by "
        "two member TINs counts twice).",
        f"Outlier handling: {fs.described.get('hide_outliers')}",
        # honesty: with no month filter the grain keeps each file_month as its
        # own row, so a rate republished monthly appears once per month and the
        # medians/counts pool every vintage. Say so, or an exported median reads
        # as one number when it blends several months.
        ("As-of month: " + str(fs.described["as_of_month"]) + " (only this month's rates)."
         if "as_of_month" in fs.described else
         "As-of month: ALL months pooled (no month filter was set) — a rate "
         "republished across months is counted once per month, so these "
         "medians/counts blend every vintage. Set a month filter, or use the "
         "report tabs' 'latest' view, for a single-vintage number."),
        "Non-dollar negotiated_type rows (percentage, per diem) are excluded when "
        f"dollar_rates_only is true (currently: {fs.described.get('dollar_rates_only')}).",
        "SSN-pattern TINs are masked in every export.",
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
    app = FastAPI(title="MRF Explorer", docs_url="/api/docs")
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
        just far cheaper."""
        return (grain == "tin" and not fs.hide_outliers
                and not fs.uses_dim_cols and sort not in ("display_name", "tin_count"))

    def _filtered_count(con, grain: str, fs: FilterSet) -> int:
        key = (grain, fs.where, tuple(fs.params), fs.hide_outliers, store.data_generation)
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
                f"SELECT count(*) FROM rates_by_tin WHERE {fs.where}", fs.params).fetchone()[0]
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
                   f"WHERE {fs.where} {order} LIMIT ? OFFSET ?) "
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
    _SUMMARY_TTL = 30.0
    # the market overview does several whole-spine GROUP BYs — heavy at book
    # scale — so cache per (state, data_generation); it changes only on a rebuild.
    _overview_cache: dict[tuple, dict] = {}
    _overview_lock = threading.Lock()

    @app.get("/api/summary")
    def summary(request: Request):
        qp = _qp(request)
        grain = grain_of(qp, cfg, store)
        fs = _fs(qp)
        # hide_outliers is applied inside rel_sql but is NOT part of fs.where/
        # params — omitting it from the key served the unfiltered stats to the
        # outlier-hidden view (and vice versa) for a TTL: the strip disagreed
        # with the table it sits above (caught by the verification audit,
        # reproduced live: max=$9000 shown with the outlier "hidden")
        key = (grain, fs.where, tuple(fs.params), fs.hide_outliers,
               store.data_generation)
        hit = _summary_cache.get(key)
        now = time.monotonic()
        if hit is not None and now - hit[0] < _SUMMARY_TTL:
            return hit[1]
        if not _summary_lock.acquire(blocking=False):
            # a heavy scan is already running — serve the last value for THIS
            # key, else say "busy" honestly (serving another filter's numbers
            # under this view's label would be a wrong number; and iterating
            # the cache dict here races the winner's insert/clear)
            if hit is not None:
                return hit[1]
            raise HTTPException(503, "summary is busy — it will load on the next refresh")
        try:
            hit = _summary_cache.get(key)  # re-check under the lock
            if hit is not None and time.monotonic() - hit[0] < _SUMMARY_TTL:
                return hit[1]
            with store.connect() as con:
                row = con.execute(
                    f"""
                    SELECT count(*) AS n,
                           count(DISTINCT unit_id) AS entities,
                           count(DISTINCT billing_code) AS codes,
                           min(negotiated_rate) FILTER (is_dollar_rate AND negotiated_rate > 0.01) AS min,
                           quantile_cont(negotiated_rate, .25) FILTER (is_dollar_rate AND negotiated_rate > 0.01) AS p25,
                           median(negotiated_rate) FILTER (is_dollar_rate AND negotiated_rate > 0.01) AS median,
                           quantile_cont(negotiated_rate, .75) FILTER (is_dollar_rate AND negotiated_rate > 0.01) AS p75,
                           max(negotiated_rate) FILTER (is_dollar_rate AND negotiated_rate > 0.01) AS max
                    FROM ({rel_sql(grain, fs)})
                    """,
                    fs.params,
                ).fetchone()
                # PER-CODE breakdown: one blended median across 97110+97530+…
                # is analytically meaningless (different services), so give a row
                # per code — median, spread (IQR), practice count, and % of
                # Medicare / below-Medicare flag when an MPFS anchor is loaded.
                by_code = _dicts(con.execute(
                    f"""
                    WITH r AS (SELECT billing_code, negotiated_rate, unit_id
                               FROM ({rel_sql(grain, fs)})
                               WHERE is_dollar_rate AND negotiated_rate > 0.01)
                    SELECT r.billing_code,
                           median(r.negotiated_rate)             AS median,
                           quantile_cont(r.negotiated_rate, .25) AS p25,
                           quantile_cont(r.negotiated_rate, .75) AS p75,
                           count(DISTINCT r.unit_id)             AS entities,
                           m.mc                                  AS mpfs_rate
                    FROM r LEFT JOIN (SELECT code, median(non_facility_rate) AS mc
                                      FROM mpfs GROUP BY code) m ON m.code = r.billing_code
                    GROUP BY r.billing_code, m.mc
                    ORDER BY r.billing_code
                    """,
                    fs.params,
                ))
            for c in by_code:
                mc = c.pop("mpfs_rate", None)
                c["pct_medicare"] = (round(100 * c["median"] / mc)
                                     if mc and c.get("median") else None)
                c["below_medicare"] = bool(mc and c.get("median") is not None
                                           and c["median"] < mc)
            keys = ["n", "entities", "codes", "min", "p25", "median", "p75", "max"]
            result = {**dict(zip(keys, row)), "grain": grain, "by_code": by_code,
                      "mpfs_loaded": any(c["pct_medicare"] is not None for c in by_code)}
            if len(_summary_cache) > 256:
                _summary_cache.clear()
            _summary_cache[key] = (time.monotonic(), result)
            return result
        finally:
            _summary_lock.release()

    # -- detail views -----------------------------------------------------------

    @app.get("/api/entity/{grain}/{unit_id:path}")
    def entity_detail(grain: str, unit_id: str):
        if grain not in GRAIN_REL:
            raise HTTPException(404, "grain must be entity|tin|npi")
        with store.connect() as con:
            rows = _dicts(con.execute(
                f"SELECT * FROM ({GRAIN_REL[grain]}) WHERE unit_id = ? "
                "ORDER BY billing_code, payer, modifier_set, file_month",
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
                    # Entity: resolve EVERY tax id that rolls up to this entity,
                    # whether by a manual entity_map name or by the automatic
                    # NPPES-name grouping. The TIN relation's display_name IS the
                    # entity_key the grain groups on (it already coalesces the
                    # manual map over the NPPES name), so matching on it here
                    # yields exactly the members the entity row aggregated —
                    # without this, an auto-grouped org showed an empty drawer.
                    # `OR tin_value = ?`: a NO-NAME entity's key falls back to
                    # the raw tin_value while its display_name is the masked
                    # 'TIN …' label — display_name alone can't match, and the
                    # drawer showed rates with no member table.
                    tin_list = [r[0] for r in con.execute(
                        f"SELECT DISTINCT tin_value FROM ({GRAIN_REL['tin']}) "
                        "WHERE display_name = ? OR tin_value = ?",
                        [unit_id, unit_id],
                    ).fetchall()]
                tins = _dicts(con.execute(
                    f"SELECT * FROM tin_directory WHERE tin_value IN ({', '.join('?' for _ in tin_list)})",
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
        grain = grain_of(qp, cfg, store)
        fs = _fs(qp)
        with store.connect() as con:
            ranked = _dicts(con.execute(
                f"""
                SELECT unit_id, any_value(display_name) AS display_name,
                       payer, modifier_set, billing_class,
                       any_value(discipline) AS discipline,
                       -- is_dollar_rate in the GROUP BY: under dollar_only=0 a
                       -- unit holding $85 AND a 150% percentage row must not
                       -- blend them into one median that matches no published
                       -- number (dollars and percentages are incommensurable)
                       is_dollar_rate,
                       median(negotiated_rate) AS median_rate,
                       min(negotiated_rate) AS min_rate, max(negotiated_rate) AS max_rate,
                       sum(npi_count) AS npi_count, count(*) AS n
                FROM ({rel_sql(grain, fs)})
                GROUP BY unit_id, payer, modifier_set, billing_class, is_dollar_rate
                ORDER BY median_rate DESC LIMIT 500
                """,
                fs.params,
            ))
            hist = _dicts(con.execute(
                f"""
                WITH r AS (SELECT negotiated_rate FROM ({rel_sql(grain, fs)}) WHERE is_dollar_rate AND negotiated_rate > 0.01)
                SELECT floor(negotiated_rate / g.w) * g.w AS bucket, count(*) AS n
                FROM r, (SELECT greatest((max(negotiated_rate) - min(negotiated_rate)) / 20, 0.01) AS w FROM r) g
                GROUP BY 1 ORDER BY 1
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
            payer_rank = _dicts(con.execute(
                f"""
                WITH per_unit AS (
                    SELECT payer, unit_id, median(negotiated_rate) AS rate
                    FROM ({rel_sql(grain, fs)})
                    WHERE is_dollar_rate AND negotiated_rate > 0.01
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
                GROUP BY payer ORDER BY median_rate DESC
                """,
                fs.params,
            ))
            base = con.execute(
                "SELECT median(non_facility_rate) FROM mpfs WHERE code = ?", [code]
            ).fetchone()[0]
        # % of Medicare per payer when an MPFS anchor is loaded; the key is
        # always present (None without an anchor) so the frontend never reads
        # undefined — matching /api/summary by_code.
        for p in payer_rank:
            p["pct_medicare"] = (round(100 * p["median_rate"] / base)
                                 if base and p.get("median_rate") else None)
        info = catalog_json().get(code, {})
        return {"billing_code": code, "grain": grain, **info, "ranked": ranked,
                "histogram": hist, "payer_rank": payer_rank,
                "mpfs_rate": round(base, 2) if base else None}

    @app.get("/api/trend")
    def trend(request: Request):
        qp = _qp(request)
        qp.pop("month", None)  # trend spans months by definition
        grain = grain_of(qp, cfg, store)
        fs = _fs(qp)
        with store.connect() as con:
            rows = _dicts(con.execute(
                f"""
                SELECT billing_code, payer, file_month,
                       median(negotiated_rate) AS median_rate,
                       count(DISTINCT unit_id) AS entities
                FROM ({rel_sql(grain, fs)}) WHERE is_dollar_rate AND negotiated_rate > 0.01
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
                ORDER BY payer, file_month, negotiated_rate
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
    def urls_add_known():
        from .fetch import add_urls
        from .known_sources import load_known_sources

        sources = load_known_sources(cfg.known_sources_path)
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
                state=str(body.get("state") or "").strip() or None)
        except NppesFeedError as e:
            raise HTTPException(422, str(e))
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"bad input: {e}")

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
                limit=_as_int(body.get("limit"), 50),
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
            limit = min(max(_as_int(body.get("limit"), 100), 1), 500)
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
                        + "GROUP BY display_name HAVING count(*) > 1 LIMIT ?")
            auto = [r[0] for r in con.execute(
                auto_sql, ([like] if ql else []) + [limit]).fetchall()]
            if ql:
                tins = _dicts(con.execute(
                    "SELECT tin_value, display_name, npi_count, states FROM tin_directory "
                    "WHERE lower(display_name) LIKE ? OR tin_value LIKE ? "
                    "ORDER BY npi_count DESC NULLS LAST, display_name LIMIT ?",
                    [like, f"{ql}%", limit]))
            else:
                tins = _dicts(con.execute(
                    "SELECT tin_value, display_name, npi_count, states FROM tin_directory "
                    "ORDER BY npi_count DESC NULLS LAST, display_name LIMIT ?", [limit]))
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
                comparables=[str(c) for c in (body.get("comparables") or [])]
                if isinstance(body.get("comparables") or [], list) else [],
            )
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/report/payer-compare", response_class=HTMLResponse)
    def payer_compare_report(body: dict = Body(...)):
        try:
            comp = compute_payer_comparison(
                store, str(body.get("subject", "")), str(body.get("payer", "")),
                body.get("market") or {},
                comparables=[str(c) for c in (body.get("comparables") or [])]
                if isinstance(body.get("comparables") or [], list) else [],
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
                comparables=body.get("comparables") or None)
        except BenchmarkError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/report/proposal", response_class=HTMLResponse)
    def report_proposal(body: dict = Body(...)):
        from .benchmark import compute_rate_proposal, render_proposal_report
        try:
            prop = compute_rate_proposal(
                store, str(body.get("subject", "")), str(body.get("payer", "")),
                body.get("market") or {}, body.get("target") or {},
                volumes=body.get("volumes") or None,
                comparables=body.get("comparables") or None)
            return render_proposal_report(cfg, store, prop)
        except BenchmarkError as e:
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
                min_codes=_as_int(body.get("min_codes"), 3),
                limit=_as_int(body.get("limit"), 100),
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
                min_codes=_as_int(body.get("min_codes"), 3),
                limit=_as_int(body.get("limit"), 1000),
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
                                    limit=_as_int(body.get("limit"), 60))
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
                limit=_as_int(body.get("limit"), 100))
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/leaderboard")
    def leaderboard_ep(body: dict = Body(...)):
        try:
            return compute_leaderboard(
                store, body.get("market") or {},
                min_codes=_as_int(body.get("min_codes"), 3),
                limit=_as_int(body.get("limit"), 50),
                sort=str(body.get("sort") or "size"))
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/trajectory")
    def trajectory_ep(body: dict = Body(...)):
        try:
            return compute_payer_trajectory(store, body.get("market") or {})
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/roster")
    def roster_ep(body: dict = Body(...)):
        try:
            return compute_payer_roster(
                store, str(body.get("payer") or ""), body.get("market") or {},
                min_codes=_as_int(body.get("min_codes"), 1),
                limit=_as_int(body.get("limit"), 500),
                sort=str(body.get("sort") or "name"))
        except (BenchmarkError, ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/roster.csv", response_class=PlainTextResponse)
    def roster_csv_ep(body: dict = Body(...)):
        try:
            result = compute_payer_roster(
                store, str(body.get("payer") or ""), body.get("market") or {},
                min_codes=_as_int(body.get("min_codes"), 1),
                limit=_as_int(body.get("limit"), 2000),
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
        state = (_qp(request).get("state") or "").strip().upper() or None
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

    @app.on_event("startup")
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

    @app.on_event("startup")
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
            "non_facility_rate": rate,
        })
    return store.load_mpfs(rows, source)


def _dicts(cursor) -> list[dict]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]
