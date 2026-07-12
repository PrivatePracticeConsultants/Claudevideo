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
import tempfile
import zipfile
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .benchmark import (
    BenchmarkError,
    compute_benchmark,
    compute_opportunity,
    compute_payer_negotiation,
    render_negotiation_report,
    render_pitch_report,
)
from .catalog import catalog_json
from .config import MrfxConfig
from .leads import compute_leads, leads_csv
from .monitor import compute_rate_changes, rate_changes_csv
from .schedule import (
    compute_fee_schedule,
    fee_schedule_csv,
    payer_scorecard,
    render_rate_card,
)
from .entities import sync_entity_map, update_entity
from .ingest import ingest_file, scan_inbox
from .registry import Registry
from .store import Store, mask_tin, sql_path

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"

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

_TIN_REL = f"""
    SELECT coalesce(em.entity_name, td.display_name, nn.org_name,
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
           coalesce(td.cities, CASE WHEN nn.city IS NULL THEN [] ELSE [nn.city] END) AS cities
    FROM rates_by_tin t
    LEFT JOIN tin_directory td USING (tin_value)
    LEFT JOIN entity_map em USING (tin_value)
    -- payers that set tin.type='npi' publish an NPI in the TIN slot: name and
    -- locate those rows from the NPI directory instead of leaving raw numbers
    LEFT JOIN npi_directory nn ON t.tin_is_really_npi AND nn.npi = t.tin_value
"""

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
           median(negotiated_rate) AS negotiated_rate,
           any_value(negotiated_type) AS negotiated_type, is_dollar_rate,
           sum(source_count) AS source_count,
           string_agg(DISTINCT source_files, ';') AS source_files,
           any_value(schema_version) AS schema_version,
           max(last_updated_on) AS last_updated_on,
           list_sort(list_distinct(flatten(list(states)))) AS states,
           list_sort(list_distinct(flatten(list(cities)))) AS cities
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

_NPI_REL = """
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
           CASE WHEN n.city IS NULL THEN [] ELSE [n.city] END AS cities
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
        if qp.get("city"):
            clauses.append("len(list_filter(cities, c -> upper(c) = upper(?))) > 0")
            params.append(qp["city"])
            add("city", qp["city"])
        if qp.get("month"):
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
        base = f"""
        WITH filtered AS ({base}),
        med AS (
            SELECT billing_code, median(negotiated_rate) AS m
            FROM filtered WHERE is_dollar_rate GROUP BY billing_code
        )
        SELECT filtered.* FROM filtered LEFT JOIN med USING (billing_code)
        WHERE m IS NULL OR (negotiated_rate <= 5 * m AND negotiated_rate >= 0.2 * m)
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
        "median of its distinct published values (rate_variants counts them); an "
        "ENTITY rate is the median of its member TINs' rates, and entity npi_count/"
        "source_count sum members (an NPI shared by two member TINs counts twice).",
        f"Outlier handling: {fs.described.get('hide_outliers')}",
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
        fs = FilterSet(qp)
        sql = f"{rel_sql(grain, fs)} {order_sql(sort, dir)} LIMIT ? OFFSET ?"
        with store.connect() as con:
            rows = _dicts(con.execute(sql, [*fs.params, page_size, (page - 1) * page_size]))
            total = con.execute(f"SELECT count(*) FROM ({rel_sql(grain, fs)})", fs.params).fetchone()[0]
        return {"rows": _mask_row_tins(rows), "total": total, "page": page,
                "page_size": page_size, "grain": grain}

    @app.get("/api/summary")
    def summary(request: Request):
        qp = _qp(request)
        grain = grain_of(qp, cfg, store)
        fs = FilterSet(qp)
        with store.connect() as con:
            row = con.execute(
                f"""
                SELECT count(*) AS n,
                       count(DISTINCT unit_id) AS entities,
                       count(DISTINCT billing_code) AS codes,
                       min(negotiated_rate) FILTER (is_dollar_rate) AS min,
                       quantile_cont(negotiated_rate, .25) FILTER (is_dollar_rate) AS p25,
                       median(negotiated_rate) FILTER (is_dollar_rate) AS median,
                       quantile_cont(negotiated_rate, .75) FILTER (is_dollar_rate) AS p75,
                       max(negotiated_rate) FILTER (is_dollar_rate) AS max
                FROM ({rel_sql(grain, fs)})
                """,
                fs.params,
            ).fetchone()
        keys = ["n", "entities", "codes", "min", "p25", "median", "p75", "max"]
        return {**dict(zip(keys, row)), "grain": grain}

    # -- detail views -----------------------------------------------------------

    @app.get("/api/entity/{grain}/{unit_id:path}")
    def entity_detail(grain: str, unit_id: str):
        if grain not in GRAIN_REL:
            raise HTTPException(404, "grain must be entity|tin|npi")
        fs = FilterSet({"dollar_only": "0"})
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
                    tin_list = [r[0] for r in con.execute(
                        f"SELECT DISTINCT tin_value FROM ({GRAIN_REL['tin']}) WHERE display_name = ?",
                        [unit_id],
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
        geo_row = (tins[0] if tins else (member_npis[0] if member_npis else {}))
        website_lookup = website_lookup_url(rows[0]["display_name"],
                                            geo_row.get("city"), geo_row.get("state"))
        for t in tins:
            t["tin_value"] = mask_tin(t["tin_value"])
        return {
            "unit_id": unit_id, "grain": grain,
            "display_name": rows[0]["display_name"],
            "rates": _mask_row_tins(rows), "tins": tins, "npis": member_npis,
            "chart": chart, "variants": len(variance),
            "website": website, "website_lookup": website_lookup,
            "website_tins": tin_list if grain != "npi" else [],
        }

    @app.get("/api/code/{code}")
    def code_detail(code: str, request: Request):
        qp = _qp(request)
        qp["code"] = code
        grain = grain_of(qp, cfg, store)
        fs = FilterSet(qp)
        with store.connect() as con:
            ranked = _dicts(con.execute(
                f"""
                SELECT unit_id, any_value(display_name) AS display_name,
                       payer, modifier_set, billing_class,
                       any_value(discipline) AS discipline,
                       median(negotiated_rate) AS median_rate,
                       min(negotiated_rate) AS min_rate, max(negotiated_rate) AS max_rate,
                       sum(npi_count) AS npi_count, count(*) AS n
                FROM ({rel_sql(grain, fs)})
                GROUP BY unit_id, payer, modifier_set, billing_class
                ORDER BY median_rate DESC LIMIT 500
                """,
                fs.params,
            ))
            hist = _dicts(con.execute(
                f"""
                WITH r AS (SELECT negotiated_rate FROM ({rel_sql(grain, fs)}) WHERE is_dollar_rate)
                SELECT floor(negotiated_rate / g.w) * g.w AS bucket, count(*) AS n
                FROM r, (SELECT greatest((max(negotiated_rate) - min(negotiated_rate)) / 20, 0.01) AS w FROM r) g
                GROUP BY 1 ORDER BY 1
                """,
                fs.params,
            ))
        info = catalog_json().get(code, {})
        return {"billing_code": code, "grain": grain, **info, "ranked": ranked, "histogram": hist}

    @app.get("/api/trend")
    def trend(request: Request):
        qp = _qp(request)
        qp.pop("month", None)  # trend spans months by definition
        grain = grain_of(qp, cfg, store)
        fs = FilterSet(qp)
        with store.connect() as con:
            rows = _dicts(con.execute(
                f"""
                SELECT billing_code, payer, file_month,
                       median(negotiated_rate) AS median_rate,
                       count(DISTINCT unit_id) AS entities
                FROM ({rel_sql(grain, fs)}) WHERE is_dollar_rate
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
                "SELECT DISTINCT file_month FROM rates_by_tin ORDER BY file_month DESC"
            ).fetchall()
        return {"months": [r[0] for r in rows]}

    # -- validation cross-check (§7A.10) -----------------------------------------

    @app.post("/api/validate")
    async def validate(request: Request):
        body = await request.json()
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
    async def entities_update(request: Request):
        body = await request.json()
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(422, "entity name required")
        update_entity(cfg, store, name, body.get("add_tins") or [], body.get("remove_tins") or [])
        return entities_map()

    @app.post("/api/org-website")
    async def org_website(request: Request):
        """Save (or clear, when url is empty) the hand-verified website for an
        org — applied to all the tax ids passed (an entity's constituent TINs),
        so it shows on the org however a later view resolves it. Accepts only
        http(s) URLs; anything else is rejected rather than stored as a bad
        link."""
        body = await request.json()
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
    def files_scan(background: BackgroundTasks):
        background.add_task(scan_inbox, cfg, store)
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
    def confirm_file(filename: str, background: BackgroundTasks):
        st = store.file_status(filename)
        if not st or st.get("status") != "pending_confirmation":
            raise HTTPException(404, "file is not awaiting confirmation")
        path = cfg.inbox_dir / filename
        if not path.exists():
            raise HTTPException(404, "file no longer in inbox")
        background.add_task(ingest_file, cfg, store, path)
        store.upsert_file(filename, status="queued")
        return {"status": "queued"}

    @app.post("/api/upload")
    async def upload(file: UploadFile, background: BackgroundTasks):
        dest = cfg.inbox_dir / Path(file.filename or "upload.json").name
        # stream to a name the inbox scanner ignores, rename when COMPLETE —
        # the watcher fires on creation and would otherwise preflight (and
        # quarantine/move!) a half-written file out from under this handler
        tmp = dest.with_name(dest.name + ".uploading")
        size = 0
        try:
            with open(tmp, "wb") as out:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    if size > UPLOAD_LIMIT_BYTES:
                        raise HTTPException(413, "over 1 GB — drop the file into data/inbox/ instead")
                    out.write(chunk)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
        background.add_task(scan_inbox, cfg, store)
        return {"status": "queued", "filename": dest.name, "bytes": size}

    # -- export (CSV + methodology sidecar, §7A.6) ---------------------------------


    def _mktemp(suffix: str) -> Path:
        fd, p = tempfile.mkstemp(suffix=suffix)
        os.close(fd)  # mkstemp returns an OPEN fd — dropping it leaks one per export
        return Path(p)

    def _export_payload(request: Request, view: str, sort: str, dir: str, full: bool):
        qp = {} if full else _qp(request)
        grain = grain_of(qp, cfg, store)
        fs = FilterSet(qp if not full else {"dollar_only": "0"})
        select, params = export_select(grain, fs, sort, dir)
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
        tmp = _mktemp(".csv")
        with store.connect() as con:
            con.execute(f"COPY ({select}) TO '{sql_path(tmp)}' (FORMAT CSV, HEADER)", params)
        raw = tmp.read_text()
        tmp.unlink()
        method = methodology_text(cfg, store, grain, fs, sort, dir, view)
        return stamp, "﻿" + raw, method  # BOM for Excel

    @app.get("/api/export.csv")
    def export_csv(
        request: Request,
        background: BackgroundTasks,
        view: str = "explorer",
        sort: str = "negotiated_rate",
        dir: str = "desc",
        full: bool = False,
    ):
        stamp, csv_text, method = _export_payload(request, view, sort, dir, full)
        out = _mktemp(".csv")
        out.write_text(csv_text, encoding="utf-8")
        sidecar = out.with_suffix(".methodology.txt")
        sidecar.write_text(method)
        background.add_task(out.unlink, missing_ok=True)
        background.add_task(sidecar.unlink, missing_ok=True)  # or it orphans per export
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
        stamp, csv_text, method = _export_payload(request, view, sort, dir, full)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"mrfx_{view}_{stamp}.csv", csv_text.encode("utf-8"))
            z.writestr(f"mrfx_{view}_{stamp}_methodology.txt", method)
        out = _mktemp(".zip")
        out.write_bytes(buf.getvalue())
        background.add_task(out.unlink, missing_ok=True)
        return FileResponse(out, filename=f"mrfx_{view}_{stamp}.zip", media_type="application/zip")

    @app.get("/api/export/outreach.csv")
    def export_outreach(request: Request, background: BackgroundTasks):
        """One row per entity (org name + geography + per-code merge fields),
        for cross-referencing a contact list / Brevo mail merge."""
        from .outreach import build_outreach_rows, outreach_csv

        qp = _qp(request)
        grain = grain_of(qp, cfg, store)
        if grain == "npi":
            grain = "tin"  # outreach is entity-level by definition
        fs = FilterSet(qp)
        headers, rows = build_outreach_rows(
            store, rel_sql(grain, fs), fs.params, fs.described.get("codes")
        )
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
        out = _mktemp(".csv")
        out.write_text(outreach_csv(headers, rows), encoding="utf-8")
        sidecar_note = methodology_text(cfg, store, grain, fs, "display_name", "asc", "outreach")
        sidecar = out.with_suffix(".methodology.txt")
        sidecar.write_text(sidecar_note)
        background.add_task(out.unlink, missing_ok=True)
        background.add_task(sidecar.unlink, missing_ok=True)
        return FileResponse(out, filename=f"mrfx_outreach_{stamp}.csv", media_type="text/csv")

    # -- benchmarks (§7B) --------------------------------------------------------------

    @app.get("/api/benchmark/subjects")
    def benchmark_subjects():
        emap = store.entity_map()
        with store.connect() as con:
            tins = _dicts(con.execute(
                "SELECT tin_value, display_name, npi_count, states FROM tin_directory "
                "ORDER BY display_name"
            ))
            # auto-grouped orgs: an NPPES org name shared by >1 TIN is ONE
            # practice on the Explorer's entity grain, and resolve_subject_tins
            # expands the name to all its TINs — but without this it had no
            # single picker option, so a multi-location org got benchmarked as a
            # lone fragment TIN. Exclude the 'TIN …' no-name fallback labels.
            auto = [r[0] for r in con.execute(
                "SELECT display_name FROM tin_directory WHERE display_name NOT LIKE 'TIN %' "
                "GROUP BY display_name HAVING count(*) > 1"
            ).fetchall()]
        entities = sorted(set(emap.values()) | set(auto))
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
    async def benchmark_market(request: Request):
        body = await request.json()
        try:
            return compute_benchmark(store, str(body.get("subject", "")), body.get("market") or {})
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body (int()/float()/None) — a client input error, so 422.
            raise HTTPException(422, str(e))

    @app.post("/api/benchmark/opportunity")
    async def benchmark_opportunity(request: Request):
        body = await request.json()
        try:
            bench = compute_benchmark(store, str(body.get("subject", "")), body.get("market") or {})
            volumes = {str(k): float(v) for k, v in (body.get("volumes") or {}).items()}
            opp = compute_opportunity(bench, volumes,
                                      int(body.get("conservative_percentile", 40)))
            return {"benchmark": bench, "opportunity": opp}
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body (int()/float()/None) — a client input error, so 422.
            raise HTTPException(422, str(e))

    @app.post("/api/report/pitch", response_class=HTMLResponse)
    async def pitch_report(request: Request):
        body = await request.json()
        try:
            bench = compute_benchmark(store, str(body.get("subject", "")), body.get("market") or {})
            opp = None
            volumes = {str(k): float(v) for k, v in (body.get("volumes") or {}).items()}
            if volumes:
                opp = compute_opportunity(bench, volumes,
                                          int(body.get("conservative_percentile", 40)))
            return HTMLResponse(render_pitch_report(cfg, store, bench, opp))
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body (int()/float()/None) — a client input error, so 422.
            raise HTTPException(422, str(e))

    @app.post("/api/report/negotiation", response_class=HTMLResponse)
    async def negotiation_report(request: Request):
        body = await request.json()
        try:
            volumes = {str(k): float(v) for k, v in (body.get("volumes") or {}).items()}
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

    # fee schedule / payer scorecard (§7C)
    @app.post("/api/schedule/fee")
    async def schedule_fee(request: Request):
        body = await request.json()
        try:
            fs = compute_fee_schedule(store, str(body.get("subject", "")), body.get("market") or {})
            return {"fee_schedule": fs, "scorecard": payer_scorecard(fs)}
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body — a client input error (422), never a 500.
            raise HTTPException(422, str(e))

    @app.post("/api/report/ratecard", response_class=HTMLResponse)
    async def ratecard_report(request: Request):
        body = await request.json()
        try:
            fs = compute_fee_schedule(store, str(body.get("subject", "")), body.get("market") or {})
            return HTMLResponse(render_rate_card(cfg, store, fs, payer_scorecard(fs)))
        except (BenchmarkError, ValueError, TypeError) as e:
            # ValueError/TypeError: a non-numeric or null volume/percentile in
            # the JSON body — a client input error (422), never a 500.
            raise HTTPException(422, str(e))

    @app.post("/api/schedule/fee.csv", response_class=PlainTextResponse)
    async def schedule_fee_csv(request: Request):
        body = await request.json()
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
    async def leads(request: Request):
        body = await request.json()
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
    async def leads_export(request: Request):
        body = await request.json()
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
    async def changes(request: Request):
        body = await request.json()
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
    async def changes_export(request: Request):
        body = await request.json()
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

    # peer sets
    @app.get("/api/peersets")
    def peersets():
        return {"peer_sets": store.peer_sets()}

    @app.post("/api/peersets")
    async def peersets_save(request: Request):
        body = await request.json()
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

    # MPFS
    @app.get("/api/mpfs/status")
    def mpfs_status():
        return {"loaded": store.mpfs_loaded()}

    @app.post("/api/mpfs/upload")
    async def mpfs_upload(file: UploadFile):
        data = await file.read()
        try:
            n = _load_mpfs_csv(store, data, file.filename or "upload.csv")
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
    async def sources_override(request: Request):
        body = await request.json()
        key, url = body.get("key"), (body.get("mrf_url") or "").strip()
        if not key or not url:
            raise HTTPException(422, "key and mrf_url required")
        if not url.startswith("https://"):
            raise HTTPException(422, "mrf_url must be https://")
        return registry.save_override(key, url)

    # -- misc -------------------------------------------------------------------------------

    @app.get("/api/stats")
    def stats():
        with store.connect() as con:
            rates_n, tins, payers = con.execute(
                "SELECT count(*), count(DISTINCT tin_value), count(DISTINCT payer) FROM rates"
            ).fetchone()
            files_done = con.execute("SELECT count(*) FROM files WHERE status = 'done'").fetchone()[0]
            attention = _dicts(con.execute(
                """
                SELECT filename, status, ref_groups_skipped, error FROM files
                WHERE status IN ('failed', 'quarantined', 'pending_confirmation')
                   OR ref_groups_skipped > 0
                """
            ))
        return {"rates": rates_n, "tins": tins, "payers": payers,
                "files_done": files_done, "attention": attention,
                "enrichment": store.enrichment_progress(),
                "enrichment_mode": cfg.enrichment.mode,
                "default_grain": grain_of({}, cfg, store)}

    @app.get("/api/states")
    def states():
        """Distinct states that actually have enriched data — populates the
        state filter so an empty pick reads as 'no data yet', not a broken box."""
        return {"states": store.available_states()}

    @app.get("/api/payers")
    def payers():
        with store.connect() as con:
            rows = con.execute("SELECT DISTINCT payer FROM rates ORDER BY payer").fetchall()
        return {"payers": [r[0] for r in rows]}

    @app.get("/api/catalog")
    def catalog():
        return catalog_json()

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("API error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
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
