"""Experiment (in-process, server stopped): where does a name search's time go, and what does
'resolve the practices from a per-generation practice list first' buy?
  V0  current SQL (mrfx.api.rel_sql): DISTINCT over the whole spine, then SEMI JOIN
  V1  same SQL, the practice list read from a materialised 194k-row list instead of the spine
  V2  V1 resolved in a first statement, then pushed into the spine scan as tin_value IN (...)
Checks V0 == V1 == V2 row for row (page in order; count; strip; by-code).
usage: exp_units.py <config.yaml> <label> <q> [<q> ...]
"""
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path


def load():
    return subprocess.run(["uptime"], capture_output=True, text=True).stdout.split("load average:")[-1].strip()


def main():
    cfgp, label = sys.argv[1:3]
    import mrfx.api as api
    from mrfx.config import load_mrfx_config
    from mrfx.store import Store
    cfg = load_mrfx_config(Path(cfgp))
    st = Store(cfg.store_dir, cfg.duckdb_memory_gb, keep_warm=True, temp_dir=cfg.duckdb_temp_dir)
    unit_cols = ", ".join(f"{sql} AS {name}" for name, sql in api._UNIT_COLS.items())
    out = []
    with st.connect() as con:
        t = time.perf_counter()
        con.execute("CREATE OR REPLACE TEMP TABLE spine_units AS "
                    "SELECT DISTINCT tin_value, tin_is_really_npi FROM rates_by_tin")
        t_units = time.perf_counter() - t
        n_units = con.execute("SELECT count(*) FROM spine_units").fetchone()[0]
        print(f"[{label}] practice list built once per data generation: {n_units} practices in {t_units:.2f} s; load {load()}")

        def timed(sql, params, reps=2):
            best, rows = None, None
            ts = []
            for _ in range(reps):
                t0 = time.perf_counter()
                rows = con.execute(sql, params).fetchall()
                ts.append(round(time.perf_counter() - t0, 3))
            return ts, rows

        def dig(rows):
            return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()[:12]

        for q in sys.argv[3:]:
            fs = api.FilterSet({"grain": "tin", "dollar_only": "1", "q": q})
            order = api.order_sql("negotiated_rate", "desc")
            basis, _ = api.practice_basis(fs)
            where_key, where_unit = fs.where_of("key"), fs.where_of("unit", "ident")
            row_where = fs.where_of("row")
            v0 = api.rel_sql("tin", fs)
            # V1: identical text, the DISTINCT read from the practice list
            v1 = v0.replace("(SELECT DISTINCT tin_value, tin_is_really_npi FROM k)", "spine_units")
            assert v1 != v0
            # V2: resolve first ...
            resolve = f"""SELECT tin_value, tin_is_really_npi FROM (
                SELECT t.tin_value, t.tin_value AS unit_id, t.tin_is_really_npi, {unit_cols}
                FROM spine_units t {api._TIN_JOINS}) WHERE {where_unit} ORDER BY 1, 2"""
            n_unit_params = len(fs._buckets["unit"][1]) + len(fs._buckets["ident"][1])
            n_key = len(fs._buckets["key"][1])
            unit_params = fs.params[n_key:n_key + n_unit_params]
            ts_res, units = timed(resolve, unit_params)
            INLINE_MAX = 5000
            stmts = {
                "rates page": lambda rel: (f"{rel} {order} LIMIT 100 OFFSET 0"),
                "rates count": lambda rel: f"SELECT count(*) FROM ({rel})",
                "summary strip": lambda rel: f"""SELECT n, entities, codes, mn, q[1], q[2], q[3], mx FROM (
                    SELECT count(*) AS n, count(DISTINCT unit_id) AS entities, count(DISTINCT billing_code) AS codes,
                    min(negotiated_rate) FILTER (is_dollar_rate AND negotiated_rate > 0.01) AS mn,
                    quantile_cont(negotiated_rate, [0.25, 0.5, 0.75]) FILTER (is_dollar_rate AND negotiated_rate > 0.01) AS q,
                    max(negotiated_rate) FILTER (is_dollar_rate AND negotiated_rate > 0.01) AS mx FROM ({rel}))""",
                "summary by-code": lambda rel: f"""WITH r AS (SELECT billing_code, unit_id, median(negotiated_rate) AS negotiated_rate
                    FROM ({rel}) WHERE {basis} GROUP BY billing_code, unit_id)
                    SELECT billing_code, q[2] AS median, q[1] AS p25, q[3] AS p75, entities FROM (
                    SELECT r.billing_code, quantile_cont(r.negotiated_rate, [0.25, 0.5, 0.75]) AS q, count(DISTINCT r.unit_id) AS entities
                    FROM r GROUP BY r.billing_code) ORDER BY billing_code""",
            }
            if len(units) <= INLINE_MAX and units:
                # ... then the spine is read with a plain IN list (zone-map prunable: the spine is
                # stored ORDER BY billing_code, tin_value). The pair test keeps EIN vs NPI-in-slot apart.
                tins = sorted({u[0] for u in units})
                inl = ", ".join("?" for _ in tins)
                pairs = ", ".join("(?, ?)" for _ in units)
                v2 = (f"WITH base AS (SELECT {api._TIN_PROJECTION} FROM "
                      f"(SELECT * FROM rates_by_tin WHERE {where_key} AND tin_value IN ({inl}) "
                      f"AND (tin_value, tin_is_really_npi) IN ({pairs})) t {api._TIN_JOINS}) "
                      f"SELECT * FROM base WHERE {row_where}")
                v2_params = [*fs.params[:n_key], *tins, *[x for u in units for x in u], *fs.params[n_key + n_unit_params:]]
            else:
                v2, v2_params = None, None
            print(f"\n[{label}] q={q!r}: {len(units)} practices match (resolved from the list in {ts_res} s); load {load()}")
            for title, mk in stmts.items():
                t0, r0 = timed(mk(v0), fs.params)
                t1, r1 = timed(mk(v1), fs.params)
                line = f"   {title:16s} V0 {t0} s  V1 {t1} s  same={r0 == r1}"
                rec = {"q": q, "stmt": title, "units": len(units), "v0": t0, "v1": t1, "v1_same": r0 == r1,
                       "sha_v0": dig(r0), "sha_v1": dig(r1), "load": load()}
                if v2:
                    t2, r2 = timed(mk(v2), v2_params)
                    line += f"  V2 {t2} s  same={r0 == r2}"
                    rec.update(v2=t2, v2_same=r0 == r2, sha_v2=dig(r2))
                print(line + f"  sha {dig(r0)}", flush=True)
                out.append(rec)
    Path(f"/tmp/mrfx_review10/search/out/exp_units_{label}.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
