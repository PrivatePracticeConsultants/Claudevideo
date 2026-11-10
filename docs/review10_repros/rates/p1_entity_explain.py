"""EXPLAIN ANALYZE of the Explorer's entity-grain 'latest' page on the 32M copy (server must be stopped)."""
import sys, time, os
sys.path.insert(0, "/home/user/Claudevideo")
from mrfx.config import load_mrfx_config as load_config
from mrfx.store import Store
import mrfx.api as api

def main():
    cfg = load_config("/tmp/mrfx_review10/rates/big/mrfx.yaml")
    store = Store(cfg.store_dir)
    which = sys.argv[1] if len(sys.argv) > 1 else "entity"
    qp = {"grain": which, "latest": "1", "dollar_only": "1"}
    if len(sys.argv) > 2: qp["state"] = sys.argv[2]
    fs = api.FilterSet(qp)
    sql = f"{api.rel_sql(which, fs)} {api.order_sql('negotiated_rate', 'desc')} LIMIT ? OFFSET ?"
    with store.connect() as con:
        print("load", os.getloadavg())
        for i in range(2):
            t = time.time(); con.execute(sql, [*fs.params, 100, 0]).fetchall(); print("page query", round(time.time() - t, 2))
        t = time.time(); n = con.execute(f"SELECT count(*) FROM ({api.rel_sql(which, fs)})", fs.params).fetchone()[0]; print("count", n, round(time.time() - t, 2))
        con.execute("PRAGMA enable_profiling='query_tree'")
        plan = con.execute("EXPLAIN ANALYZE " + sql, [*fs.params, 100, 0]).fetchall()
        open(f"/tmp/mrfx_review10/rates/explain_{which}_{qp.get('state','all')}.txt", "w").write(plan[0][1])
        print(plan[0][1][:200])
        print(con.execute("select count(*) from rates_by_tin").fetchone(), con.execute("select count(*) from rates_by_tin where is_latest").fetchone())

if __name__ == "__main__":
    main()
