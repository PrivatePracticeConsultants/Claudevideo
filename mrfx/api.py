"""FastAPI JSON API over the DuckDB store + static dashboard hosting.

Every list endpoint filters/sorts/paginates SERVER-SIDE (spec §8.3); CSV export
reuses the exact same WHERE/ORDER SQL as the view that triggered it (§8.7).
"""

from __future__ import annotations

import datetime as dt
import logging
import tempfile
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import CPT_DESCRIPTIONS, MrfxConfig
from .ingest import ingest_file, scan_inbox
from .registry import Registry
from .store import Store

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"

SORTABLE = {
    "org_name", "npi", "payer", "billing_code", "modifier_set",
    "billing_class", "negotiated_rate", "negotiated_type", "source_count",
    "last_updated_on",
}

UPLOAD_LIMIT_BYTES = 1 << 30  # browser uploads capped at 1 GB per spec §5.4


class FilterSet:
    """Translates query params into a parameterized WHERE clause, shared by
    table, summary, and CSV export so the numbers always agree."""

    def __init__(
        self,
        payer: list[str],
        cpt: list[str],
        modifier: str | None,
        billing_class: str | None,
        q: str | None,
        dollar_only: bool,
        rate_min: float | None,
        rate_max: float | None,
    ):
        clauses, params = [], []
        if payer:
            clauses.append(f"payer IN ({', '.join('?' for _ in payer)})")
            params += payer
        if cpt:
            clauses.append(f"billing_code IN ({', '.join('?' for _ in cpt)})")
            params += cpt
        if modifier == "base":
            clauses.append("modifier_set = ''")
        elif modifier:
            clauses.append("('|' || modifier_set || '|') LIKE ('%|' || ? || '|%')")
            params.append(modifier)
        if billing_class:
            clauses.append("billing_class = ?")
            params.append(billing_class)
        if q:
            clauses.append("(npi LIKE ? OR upper(coalesce(org_name, '')) LIKE upper(?))")
            params += [f"%{q}%", f"%{q}%"]
        if dollar_only:
            clauses.append("is_dollar_rate")
        if rate_min is not None:
            clauses.append("negotiated_rate >= ?")
            params.append(rate_min)
        if rate_max is not None:
            clauses.append("negotiated_rate <= ?")
            params.append(rate_max)
        self.where = " AND ".join(clauses) if clauses else "1=1"
        self.params = params


BASE_REL = """
    SELECT d.*, coalesce(n.org_name, '') AS org_name, n.city, n.state
    FROM rates_dedup d LEFT JOIN npi_directory n USING (npi)
"""


def parse_filters(
    payer: str | None = None,
    cpt: str | None = None,
    modifier: str | None = None,
    billing_class: str | None = None,
    q: str | None = None,
    dollar_only: bool = True,
    rate_min: float | None = None,
    rate_max: float | None = None,
) -> FilterSet:
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()] if s else []  # noqa: E731
    return FilterSet(split(payer), split(cpt), modifier, billing_class, q, dollar_only, rate_min, rate_max)


def rel_sql(fs: FilterSet) -> str:
    return f"WITH base AS ({BASE_REL}) SELECT * FROM base WHERE {fs.where}"


def order_sql(sort: str, direction: str) -> str:
    if sort not in SORTABLE:
        sort = "negotiated_rate"
    direction = "ASC" if direction.lower() == "asc" else "DESC"
    tiebreak = ", npi ASC, billing_code ASC, modifier_set ASC" if sort != "npi" else ", billing_code ASC"
    return f"ORDER BY {sort} {direction} NULLS LAST{tiebreak}"


def export_select(fs: FilterSet, sort: str = "negotiated_rate", direction: str = "desc") -> tuple[str, list]:
    """The one export query. Excel-friendly: arrays ;-joined, plain numbers."""
    sql = f"""
        SELECT payer, npi, org_name, billing_code, billing_code_type,
               replace(modifier_set, '|', ';') AS modifiers,
               negotiated_rate, negotiated_type, is_dollar_rate, billing_class,
               replace(service_code_set, '|', ';') AS service_codes,
               last_updated_on, expiration_date, source_count
        FROM ({rel_sql(fs)}) {order_sql(sort, direction)}
    """
    return sql, fs.params


def order_export_sql(args) -> tuple[str, list]:
    """CLI adapter: argparse namespace -> the same export query the API uses."""
    fs = parse_filters(
        payer=args.payer, cpt=args.cpt, modifier=args.modifier,
        billing_class=args.billing_class, q=args.q,
        dollar_only=not args.all_types,
        rate_min=args.rate_min, rate_max=args.rate_max,
    )
    return export_select(fs)


def create_app(cfg: MrfxConfig, store: Store) -> FastAPI:
    app = FastAPI(title="MRF Explorer", docs_url="/api/docs")
    registry = Registry(cfg)

    # -- rates table ---------------------------------------------------------

    @app.get("/api/rates")
    def rates(
        request: Request,
        sort: str = "negotiated_rate",
        dir: str = "desc",
        page: int = Query(1, ge=1),
        page_size: int = Query(100, ge=1, le=1000),
    ):
        fs = _fs(request)
        offset = (page - 1) * page_size
        sql = f"{rel_sql(fs)} {order_sql(sort, dir)} LIMIT ? OFFSET ?"
        with store.connect() as con:
            rows = _dicts(con.execute(sql, [*fs.params, page_size, offset]))
            total = con.execute(
                f"SELECT count(*) FROM ({rel_sql(fs)})", fs.params
            ).fetchone()[0]
        return {"rows": rows, "total": total, "page": page, "page_size": page_size}

    @app.get("/api/summary")
    def summary(request: Request):
        fs = _fs(request)
        with store.connect() as con:
            row = con.execute(
                f"""
                SELECT count(*) AS n, count(DISTINCT npi) AS orgs,
                       min(negotiated_rate) AS min, quantile_cont(negotiated_rate, .25) AS p25,
                       median(negotiated_rate) AS median, quantile_cont(negotiated_rate, .75) AS p75,
                       max(negotiated_rate) AS max
                FROM ({rel_sql(fs)})
                """,
                fs.params,
            ).fetchone()
        keys = ["n", "orgs", "min", "p25", "median", "p75", "max"]
        return dict(zip(keys, row))

    # -- org & cpt views -------------------------------------------------------

    @app.get("/api/org/{npi}")
    def org_detail(npi: str):
        with store.connect() as con:
            info = _dicts(con.execute("SELECT * FROM npi_directory WHERE npi = ?", [npi]))
            rows = _dicts(con.execute(
                f"""
                SELECT payer, billing_code, modifier_set, billing_class, negotiated_type,
                       is_dollar_rate, negotiated_rate, source_count, last_updated_on
                FROM ({BASE_REL}) WHERE npi = ?
                ORDER BY billing_code, payer, modifier_set
                """,
                [npi],
            ))
            chart = _dicts(con.execute(
                f"""
                SELECT billing_code, payer, median(negotiated_rate) AS median_rate
                FROM ({BASE_REL}) WHERE npi = ? AND is_dollar_rate
                GROUP BY billing_code, payer ORDER BY billing_code, payer
                """,
                [npi],
            ))
        if not rows:
            raise HTTPException(404, f"no rates for NPI {npi}")
        return {"npi": npi, "directory": info[0] if info else None, "rates": rows, "chart": chart}

    @app.get("/api/cpt/{code}")
    def cpt_detail(code: str, dollar_only: bool = True, modifier: str | None = None):
        mod_clause = "AND modifier_set = ''" if modifier == "base" else (
            "AND ('|' || modifier_set || '|') LIKE ('%|' || ? || '|%')" if modifier else ""
        )
        mod_params = [modifier] if modifier and modifier != "base" else []
        dollar_clause = "AND is_dollar_rate" if dollar_only else ""
        with store.connect() as con:
            ranked = _dicts(con.execute(
                f"""
                SELECT npi, org_name, payer, modifier_set, billing_class,
                       median(negotiated_rate) AS median_rate,
                       min(negotiated_rate) AS min_rate, max(negotiated_rate) AS max_rate,
                       count(*) AS n
                FROM ({BASE_REL})
                WHERE billing_code = ? {dollar_clause} {mod_clause}
                GROUP BY ALL ORDER BY median_rate DESC
                LIMIT 500
                """,
                [code, *mod_params],
            ))
            hist = _dicts(con.execute(
                f"""
                WITH r AS (
                    SELECT negotiated_rate FROM ({BASE_REL})
                    WHERE billing_code = ? {dollar_clause} {mod_clause}
                )
                SELECT floor(negotiated_rate / g.w) * g.w AS bucket, count(*) AS n
                FROM r, (SELECT greatest((max(negotiated_rate) - min(negotiated_rate)) / 20, 0.01) AS w FROM r) g
                GROUP BY 1 ORDER BY 1
                """,
                [code, *mod_params],
            ))
        return {
            "billing_code": code,
            "description": CPT_DESCRIPTIONS.get(code),
            "ranked": ranked,
            "histogram": hist,
        }

    # -- files -----------------------------------------------------------------

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
        return {"files": rows}

    @app.post("/api/files/scan")
    def files_scan(background: BackgroundTasks):
        background.add_task(scan_inbox, cfg, store)
        return {"status": "scanning"}

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
        size = 0
        with open(dest, "wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > UPLOAD_LIMIT_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        413, "over 1 GB — drop the file into data/inbox/ instead of uploading"
                    )
                out.write(chunk)
        background.add_task(scan_inbox, cfg, store)
        return {"status": "queued", "filename": dest.name, "bytes": size}

    # -- export -----------------------------------------------------------------

    @app.get("/api/export.csv")
    def export_csv(
        request: Request,
        background: BackgroundTasks,
        view: str = "explorer",
        sort: str = "negotiated_rate",
        dir: str = "desc",
        full: bool = False,
    ):
        fs = _fs(request) if not full else parse_filters(dollar_only=False)
        select, params = export_select(fs, sort, dir)
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
        name = f"mrfx_{view}_{stamp}.csv"
        tmp = Path(tempfile.mkstemp(suffix=".csv")[1])
        with store.connect() as con:
            con.execute(
                f"COPY ({select}) TO '{tmp}' (FORMAT CSV, HEADER)", params
            )
        final = tmp.with_suffix(".bom.csv")
        with open(final, "wb") as out:
            out.write(b"\xef\xbb\xbf")
            out.write(tmp.read_bytes())
        tmp.unlink()
        background.add_task(final.unlink, missing_ok=True)
        return FileResponse(final, filename=name, media_type="text/csv")

    # -- sources / registry ------------------------------------------------------

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

    # -- misc ---------------------------------------------------------------------

    @app.get("/api/stats")
    def stats():
        with store.connect() as con:
            rates_n, orgs, payers = con.execute(
                "SELECT count(*), count(DISTINCT npi), count(DISTINCT payer) FROM rates"
            ).fetchone()
            files_done = con.execute(
                "SELECT count(*) FROM files WHERE status = 'done'"
            ).fetchone()[0]
            attention = _dicts(con.execute(
                """
                SELECT filename, status, ref_groups_skipped, error FROM files
                WHERE status IN ('failed', 'quarantined', 'pending_confirmation')
                   OR ref_groups_skipped > 0
                """
            ))
        return {
            "rates": rates_n, "orgs": orgs, "payers": payers,
            "files_done": files_done, "attention": attention,
        }

    @app.get("/api/payers")
    def payers():
        with store.connect() as con:
            rows = con.execute("SELECT DISTINCT payer FROM rates ORDER BY payer").fetchall()
        return {"payers": [r[0] for r in rows]}

    @app.get("/api/cpt_descriptions")
    def cpt_descriptions():
        return CPT_DESCRIPTIONS

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("API error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def _fs(request: Request) -> FilterSet:
    qp = request.query_params
    return parse_filters(
        payer=qp.get("payer"),
        cpt=qp.get("cpt"),
        modifier=qp.get("modifier"),
        billing_class=qp.get("billing_class"),
        q=qp.get("q"),
        dollar_only=qp.get("dollar_only", "1") not in ("0", "false"),
        rate_min=float(qp["rate_min"]) if qp.get("rate_min") else None,
        rate_max=float(qp["rate_max"]) if qp.get("rate_max") else None,
    )


def _dicts(cursor) -> list[dict]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]
