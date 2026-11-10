"""Scenario 8: confirm_over_gb. A sparse file whose on-disk size lies (6 GB apparent, real
gz data at the front, zeros after) -> pending_confirmation; confirm via the API; restart while
pending; forget while pending; a gz whose TRAILER lies about the uncompressed size."""
import os, struct
from h import *
from fastapi.testclient import TestClient
from mrfx.ingest import scan_inbox
from mrfx.sniff import preflight
import mrfx.api as api

if __name__ == "__main__":
    cfg = mkcfg("s8", confirm_over_gb=1.0); store = Store(cfg.store_dir)
    gz = gzip.compress(json.dumps(doc(n=3)).encode(), mtime=0)
    big = cfg.inbox_dir / "acme_sparse_2026-05.json.gz"
    with open(big, "wb") as f:
        f.write(gz); f.truncate(6 * 1024 ** 3)          # 6 GB apparent, ~1 KB real
    print("apparent size: %.1f GB, blocks used: %d KB" % (big.stat().st_size / 1e9, big.stat().st_blocks // 2))
    pf = preflight(big, cfg, store)
    print("preflight:", pf.file_type, pf.verdict, pf.est_uncompressed_bytes, pf.messages)
    print("scan:", scan_inbox(cfg, store))
    print("row:", {k: v for k, v in store.file_status(big.name).items() if k in ("status", "error", "size_bytes")})
    print("restart + scan again:", scan_inbox(cfg, Store(cfg.store_dir)), "| row:", store.file_status(big.name)["status"])
    c = TestClient(api.create_app(cfg, store))
    print("confirm:", c.post(f"/api/files/{big.name}/confirm").json())
    t0 = time.time()
    import mrfx.ingest as ing
    while (ing.ingest_in_progress(big.name) or store.file_status(big.name)["status"] in ("queued", "processing")) and time.time() - t0 < 120:
        time.sleep(0.1)
    st = store.file_status(big.name)
    print("after confirm: status=%s rows=%s error=%r where=%s in %.1fs" % (st["status"], st["rows_emitted"], (st["error"] or "")[:150], where_is(cfg, big.name), time.time() - t0))
    print("rates:", rate_counts(store))
    print("confirm again (not pending any more):", c.post(f"/api/files/{big.name}/confirm").status_code)

    print("\n== pending -> forget (user declines) -> re-drop")
    big2 = cfg.inbox_dir / "acme_sparse2_2026-05.json.gz"
    with open(big2, "wb") as f:
        f.write(gz); f.truncate(2 * 1024 ** 3)
    scan_inbox(cfg, store)
    print("row:", store.file_status(big2.name)["status"], "| DELETE:", c.delete(f"/api/files/{big2.name}").json())
    print("where:", where_is(cfg, big2.name), "| row:", store.file_status(big2.name))
    print("scan after forget:", scan_inbox(cfg, store))

    print("\n== a gz whose trailer claims 4 GiB-1 uncompressed (estimate routing) on a 1 KB body")
    bad = bytearray(gz); bad[-4:] = struct.pack("<I", 0xFFFFFFFF)
    lie = put(cfg, "acme_lie_2026-05.json.gz", bytes(bad))
    pf = preflight(lie, cfg, store)
    print("preflight est_uncompressed:", pf.est_uncompressed_bytes, "type:", pf.file_type, "msgs:", pf.messages)
    print("scan:", scan_inbox(cfg, store))
    print("row:", {k: v for k, v in store.file_status(lie.name).items() if k in ("status", "error", "rows_emitted")}, "| rates:", rate_counts(store))
    big.unlink(missing_ok=True)
