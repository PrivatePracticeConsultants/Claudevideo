"""How does DuckDB read the spine for a SMALL set of practices? (server stopped)
usage: exp_in.py <config.yaml>"""
import sys
import time
from pathlib import Path

if __name__ == "__main__":
    from mrfx.config import load_mrfx_config
    from mrfx.store import Store
    cfg = load_mrfx_config(Path(sys.argv[1]))
    st = Store(cfg.store_dir, cfg.duckdb_memory_gb, keep_warm=True, temp_dir=cfg.duckdb_temp_dir)
    with st.connect() as con:
        tins = [r[0] for r in con.execute(
            "SELECT tin_value FROM tin_directory WHERE upper(display_name) LIKE '%PRACTICE 1234%' ORDER BY 1").fetchall()]
        print(len(tins), "tins", tins[:3], tins[-2:])
        lit = ", ".join(f"'{t}'" for t in tins)
        forms = {
            "tin_value = one": f"SELECT count(*), sum(negotiated_rate) FROM rates_by_tin WHERE tin_value = '{tins[0]}'",
            "IN (56 literals)": f"SELECT count(*), sum(negotiated_rate) FROM rates_by_tin WHERE tin_value IN ({lit})",
            "IN (2 literals)": f"SELECT count(*), sum(negotiated_rate) FROM rates_by_tin WHERE tin_value IN ('{tins[0]}', '{tins[1]}')",
            "OR of 56 equalities": "SELECT count(*), sum(negotiated_rate) FROM rates_by_tin WHERE " + " OR ".join(f"tin_value = '{t}'" for t in tins),
            "UNION ALL of 56 equality scans": "SELECT count(*), sum(negotiated_rate) FROM (" + " UNION ALL ".join(
                f"SELECT negotiated_rate FROM rates_by_tin WHERE tin_value = '{t}'" for t in tins) + ")",
            "semi join VALUES": f"SELECT count(*), sum(negotiated_rate) FROM rates_by_tin t SEMI JOIN (SELECT unnest([{lit}]) AS tin_value) u USING (tin_value)",
        }
        for name, sql in forms.items():
            ts = []
            for _ in range(3):
                t0 = time.perf_counter(); r = con.execute(sql).fetchall(); ts.append(round(time.perf_counter() - t0, 3))
            print(f"{name:34s} {ts} {r}")
        print(con.execute("SELECT count(*) AS row_groups FROM pragma_storage_info('rates_by_tin_tbl') WHERE column_name='tin_value' AND segment_type='VARCHAR'").fetchall())
        print(con.execute("SELECT index_name, sql FROM duckdb_indexes()").fetchall())
