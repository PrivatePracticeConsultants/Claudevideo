"""Caches keyed correctly after a forget? Every cached / persisted answer is
read before and after store.forget_file of one payer's file (+ the rollup the
forget queues), in the same process (in-memory caches) and in a fresh one
(the on-disk spine_counts / summary_strips / practice medians)."""
import json, tempfile
from pathlib import Path
import h
from fastapi.testclient import TestClient

def snap(c, T):
    M = {"month": "latest", "state": "MO", "payers": []}
    out = {}
    out["stats.payers"] = c.get("/api/stats").json().get("payers")
    out["payers"] = c.get("/api/payers").json().get("payers")
    out["overview.payers"] = c.get("/api/overview").json().get("payers")
    s = c.get("/api/summary?grain=tin").json()
    out["summary.n"] = s.get("count") or s.get("n_rates") or s.get("rows") or json.dumps(s)[:80]
    lb = c.post("/api/leaderboard", json={"market": M}).json()
    out["leaderboard.n"] = len(lb.get("rows") or lb.get("leaderboard") or [])
    out["leads.n"] = len((c.post("/api/leads", json={"market": M, "min_codes": 1}).json()).get("leads") or [])
    out["freshness"] = [p["payer"] for p in c.get("/api/quality/freshness").json().get("payers", [])]
    b = c.post("/api/benchmark/market", json={"subject": T[0], "market": M}).json()
    out["benchmark.payers"] = sorted({r.get("payer") for r in b.get("rows", [])})
    return out

def main():
    root = Path(tempfile.mkdtemp(dir=Path(__file__).parent, prefix="t13_"))
    cfg = h.mkcfg(root); store = h.Store(cfg.store_dir)
    from tests.mrfx.test_review13 import _market
    T, _ = _market(cfg, store, "Alpha Health", "71000", [40, 50, 60, 70, 80])
    _market(cfg, store, "Beta Health", "71000", [42, 52, 62, 72, 82], day="2026-06-02")
    store.rebuild_rollups()
    c = TestClient(h._api.create_app(cfg, store))
    before = snap(c, T)
    beta = [f for f in store.list_files() if "2026-06" in (f.get("filename") or "")] if hasattr(store, "list_files") else None
    with store.connect() as con:
        files = [r[0] for r in con.execute("SELECT filename FROM files WHERE payer='Beta Health'").fetchall()]
    print("forgetting:", files, store.forget_file(files[0]))
    # whatever rollup the forget queued, as the queue worker would
    try:
        store.update_rollups_incremental()
    except Exception as e:
        print("incremental:", e)
    after = snap(c, T)
    c2 = TestClient(h._api.create_app(cfg, h.Store(cfg.store_dir)))
    fresh = snap(c2, T)
    for k in before:
        flag = "" if ("Beta Health" not in json.dumps(after[k]) and "Beta Health" not in json.dumps(fresh[k])) else "   <-- STILL SHOWS THE FORGOTTEN PAYER"
        print(f"{k:18s} before={before[k]!s:60.60s} after={after[k]!s:45.45s} fresh={fresh[k]!s:30.30s}{flag}")

if __name__ == "__main__":
    main()
