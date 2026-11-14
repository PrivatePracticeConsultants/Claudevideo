import io, json, shutil, sys, tempfile
from pathlib import Path
if __name__ == "__main__":
    import mrfx.api as _api
    _api.TRUSTED_HOSTS.add("testserver")
    from fastapi.testclient import TestClient
    from mrfx.config import MrfxConfig
    from mrfx.store import Store
    from mrfx.ingest import ingest_file
    from mrfx.api import create_app
    tmp = Path(tempfile.mkdtemp(prefix="mpfsprobe_", dir=sys.argv[1]))
    cfg = MrfxConfig(inbox_dir=tmp/"inbox", processed_dir=tmp/"processed", failed_dir=tmp/"failed",
                     store_dir=tmp/"store", downloads_dir=tmp/"downloads", entity_map_path=tmp/"entity_map.yaml")
    cfg.ensure_dirs()
    store = Store(cfg.store_dir)
    fx = Path("tests/mrfx/fixtures/innetwork_mixed.json")
    dst = cfg.inbox_dir / "testco_2026-05-01.json"; dst.write_bytes(fx.read_bytes())
    ingest_file(cfg, store, dst)
    with store.connect() as con:
        codes = [c for (c,) in con.execute("SELECT DISTINCT billing_code FROM rates_by_tin ORDER BY 1").fetchall()]
        states = con.execute("SELECT DISTINCT unnest(states) FROM tin_directory").fetchall()
    print("codes", codes[:5], "states", states[:3])
    app = create_app(cfg, store)
    c = TestClient(app, raise_server_exceptions=False)
    csv = "code,locality,non_facility_rate\n" + f"{codes[0]},00,nan\n" + (f"{codes[1]},00,inf\n" if len(codes) > 1 else "")
    r = c.post("/api/mpfs/upload", files={"file": ("m.csv", csv.encode(), "text/csv")})
    print("upload", r.status_code, r.text[:200])
    r = c.get("/api/summary"); print("summary", r.status_code, r.text[:200])
    r = c.get(f"/api/code/{codes[0]}"); print("code", r.status_code, r.text[:200])
    r = c.post("/api/benchmark/market", json={"subject": "", "market": {"month": "latest"}}); print("bench", r.status_code, r.text[:120])
    shutil.rmtree(tmp, ignore_errors=True)
