"""Stop on /api/quality/size (the Benchmark tab's 'size premium' panel):
the nested compute_benchmark's Stopped is swallowed by a broad except and the
degraded answer (position None) is stored in the generation cache."""
import tempfile, threading, time, secrets
from pathlib import Path
import h
from fastapi.testclient import TestClient

def main():
    root = Path(tempfile.mkdtemp(dir=Path(__file__).parent, prefix="t02_"))
    cfg = h.mkcfg(root); store = h.Store(cfg.store_dir)
    from tests.mrfx.test_quality import _size_market, MARKET
    _size_market(store); A = ["431000000"]
    app = h._api.create_app(cfg, store)
    c = TestClient(app)
    body = {"market": MARKET, "subject": A[0]}
    import mrfx.quality as q
    ref = q.size_premium.uncached(store, body["market"], subject=A[0])
    print("reference subject.position =", (ref.get("subject") or {}).get("position"))
    import mrfx.benchmark as bm
    orig = bm.compute_benchmark
    def slow(*a, **k):
        time.sleep(0.8)
        return orig(*a, **k)
    bm.compute_benchmark = slow
    tok = secrets.token_hex(12); res = {}
    def run():
        r = c.post("/api/quality/size", json=body, headers={"X-Mrfx-Progress": tok})
        res["code"] = r.status_code
    t = threading.Thread(target=run); t.start(); time.sleep(0.3)
    print("DELETE ->", c.delete(f"/api/progress/{tok}").status_code); t.join()
    bm.compute_benchmark = orig
    print("stopped run answered:", res["code"])
    r = c.post("/api/quality/size", json=body)
    print("next plain request:", r.status_code, "subject.position =",
          (r.json().get("subject") or {}).get("position"))

if __name__ == "__main__":
    main()
