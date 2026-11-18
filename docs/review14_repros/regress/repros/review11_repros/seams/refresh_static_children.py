"""A static index (same URL every month) lists a rate file whose URL is ALSO
static; the payer republishes both in place. The monthly refresh re-queues the
index (`requeue_urls`) and re-expands it. Is the child's new vintage loaded?
Default config (delete_raw_after_ingest True)."""
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness  # noqa: E402

CUR = {"child": b"", "toc": b""}


def static(key):
    def fn(h, head):
        data = CUR[key]
        h.send_response(200)
        h.send_header("Content-Length", str(len(data)))
        h.send_header("ETag", f'"{hash(data) & 0xffff}"')
        h.end_headers()
        if not head:
            h.wfile.write(data)
    return fn


def drain(cfg, store, secs=60):
    stop = threading.Event()
    from mrfx.fetch import run_queue
    th = threading.Thread(target=lambda: run_queue(cfg, store, stop=stop), daemon=True)
    th.start()
    t = time.monotonic() + secs
    while time.monotonic() < t:
        rows = harness.queue_rows(store)
        if rows and all(r["status"] in ("done", "failed", "skipped") for r in rows):
            break
        time.sleep(0.3)
    stop.set(); th.join(5)


def main():
    root = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/mine/refresh")
    cfg = harness.scratch_cfg(root, early_dedup_mb=0, parallel_downloads=1, parallel_ingests=1)
    store = harness.open_store(cfg)
    httpd, base = harness.start_server()
    child_url = f"{base}/files/in-network-rates.json.gz"
    CUR["child"] = harness.rate_file_bytes(50, day="2026-05-01")
    CUR["toc"] = json.dumps({"reporting_entity_name": "Static Payer", "reporting_entity_type": "issuer",
                             "reporting_structure": [{"reporting_plans": [{"plan_name": "PPO", "plan_id_type": "EIN",
                                                                           "plan_id": "1", "plan_market_type": "group"}],
                                                      "in_network_files": [{"description": "rates", "location": child_url}]}]}).encode()
    harness.ROUTES["/index.json"] = static("toc")
    harness.ROUTES["/files/in-network-rates.json.gz"] = static("child")
    from mrfx import fetch
    toc_url = f"{base}/index.json"
    fetch.add_urls(store, [toc_url])
    drain(cfg, store)
    with store.connect() as con:
        print("after first load:", con.execute("SELECT filename, file_month, status FROM files").fetchall(),
              con.execute("SELECT count(*) FROM rates").fetchone())
    print("queue:", [(r["id"], r["status"], r["kind"]) for r in harness.queue_rows(store)])
    # the payer republishes both in place (June vintage)
    CUR["child"] = harness.rate_file_bytes(80, day="2026-06-01", tin0=450000000)
    CUR["toc"] = CUR["toc"].replace(b'"description": "rates"', b'"description": "rates v2"')
    n = len(harness.LOG)
    print("requeued index:", store.requeue_urls([toc_url]))
    drain(cfg, store)
    print("requests on refresh:", [(m, p) for m, p, h in harness.LOG[n:]])
    with store.connect() as con:
        print("after refresh:", con.execute("SELECT filename, file_month, status FROM files").fetchall(),
              con.execute("SELECT count(*) FROM rates").fetchone())
    print("queue:", [(r["id"], r["status"], r["kind"], (r["error"] or "")[:60]) for r in harness.queue_rows(store)])
    httpd.shutdown()


if __name__ == "__main__":
    main()
