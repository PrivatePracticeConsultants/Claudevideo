"""A rebuilt rate spine must be visible the moment its transaction commits
(round 5, P1.2).

DuckDB 1.5.5: after an EMPTY `CREATE TABLE … AS (… WHERE FALSE)`, rows that a
later `INSERT … SELECT … ORDER BY` of more than one row group (~123k rows) adds
in the same transaction stay invisible to every connection — even after COMMIT
— until a CHECKPOINT. The multi-slice full rebuild used exactly that shape and
only looked right because it checkpoints after each table; when another
connection had a write open (constant during a grind) that CHECKPOINT failed
("Cannot CHECKPOINT: there are other write transactions active"), the error
was deliberately swallowed, and every screen read an EMPTY spine until some
later checkpoint. Small test stores never reach the sliced path or the
row-group size, so nothing caught it."""

from __future__ import annotations

import duckdb
import pytest

N_ROWS = 400_000          # ~390k spine rows: > 1 row group per slice in two


def _part_sql(n: int, out: str) -> str:
    codes = "['97110','97112','97116','97140','97530','97535','92507','92521']"
    return f"""
    COPY (
      WITH g AS (SELECT i, (hash(i, 7) % 400000) AS tin_i, (hash(i, 3) % 8)::INT + 1 AS ci,
                        hash(i, 5) % 6 AS v FROM range({n}) t(i))
      SELECT 'Payer V' AS payer, 'vis_2026-05_in-network.json' AS source_file,
             '2026-05' AS file_month, '1.3.1' AS schema_version, '2026-05-01' AS last_updated_on,
             lpad((100000000 + tin_i)::VARCHAR, 9, '0') AS tin_value, 'ein' AS tin_type,
             FALSE AS tin_is_really_npi, (1000000000 + tin_i * 10 + v)::VARCHAR AS npi,
             {codes}[ci] AS billing_code, 'CPT' AS billing_code_type, 'PT' AS discipline,
             TRUE AS is_timed,
             CASE WHEN v < 3 THEN ['GP'] ELSE ['59', 'GP'] END AS billing_code_modifier,
             round(20 + (hash(tin_i, ci) % 9000) / 100.0, 2) AS negotiated_rate,
             'negotiated' AS negotiated_type, TRUE AS is_dollar_rate, 'professional' AS billing_class,
             CASE WHEN v % 2 = 0 THEN ['11'] ELSE ['11', '22'] END AS service_code,
             '9999-12-31' AS expiration_date, '2026-11-05T00:00:00' AS ingested_at
      FROM g) TO '{out}' (FORMAT PARQUET)"""


@pytest.fixture
def big_store(store, tmp_path):
    from mrfx.store import FINISHED_NOW
    tmp = tmp_path / "vis.parquet.tmp"
    duckdb.connect().execute(_part_sql(N_ROWS, str(tmp)))
    store.finalize_rates_part("vis_2026-05_in-network.json", tmp, N_ROWS)
    store.upsert_file("vis_2026-05_in-network.json", payer="Payer V", file_type="in_network",
                      status="done", schema_version="1.3.1", last_updated_on="2026-05-01",
                      rows_emitted=N_ROWS, finished_at=FINISHED_NOW)
    store.rebuild_rollups()                         # one-shot build: the reference
    with store.connect() as con:
        ref = con.execute("SELECT count(*), count(*) FILTER (is_latest), "
                          "sum(hash(tin_value, billing_code, modifier_set, service_code_set, "
                          "negotiated_rate)) FROM rates_by_tin_tbl").fetchone()
    assert ref[0] > 250_000
    return store, ref


def test_sliced_rebuild_is_visible_at_commit_even_when_checkpoint_cannot_run(big_store, monkeypatch):
    store, ref = big_store
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: N_ROWS // 2)
    other = store.connect()                         # e.g. a queue update in flight
    other.execute("CREATE TABLE IF NOT EXISTS _probe (x INT)")
    other.execute("BEGIN")
    other.execute("INSERT INTO _probe VALUES (1)")
    try:
        store.rebuild_rollups()
        with store.connect() as con:                 # what every screen reads, right now
            got = con.execute("SELECT count(*), count(*) FILTER (is_latest), "
                              "sum(hash(tin_value, billing_code, modifier_set, service_code_set, "
                              "negotiated_rate)) FROM rates_by_tin_tbl").fetchone()
            directory = con.execute("SELECT count(*) FROM tin_directory_tbl").fetchone()[0]
    finally:
        other.execute("COMMIT")
        other.close()
    assert got == ref                                # every row, same rows, same flags
    assert directory > 0
