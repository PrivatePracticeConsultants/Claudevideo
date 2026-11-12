"""Probe: can duckdb's query progress be read from Python while con.execute()
blocks in another thread? What does it return, how often does it change, and
what does enabling it cost? Scratch store only (a few million synthetic rows)."""
import os, sys, time, threading, statistics
import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "probe_store.duckdb")

def build(n_rows):
    if os.path.exists(DB):
        os.remove(DB)
    con = duckdb.connect(DB)
    con.execute(f"""
        CREATE TABLE rates AS
        SELECT (random()*200000)::BIGINT AS unit_id,
               ('97' || lpad(((random()*60)::INT)::VARCHAR, 3, '0')) AS billing_code,
               (random()*300)::DOUBLE AS negotiated_rate,
               (random()*12)::INT AS payer_id
        FROM range({n_rows})""")
    con.close()

def heavy_sql():
    return """
      SELECT billing_code, quantile_cont(m, [0.25,0.5,0.75]) AS q, count(*) AS n FROM (
        SELECT billing_code, unit_id, median(negotiated_rate) AS m
        FROM rates GROUP BY billing_code, unit_id) GROUP BY billing_code ORDER BY billing_code"""

def run(enable, sample_every=0.05):
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET threads = 4")
    if enable:
        con.execute("SET enable_progress_bar = true")
        con.execute("SET enable_progress_bar_print = false")
        con.execute("SET progress_bar_time = 0")  # show at once
    samples = []
    stop = threading.Event()
    def watcher():
        t0 = time.perf_counter()
        while not stop.is_set():
            try:
                p = con.query_progress()
                samples.append((round(time.perf_counter() - t0, 3), float(p), None, None))
            except Exception as e:  # noqa
                samples.append((round(time.perf_counter() - t0, 3), f"ERR {type(e).__name__}: {e}", None, None))
            time.sleep(sample_every)
    th = threading.Thread(target=watcher, daemon=True)
    t0 = time.perf_counter()
    th.start()
    rows = con.execute(heavy_sql()).fetchall()
    dt = time.perf_counter() - t0
    stop.set(); th.join()
    after = float(con.query_progress())
    con.close()
    return dt, samples, after, len(rows)

def interrupt_latency():
    """How fast does con.interrupt() from another thread stop a running query?"""
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET threads = 4")
    res = {}
    def killer():
        time.sleep(0.3)
        res["t_int"] = time.perf_counter()
        con.interrupt()
    threading.Thread(target=killer, daemon=True).start()
    t0 = time.perf_counter()
    try:
        con.execute(heavy_sql()).fetchall()
        res["outcome"] = "completed (not interrupted)"
    except duckdb.InterruptException as e:
        res["outcome"] = f"InterruptException after {1000*(time.perf_counter()-res['t_int']):.0f} ms from interrupt() call"
    except Exception as e:
        res["outcome"] = f"{type(e).__name__}: {e}"
    res["total_ms"] = round(1000 * (time.perf_counter() - t0))
    # is the connection reusable afterwards?
    try:
        con.execute("SELECT 1").fetchone()
        res["reusable"] = True
    except Exception as e:
        res["reusable"] = f"{type(e).__name__}: {e}"
    con.close()
    return res

def cursor_progress():
    """Does query_progress on a cursor() (duplicate connection) see the parent's query?"""
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET enable_progress_bar = true"); con.execute("SET enable_progress_bar_print = false"); con.execute("SET progress_bar_time = 0")
    cur = con.cursor()
    seen = []
    stop = threading.Event()
    def w():
        while not stop.is_set():
            try: seen.append(float(cur.query_progress()))
            except Exception as e: seen.append(f"ERR {e}")
            time.sleep(0.05)
    th = threading.Thread(target=w, daemon=True); th.start()
    con.execute(heavy_sql()).fetchall()
    stop.set(); th.join(); con.close()
    return sorted(set(str(s) for s in seen))[:8]

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8_000_000
    print("duckdb", duckdb.__version__, "| rows", n)
    build(n)
    for enable in (False, True, False, True):
        dt, samples, after, nrows = run(enable)
        pcts = [s[1] for s in samples if isinstance(s[1], (int, float))]
        distinct = sorted(set(round(p, 1) for p in pcts))
        print(f"\nenable_progress_bar={enable}: query {dt*1000:.0f} ms, {nrows} result rows, {len(samples)} samples")
        print("  first 6 samples:", samples[:6])
        print("  distinct pct values seen:", distinct[:30], "... total", len(distinct))
        print("  after completion:", after)
        errs = [s for s in samples if isinstance(s[1], str)]
        if errs: print("  errors:", errs[:3])
    print("\ninterrupt latency:", interrupt_latency())
    print("cursor() sees parent progress? distinct values:", cursor_progress())
    # What progress is reported during a plain scan vs a sort vs a window?
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET enable_progress_bar = true"); con.execute("SET enable_progress_bar_print = false"); con.execute("SET progress_bar_time = 0")
    for label, sql in [("count(*)", "SELECT count(*) FROM rates WHERE negotiated_rate > 100"),
                       ("order by limit", "SELECT * FROM rates ORDER BY negotiated_rate DESC LIMIT 100"),
                       ("window", "SELECT count(*) FROM (SELECT percent_rank() OVER (PARTITION BY billing_code ORDER BY negotiated_rate) p FROM rates)"),
                       ("COPY to parquet", f"COPY (SELECT * FROM rates) TO '{os.path.join(HERE,'probe_out.parquet')}' (FORMAT PARQUET)")]:
        seen = []; stop = threading.Event()
        def w():
            while not stop.is_set():
                seen.append(float(con.query_progress())); time.sleep(0.02)
        th = threading.Thread(target=w, daemon=True); th.start()
        t0 = time.perf_counter(); con.execute(sql).fetchall(); dt = time.perf_counter() - t0
        stop.set(); th.join()
        d = sorted(set(round(p) for p in seen))
        print(f"  {label:16s} {dt*1000:6.0f} ms  samples={len(seen):4d} distinct%={d[:12]}{'...' if len(d)>12 else ''}  max={max(seen) if seen else None}")
    con.close()
