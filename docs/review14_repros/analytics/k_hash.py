import json, sys, time, duckdb
from pathlib import Path
if __name__ == "__main__":
    db = sys.argv[1]
    con = duckdb.connect(db, read_only=True); con.execute("SET enable_progress_bar=false")
    t = time.time(); out = {}
    out["files"] = con.execute("SELECT count(*), count(*) FILTER (WHERE status='done'), sum(rows_emitted) FROM files").fetchone()
    out["meta"] = {k: v[:100] for k, v in con.execute("SELECT key, value FROM meta WHERE key LIKE 'rollup%' OR key LIKE 'directory%' OR key='spine_token'").fetchall()}
    for tbl in ("rates_by_tin_tbl", "tin_directory_tbl"):
        r = con.execute(f"SELECT count(*), sum(h::HUGEINT)::VARCHAR, bit_xor(h) FROM (SELECT hash(COLUMNS(*)) AS h FROM {tbl})").fetchone()
        out[tbl] = f"set:{r[0]}:{r[1]}:{r[2]}"
    out["payers"] = con.execute("SELECT payer, count(*) FROM rates_by_tin_tbl GROUP BY 1 ORDER BY 1").fetchall()
    out["hash_s"] = round(time.time() - t, 1)
    print(json.dumps(out, default=str))
