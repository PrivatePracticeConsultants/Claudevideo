"""Scenario 8: confirm_over_gb. (a) a gz with zero padding after its member (what a sparse /
pre-allocated copy looks like): how long preflight takes per MB of padding (CPython's gzip
consumes padding one byte at a time); (b) pending_confirmation -> restart -> confirm via the API;
(c) pending -> forget -> re-drop; (d) a gz whose TRAILER lies about the uncompressed size."""
import struct
from h import *
from fastapi.testclient import TestClient
from mrfx.ingest import scan_inbox
from mrfx.sniff import preflight
import mrfx.api as api
import mrfx.ingest as ing

if __name__ == "__main__":
    cfg = mkcfg("s8", confirm_over_gb=0.001); store = Store(cfg.store_dir)   # 1 MB threshold
    gz = gzip.compress(json.dumps(doc(n=3)).encode(), mtime=0)
    print("== (a) zero padding after the gzip member")
    for mb in (1, 8):
        pad = cfg.inbox_dir / f"pad{mb}.json.gz"
        with open(pad, "wb") as f:
            f.write(gz); f.truncate(len(gz) + mb * 1024 * 1024)
        t0 = time.time(); pf = preflight(pad, cfg, store); dt_ = time.time() - t0
        print(f"  {mb} MB padding: preflight {dt_:.2f}s -> {pf.file_type} {pf.verdict} ({dt_/mb:.2f} s/MB; a 6 GB sparse file ~{dt_/mb*6144/60:.0f} min)")
        pad.unlink()

    print("== (b) pending_confirmation -> restart -> confirm")
    big = put(cfg, "acme_big_2026-05.json.gz", doc(n=5, pad_mb=1.5), gz=True)
    print("  size: %.2f MB" % (big.stat().st_size / 1e6), "| scan:", scan_inbox(cfg, store))
    print("  row:", {k: v for k, v in store.file_status(big.name).items() if k in ("status", "error")})
    print("  restart + scan again:", scan_inbox(cfg, Store(cfg.store_dir)), "| row:", store.file_status(big.name)["status"])
    c = TestClient(api.create_app(cfg, store))
    print("  confirm:", c.post(f"/api/files/{big.name}/confirm").json())
    t0 = time.time()
    while (ing.ingest_in_progress(big.name) or store.file_status(big.name)["status"] in ("queued", "processing")) and time.time() - t0 < 120:
        time.sleep(0.1)
    st = store.file_status(big.name)
    print("  after confirm: status=%s rows=%s error=%r where=%s" % (st["status"], st["rows_emitted"], (st["error"] or "")[:100], where_is(cfg, big.name)))
    print("  rates:", rate_counts(store), "| confirm again:", c.post(f"/api/files/{big.name}/confirm").status_code)

    print("== (c) pending -> forget (decline) -> re-drop")
    big2 = put(cfg, "acme_big2_2026-05.json.gz", doc(n=5, seed=2, pad_mb=1.5), gz=True)
    scan_inbox(cfg, store)
    print("  row:", store.file_status(big2.name)["status"], "| DELETE:", c.delete(f"/api/files/{big2.name}").json())
    print("  where:", where_is(cfg, big2.name), "| row:", store.file_status(big2.name), "| scan after forget:", scan_inbox(cfg, store))

    print("== (d) a gz whose trailer claims 4 GiB-1 uncompressed on a 1 KB body")
    bad = bytearray(gz); bad[-4:] = struct.pack("<I", 0xFFFFFFFF)
    lie = put(cfg, "acme_lie_2026-05.json.gz", bytes(bad))
    pf = preflight(lie, cfg, store)
    print("  preflight est_uncompressed: %.1f GB" % (pf.est_uncompressed_bytes / 1e9), "type:", pf.file_type, "| scan:", scan_inbox(cfg, store))
    print("  row:", {k: v for k, v in store.file_status(lie.name).items() if k in ("status", "error", "rows_emitted")}, "| rates:", rate_counts(store))
