"""DuckDB-backed Parquet store: rates parts + files / npi_directory / provider_refs tables.

Rate rows are written as one Parquet part per ingested source file (re-ingest
overwrites the part, keeping ingestion idempotent). Metadata lives in a
persistent DuckDB database; every reader connection registers a `rates` view
over the parts glob plus the `rates_dedup` view.

Writes are serialized behind a lock (watcher thread + API share the store).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

RATES_SCHEMA = pa.schema(
    [
        pa.field("payer", pa.string(), nullable=False),
        pa.field("source_file", pa.string(), nullable=False),
        pa.field("schema_version", pa.string()),
        pa.field("last_updated_on", pa.string()),
        pa.field("npi", pa.string(), nullable=False),
        pa.field("tin", pa.string()),
        pa.field("billing_code", pa.string(), nullable=False),
        pa.field("billing_code_type", pa.string()),
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

_RATES_COLS_SQL = """
    payer VARCHAR, source_file VARCHAR, schema_version VARCHAR,
    last_updated_on VARCHAR, npi VARCHAR, tin VARCHAR,
    billing_code VARCHAR, billing_code_type VARCHAR,
    billing_code_modifier VARCHAR[], negotiated_rate DOUBLE,
    negotiated_type VARCHAR, is_dollar_rate BOOLEAN, billing_class VARCHAR,
    service_code VARCHAR[], expiration_date VARCHAR, ingested_at VARCHAR
"""


def file_key(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()[:16]


# Deduped rollup: one row per distinct negotiated fact. List columns are
# collapsed to '|'-joined strings so the GROUP BY stays cheap; the UI splits
# them back apart for display.
DEDUP_QUERY = """
    SELECT payer, npi, billing_code,
           any_value(billing_code_type)                              AS billing_code_type,
           coalesce(array_to_string(billing_code_modifier, '|'), '') AS modifier_set,
           negotiated_rate, billing_class,
           coalesce(array_to_string(service_code, '|'), '')          AS service_code_set,
           any_value(negotiated_type)                                AS negotiated_type,
           bool_or(is_dollar_rate)                                   AS is_dollar_rate,
           any_value(tin)                                            AS tin,
           any_value(schema_version)                                 AS schema_version,
           max(last_updated_on)                                      AS last_updated_on,
           any_value(expiration_date)                                AS expiration_date,
           count(DISTINCT source_file)                               AS source_count
    FROM rates
    GROUP BY payer, npi, billing_code,
             coalesce(array_to_string(billing_code_modifier, '|'), ''),
             negotiated_rate, billing_class,
             coalesce(array_to_string(service_code, '|'), '')
"""


class Store:
    def __init__(self, store_dir: Path):
        self.dir = Path(store_dir)
        self.rates_dir = self.dir / "rates"
        self.rates_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "mrfx.duckdb"
        self.write_lock = threading.Lock()
        with self.write_lock, self.connect() as con:
            self._init_tables(con)
            self._register_views(con)

    # -- connections --------------------------------------------------------

    def connect(self) -> duckdb.DuckDBPyConnection:
        """Plain connection. Views are persisted in the database and their
        parquet glob re-resolves per query, so readers NEVER touch the catalog
        (concurrent CREATE OR REPLACE VIEW = write-write conflict). Views are
        (re)registered only at init and inside locked write paths."""
        return duckdb.connect(str(self.db_path))

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
                started_at TIMESTAMP,
                finished_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS npi_directory (
                npi VARCHAR PRIMARY KEY,
                org_name VARCHAR,
                taxonomy_code VARCHAR,
                taxonomy_desc VARCHAR,
                city VARCHAR,
                state VARCHAR,
                enriched_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS provider_refs (
                payer VARCHAR,
                source_file VARCHAR,
                last_updated_on VARCHAR,
                ref_id BIGINT,
                npis VARCHAR[],
                tins VARCHAR[]
            );
            """
        )

    def _register_views(self, con: duckdb.DuckDBPyConnection) -> None:
        """(Re)point the rates/rates_dedup views. Callers must hold write_lock."""
        glob = str(self.rates_dir / "*.parquet")
        if any(self.rates_dir.glob("*.parquet")):
            con.execute(f"CREATE OR REPLACE VIEW rates AS SELECT * FROM read_parquet('{glob}')")
        else:
            con.execute(
                f"CREATE OR REPLACE VIEW rates AS SELECT * FROM (SELECT {_empty_row()}) WHERE FALSE"
            )
        # rates_dedup reads the materialized table when one has been built
        # (rebuild_dedup after each ingest); the GROUP BY view is the fallback
        # so ad-hoc setups still work.
        has_mat = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'rates_dedup_tbl'"
        ).fetchone()[0]
        if has_mat:
            con.execute("CREATE OR REPLACE VIEW rates_dedup AS SELECT * FROM rates_dedup_tbl")
        else:
            con.execute(f"CREATE OR REPLACE VIEW rates_dedup AS {DEDUP_QUERY}")

    # -- rates parts ---------------------------------------------------------

    def write_rates_part(self, source_file: str, rows: list[dict]) -> Path | None:
        if not rows:
            return None
        table = pa.Table.from_pylist(rows, schema=RATES_SCHEMA)
        path = self.rates_dir / f"{file_key(source_file)}.parquet"
        with self.write_lock:
            pq.write_table(table, path)
            with self.connect() as con:
                self._register_views(con)  # first part swaps the empty view for the glob
        return path

    def drop_rates_part(self, source_file: str) -> None:
        path = self.rates_dir / f"{file_key(source_file)}.parquet"
        with self.write_lock:
            path.unlink(missing_ok=True)
            with self.connect() as con:
                self._register_views(con)

    def rebuild_dedup(self) -> None:
        """Materialize rates_dedup after ingest so page queries stay fast."""
        with self.write_lock, self.connect() as con:
            self._register_views(con)  # rates view must see current parts first
            con.execute(f"CREATE OR REPLACE TABLE rates_dedup_tbl AS {DEDUP_QUERY}")
            con.execute("CREATE OR REPLACE VIEW rates_dedup AS SELECT * FROM rates_dedup_tbl")

    # -- files table ---------------------------------------------------------

    def upsert_file(self, filename: str, **fields) -> None:
        cols = {
            "payer", "file_type", "status", "schema_version", "last_updated_on",
            "size_bytes", "rows_emitted", "ref_groups_skipped", "error",
            "preflight", "started_at", "finished_at",
        }
        fields = {k: v for k, v in fields.items() if k in cols}
        if "preflight" in fields and not isinstance(fields["preflight"], (str, type(None))):
            fields["preflight"] = json.dumps(fields["preflight"])
        with self.write_lock, self.connect() as con:
            exists = con.execute(
                "SELECT 1 FROM files WHERE filename = ?", [filename]
            ).fetchone()
            if exists:
                sets = ", ".join(f"{k} = ?" for k in fields)
                con.execute(
                    f"UPDATE files SET {sets} WHERE filename = ?",
                    [*fields.values(), filename],
                )
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

    # -- provider refs -------------------------------------------------------

    def save_provider_refs(
        self, payer: str, source_file: str, last_updated_on: str | None, refs: dict[int, tuple[list[str], list[str]]]
    ) -> None:
        with self.write_lock, self.connect() as con:
            con.execute(
                "DELETE FROM provider_refs WHERE payer = ? AND source_file = ?",
                [payer, source_file],
            )
            con.executemany(
                "INSERT INTO provider_refs VALUES (?, ?, ?, ?, ?, ?)",
                [
                    [payer, source_file, last_updated_on, rid, npis, tins]
                    for rid, (npis, tins) in refs.items()
                ],
            )

    def load_provider_refs(self, payer: str) -> dict[int, tuple[frozenset[str], frozenset[str]]]:
        """All standalone-reference-file entries for a payer, ref_id -> (npis, tins)."""
        with self.connect() as con:
            rows = con.execute(
                "SELECT ref_id, npis, tins FROM provider_refs WHERE payer = ?", [payer]
            ).fetchall()
        out: dict[int, tuple[frozenset[str], frozenset[str]]] = {}
        for rid, npis, tins in rows:
            prev = out.get(rid, (frozenset(), frozenset()))
            out[rid] = (prev[0] | frozenset(npis or []), prev[1] | frozenset(tins or []))
        return out

    def has_provider_refs(self, payer: str, last_updated_on: str | None = None) -> bool:
        q = "SELECT 1 FROM provider_refs WHERE payer = ?"
        params: list = [payer]
        if last_updated_on:
            q += " AND last_updated_on = ?"
            params.append(last_updated_on)
        with self.connect() as con:
            return con.execute(q + " LIMIT 1", params).fetchone() is not None

    # -- npi directory --------------------------------------------------------

    def unenriched_npis(self, limit: int = 500) -> list[str]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT DISTINCT npi FROM rates
                WHERE npi NOT IN (SELECT npi FROM npi_directory)
                ORDER BY npi LIMIT ?
                """,
                [limit],
            ).fetchall()
        return [r[0] for r in rows]

    def save_npi(self, npi: str, org_name: str | None, taxonomy_code: str | None,
                 taxonomy_desc: str | None, city: str | None, state: str | None) -> None:
        with self.write_lock, self.connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO npi_directory VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [npi, org_name, taxonomy_code, taxonomy_desc, city, state,
                 dt.datetime.now(dt.timezone.utc)],
            )

    # -- reset ----------------------------------------------------------------

    def reset(self) -> None:
        with self.write_lock:
            for p in self.rates_dir.glob("*.parquet"):
                p.unlink()
            with self.connect() as con:
                con.execute("DELETE FROM files; DELETE FROM npi_directory; DELETE FROM provider_refs;")
                con.execute("DROP TABLE IF EXISTS rates_dedup_tbl")
                self._register_views(con)


def _empty_row() -> str:
    """Column expressions producing an empty typed rates relation."""
    casts = {
        "payer": "VARCHAR", "source_file": "VARCHAR", "schema_version": "VARCHAR",
        "last_updated_on": "VARCHAR", "npi": "VARCHAR", "tin": "VARCHAR",
        "billing_code": "VARCHAR", "billing_code_type": "VARCHAR",
        "billing_code_modifier": "VARCHAR[]", "negotiated_rate": "DOUBLE",
        "negotiated_type": "VARCHAR", "is_dollar_rate": "BOOLEAN",
        "billing_class": "VARCHAR", "service_code": "VARCHAR[]",
        "expiration_date": "VARCHAR", "ingested_at": "VARCHAR",
    }
    return ", ".join(f"CAST(NULL AS {t}) AS {c}" for c, t in casts.items())
