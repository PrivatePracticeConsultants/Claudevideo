"""The async poll/stop answer a RUNNING token while all 100 worker threads are
blocked (110 slow sync requests), and the stop takes effect when the run
gets to its next connection."""
import secrets, tempfile, threading, time
from pathlib import Path
import h
from fastapi.testclient import TestClient

def main():
    root = Path(tempfile.mkdtemp(dir=Path(__file__).parent, prefix="t14_"))
    cfg = h.mkcfg(root); store = h.Store(cfg.store_dir)
    from tests.mrfx.test_review13 import _market
    T, _ = _market(cfg, store, "Alpha Health", "71000", [40, 50, 60, 70, 80]); store.rebuild_rollups()
    app = h._api.create_app(cfg, store)
    gate = threading.Event()
    real_stats = store.store_stats
    store.store_stats = lambda *a, **k: (gate.wait(20), real_stats())[1]
    import mrfx.api as api
    orig = api.compute_benchmark
    def bench(*a, **k):
        gate.wait(20)          # the run itself is also parked in the pool
        return orig(*a, **k)
    api.compute_benchmark = bench
    with TestClient(app) as c:
        tok = secrets.token_hex(12); res = {}
        tr = threading.Thread(target=lambda: res.update(r=c.post("/api/benchmark/market",
                 json={"subject": T[0], "market": {"month": "latest", "state": "MO"}},
                 headers={"X-Mrfx-Progress": tok}).status_code)); tr.start()
        ths = [threading.Thread(target=lambda: c.get("/api/stats")) for _ in range(110)]
        [t.start() for t in ths]; time.sleep(1.0)
        t0 = time.monotonic(); p = c.get(f"/api/progress/{tok}"); t1 = time.monotonic()
        d = c.delete(f"/api/progress/{tok}"); t2 = time.monotonic()
        print(f"poll while saturated: {p.status_code} {p.json().get('state')} in {t1-t0:.3f} s; "
              f"DELETE {d.status_code} in {t2-t1:.3f} s")
        gate.set(); tr.join(30); [t.join(30) for t in ths]
        print("the stopped run answered:", res.get("r"))

if __name__ == "__main__":
    main()
