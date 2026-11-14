"""/api/upload and inbox same-name handling: odd names, same file twice,
different files in quick succession."""
import time
from h import *
from fastapi.testclient import TestClient
from mrfx.api import create_app
from mrfx.ingest import unique_inbox_name, scan_inbox

def doc(n, rate=40.0, payer="Acme Health"):
    return json.dumps({**HDR, "reporting_entity_name": payer, "in_network": [
        item("97110", groups=[grp([str(1000000000 + i)], f"{100000000+i}")], rate=rate) for i in range(n)]}).encode()

def settle(st, n_files, secs=40):
    t0 = time.time()
    while time.time() - t0 < secs:
        rows = q(st, "SELECT status FROM files")
        if len(rows) >= n_files and all(r[0] in ("done", "failed", "quarantined") for r in rows):
            return
        time.sleep(0.3)

if __name__ == "__main__":
    cfg = mkcfg("n9"); st = Store(cfg.store_dir)
    c = TestClient(create_app(cfg, st))
    print("A. the SAME file uploaded twice")
    for label, n in (("0.2 MiB", 600), ("2.3 MiB", 7000)):
        body = doc(n)
        name = f"acme_{n}_2026-05.json"
        r1 = c.post("/api/upload", files={"file": (name, body)}).json(); settle(st, 1)
        time.sleep(1)
        r2 = c.post("/api/upload", files={"file": (name, body)}).json(); time.sleep(3); settle(st, 1)
        print(f"   {label}: first -> {r1['status']}; second (identical bytes) -> {r2['status']} {r2.get('filename')} {r2.get('note', '')[:70]}")
    print("   files:", q(st, "SELECT filename, status, rows_emitted FROM files ORDER BY 1"))
    print("   rates rows per source:", q(st, "SELECT source_file, count(*) FROM rates GROUP BY 1 ORDER BY 1"))

    print("B. unique_inbox_name on odd names")
    for nm in ("plan.v2.2026-05.json.gz", "rates", "tarifs_été_2026-05.json", "rates.json.2026-05.json",
               "data.json.backup.json", "x.JSON.GZ", "archive.2026-05.zip", "a.b.c"):
        print(f"   {nm!r:34} -> {unique_inbox_name(cfg.inbox_dir, nm, st, 'abc123')!r}")

    print("C. two DIFFERENT files, same name, uploaded 0.2 s apart")
    cfg2 = mkcfg("n9c"); st2 = Store(cfg2.store_dir)
    c2 = TestClient(create_app(cfg2, st2))
    ra = c2.post("/api/upload", files={"file": ("acme_2026-05.json", doc(3000, 40.0))}).json()
    time.sleep(0.2)
    rb = c2.post("/api/upload", files={"file": ("acme_2026-05.json", doc(3100, 55.0))}).json()
    time.sleep(2); settle(st2, 2)
    print("   first ->", ra, "\n   second ->", {k: v for k, v in rb.items() if k != 'note'})
    print("   files:", q(st2, "SELECT filename, status, rows_emitted FROM files ORDER BY 1"))
    print("   rates:", q(st2, "SELECT source_file, negotiated_rate, count(*) FROM rates GROUP BY 1, 2 ORDER BY 1"), "(expected 3000 @40 and 3100 @55)")
    print("   inbox:", [x.name for x in cfg2.inbox_dir.iterdir()], "processed:", [x.name for x in cfg2.processed_dir.iterdir()])
