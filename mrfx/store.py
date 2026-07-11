"""DuckDB-backed Parquet store — V4 TIN-grain data model.

- `rates`: raw rows (one per extracted price × provider), parquet part per
  source file (re-ingest overwrites its part; idempotent).
- `rates_by_tin` (materialized): the app's spine — one row per
  (payer, tin_value, billing_code, modifier_set, billing_class,
  service_code_set, file_month) with npi_count / source_count /
  rate_variants. NPI rows stay available for drill-down (`rates_dedup`).
- `tin_directory` (derived): TIN -> display name (rolled up from the NPPES
  org names of its NPIs), entity kind, npi_count, states, discipline.
- `entity_map`: user-defined TIN -> entity grouping (config/entity_map.yaml).
- `files`, `npi_directory`, `provider_refs`, `peer_sets`, `mpfs`.

Writes are serialized behind a lock; readers never touch the DuckDB catalog
(views are registered only at init and inside locked write paths).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

RATES_SCHEMA = pa.schema(
    [
        pa.field("payer", pa.string(), nullable=False),
        pa.field("source_file", pa.string(), nullable=False),
        pa.field("file_month", pa.string()),
        pa.field("schema_version", pa.string()),
        pa.field("last_updated_on", pa.string()),
        pa.field("tin_value", pa.string()),
        pa.field("tin_type", pa.string()),
        pa.field("tin_is_really_npi", pa.bool_()),
        pa.field("npi", pa.string(), nullable=False),
        pa.field("billing_code", pa.string(), nullable=False),
        pa.field("billing_code_type", pa.string()),
        pa.field("discipline", pa.string()),
        pa.field("is_timed", pa.bool_()),
        pa.field("billing_code_modifier", pa.list_(pa.string())),
        pa.field("negotiated_rate", pa.float64()),
        pa.field("negotiated_type", pa.string()),
        pa.field("is_dollar_rate", pa.bool_()),
        pa.field("billing_class", pa.string()),
        pa.field("service_code", pa.list_(pa.string())),
        pa.field("expiration_date", pa.string()),
        pa.field("ingested_at", pa.string()),
    ]
)


def file_key(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()[:16]


def sql_path(p) -> str:
    """Quote a filesystem path for inlining in SQL: DuckDB has no parameter
    binding for SET/DDL statements, and a directory named O'Brien would
    otherwise break the statement."""
    return str(p).replace("'", "''")


# --- SSN masking (§7A.7b) ----------------------------------------------------
# EINs and SSNs are both 9 digits. A number is masked when it satisfies SSN
# structural rules AND its 2-digit prefix is not a valid IRS EIN campus prefix.
_VALID_EIN_PREFIXES = {
    "01","02","03","04","05","06","10","11","12","13","14","15","16",
    "20","21","22","23","24","25","26","27","30","31","32","33","34","35",
    "36","37","38","39","40","41","42","43","44","45","46","47","48",
    "50","51","52","53","54","55","56","57","58","59","60","61","62","63",
    "64","65","66","67","68","71","72","73","74","75","76","77",
    "80","81","82","83","84","85","86","87","88","90","91","92","93","94",
    "95","98","99",
}
_SSN_RE = re.compile(r"^(?!000|666|9\d\d)\d{3}(?!00\d{4})\d{2}(?!0000)\d{4}$")


def looks_like_ssn(tin: str | None) -> bool:
    if not tin or not re.fullmatch(r"\d{9}", tin):
        return False
    return tin[:2] not in _VALID_EIN_PREFIXES and bool(_SSN_RE.match(tin))


def mask_tin(tin: str | None) -> str | None:
    """UI/export-safe TIN: SSN-pattern numbers are never shown raw."""
    if tin is None:
        return None
    return "MASKED-SSN" if looks_like_ssn(tin) else tin


# --- dedup / rollup queries ----------------------------------------------------

# NPI-grain dedup (drill-down + npi grain toggle)
# materialized rollups rebuild in hash-partitioned passes above this many raw
# rows — identical output, bounded memory/temp-disk per pass
ROLLUP_PARTITION_ROWS = 15_000_000

DEDUP_QUERY = """
    SELECT payer, npi, billing_code,
           any_value(billing_code_type)                              AS billing_code_type,
           any_value(discipline)                                     AS discipline,
           any_value(is_timed)                                       AS is_timed,
           coalesce(array_to_string(billing_code_modifier, '|'), '') AS modifier_set,
           negotiated_rate, billing_class,
           coalesce(array_to_string(service_code, '|'), '')          AS service_code_set,
           file_month,
           any_value(tin_value)                                      AS tin_value,
           bool_or(tin_is_really_npi)                                AS tin_is_really_npi,
           any_value(negotiated_type)                                AS negotiated_type,
           bool_or(is_dollar_rate)                                   AS is_dollar_rate,
           any_value(schema_version)                                 AS schema_version,
           max(last_updated_on)                                      AS last_updated_on,
           any_value(expiration_date)                                AS expiration_date,
           count(DISTINCT source_file)                               AS source_count,
           -- representative file, not the full list: string_agg(DISTINCT)
           -- over tens of millions of near-unique groups cannot spill and
           -- explodes memory/temp disk; source_count carries the cardinality
           -- and the raw rates table has the complete list on demand
           any_value(source_file)                                    AS source_files,
           1                                                         AS rate_variants,
           1                                                         AS npi_count
    FROM rates
    GROUP BY payer, npi, billing_code,
             coalesce(array_to_string(billing_code_modifier, '|'), ''),
             negotiated_rate, billing_class,
             coalesce(array_to_string(service_code, '|'), ''), file_month
"""

# TIN-grain spine (§4). Distinct rates within the tuple become rate_variants;
# negotiated_rate is the median of the distinct values (min/max kept).
# {part} is a partition predicate (TRUE for a single-shot build): billing_code
# is part of the GROUP BY key, so building the table in hash(billing_code)
# slices yields byte-identical results at a fraction of the memory/temp-disk
# peak — the DISTINCT/median aggregates are what exhausted a 23 GB temp dir
# on a 64M-row store when run in one shot.
BY_TIN_QUERY = """
    SELECT payer, tin_value, billing_code,
           any_value(tin_type)                                       AS tin_type,
           bool_or(tin_is_really_npi)                                AS tin_is_really_npi,
           any_value(billing_code_type)                              AS billing_code_type,
           any_value(discipline)                                     AS discipline,
           any_value(is_timed)                                       AS is_timed,
           coalesce(array_to_string(billing_code_modifier, '|'), '') AS modifier_set,
           billing_class,
           coalesce(array_to_string(service_code, '|'), '')          AS service_code_set,
           file_month, is_dollar_rate,
           -- round the aggregate to 4 dp: kills float-median noise
           -- (86.835000000001) while preserving any legitimate sub-cent median
           round(median(DISTINCT negotiated_rate), 4)                AS negotiated_rate,
           round(min(negotiated_rate), 4)                            AS rate_min,
           round(max(negotiated_rate), 4)                            AS rate_max,
           count(DISTINCT negotiated_rate)                           AS rate_variants,
           count(DISTINCT npi)                                       AS npi_count,
           count(DISTINCT source_file)                               AS source_count,
           any_value(source_file)                                    AS source_files,
           any_value(negotiated_type)                                AS negotiated_type,
           any_value(schema_version)                                 AS schema_version,
           max(last_updated_on)                                      AS last_updated_on,
           any_value(expiration_date)                                AS expiration_date
    FROM rates
    WHERE {part}
    GROUP BY payer, tin_value, billing_code,
             coalesce(array_to_string(billing_code_modifier, '|'), ''),
             billing_class,
             coalesce(array_to_string(service_code, '|'), ''),
             file_month, is_dollar_rate
"""

# TIN directory (§3.3a): display name from NPPES org names of the TIN's
# Type-2 NPIs (mode); individual-billed TINs labeled by dominant person name.
# {part} partitions on tin_value (every group key below is a single TIN, so
# hash(tin_value) slices rebuild to identical results) — see BY_TIN_QUERY.
TIN_DIRECTORY_QUERY = """
    WITH tin_npis AS (
        SELECT DISTINCT tin_value, npi
        FROM rates
        WHERE tin_value IS NOT NULL AND NOT tin_is_really_npi AND {part}
    ),
    joined AS (
        SELECT t.tin_value, t.npi, d.entity_type, d.org_name, d.state, d.city
        FROM tin_npis t LEFT JOIN npi_directory d USING (npi)
    ),
    names AS (
        SELECT tin_value,
               mode(org_name) FILTER (entity_type = 'NPI-2' AND org_name IS NOT NULL) AS org_mode,
               mode(org_name) FILTER (org_name IS NOT NULL)                           AS any_mode,
               count(*) FILTER (entity_type = 'NPI-2')                                AS n_orgs
        FROM joined GROUP BY tin_value
    ),
    disc AS (
        SELECT tin_value, mode(discipline) AS primary_discipline
        FROM rates WHERE tin_value IS NOT NULL AND discipline != 'unspecified' AND {part}
        GROUP BY tin_value
    )
    SELECT j.tin_value,
           any_value(r.tin_type)                                    AS tin_type,
           -- the no-name fallback label must never surface a raw SSN-pattern
           -- TIN: display_name renders in the UI and every export
           coalesce(any_value(n.org_mode), any_value(n.any_mode),
                    'TIN ' || CASE WHEN regexp_full_match(j.tin_value, '[0-9]{{9}}')
                                        AND substr(j.tin_value, 1, 2) IN
                                        ('00','07','08','09','17','18','19','28','29',
                                         '49','69','70','78','79','89','96','97')
                                   THEN 'MASKED-SSN' ELSE j.tin_value END) AS display_name,
           CASE WHEN any_value(n.n_orgs) > 0 THEN 'org'
                ELSE 'individual-billed' END                        AS entity_kind,
           count(DISTINCT j.npi)                                    AS npi_count,
           list_sort(list_distinct(list(j.state)
                     FILTER (j.state IS NOT NULL)))                 AS states,
           list_sort(list_distinct(list(j.city)
                     FILTER (j.city IS NOT NULL)))                  AS cities,
           any_value(d2.primary_discipline)                         AS primary_discipline
    FROM joined j
    LEFT JOIN names n ON n.tin_value = j.tin_value
    LEFT JOIN disc d2 ON d2.tin_value = j.tin_value
    LEFT JOIN (SELECT DISTINCT tin_value, tin_type FROM rates WHERE {part}) r
           ON r.tin_value = j.tin_value
    GROUP BY j.tin_value
"""


class Store:
    def __init__(self, store_dir: Path):
        self.dir = Path(store_dir)
        self.rates_dir = self.dir / "rates"
        self.rates_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_dir = self.dir / "duckdb_tmp"
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "mrfx.duckdb"
        self.write_lock = threading.Lock()
        self._sweep_orphan_tmps()
        try:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            self._memory_limit_gb = max(2, min(12, int(total * 0.4 / 1e9)))
        except (ValueError, OSError, AttributeError):
            self._memory_limit_gb = 4
        self._temp_cap_gb: int | None = None  # computed once, in connect()
        with self.write_lock, self.connect() as con:
            self._init_tables(con)
            self._register_views(con)

    def _sweep_orphan_tmps(self) -> None:
        """Remove half-written `.{key}.{pid}.parquet.tmp` parts (and
        `.{key}.{pid}.progress` sidecars) whose writing process is gone
        (crash/kill mid-ingest). Temps owned by a LIVE process are left
        alone — that's the whole point of the pid in the name."""
        for p in [*self.rates_dir.glob(".*.parquet.tmp"),
                  *self.rates_dir.glob(".*.progress"),
                  *self.rates_dir.glob(".*.progress.tmp")]:
            # both name shapes carry the pid right after the file key:
            # .{key}.{pid}.parquet.tmp / .{key}.{pid}.progress[.tmp]
            m = re.match(r"^\.(.+)\.(\d+)\.(parquet\.tmp|progress(\.tmp)?)$", p.name)
            pid = m.group(2) if m else None
            if pid is not None:
                try:
                    os.kill(int(pid), 0)
                    continue  # owner is alive — in-progress write, keep it
                except ProcessLookupError:
                    pass  # owner is dead — orphan
                except PermissionError:
                    continue  # alive, owned by another user
            p.unlink(missing_ok=True)

    # -- connections --------------------------------------------------------

    def connect(self) -> duckdb.DuckDBPyConnection:
        """Plain connection. Readers never touch the catalog; views are
        (re)registered only at init and inside locked write paths.

        Each connection is told to spill to a temp dir under the store so the
        big rollup GROUP BYs over national files (millions of rows) never OOM —
        DuckDB streams to disk under memory pressure instead."""
        # Transient lock conflicts are a fact of life: the user may open the
        # .duckdb file read-only (CLI, a BI tool) while the app runs, and even
        # a millisecond-held external lock lands exactly between our
        # short-lived connections sometimes. Retry briefly instead of letting
        # one unlucky race kill an hours-long ingest run.
        last_exc: Exception | None = None
        for attempt in range(6):
            try:
                con = duckdb.connect(str(self.db_path))
                break
            except duckdb.IOException as e:
                if "lock" not in str(e).lower():
                    raise
                last_exc = e
                time.sleep(min(0.2 * (2 ** attempt), 3.0))
        else:
            raise last_exc  # 6 attempts over ~6s — something genuinely holds it
        try:
            con.execute(f"SET temp_directory = '{sql_path(self._tmp_dir)}'")
            con.execute("SET preserve_insertion_order = false")
            # DuckDB's default memory limit is ~80% of system RAM; a rollup
            # rebuild over tens of millions of rows will happily balloon to
            # that before spilling — enough to get the process OOM-killed
            # when anything else is running. But too tight a cap fails the
            # rollup outright (its DISTINCT aggregates cannot spill), so use
            # 40% of RAM clamped to [2GB, 12GB]: kernel-safe headroom AND
            # room for stores with tens of millions of rows.
            con.execute(f"SET memory_limit = '{self._memory_limit_gb}GB'")
            # cap spill so one huge rollup can NEVER fill the disk and take
            # down unrelated work: leave ~20% of free space untouched.
            # CRITICAL: this SET is GLOBAL to the process's shared DuckDB
            # instance, and the cap is computed ONCE per Store — recomputing
            # per connection let every dashboard poll shrink the cap to 80%
            # of the free space a RUNNING rebuild had already spilled into,
            # strangling the rebuild it was meant to protect.
            if self._temp_cap_gb is None:
                free_gb = shutil.disk_usage(self._tmp_dir).free / 1e9
                self._temp_cap_gb = max(1, int(free_gb * 0.8))
            con.execute(f"SET max_temp_directory_size = '{self._temp_cap_gb}GB'")
        except duckdb.Error:  # older duckdb without these knobs
            pass
        return con

    def _init_tables(self, con: duckdb.DuckDBPyConnection) -> None:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                filename VARCHAR PRIMARY KEY,
                payer VARCHAR,
                file_type VARCHAR,
                status VARCHAR,
                schema_version VARCHAR,
                last_updated_on VARCHAR,
                size_bytes BIGINT,
                rows_emitted BIGINT DEFAULT 0,
                ref_groups_skipped BIGINT DEFAULT 0,
                error VARCHAR,
                preflight VARCHAR,
                qa VARCHAR,
                progress DOUBLE DEFAULT 0,
                chunks_done BIGINT DEFAULT 0,
                chunks_total BIGINT DEFAULT 0,
                started_at TIMESTAMP,
                finished_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS npi_directory (
                npi VARCHAR PRIMARY KEY,
                entity_type VARCHAR,
                org_name VARCHAR,
                taxonomy_code VARCHAR,
                taxonomy_desc VARCHAR,
                city VARCHAR,
                state VARCHAR,
                address VARCHAR,
                zip VARCHAR,
                phone VARCHAR,
                enriched_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS provider_refs (
                payer VARCHAR,
                source_file VARCHAR,
                last_updated_on VARCHAR,
                ref_id BIGINT,
                tin_value VARCHAR,
                tin_type VARCHAR,
                npis VARCHAR[]
            );
            CREATE TABLE IF NOT EXISTS entity_map (
                tin_value VARCHAR PRIMARY KEY,
                entity_name VARCHAR
            );
            CREATE TABLE IF NOT EXISTS peer_sets (
                name VARCHAR PRIMARY KEY,
                definition VARCHAR,      -- JSON: mode, filters or tin list
                created_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS mpfs (
                code VARCHAR,
                locality VARCHAR,
                non_facility_rate DOUBLE,
                source VARCHAR
            );
            CREATE TABLE IF NOT EXISTS url_queue (
                id BIGINT PRIMARY KEY,
                url VARCHAR,
                dedup_key VARCHAR,          -- url without volatile query (signed URLs)
                kind VARCHAR,               -- unknown | in_network | provider_reference | toc
                status VARCHAR,             -- queued | downloading | expanding | ingesting | done | failed | skipped
                parent_id BIGINT,           -- the TOC this url was discovered from
                filename VARCHAR,           -- local filename once downloaded/ingested
                bytes_total BIGINT,
                bytes_done BIGINT,
                progress DOUBLE DEFAULT 0,
                rows_emitted BIGINT DEFAULT 0,
                child_count BIGINT DEFAULT 0,
                error VARCHAR,
                content_sha VARCHAR,        -- sha256 of downloaded bytes (cross-host dupe detection)
                force_size BOOLEAN DEFAULT false,  -- user chose "download anyway" past confirm_over_gb
                added_at TIMESTAMP,
                finished_at TIMESTAMP
            );
            """
        )
        # migrate pre-existing stores created before the outreach columns
        for col in ("address", "zip", "phone"):
            con.execute(f"ALTER TABLE npi_directory ADD COLUMN IF NOT EXISTS {col} VARCHAR")
        # migrate stores created before the chunked-progress columns
        for col, typ in (("progress", "DOUBLE"), ("chunks_done", "BIGINT"), ("chunks_total", "BIGINT")):
            con.execute(f"ALTER TABLE files ADD COLUMN IF NOT EXISTS {col} {typ}")
        # migrate stores created before content-hash duplicate detection
        con.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS content_sha VARCHAR")
        # migrate stores created before the "download anyway" size override
        con.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS force_size BOOLEAN DEFAULT false")

    def _register_views(self, con: duckdb.DuckDBPyConnection) -> None:
        """(Re)point the derived views. Callers must hold write_lock."""
        glob = sql_path(self.rates_dir / "*.parquet")
        if any(self.rates_dir.glob("*.parquet")):
            con.execute(f"CREATE OR REPLACE VIEW rates AS SELECT * FROM read_parquet('{glob}')")
        else:
            cols = ", ".join(
                f"CAST(NULL AS {t}) AS {f.name}"
                for f, t in zip(RATES_SCHEMA, _duck_types())
            )
            con.execute(f"CREATE OR REPLACE VIEW rates AS SELECT * FROM (SELECT {cols}) WHERE FALSE")
        for view, tbl, q in (
            ("rates_dedup", "rates_dedup_tbl", DEDUP_QUERY),
            ("rates_by_tin", "rates_by_tin_tbl", BY_TIN_QUERY.format(part="TRUE")),
            ("tin_directory", "tin_directory_tbl", TIN_DIRECTORY_QUERY.format(part="TRUE")),
        ):
            has = con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [tbl]
            ).fetchone()[0]
            con.execute(
                f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM {tbl}" if has
                else f"CREATE OR REPLACE VIEW {view} AS {q}"
            )

    # -- rates parts ---------------------------------------------------------

    def write_rates_part(self, source_file: str, rows: list[dict]) -> Path | None:
        if not rows:
            return None
        table = pa.Table.from_pylist(rows, schema=RATES_SCHEMA)
        path = self.rates_dir / f"{file_key(source_file)}.parquet"
        with self.write_lock:
            pq.write_table(table, path)
            with self.connect() as con:
                self._register_views(con)
        return path

    def finalize_rates_part(self, source_file: str, tmp_path: Path, rows_written: int) -> None:
        """Adopt a parquet part written OUTSIDE this store (a parallel parse
        worker writes to a temp path with no DB access; only this process
        touches DuckDB). Mirrors RatesPartWriter's atomic finish."""
        path = self.rates_dir / f"{file_key(source_file)}.parquet"
        tmp = Path(tmp_path)
        with self.write_lock:
            if rows_written:
                # replace() overwrites atomically — a preceding unlink would
                # open a no-file window for lock-free concurrent readers
                tmp.replace(path)
            else:
                tmp.unlink(missing_ok=True)
                path.unlink(missing_ok=True)  # re-ingest that now yields 0 rows
            with self.connect() as con:
                self._register_views(con)

    def rates_part_writer(self, source_file: str) -> "RatesPartWriter":
        """Streaming writer: accept row batches and flush them to the part
        incrementally, so a huge file never holds all its rows in memory.
        Writes to a temp file, then atomically swaps it into place on close."""
        return RatesPartWriter(self, source_file)

    def update_progress(self, filename: str, progress: float,
                        chunks_done: int | None = None, chunks_total: int | None = None) -> None:
        """Lightweight progress write for the dashboard bar (no view churn).
        BEST-EFFORT by contract: it is called from inside multi-hour parse
        loops, and a transient store error here (user opened the .duckdb in a
        BI tool) must never fail the parse it decorates."""
        sets = ["progress = ?"]
        params: list = [round(progress, 3)]
        if chunks_done is not None:
            sets.append("chunks_done = ?")
            params.append(chunks_done)
        if chunks_total is not None:
            sets.append("chunks_total = ?")
            params.append(chunks_total)
        params.append(filename)
        try:
            with self.write_lock, self.connect() as con:
                con.execute(f"UPDATE files SET {', '.join(sets)} WHERE filename = ?", params)
        except Exception as e:  # noqa: BLE001
            import logging as _logging

            _logging.getLogger(__name__).debug(
                "progress write for %s skipped (%s) — cosmetic only", filename, e)

    # -- URL queue ----------------------------------------------------------

    def enqueue_url(self, url: str, dedup_key: str, parent_id: int | None = None) -> int | None:
        """Add a URL to the download queue. Deduped by dedup_key (URL without
        volatile signed-query params). Returns the new row id, or None if it's
        already queued/done."""
        with self.write_lock, self.connect() as con:
            return self._enqueue_one(con, url, dedup_key, parent_id)

    def enqueue_urls(self, pairs: list[tuple[str, str]], parent_id: int | None = None) -> int:
        """Batch enqueue under ONE lock + connection — a 2,000-child index
        expansion must not pay a connection/lock cycle per child. Same
        semantics as enqueue_url per row. Returns how many were newly queued
        (or revived)."""
        added = 0
        with self.write_lock, self.connect() as con:
            for url, dk in pairs:
                if self._enqueue_one(con, url, dk, parent_id) is not None:
                    added += 1
        return added

    def _enqueue_one(self, con: duckdb.DuckDBPyConnection, url: str, dedup_key: str,
                     parent_id: int | None) -> int | None:
        exists = con.execute(
            "SELECT id, status FROM url_queue WHERE dedup_key = ? LIMIT 1", [dedup_key]
        ).fetchone()
        if exists:
            # Re-adding a failed/over-size link retries it (a fresh URL may
            # carry a new signature; or the user raised confirm_over_gb). A
            # 'skipped' row is only revived by a DIRECT paste (parent_id
            # None): re-expanding a TOC must not undo the user's explicit
            # skip or re-download a known duplicate. force_size is reset so a
            # re-paste re-checks the size ceiling rather than silently
            # inheriting an old "download anyway".
            retryable = (("failed", "oversize") if parent_id is not None
                         else ("failed", "skipped", "oversize"))
            if exists[1] in retryable:
                con.execute(
                    "UPDATE url_queue SET url = ?, status = 'queued', error = NULL, "
                    "progress = 0, bytes_done = 0, force_size = false WHERE id = ?",
                    [url, exists[0]],
                )
                return exists[0]
            return None  # already queued / in flight / done
        nid = (con.execute("SELECT coalesce(max(id), 0) + 1 FROM url_queue").fetchone()[0])
        con.execute(
            "INSERT INTO url_queue (id, url, dedup_key, kind, status, parent_id, "
            "bytes_total, bytes_done, progress, added_at) "
            "VALUES (?, ?, ?, 'unknown', 'queued', ?, 0, 0, 0, current_timestamp)",
            [nid, url, dedup_key, parent_id],
        )
        return nid

    def next_queued_url(self) -> dict | None:
        return self._claim_next("queued", "downloading")

    def next_fetched_url(self) -> dict | None:
        """Claim the next prefetched row (file already on disk) for processing."""
        return self._claim_next("fetched", "ingesting")

    def _claim_next(self, from_status: str, to_status: str) -> dict | None:
        with self.write_lock, self.connect() as con:
            row = con.execute(
                f"SELECT * FROM url_queue WHERE status = '{from_status}' ORDER BY id LIMIT 1"
            ).fetchall()
            if not row:
                return None
            cols = [d[0] for d in con.description]
            rec = dict(zip(cols, row[0]))
            con.execute("UPDATE url_queue SET status = ? WHERE id = ?", [to_status, rec["id"]])
            return rec

    def update_url(self, url_id: int, **fields) -> None:
        allowed = {"kind", "status", "filename", "bytes_total", "bytes_done",
                   "progress", "rows_emitted", "child_count", "error", "parent_id",
                   "content_sha"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if fields.get("status") in ("done", "failed", "skipped", "oversize"):
            fields["finished_at"] = dt.datetime.now(dt.timezone.utc)
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.write_lock, self.connect() as con:
            con.execute(f"UPDATE url_queue SET {sets} WHERE id = ?", [*fields.values(), url_id])

    def url_progress(self, url_id: int, bytes_done: int, bytes_total: int | None) -> None:
        """BEST-EFFORT by contract: called from inside download stream loops —
        a transient store error must never fail the download it decorates."""
        pct = (100.0 * bytes_done / bytes_total) if bytes_total else 0.0
        try:
            with self.write_lock, self.connect() as con:
                con.execute(
                    "UPDATE url_queue SET bytes_done = ?, bytes_total = ?, progress = ? WHERE id = ?",
                    [bytes_done, bytes_total or 0, round(pct, 2), url_id],
                )
        except Exception as e:  # noqa: BLE001
            import logging as _logging

            _logging.getLogger(__name__).debug(
                "download-progress write for url %s skipped (%s) — cosmetic only", url_id, e)

    def list_urls(self, limit: int = 500) -> list[dict]:
        """Rows the user pasted (top-level, no parent) are listed first — a
        764-file index expansion must not push the row the user is watching
        out of the window — then children with the ACTIONABLE statuses
        (failed/skipped: the only rows with retry buttons — a file forgotten
        mid-grind on a 2,000-row queue must keep its retry button reachable),
        then the newest `limit` other children. Each slice is capped at
        `limit` so the payload stays bounded."""
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM url_queue WHERE parent_id IS NULL ORDER BY id DESC LIMIT ?",
                [limit],
            ).fetchall()
            rows += con.execute(
                "SELECT * FROM url_queue WHERE parent_id IS NOT NULL "
                "AND status IN ('failed', 'skipped', 'oversize') ORDER BY id DESC LIMIT ?",
                [limit],
            ).fetchall()
            rows += con.execute(
                "SELECT * FROM url_queue WHERE parent_id IS NOT NULL "
                "AND status NOT IN ('failed', 'skipped', 'oversize') ORDER BY id DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in con.description]
        out, seen = [], set()
        for r in rows:
            d = dict(zip(cols, r))
            if d["id"] in seen:
                continue  # slices run as separate queries; a row changing
                # status between them must not render twice in one poll
            seen.add(d["id"])
            for k in ("added_at", "finished_at"):
                if d.get(k) is not None:
                    d[k] = str(d[k])
            out.append(d)
        return out

    def url_queue_counts(self) -> dict:
        with self.connect() as con:
            rows = con.execute("SELECT status, count(*) FROM url_queue GROUP BY status").fetchall()
        return {s: n for s, n in rows}

    def find_url_with_same_content(self, content_sha: str, exclude_id: int) -> tuple[str, str] | None:
        """(url, status) of an already-ingested queue row whose downloaded
        bytes were identical (Blue plans host copies of each other's national
        files, so the same file arrives under many domains). None if no match.

        'ingesting' twins count too: with parallel workers, two mirror copies
        can be claimed simultaneously — if only 'done' matched, both would
        pass this check and the same file would land twice. Only LOWER-id
        in-flight twins defer this row (asymmetric on purpose: two concurrent
        twins must not both skip in favor of each other — the lower id always
        proceeds). If the ingesting twin later fails, the skipped row can be
        retried from the dashboard."""
        if not content_sha:
            return None
        with self.connect() as con:
            row = con.execute(
                "SELECT url, status FROM url_queue WHERE content_sha = ? AND id != ? "
                "AND (status = 'done' OR (status = 'ingesting' AND id < ?)) "
                "ORDER BY id LIMIT 1",
                [content_sha, exclude_id, exclude_id],
            ).fetchone()
        return (row[0], row[1]) if row else None

    def revive_skipped_duplicates(self, content_sha: str, failed_id: int) -> int:
        """The in-flight twin this sha's duplicates deferred to has FAILED:
        re-queue them. Their downloads were kept on disk, so they ingest
        without re-downloading (expired signed URLs don't matter).

        Only kind='duplicate' rows qualify — those are the ones the DEDUP
        deferred. Rows the USER skipped or forgot share the sha but must
        never be resurrected behind their back."""
        if not content_sha:
            return 0
        with self.write_lock, self.connect() as con:
            n = con.execute(
                "SELECT count(*) FROM url_queue WHERE content_sha = ? "
                "AND status = 'skipped' AND kind = 'duplicate' AND id != ?",
                [content_sha, failed_id],
            ).fetchone()[0]
            if n:
                con.execute(
                    "UPDATE url_queue SET status = 'queued', "
                    "error = 'the identical link this deferred to failed — "
                    "retrying (from the kept download when still present)' "
                    "WHERE content_sha = ? AND status = 'skipped' "
                    "AND kind = 'duplicate' AND id != ?",
                    [content_sha, failed_id])
        return n

    def skipped_duplicate_urls(self, content_sha: str, done_id: int) -> list[str]:
        """URLs of dedup-deferred duplicates of this sha (for cleaning their
        kept downloads once a twin has ingested successfully)."""
        if not content_sha:
            return []
        with self.connect() as con:
            rows = con.execute(
                "SELECT url FROM url_queue WHERE content_sha = ? "
                "AND status = 'skipped' AND kind = 'duplicate' AND id != ?",
                [content_sha, done_id],
            ).fetchall()
        return [r[0] for r in rows]

    def url_inflight_for_filename(self, filename: str) -> bool:
        """True if any queue row that previously produced this file is being
        re-processed right now (user pressed retry; the files row keeps its
        old terminal status for the whole download, so forget must ask the
        QUEUE, not just the files table)."""
        with self.connect() as con:
            return con.execute(
                "SELECT 1 FROM url_queue WHERE filename = ? AND status IN "
                "('queued', 'downloading', 'fetched', 'expanding', 'ingesting') LIMIT 1",
                [filename],
            ).fetchone() is not None

    def recover_stuck_files(self) -> int:
        """Files-table twin of recover_stuck_urls: an inbox ingest killed
        mid-parse leaves its row at 'processing', and scan_inbox skips
        'processing' files forever. Flip them to failed with a plain reason —
        the next inbox scan re-ingests them. Called when a worker starts
        (same single-owner assumption as recover_stuck_urls)."""
        with self.write_lock, self.connect() as con:
            n = con.execute(
                "SELECT count(*) FROM files WHERE status = 'processing'"
            ).fetchone()[0]
            if n:
                con.execute(
                    "UPDATE files SET status = 'failed', "
                    "error = 'interrupted by a restart — will re-ingest on the next scan' "
                    "WHERE status = 'processing'"
                )
        return n

    def recover_stuck_urls(self) -> int:
        """Rows left mid-flight by a crash/Ctrl-C go back to queued so the
        next run resumes them ('fetched' rows whose download + sidecar
        survived are reused as-is; otherwise they re-download — either way
        the content hash prevents any double ingest). Called when a queue
        worker starts."""
        with self.write_lock, self.connect() as con:
            n = con.execute(
                "SELECT count(*) FROM url_queue WHERE status IN "
                "('downloading', 'fetched', 'expanding', 'ingesting')"
            ).fetchone()[0]
            if n:
                con.execute(
                    "UPDATE url_queue SET status = 'queued', progress = 0, bytes_done = 0 "
                    "WHERE status IN ('downloading', 'fetched', 'expanding', 'ingesting')"
                )
        return n

    # legal transitions for user actions: retry revives dead rows; skip
    # cancels rows not yet claimed by the worker. Nothing may touch 'done'
    # (its content_sha anchors duplicate detection) or in-flight rows (the
    # worker's final write would silently overwrite the change anyway).
    _USER_TRANSITIONS = {
        # 'oversize' is a too-big-needs-OK terminal state: retry (→queued)
        # works once the user has raised confirm_over_gb, and skip dismisses it
        "queued": ("failed", "skipped", "oversize"),
        "skipped": ("queued", "failed", "oversize"),
    }

    def set_url_status_by_id(self, url_id: int, status: str) -> bool:
        allowed_from = self._USER_TRANSITIONS.get(status)
        if allowed_from is None:
            return False
        with self.write_lock, self.connect() as con:
            r = con.execute("SELECT status FROM url_queue WHERE id = ?", [url_id]).fetchone()
            if not r or r[0] not in allowed_from:
                return False
            con.execute(
                "UPDATE url_queue SET status = ?, error = NULL, progress = 0, bytes_done = 0, "
                # plain retry must NOT inherit a prior "download anyway" — that
                # override is one-shot; only force_size_requeue re-sets it
                "force_size = false WHERE id = ?", [status, url_id])
            return True

    def force_size_requeue(self, url_id: int) -> bool:
        """User pressed 'download anyway' on a row the confirm_over_gb guard
        stopped: set the per-row override and re-queue it. Only a failed or
        skipped row qualifies (same rule as retry); the disk-space guard
        still applies, so this can't fill the drive — it only lifts the
        are-you-sure size ceiling for THIS file."""
        with self.write_lock, self.connect() as con:
            r = con.execute("SELECT status FROM url_queue WHERE id = ?", [url_id]).fetchone()
            if not r or r[0] not in ("failed", "skipped", "oversize"):
                return False
            con.execute(
                "UPDATE url_queue SET status = 'queued', force_size = true, error = NULL, "
                "progress = 0, bytes_done = 0 WHERE id = ?", [url_id])
            return True

    def requeue_failed(self) -> int:
        """Bulk retry: every failed row back to queued in ONE statement — the
        per-row path capped at list_urls' window silently stranded failures
        beyond the newest 500. Returns rows re-queued."""
        with self.write_lock, self.connect() as con:
            n = con.execute("SELECT count(*) FROM url_queue WHERE status = 'failed'").fetchone()[0]
            if n:
                con.execute(
                    "UPDATE url_queue SET status = 'queued', error = NULL, "
                    # bulk retry never inherits a one-shot "download anyway"
                    "progress = 0, bytes_done = 0, force_size = false WHERE status = 'failed'"
                )
        return n

    def drop_rates_part(self, source_file: str) -> None:
        path = self.rates_dir / f"{file_key(source_file)}.parquet"
        with self.write_lock:
            path.unlink(missing_ok=True)
            with self.connect() as con:
                self._register_views(con)

    def forget_file(self, filename: str) -> dict:
        """User-driven per-file erasure: delete this file's rates (its parquet
        part), its provider references, and its files-table row, so the user
        can trim the store without a full reset. Any url_queue row that
        produced the file flips to 'skipped' (never deleted: a surviving
        'done' row would dedup-away the same bytes forever with no data behind
        the claim) — its retry button re-downloads if the user wants it back.
        Returns {rows, bytes} removed. Rollups are the caller's job."""
        path = self.rates_dir / f"{file_key(filename)}.parquet"
        freed = path.stat().st_size if path.exists() else 0
        with self.write_lock, self.connect() as con:
            row = con.execute(
                "SELECT rows_emitted FROM files WHERE filename = ?", [filename]
            ).fetchone()
            rows = int(row[0] or 0) if row else 0
            # ORDER MATTERS for retryability: the parquet part goes first
            # (with the view immediately re-registered so readers never see a
            # dangling glob entry), and the files row is deleted LAST inside
            # one transaction. If anything fails part-way, the files row
            # survives — so `forget` can simply be run again — instead of
            # leaving orphan rates that a missing files row makes unreachable.
            path.unlink(missing_ok=True)
            self._register_views(con)
            con.execute("BEGIN")
            try:
                con.execute("DELETE FROM provider_refs WHERE source_file = ?", [filename])
                con.execute(
                    "UPDATE url_queue SET status = 'skipped', "
                    "error = 'data removed by user — press retry to re-download and re-ingest' "
                    "WHERE filename = ? AND status = 'done'", [filename])
                con.execute("DELETE FROM files WHERE filename = ?", [filename])
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except duckdb.Error:
                    pass  # a failed COMMIT already aborted the transaction —
                    # a ROLLBACK error here must not mask the original failure
                raise
        return {"rows": rows, "bytes": freed}

    def rebuild_rollups(self) -> None:
        """Materialize the dedup/by-TIN/tin-directory rollups after ingest or
        enrichment so page queries stay fast. If the build hits the memory
        limit (the DISTINCT aggregates cannot spill to disk), retry once
        single-threaded — per-thread hash tables are the dominant cost."""
        with self.write_lock, self.connect() as con:
            self._register_views(con)  # rates view must see current parts first
            try:
                self._build_rollup_tables(con)
            except duckdb.OutOfMemoryException:
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "rollup rebuild hit the memory limit; retrying single-threaded")
                # SET threads is GLOBAL to the shared instance: restore it or
                # every other connection stays single-threaded forever
                con.execute("SET threads = 1")
                try:
                    self._build_rollup_tables(con)
                finally:
                    try:
                        con.execute("RESET threads")
                    except duckdb.Error:
                        pass

    def _build_rollup_tables(self, con: duckdb.DuckDBPyConnection) -> None:
        # rates_dedup stays a LIVE VIEW: at NPI×rate grain its groups are
        # nearly one-per-row (a 30M-row store means a ~30M-group hash
        # aggregation whose spill can exceed any reasonable disk). Drill-down
        # queries always hit it WITH filters, which DuckDB pushes into the
        # parquet scan — milliseconds, no materialization. The TIN-grain
        # spine and directory have few groups and stay materialized.
        #
        # Big stores build the materialized rollups in HASH PARTITIONS: the
        # partition column is part of each GROUP BY key, so N slices produce
        # identical rows to a single shot while peaking at ~1/N the
        # memory/temp-disk (a one-shot build over 64M rows exhausted a 23 GB
        # temp dir; the same build in 15M-row slices fits comfortably).
        import logging as _logging

        log = _logging.getLogger(__name__)
        con.execute("DROP TABLE IF EXISTS rates_dedup_tbl")
        con.execute(f"CREATE OR REPLACE VIEW rates_dedup AS {DEDUP_QUERY}")
        n_rows = con.execute("SELECT count(*) FROM rates").fetchone()[0] or 0
        parts = max(1, -(-n_rows // ROLLUP_PARTITION_ROWS))
        # ONE TRANSACTION around the whole rebuild: a mid-slice failure (temp
        # cap, memory, crash) must roll back to the PREVIOUS complete tables —
        # a partially-filled rollup served as truth silently loses billing
        # codes, which is far worse than stale analytics.
        con.execute("BEGIN TRANSACTION")
        try:
            for tbl, query, key in (
                ("rates_by_tin_tbl", BY_TIN_QUERY, "billing_code"),
                ("tin_directory_tbl", TIN_DIRECTORY_QUERY, "tin_value"),
            ):
                if parts == 1:
                    con.execute(f"CREATE OR REPLACE TABLE {tbl} AS {query.format(part='TRUE')}")
                    continue
                con.execute(  # schema only; slices append below
                    f"CREATE OR REPLACE TABLE {tbl} AS {query.format(part='FALSE')}")
                for i in range(parts):
                    log.info("rollup %s: partition %d/%d (%d rows total)", tbl, i + 1, parts, n_rows)
                    pred = f"hash({key}) % {parts} = {i}"
                    con.execute(f"INSERT INTO {tbl} {query.format(part=pred)}")
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except duckdb.Error:
                pass  # connection already aborted the transaction
            raise
        con.execute("CREATE OR REPLACE VIEW rates_by_tin AS SELECT * FROM rates_by_tin_tbl")
        con.execute("CREATE OR REPLACE VIEW tin_directory AS SELECT * FROM tin_directory_tbl")

    # legacy name used by tests/older callers
    def rebuild_dedup(self) -> None:
        self.rebuild_rollups()

    # -- files table ---------------------------------------------------------

    def upsert_file(self, filename: str, **fields) -> None:
        cols = {
            "payer", "file_type", "status", "schema_version", "last_updated_on",
            "size_bytes", "rows_emitted", "ref_groups_skipped", "error",
            "preflight", "qa", "progress", "chunks_done", "chunks_total",
            "started_at", "finished_at",
        }
        fields = {k: v for k, v in fields.items() if k in cols}
        for jcol in ("preflight", "qa"):
            if jcol in fields and not isinstance(fields[jcol], (str, type(None))):
                fields[jcol] = json.dumps(fields[jcol])
        if not fields:
            return  # nothing to write (all keys filtered out) — never emit empty SET
        with self.write_lock, self.connect() as con:
            exists = con.execute("SELECT 1 FROM files WHERE filename = ?", [filename]).fetchone()
            if exists:
                sets = ", ".join(f"{k} = ?" for k in fields)
                con.execute(f"UPDATE files SET {sets} WHERE filename = ?", [*fields.values(), filename])
            else:
                keys = ["filename", *fields.keys()]
                con.execute(
                    f"INSERT INTO files ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                    [filename, *fields.values()],
                )

    def file_status(self, filename: str) -> dict | None:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM files WHERE filename = ?", [filename]).fetchall()
            if not rows:
                return None
            cols = [d[0] for d in con.description]
            return dict(zip(cols, rows[0]))

    # -- provider refs ---------------------------------------------------------

    def save_provider_refs(
        self, payer: str, source_file: str, last_updated_on: str | None,
        refs: dict[int, list[tuple[str | None, str | None, tuple[str, ...]]]],
    ) -> None:
        with self.write_lock, self.connect() as con:
            con.execute(
                "DELETE FROM provider_refs WHERE payer = ? AND source_file = ?",
                [payer, source_file],
            )
            if not refs:
                return
            con.executemany(
                "INSERT INTO provider_refs VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    [payer, source_file, last_updated_on, rid, tin_value, tin_type, list(npis)]
                    for rid, groups in refs.items()
                    for tin_value, tin_type, npis in (groups or [(None, None, ())])
                ],
            )

    def load_provider_refs(self, payer: str) -> dict[int, list[tuple[str | None, str | None, tuple[str, ...]]]]:
        """Per ref_id, only the NEWEST vintage's groups are served: two
        companion files for one payer (a re-download under a new name, or two
        publication months) must not UNION their provider lists — rates would
        attach to stale-vintage NPIs/TINs. Ties/NULL dates fall back to the
        highest source_file (deterministic), and duplicate identical groups
        are collapsed."""
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT ref_id, tin_value, tin_type, npis FROM (
                    SELECT *, rank() OVER (
                        PARTITION BY ref_id
                        ORDER BY coalesce(last_updated_on, '') DESC, source_file DESC
                    ) AS vintage_rank
                    FROM provider_refs WHERE payer = ?
                ) WHERE vintage_rank = 1
                """,
                [payer],
            ).fetchall()
        out: dict[int, list] = {}
        seen: set = set()
        for rid, tin_value, tin_type, npis in rows:
            key = (rid, tin_value, tin_type, tuple(npis or ()))
            if key in seen:
                continue
            seen.add(key)
            out.setdefault(rid, []).append((tin_value, tin_type, tuple(npis or ())))
        return out

    def has_provider_refs(self, payer: str, last_updated_on: str | None = None) -> bool:
        q = "SELECT 1 FROM provider_refs WHERE payer = ?"
        params: list = [payer]
        if last_updated_on:
            q += " AND last_updated_on = ?"
            params.append(last_updated_on)
        with self.connect() as con:
            return con.execute(q + " LIMIT 1", params).fetchone() is not None

    # -- npi directory -----------------------------------------------------------

    def unenriched_npis(self, limit: int = 500, after: str = "") -> list[str]:
        """`after` is a keyset cursor (npi > after): the query is
        deterministic, so callers that page through the full population
        without saving rows in between MUST advance the cursor or every
        page is identical."""
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT DISTINCT npi FROM (
                    SELECT npi FROM rates
                    UNION
                    -- tin.type='npi' rows put an NPI in the TIN slot; it names
                    -- the entity, so it needs enrichment too
                    SELECT tin_value AS npi FROM rates
                    WHERE tin_is_really_npi AND tin_value IS NOT NULL
                )
                WHERE npi > ? AND npi NOT IN (SELECT npi FROM npi_directory)
                  -- only well-formed 10-digit NPIs: junk ids from messy files
                  -- always fail at NPPES, sort to the FRONT of every batch,
                  -- and can wedge enrichment permanently at the same spot
                  AND regexp_full_match(npi, '[0-9]{10}')
                ORDER BY npi LIMIT ?
                """,
                [after, limit],
            ).fetchall()
        return [r[0] for r in rows]

    def enrichment_progress(self) -> dict:
        """How far NPI->name enrichment has gotten, for the dashboard banner.
        `total` counts well-formed NPIs actually referenced by rates (the same
        population enrichment works through); `named` is those NPPES gave a
        real organization/person name. `remaining` drives the 'still working'
        hint so an incomplete state filter reads as 'names still filling in',
        not 'broken'."""
        with self.connect() as con:
            total = con.execute(
                """
                SELECT count(*) FROM (
                    SELECT DISTINCT npi FROM (
                        SELECT npi FROM rates
                        UNION
                        SELECT tin_value AS npi FROM rates
                        WHERE tin_is_really_npi AND tin_value IS NOT NULL
                    ) WHERE regexp_full_match(npi, '[0-9]{10}')
                )
                """
            ).fetchone()[0]
            enriched, named = con.execute(
                """
                SELECT count(*), count(*) FILTER (WHERE org_name IS NOT NULL)
                FROM npi_directory
                WHERE npi IN (
                    SELECT npi FROM rates
                    UNION
                    SELECT tin_value FROM rates WHERE tin_is_really_npi AND tin_value IS NOT NULL
                )
                """
            ).fetchone()
        remaining = max(0, total - enriched)
        return {"total": total, "enriched": enriched, "named": named,
                "remaining": remaining}

    def available_states(self) -> list[str]:
        """Distinct US states present in the enriched directory — used to fill
        the dashboard's state filter so users only pick states that have data
        (an empty result then clearly means 'no rates there yet', not a bug)."""
        with self.connect() as con:
            try:
                # union both directories: the state filter applies on every
                # grain, and an NPI-grain-only state lives in npi_directory but
                # not necessarily in a tin_directory row's aggregated `states`.
                rows = con.execute(
                    """
                    SELECT DISTINCT s FROM (
                        SELECT unnest(states) AS s FROM tin_directory
                        WHERE states IS NOT NULL AND len(states) > 0
                        UNION
                        SELECT state AS s FROM npi_directory WHERE state IS NOT NULL
                    )
                    WHERE s IS NOT NULL AND s <> ''
                    ORDER BY s
                    """
                ).fetchall()
            except Exception:  # noqa: BLE001 — no rows/view yet on a fresh store
                return []
        return [r[0] for r in rows if r[0]]

    def save_npi(self, npi: str, org_name: str | None, taxonomy_code: str | None,
                 taxonomy_desc: str | None, city: str | None, state: str | None,
                 entity_type: str | None = None, address: str | None = None,
                 zip_code: str | None = None, phone: str | None = None) -> None:
        with self.write_lock, self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO npi_directory "
                "(npi, entity_type, org_name, taxonomy_code, taxonomy_desc, city, state, "
                " address, zip, phone, enriched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [npi, entity_type, org_name, taxonomy_code, taxonomy_desc, city, state,
                 address, (zip_code or "")[:5] or None, phone,
                 dt.datetime.now(dt.timezone.utc)],
            )

    # -- entity map -----------------------------------------------------------

    def set_entity_map(self, mapping: dict[str, str]) -> None:
        """Replace the TIN -> entity-name table (from config/entity_map.yaml)."""
        with self.write_lock, self.connect() as con:
            con.execute("DELETE FROM entity_map")
            if mapping:
                con.executemany(
                    "INSERT INTO entity_map VALUES (?, ?)",
                    [[tin, name] for tin, name in mapping.items()],
                )

    def entity_map(self) -> dict[str, str]:
        with self.connect() as con:
            return dict(con.execute("SELECT tin_value, entity_name FROM entity_map").fetchall())

    # -- peer sets ---------------------------------------------------------------

    def save_peer_set(self, name: str, definition: dict) -> None:
        with self.write_lock, self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO peer_sets VALUES (?, ?, ?)",
                [name, json.dumps(definition), dt.datetime.now(dt.timezone.utc)],
            )

    def peer_sets(self) -> dict[str, dict]:
        with self.connect() as con:
            return {
                name: json.loads(defn)
                for name, defn in con.execute("SELECT name, definition FROM peer_sets").fetchall()
            }

    def delete_peer_set(self, name: str) -> None:
        with self.write_lock, self.connect() as con:
            con.execute("DELETE FROM peer_sets WHERE name = ?", [name])

    # -- MPFS ----------------------------------------------------------------------

    def load_mpfs(self, rows: list[dict], source: str) -> int:
        with self.write_lock, self.connect() as con:
            con.execute("DELETE FROM mpfs")
            if not rows:
                return 0
            con.executemany(
                "INSERT INTO mpfs VALUES (?, ?, ?, ?)",
                [[r["code"], r.get("locality", ""), float(r["non_facility_rate"]), source] for r in rows],
            )
        return len(rows)

    def mpfs_loaded(self) -> str | None:
        with self.connect() as con:
            row = con.execute("SELECT any_value(source) FROM mpfs").fetchone()
        return row[0] if row else None

    # -- reset ------------------------------------------------------------------------

    def reset(self) -> None:
        with self.write_lock:
            for p in self.rates_dir.glob("*.parquet"):
                p.unlink()
            with self.connect() as con:
                con.execute(
                    "DELETE FROM files; DELETE FROM npi_directory; DELETE FROM provider_refs; "
                    "DELETE FROM peer_sets; DELETE FROM mpfs; "
                    # the queue too: surviving 'done' rows (and their content
                    # hashes) would refuse to re-queue / dedup-away the very
                    # URLs the user re-pastes to rebuild the store they just
                    # wiped. entity_map stays — that's user configuration.
                    "DELETE FROM url_queue;"
                )
                for tbl in ("rates_dedup_tbl", "rates_by_tin_tbl", "tin_directory_tbl"):
                    con.execute(f"DROP TABLE IF EXISTS {tbl}")
                self._register_views(con)


class RatesPartWriter:
    """Incremental parquet writer for one source file's rows. Use as a context
    manager; call write_batch(rows) any number of times. Rows are appended to a
    temp parquet, atomically renamed into the part path on a clean exit, and
    discarded on error — so a failed parse never leaves a partial part behind."""

    def __init__(self, store: "Store", source_file: str):
        self.store = store
        self.path = store.rates_dir / f"{file_key(source_file)}.parquet"
        # NOT *.parquet: the rates view binds a glob over rates_dir, and a
        # half-written temp matching it would break concurrent readers.
        # The pid keeps the temp private to this process — two processes on
        # one store (a second serve, CLI ingest next to the app) must never
        # unlink each other's in-progress writes (Store.__init__ sweeps
        # orphans left by dead processes).
        self.tmp = store.rates_dir / f".{file_key(source_file)}.{os.getpid()}.parquet.tmp"
        self._writer: pq.ParquetWriter | None = None
        self.rows_written = 0

    def __enter__(self) -> "RatesPartWriter":
        self.tmp.unlink(missing_ok=True)
        return self

    def write_batch(self, rows: list[dict]) -> None:
        if not rows:
            return
        table = pa.Table.from_pylist(rows, schema=RATES_SCHEMA)
        if self._writer is None:
            self._writer = pq.ParquetWriter(self.tmp, RATES_SCHEMA)
        self._writer.write_table(table)
        self.rows_written += len(rows)

    def __exit__(self, exc_type, exc, tb):
        if self._writer is not None:
            self._writer.close()
        if exc_type is not None:
            self.tmp.unlink(missing_ok=True)
            return False
        with self.store.write_lock:
            if self.rows_written:
                # replace() overwrites atomically — no unlink-first window
                self.tmp.replace(self.path)
            else:
                self.tmp.unlink(missing_ok=True)
                self.path.unlink(missing_ok=True)  # re-ingest that now yields 0 rows
            with self.store.connect() as con:
                self.store._register_views(con)
        return False


def _duck_types() -> list[str]:
    """DuckDB column types matching RATES_SCHEMA order (for the empty view)."""
    mapping = {
        "string": "VARCHAR", "double": "DOUBLE", "bool": "BOOLEAN",
        "list<item: string>": "VARCHAR[]",
    }
    return [mapping[str(f.type)] for f in RATES_SCHEMA]
