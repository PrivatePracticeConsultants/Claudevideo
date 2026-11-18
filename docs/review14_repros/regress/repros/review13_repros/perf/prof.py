"""Profile the slow routes IN-PROCESS (TestClient over the same 99M store),
after the server has stopped. For each named call: cProfile top-N cumulative
functions + every DuckDB statement's wall time (phases), cold then warm.
usage: prof.py <out.json> name1,name2,...   (names from calls.py)"""
import cProfile
import io
import json
import pstats
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from calls import calls  # noqa: E402

STORE_PARENT = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/scale4n")


def main():
    out = Path(sys.argv[1])
    wanted = sys.argv[2].split(",")
    import duckdb
    import mrfx.api as api
    from fastapi.testclient import TestClient
    from mrfx.config import MrfxConfig
    from mrfx.store import Store
    cfg = MrfxConfig(inbox_dir=STORE_PARENT / "in", processed_dir=STORE_PARENT / "p",
                     failed_dir=STORE_PARENT / "f", store_dir=STORE_PARENT / "store",
                     downloads_dir=STORE_PARENT / "dl", entity_map_path=STORE_PARENT / "em.yaml",
                     port=8399, enrichment={"mode": "off"})
    cfg.ensure_dirs()
    st = Store(cfg.store_dir, keep_warm=True)
    api.TRUSTED_HOSTS.add("testserver")
    c = TestClient(api.create_app(cfg, st), raise_server_exceptions=False)
    # statement-level timing: wrap DuckDBPyConnection.execute
    stmts = []
    real_exec = duckdb.DuckDBPyConnection.execute

    def timed_exec(self, q, *a, **k):
        t = time.perf_counter()
        try:
            return real_exec(self, q, *a, **k)
        finally:
            stmts.append((time.perf_counter() - t, " ".join(str(q).split())[:160]))
    duckdb.DuckDBPyConnection.execute = timed_exec
    by_name = {x[0]: x for x in calls()}
    results = {}
    for name in wanted:
        if name not in by_name:
            print("unknown", name)
            continue
        _, view, method, url, body, var = by_name[name]
        res = {"url": url, "payload": body, "runs": []}
        for run in ("cold", "warm"):
            stmts.clear()
            pr = cProfile.Profile()
            t0 = time.perf_counter()
            pr.enable()
            try:
                r = c.post(url, json=body) if method == "POST" else c.get(url)
                status, nbytes = r.status_code, len(r.content)
            except Exception:
                status, nbytes = -1, 0
                traceback.print_exc()
            pr.disable()
            total = time.perf_counter() - t0
            s = io.StringIO()
            ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
            ps.print_stats(28)
            lines = [ln for ln in s.getvalue().splitlines() if ln.strip() and ("mrfx/" in ln or "duckdb" in ln or "{built-in" in ln)]
            top_sql = sorted(stmts, key=lambda x: -x[0])[:8]
            res["runs"].append({"run": run, "status": status, "bytes": nbytes, "total_s": round(total, 3),
                                "n_statements": len(stmts), "sql_total_s": round(sum(x[0] for x in stmts), 3),
                                "top_sql": [(round(t, 3), q) for t, q in top_sql],
                                "top_funcs": lines[:14]})
            print(f"{name} {run}: {status} {total:.2f}s, {len(stmts)} stmts, sql {sum(x[0] for x in stmts):.2f}s", flush=True)
            for t, q in top_sql[:5]:
                print(f"    {t:7.2f}s  {q[:140]}")
        results[name] = res
        out.write_text(json.dumps(results, indent=1, default=str))
    st.close() if hasattr(st, "close") else None


if __name__ == "__main__":
    main()
