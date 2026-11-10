"""In-process: build the EXACT SQL the search endpoints run (mrfx.api's own FilterSet / rel_sql /
stat_rel), time each statement twice, and EXPLAIN ANALYZE it (operators by time).
The server must NOT be running on the same store.
usage: explain_search.py <config.yaml> <label> <name> <partial> <tin> <state> [mrfx_src_dir]
"""
import json
import subprocess
import sys
import time
from pathlib import Path

OUT = Path("/tmp/mrfx_review10/search/out")


def load():
    return subprocess.run(["uptime"], capture_output=True, text=True).stdout.split("load average:")[-1].strip()


def walk(node, acc, depth=0):
    name = node.get("operator_name") or node.get("operator_type") or node.get("name") or "?"
    t = node.get("operator_timing", node.get("timing", 0)) or 0
    card = node.get("operator_cardinality", node.get("cardinality", 0))
    extra = node.get("extra_info") or {}
    tag = ""
    if isinstance(extra, dict):
        tag = str(extra.get("Table") or extra.get("Function") or extra.get("Join Type") or "")
        if extra.get("Filters"):
            tag += " filters=" + str(extra.get("Filters"))[:90]
    acc.append((t, name.strip(), card, tag))
    for c in node.get("children", []):
        walk(c, acc, depth + 1)


def main():
    cfgp, label, name, partial, tin, state = sys.argv[1:7]
    if len(sys.argv) > 7:
        sys.path.insert(0, sys.argv[7])
    import mrfx.api as api
    from mrfx.config import load_mrfx_config as load_config
    from mrfx.store import Store
    cfg = load_config(Path(cfgp))
    st = Store(cfg.store_dir, cfg.duckdb_memory_gb, keep_warm=True, temp_dir=cfg.duckdb_temp_dir)
    res = {"label": label, "mrfx": api.__file__, "queries": []}

    def run(title, sql, params):
        with st.connect() as con:
            times = []
            for _ in range(2):
                t = time.perf_counter()
                n = len(con.execute(sql, params).fetchall())
                times.append(round(time.perf_counter() - t, 3))
            t = time.perf_counter()
            raw = con.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params).fetchall()[0][1]
            te = round(time.perf_counter() - t, 3)
        tree = json.loads(raw)
        acc = []
        for n0 in (tree if isinstance(tree, list) else [tree]):
            walk(n0, acc)
        cpu = sum(a[0] for a in acc)
        top = sorted(acc, reverse=True)[:6]
        (OUT / "plans").mkdir(exist_ok=True)
        (OUT / "plans" / f"{label}_{title.replace(' ', '_').replace('/', '-')[:60]}.json").write_text(raw)
        print(f"\n## {title}: wall {times} s (rows {n}); explain-analyze wall {te} s; operator thread-seconds {cpu:.2f}; load {load()}", flush=True)
        for t_, nm, card, tag in top:
            print(f"     {t_:8.3f}s  {nm:22s} rows={card:<12} {tag}")
        res["queries"].append({"title": title, "wall_s": times, "rows": n, "thread_s": round(cpu, 2), "load": load(),
                               "top": [[round(t_, 3), nm, card, tag] for t_, nm, card, tag in top]})

    def explorer(title, qp, sort="negotiated_rate", dir_="desc"):
        qp = {"grain": "tin", "dollar_only": "1", **qp}
        fs = api.FilterSet(qp)
        grain = qp["grain"]
        order = api.order_sql(sort, dir_)
        late = (grain == "tin" and not fs.hide_outliers and not fs.uses_dim_cols
                and sort not in ("display_name", "tin_count"))
        lim = [*fs.params, 100, 0]
        if late:
            sql = (f"WITH page AS (SELECT *, tin_value AS unit_id FROM rates_by_tin "
                   f"WHERE {api.spine_where(fs)} {order} LIMIT ? OFFSET ?) "
                   f"SELECT * FROM (SELECT {api._TIN_PROJECTION} FROM page t {api._TIN_JOINS}) {order}")
            cnt = f"SELECT count(*) FROM rates_by_tin WHERE {api.spine_where(fs)}"
        else:
            sql = f"{api.rel_sql(grain, fs)} {order} LIMIT ? OFFSET ?"
            cnt = f"SELECT count(*) FROM ({api.rel_sql(grain, fs)})"
        run(f"{title} | rates page", sql, lim)
        run(f"{title} | rates count", cnt, fs.params)
        srel = api.stat_rel(grain, fs)
        run(f"{title} | summary strip", f"""SELECT n, entities, codes, mn, q[1], q[2], q[3], mx FROM (
            SELECT count(*) AS n, count(DISTINCT unit_id) AS entities, count(DISTINCT billing_code) AS codes,
                   min(negotiated_rate) FILTER (is_dollar_rate AND negotiated_rate > 0.01) AS mn,
                   quantile_cont(negotiated_rate, [0.25, 0.5, 0.75]) FILTER (is_dollar_rate AND negotiated_rate > 0.01) AS q,
                   max(negotiated_rate) FILTER (is_dollar_rate AND negotiated_rate > 0.01) AS mx
            FROM ({srel}))""", fs.params)
        basis, _ = api.practice_basis(fs)
        run(f"{title} | summary by-code", f"""WITH r AS (SELECT billing_code, unit_id, median(negotiated_rate) AS negotiated_rate
                   FROM ({srel}) WHERE {basis} GROUP BY billing_code, unit_id)
            SELECT billing_code, q[2] AS median, q[1] AS p25, q[3] AS p75, entities FROM (
            SELECT r.billing_code, quantile_cont(r.negotiated_rate, [0.25, 0.5, 0.75]) AS q, count(DISTINCT r.unit_id) AS entities
            FROM r GROUP BY r.billing_code) ORDER BY billing_code""", fs.params)

    explorer("default (no filter)", {})
    explorer("q=full name", {"q": name})
    explorer("q=partial name", {"q": partial})
    explorer("q=one letter 'p'", {"q": "p"})
    explorer("q=9-digit TIN", {"q": tin})
    explorer("q=partial digits", {"q": tin[:5]})
    explorer("state", {"state": state})
    explorer("cpt 97110 + partial name", {"cpt": "97110", "q": partial})
    # unfiltered, sorted by name (the non-late-join table)
    fs = api.FilterSet({"grain": "tin", "dollar_only": "1"})
    run("default sorted by display_name | rates page",
        f"{api.rel_sql('tin', fs)} {api.order_sql('display_name', 'asc')} LIMIT ? OFFSET ?", [*fs.params, 100, 0])
    # the TIN drawer's three statements
    run("drawer tin | rows", f"SELECT * FROM ({api.GRAIN_REL['tin']}) WHERE unit_id = ? ORDER BY billing_code, payer, modifier_set, "
        "file_month, billing_class, service_code_set, is_dollar_rate, tin_value, negotiated_rate", [tin])
    run("drawer tin | member NPIs (raw rates)", """SELECT DISTINCT r.npi, n.org_name, n.entity_type, n.city, n.state, n.taxonomy_desc
        FROM rates r LEFT JOIN npi_directory n USING (npi) WHERE r.tin_value IN (?) AND r.npi IS NOT NULL ORDER BY r.npi""", [tin])
    run("drawer tin | chart", f"""SELECT billing_code, payer, median(negotiated_rate) AS median_rate FROM ({api.GRAIN_REL['tin']})
        WHERE unit_id = ? AND is_dollar_rate AND negotiated_rate > 0.01 GROUP BY billing_code, payer ORDER BY billing_code, payer""", [tin])
    (OUT / f"explain_{label}.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
