"""S3a: a killed names-only (directory) plan -> the queue's catch-up starts a
WHOLE-STORE full build. S3b: mid full-plan over an existing spine the header /
pickers / CSV sidecar say 0 payers covered while every payer is served."""
import shutil, sys
from pathlib import Path
D = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(D / "scripts"))
from s2_midplan import Killed, kill_after  # noqa: E402


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    import shapes
    from mrfx.store import Store
    work = D / "w_s3"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    s = Store(work / "store")
    shapes.seed(s)
    s.rebuild_rollups()
    s._rollup_partition_rows = lambda split=1, threads=1: 7   # several directory slices
    real = kill_after(s, 1)
    try:
        s.rebuild_rollups(names_only=True)       # e.g. enrichment's names refresh, killed
    except Killed:
        pass
    p = s.rollup_plan()
    print("S3a names plan part-way:", p["kind"], f"{p['next']}/{len(p['slices'])}",
          "| rollups_stale():", s.rollups_stale(), "| reason:", s.rollup_stale_reason())
    s._run_slice = real
    kinds = []
    orig_new = s._new_plan
    def spy(con, kind, log):
        pl = orig_new(con, kind, log); kinds.append((kind, len(pl["slices"]),
            sum(1 for x in pl["slices"] if x["t"] == "spine"))); return pl
    s._new_plan = spy
    # what fetch.rebuild_now / cli._catch_up do
    try:
        s.update_rollups_incremental()
    except Exception as e:
        print("   incremental refused:", e, "-> rebuild_rollups() (FULL)")
        s.rebuild_rollups()
    print("   plans created by the catch-up (kind, slices, spine slices):", kinds)

    # S3b
    from fastapi.testclient import TestClient
    import mrfx.api as api
    from mrfx.config import MrfxConfig
    api.TRUSTED_HOSTS.add("testserver")
    cfg = MrfxConfig(inbox_dir=work / "inbox", processed_dir=work / "processed",
                     failed_dir=work / "failed", store_dir=work / "store",
                     downloads_dir=work / "dl", entity_map_path=work / "em.yaml")
    cfg.ensure_dirs()
    with s.connect() as con:      # any reason the incremental refuses -> a full plan
        con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_needs_full','1')")
    s._rollup_partition_rows = lambda split=1, threads=1: 60
    real = kill_after(s, 4)       # 2 dir slices + prune + 1 Alpha slice committed
    try:
        s.rebuild_rollups()
    except Killed:
        pass
    s._run_slice = real
    s._rollup_failure = None
    with s.connect() as con:
        con.execute("DELETE FROM meta WHERE key='rollup_failure'")
    c = TestClient(api.create_app(cfg, s))
    a = c.get("/api/stats").json()["analytics"]
    print("S3b header:", a["sentence"])
    print("   /api/payers covered:", c.get("/api/payers").json().get("covered"))
    for p in shapes.PAYERS:
        r = c.get("/api/rates", params={"grain": "tin", "payer": p}).json()
        print(f"   /api/rates payer={p!r}: total={r['total']}")
    print("   document sentence:", s.analytics_note())
