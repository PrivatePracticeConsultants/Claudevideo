"""The user's screenshot: the header says 49 payers, the Leads payer chips
offer 3. Hypothesis: the chips (and every picker) read the analytics spine
(rates_by_tin_tbl) while the header reads the files table; when the rollup
is behind or has failed, the two diverge and nothing on screen says so.

Repro on a scratch store: load payer A with a working rollup, then load
payers B and C with the rollup failing (as a repeated OOM / a kill would),
then ask the API what the header and the chips would show."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/real")
FILES = ["moda_ModaTXPriceTrans_20261101.json.gz",
         "uhc_2026-11-01_HEALTHSCOPE-BENEFITS_Third-Party-Administrator_SHO_U1_3342_in-network-rates.json.gz",
         "wy_2026-10-27_000005300_in-network-rates_05_of_09.json.gz"]


def main():
    sys.path.insert(0, "/home/user/Claudevideo")
    from mrfx.config import MrfxConfig
    from mrfx.store import Store
    from mrfx.ingest import scan_inbox
    from mrfx.api import create_app
    from fastapi.testclient import TestClient
    root = Path(tempfile.mkdtemp(prefix="stale_", dir=HERE))
    try:
        cfg = MrfxConfig(inbox_dir=root / "in", processed_dir=root / "p", failed_dir=root / "f",
                         store_dir=root / "s", downloads_dir=root / "dl", parallel_ingests=1,
                         enrichment={"mode": "off"})
        for d in (cfg.inbox_dir, cfg.processed_dir, cfg.failed_dir, cfg.downloads_dir):
            d.mkdir(parents=True, exist_ok=True)
        store = Store(cfg.store_dir, 2, temp_dir=cfg.duckdb_temp_dir)
        # payer A, rollup works
        shutil.copy(REAL / FILES[0], cfg.inbox_dir / FILES[0])
        scan_inbox(cfg, store)
        # payers B and C land while every rollup attempt fails
        def boom(*a, **k):
            raise RuntimeError("Out of Memory Error: could not allocate block (simulated)")
        store.rebuild_rollups = boom
        store.update_rollups_incremental = boom
        for f in FILES[1:]:
            shutil.copy(REAL / f, cfg.inbox_dir / f)
        try:
            scan_inbox(cfg, store)
        except Exception as e:  # the scan must survive a failing rollup
            print("scan raised:", e)
        app = create_app(cfg, store)
        c = TestClient(app, base_url="http://localhost")
        stats = c.get("/api/stats").json()
        payers = c.get("/api/payers").json()
        boot = c.get("/api/boot").json()
        print("files table:", [(r[0][:20], r[1]) for r in store.connect().execute(
            "SELECT payer, status FROM files ORDER BY payer").fetchall()])
        print("header /api/stats:", {k: stats.get(k) for k in ("payers", "files_done", "rates", "tins")})
        print("chips  /api/payers:", payers)
        print("stale per store:", store.rollups_stale(), "|", store.rollup_stale_reason())
        keys = set(stats) | set(boot)
        print("any coverage/stale field in /api/stats or /api/boot?",
              sorted(k for k in keys if "stale" in k or "cover" in k or "rollup" in k or "analytics" in k) or "NONE")
        # what a Leads / Benchmark / Rate card query sees
        r = c.get("/api/rates", params={"grain": "tin", "limit": 1}).json()
        print("/api/rates total rows the Explorer counts:", r.get("total") or r.get("count") or list(r)[:5])
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
