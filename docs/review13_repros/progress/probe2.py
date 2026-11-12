"""Follow-ups: is enable_progress_bar session-local? Is query_progress monotonic
within one statement? What does enabling cost across many SHORT queries?
Does the watcher thread work while the SAME connection runs several statements
in a row (per-statement reset)?"""
import os, time, threading, duckdb
HERE = os.path.dirname(os.path.abspath(__file__)); DB = os.path.join(HERE, "probe_store.duckdb")
HEAVY = """SELECT billing_code, quantile_cont(m, [0.25,0.5,0.75]) q, count(*) n FROM (
  SELECT billing_code, unit_id, median(negotiated_rate) m FROM rates GROUP BY 1,2) GROUP BY 1 ORDER BY 1"""
def enable(con):
    con.execute("SET enable_progress_bar = true"); con.execute("SET enable_progress_bar_print = false"); con.execute("SET progress_bar_time = 0")
if __name__ == "__main__":
    a = duckdb.connect(DB, read_only=True); enable(a)
    b = duckdb.connect(DB, read_only=True)
    print("scope: a =", a.execute("SELECT current_setting('enable_progress_bar')").fetchone()[0],
          "| b (untouched) =", b.execute("SELECT current_setting('enable_progress_bar')").fetchone()[0])
    print("scope print flag a =", a.execute("SELECT current_setting('enable_progress_bar_print')").fetchone()[0])
    b.close()
    # monotonic within a statement? sample at 10 ms
    seen = []; stop = threading.Event()
    def w():
        while not stop.is_set():
            seen.append(float(a.query_progress())); time.sleep(0.01)
    th = threading.Thread(target=w, daemon=True); th.start(); a.execute(HEAVY).fetchall(); stop.set(); th.join()
    vals = [v for v in seen if v >= 0]
    dec = sum(1 for x, y in zip(vals, vals[1:]) if y < x)
    print(f"monotonic check: {len(vals)} samples, {dec} decreases, first {vals[:3]}, last {vals[-3:]}")
    # multi-statement: three statements back to back, does the value reset to ~0 between?
    seen = []; stop = threading.Event(); marks = []
    th = threading.Thread(target=w, daemon=True); th.start()
    for i in range(3):
        a.execute(HEAVY).fetchall(); marks.append(len(seen))
    stop.set(); th.join()
    segs = [seen[(marks[i-1] if i else 0):marks[i]] for i in range(3)]
    print("per-statement reset: min/max per statement:", [(round(min([v for v in s if v>=0] or [-1]),1), round(max(s),1)) for s in segs])
    # overhead on many short queries
    for flag in (False, True, False, True):
        c = duckdb.connect(DB, read_only=True)
        if flag: enable(c)
        t0 = time.perf_counter()
        for i in range(300):
            c.execute("SELECT count(*) FROM rates WHERE unit_id = ?", [i]).fetchone()
        dt = time.perf_counter() - t0
        c.close()
        print(f"300 short point queries, progress={'on ' if flag else 'off'}: {dt*1000:.0f} ms")
    # does query_progress from the watcher thread hold the GIL / block execute? (measured above: no)
    # what if nothing is running?
    print("idle value:", a.query_progress())
    a.close()
