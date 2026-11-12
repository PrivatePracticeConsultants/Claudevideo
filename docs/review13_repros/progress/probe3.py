"""Safety: sample query_progress() from a side thread while the main thread
opens/closes connections and runs statements. Does anything crash? What does a
closed connection answer? Also: can duckdb.InterruptException be subclassed, and
does progress move during COPY ... TO csv (the export path)?"""
import os, time, threading, collections, duckdb
HERE = os.path.dirname(os.path.abspath(__file__)); DB = os.path.join(HERE, "probe_store.duckdb")
def enable(con):
    con.execute("SET enable_progress_bar = true"); con.execute("SET enable_progress_bar_print = false"); con.execute("SET progress_bar_time = 0")
if __name__ == "__main__":
    live = {"con": None}; stop = threading.Event(); outcomes = collections.Counter(); vals = []
    def sampler():
        while not stop.is_set():
            c = live["con"]
            if c is None:
                outcomes["none"] += 1
            else:
                try:
                    v = float(c.query_progress()); outcomes["ok"] += 1
                    if v >= 0: vals.append(v)
                except Exception as e:
                    outcomes[type(e).__name__ + ": " + str(e)[:60]] += 1
            time.sleep(0.001)
    th = threading.Thread(target=sampler, daemon=True); th.start()
    t0 = time.perf_counter(); n = 0
    while time.perf_counter() - t0 < 8:
        con = duckdb.connect(DB, read_only=True); enable(con); live["con"] = con
        con.execute("SELECT billing_code, count(*) FROM rates WHERE unit_id % 97 = ? GROUP BY 1", [n % 97]).fetchall()
        con.close(); n += 1   # the handle stays in live["con"] AFTER close, like a module's `with` exit
    stop.set(); th.join()
    print(f"open/run/close cycles: {n}; sampler outcomes: {dict(outcomes)}; moving values seen: {len(vals)}")
    # closed connection answer
    c = duckdb.connect(DB, read_only=True); c.close()
    try: print("closed connection query_progress ->", c.query_progress())
    except Exception as e: print("closed connection query_progress raises", type(e).__name__, "-", e)
    # interrupt a closed connection?
    try: c.interrupt(); print("interrupt() on closed connection: no error")
    except Exception as e: print("interrupt() on closed connection raises", type(e).__name__, "-", e)
    # subclass InterruptException?
    try:
        class Stopped(duckdb.InterruptException): pass
        try: raise Stopped("stopped by the user")
        except duckdb.InterruptException as e: print("subclass of duckdb.InterruptException works:", repr(e), "| is Exception:", isinstance(e, Exception))
    except TypeError as e: print("cannot subclass InterruptException:", e)
    # COPY to CSV progress (the export path)
    con = duckdb.connect(DB, read_only=True); enable(con); seen = []; stop = threading.Event()
    def w():
        while not stop.is_set():
            seen.append(float(con.query_progress())); time.sleep(0.02)
    th = threading.Thread(target=w, daemon=True); th.start()
    out = os.path.join(HERE, "probe_out.csv"); t0 = time.perf_counter()
    con.execute(f"COPY (SELECT * FROM rates ORDER BY negotiated_rate DESC) TO '{out}' (FORMAT CSV, HEADER)")
    dt = time.perf_counter() - t0; stop.set(); th.join(); con.close()
    d = sorted(set(round(p) for p in seen))
    print(f"COPY ... TO csv (sorted, 8M rows): {dt*1000:.0f} ms, samples={len(seen)}, distinct%={d[:8]}...{d[-4:]}, max={max(seen):.1f}")
    os.remove(out)
    # does a statement that spills/sorts hold at a value? sorted COPY above answers that; also ORDER BY whole table
    con = duckdb.connect(DB, read_only=True); enable(con); seen = []; stop = threading.Event()
    th = threading.Thread(target=w, daemon=True); th.start(); t0 = time.perf_counter()
    con.execute("SELECT count(*) FROM (SELECT * FROM rates ORDER BY negotiated_rate, unit_id)").fetchall()
    dt = time.perf_counter() - t0; stop.set(); th.join(); con.close()
    # longest plateau
    plat, best, prev = 0, 0, None
    for v in seen:
        plat = plat + 1 if v == prev else 1; best = max(best, plat); prev = v
    print(f"full ORDER BY: {dt*1000:.0f} ms, samples={len(seen)}, longest plateau={best} samples (~{best*20} ms), max={max(seen):.1f}")
