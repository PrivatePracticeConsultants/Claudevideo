"""S2: `mrfx ingest` (no URL worker, so recover_stuck_urls never runs) on a
store where a previous server was killed while a LINK row was 'ingesting'
(status + content_sha stay in url_queue). The user then drops the same bytes
in the inbox (the 403 message tells them to) and runs `mrfx ingest`. Does the
scan return, or wait forever on the 'ingesting' twin nobody owns?"""
import hashlib
import logging
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    cfg = h.scratch_cfg("s2")
    store = h.open_store(cfg)
    body = h.rate_file_bytes(30)
    sha = hashlib.sha256(body).hexdigest()
    from mrfx import fetch
    fetch.add_urls(store, ["http://127.0.0.1:9/acme_2026-06.json.gz"])
    uid = h.queue_rows(store)[0]["id"]
    # what a kill mid-ingest leaves behind (claim_content_ingest set 'ingesting')
    store.update_url(uid, status="ingesting", content_sha=sha, kind="in_network",
                     filename="x_acme_2026-06.json.gz")
    (cfg.inbox_dir / "acme_2026-06 (1).json.gz").write_bytes(body)
    store.close() if hasattr(store, "close") else None
    # ---- `mrfx ingest` as cli.cmd_ingest does it: a fresh Store, recover_stuck_files, scan
    store2 = h.Store(cfg.store_dir, cfg.duckdb_memory_gb, temp_dir=cfg.duckdb_temp_dir,
                     auto_spill=True, keep_warm=True)
    store2.recover_stuck_files()
    from mrfx.ingest import scan_inbox
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", scan_inbox(cfg, store2)), daemon=True)
    t = time.monotonic()
    th.start()
    th.join(20)
    print("scan returned:", not th.is_alive(), "after %.1fs" % (time.monotonic() - t), out.get("r"))
    print("url row:", h.queue_rows(store2, "id, status, content_sha"))
    with store2.connect() as con:
        print("files rows:", con.execute("SELECT filename, status, error FROM files").fetchall())


if __name__ == "__main__":
    main()
