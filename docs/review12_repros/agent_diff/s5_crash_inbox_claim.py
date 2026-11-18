"""S5: inbox file A is loading (claim held in memory); link B with the same
bytes defers to it ("will retry automatically from the kept download if that
one fails"). The process dies. After the restart: who revives B when A never
loads again (the user removed A from the inbox, or A was moved out)?"""
import hashlib
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402


def main():
    logging.basicConfig(level=logging.WARNING)
    cfg = h.scratch_cfg("s5")
    store = h.open_store(cfg)
    body = h.rate_file_bytes(30)
    sha = hashlib.sha256(body).hexdigest()
    from mrfx import fetch
    # A: inbox file mid-load (what _content_claim + the 'processing' upsert leave)
    store.upsert_file("A.json.gz", status="processing", payer="Test Payer", file_type="in_network")
    assert store.claim_inbox_content(sha, "A.json.gz") is None
    # B: a link whose download finished (kept) and whose claim deferred to A
    fetch.add_urls(store, ["http://127.0.0.1:9/B.json.gz"])
    uid = h.queue_rows(store)[0]["id"]
    dest = cfg.downloads_dir / fetch.filename_for("http://127.0.0.1:9/B.json.gz")
    dest.write_bytes(body)
    store.update_url(uid, content_sha=sha)
    twin = store.claim_content_ingest(sha, uid, "in_network", dest.name)
    print("B's claim ->", twin, h.queue_rows(store, "id, status, kind, error"))
    # ---- kill: a new process, new Store (the in-memory claim is gone); A is no longer in the inbox
    store2 = h.Store(cfg.store_dir, cfg.duckdb_memory_gb, temp_dir=cfg.duckdb_temp_dir, keep_warm=True)
    print("recover_stuck_files:", store2.recover_stuck_files(), "recover_stuck_urls:", store2.recover_stuck_urls())
    with store2.connect() as con:
        print("files:", con.execute("SELECT filename, status FROM files").fetchall())
    print("B after restart:", h.queue_rows(store2, "id, status, kind, duplicate_of, error"))
    # what the dashboard offers: retry? (skipped -> queued is a user transition)
    print("user retry allowed:", store2.set_url_status_by_id(uid, "queued"))
    print("B then:", h.queue_rows(store2, "id, status, kind"))
    print("kept download still there:", dest.exists())


if __name__ == "__main__":
    main()
