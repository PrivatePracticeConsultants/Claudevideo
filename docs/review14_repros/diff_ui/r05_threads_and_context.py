"""Progress plumbing probes: (a) DuckDB progress SETs stay per-connection,
(b) the contextvar never leaks into the next request served by the same worker
thread (incl. an exception path), (c) sampler threads after 200 runs and 50
Stops, (d) registry bound."""
import tempfile, threading, time, secrets, collections
from pathlib import Path
import h, duckdb
from fastapi.testclient import TestClient

def nthreads(prefix="mrfx-progress"):
    return sum(1 for t in threading.enumerate() if t.name == prefix)

def main():
    root = Path(tempfile.mkdtemp(dir=Path(__file__).parent, prefix="t05_"))
    cfg, store, A = h.small_store(root, n=40)
    # (a)
    c1 = store.connect(); c2 = store.connect()
    c1.execute("SET enable_progress_bar = true"); c1.execute("SET progress_bar_time = 0")
    print("(a) other connection sees enable_progress_bar =",
          c2.execute("SELECT current_setting('enable_progress_bar')").fetchone()[0],
          "progress_bar_time =", c2.execute("SELECT current_setting('progress_bar_time')").fetchone()[0])
    c1.close(); c2.close()
    app = h._api.create_app(cfg, store)
    from mrfx import progress as P
    seen = collections.Counter()
    @app.get("/api/zz_probe")
    def probe():
        p = P.current()
        seen["leak" if p is not P._NOOP else "clean"] += 1
        return {"tok": getattr(p, "token", None)}
    @app.get("/api/zz_boom")
    def boom():
        with store.connect() as con:
            con.execute("SELECT 1")
        raise RuntimeError("boom")
    # put the probes ahead of the SPA catch-all
    rs = app.router.routes
    for path in ("/api/zz_probe", "/api/zz_boom"):
        i = next(i for i, r in enumerate(rs) if getattr(r, "path", "") == path)
        rs.insert(0, rs.pop(i))
    c = TestClient(app, raise_server_exceptions=False)
    base = threading.active_count()
    # (b)+(c): 200 runs with a token over real output routes, interleaved with probes
    for i in range(200):
        tok = secrets.token_hex(12)
        r = c.get("/api/rates?grain=tin&limit=20", headers={"X-Mrfx-Progress": tok})
        c.get("/api/zz_boom", headers={"X-Mrfx-Progress": secrets.token_hex(12)})
        c.get("/api/zz_probe")
    time.sleep(0.5)
    print("(b) token-less probes after token runs:", dict(seen))
    print("(c) after 200 runs (+200 failing): sampler threads alive =", nthreads(),
          "active threads delta =", threading.active_count() - base)
    # 50 stops
    from mrfx.store import Store
    orig = Store.connect
    def slow(self):
        con = orig(self)
        if P.current() is not P._NOOP:
            time.sleep(0.15)     # every statement of a token run is "slow"
        return con
    Store.connect = slow
    codes = collections.Counter()
    for i in range(50):
        tok = secrets.token_hex(12); out = {}
        def run():
            out["c"] = c.post("/api/benchmark/market", json={"subject": A[0], "market": {"state": "MO"}},
                              headers={"X-Mrfx-Progress": tok}).status_code
        t = threading.Thread(target=run); t.start(); time.sleep(0.4)
        d = c.delete(f"/api/progress/{tok}").status_code; t.join(); codes[(d, out["c"])] += 1
    Store.connect = orig
    time.sleep(0.5)
    print("(c) 50 stops (DELETE code, run code):", dict(codes), "sampler threads alive =", nthreads(),
          "active threads delta =", threading.active_count() - base)
    reg = app.state.progress_registry
    print("(d) registry entries:", len(reg._by_token), "(cap", reg.cap, ")")

if __name__ == "__main__":
    main()
