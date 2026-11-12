"""(E) Same process: the URL worker's processor has claimed a link row
'ingesting' (next_fetched_url) and the identical bytes land in the inbox. The
inbox scan must WAIT (not load them a second time), and once the link's load
ends 'done' the inbox copy must end 'skipped' as a twin. Contrast: a NEW Store
instance (a new process) treats the same row as an orphan and loads."""
import hashlib
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "/home/user/Claudevideo")
from mrfx import fetch  # noqa: E402
from mrfx.config import MrfxConfig  # noqa: E402
from mrfx.ingest import scan_inbox  # noqa: E402
from mrfx.store import Store  # noqa: E402
from tests.mrfx.test_review11 import gz, q, rate_doc  # noqa: E402

ROOT = Path(__file__).parent / "scratch_pe"


def fresh(name):
    root = ROOT / name
    if root.exists():
        shutil.rmtree(root)
    cfg = MrfxConfig(inbox_dir=root / "inbox", processed_dir=root / "processed",
                     failed_dir=root / "failed", store_dir=root / "store",
                     downloads_dir=root / "downloads", entity_map_path=root / "em.yaml")
    cfg.enrichment.mode = "off"
    cfg.ensure_dirs()
    return cfg, Store(cfg.store_dir)


def main():
    body = gz(rate_doc(n=30))
    sha = hashlib.sha256(body).hexdigest()
    ok = True

    print("== live claim in THIS process ==")
    cfg, store = fresh("live")
    fetch.add_urls(store, ["http://127.0.0.1:9/acme_2026-06.json.gz"])
    store.update_url(1, status="fetched", content_sha=sha, kind="in_network",
                     filename="x_acme_2026-06.json.gz")
    rec = store.next_fetched_url()                  # the processor's claim
    print("  claimed:", rec["id"], rec["status"], "live set:", store._live_url_ingests)
    (cfg.inbox_dir / "acme_2026-06 (1).json.gz").write_bytes(body)
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", scan_inbox(cfg, store)), daemon=True)
    th.start()
    th.join(6)
    waiting = th.is_alive()
    print("  inbox scan still waiting after 6 s:", waiting,
          "| files row:", q(store, "SELECT status, error FROM files"))
    ok &= waiting
    # the link's load ends
    store.update_url(1, status="done", rows_emitted=30)
    th.join(15)
    print("  scan ended:", not th.is_alive(), "->", out.get("r"))
    ok &= (not th.is_alive()) and out["r"][0]["status"] == "skipped"
    print("  files rows:", q(store, "SELECT filename, status, rows_emitted FROM files"))

    print("== the same row seen by a NEW Store (a new process) ==")
    cfg2, store2 = fresh("orphan")
    fetch.add_urls(store2, ["http://127.0.0.1:9/acme_2026-06.json.gz"])
    store2.update_url(1, status="fetched", content_sha=sha, kind="in_network",
                      filename="x_acme_2026-06.json.gz")
    store2.next_fetched_url()
    store2.close()
    s3 = Store(cfg2.store_dir)
    (cfg2.inbox_dir / "acme_2026-06 (1).json.gz").write_bytes(body)
    t0 = time.monotonic()
    r = scan_inbox(cfg2, s3)
    print(f"  orphan: loaded in {time.monotonic() - t0:.1f} s ->", [(x["status"], x.get("rows")) for x in r])
    ok &= r[0]["status"] == "done"

    print("== the link-side content claim (claim_content_ingest) is tracked too ==")
    cfg3, store3 = fresh("claim")
    fetch.add_urls(store3, ["http://127.0.0.1:9/a.json.gz", "http://127.0.0.1:9/b.json.gz"])
    store3.update_url(1, content_sha=sha)
    got = store3.claim_content_ingest(sha, 1, "in_network", "x_a.json.gz")
    print("  claim_content_ingest ->", got, "| live set:", store3._live_url_ingests,
          "| row:", q(store3, "SELECT status FROM url_queue WHERE id = 1"))
    ok &= 1 in store3._live_url_ingests
    print("RESULT:", "held" if ok else "PROBLEM")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
