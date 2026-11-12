"""With delete_raw_after_ingest=False the raw download + .fetchmeta stay on
disk. A payer republishes NEW bytes under the SAME URL (fixed 'latest' links).
The monthly refresh / a user retry re-queues the row: fetch_url_record's reuse
path says "NEVER re-download here" when dest + sidecar exist. Do the new
bytes ever load?"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness  # noqa: E402

CUR = {"data": b""}


def serve(h, head):
    data = CUR["data"]
    h.send_response(200)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("ETag", f'"{len(data)}"')
    h.end_headers()
    if not head:
        h.wfile.write(data)


def drain(cfg, store, secs=40):
    import time
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


def rows_loaded(store):
    with store.connect() as con:
        return con.execute("SELECT count(*), count(DISTINCT negotiated_rate) FROM rates").fetchone()


def main():
    root = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/mine/stale")
    cfg = harness.scratch_cfg(root, delete_raw_after_ingest=False, early_dedup_mb=0,
                              parallel_downloads=1, parallel_ingests=1)
    store = harness.open_store(cfg)
    CUR["data"] = harness.rate_file_bytes(50, day="2026-05-01")
    harness.ROUTES["/latest.json.gz"] = serve
    httpd, base = harness.start_server()
    from mrfx import fetch
    url = f"{base}/latest.json.gz"
    fetch.add_urls(store, [url])
    drain(cfg, store)
    print("after first load:", harness.queue_rows(store)[0]["status"], rows_loaded(store))
    with store.connect() as con:
        print("file months:", con.execute("SELECT filename, file_month, status FROM files").fetchall())
    # the payer republishes: 80 practices, June, different rates
    CUR["data"] = harness.rate_file_bytes(80, day="2026-06-01", tin0=450000000)
    n_log = len(harness.LOG)
    print("requeued:", store.requeue_urls([url]))
    drain(cfg, store)
    print("requests during refresh:", [(m, p, dict(h).get("Range"), dict(h).get("If-None-Match"), dict(h).get("If-Range")) for m, p, h in harness.LOG[n_log:]])
    print("after refresh:", harness.queue_rows(store)[0]["status"], rows_loaded(store))
    with store.connect() as con:
        print("file months:", con.execute("SELECT filename, file_month, status FROM files").fetchall())
    httpd.shutdown()


if __name__ == "__main__":
    main()
