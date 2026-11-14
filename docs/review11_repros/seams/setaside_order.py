"""M2: with ONE download slot and download_max_seconds=3, does the file the cap
set aside yield the slot to the next file, or re-claim it at once?
Server: two 1.2 MB files dripped at ~0.3 MB/s (each needs ~4 s > the cap)."""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness  # noqa: E402

CLAIMS: list = []


def drip(data, bps=300_000):
    def fn(h, head):
        rng = h.headers.get("Range")
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        h.send_response(206 if rng else 200)
        h.send_header("Content-Length", str(len(data) - start))
        h.send_header("Accept-Ranges", "bytes")
        h.send_header("ETag", '"v1"')
        if rng:
            h.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        h.end_headers()
        if head:
            return
        i = start
        while i < len(data):
            h.wfile.write(data[i:i + 30_000]); h.wfile.flush()
            i += 30_000
            time.sleep(30_000 / bps)
    return fn


def main():
    root = Path("/tmp/mrfx_review11/mine/setaside")
    cfg = harness.scratch_cfg(root, parallel_downloads=1, parallel_ingests=1,
                              download_max_seconds=3, download_stall_seconds=60,
                              download_timeout_seconds=30, download_park_seconds=0,
                              early_dedup_mb=0)
    store = harness.open_store(cfg)
    import os
    data_a = os.urandom(1_500_000)   # incompressible: ~5 s at 300 KB/s, past the 3 s cap
    data_b = os.urandom(1_500_000)
    harness.ROUTES["/a.json.gz"] = drip(data_a)
    harness.ROUTES["/b.json.gz"] = drip(data_b)
    httpd, base = harness.start_server()
    from mrfx import fetch
    orig = store.next_queued_url

    def spy(*a, **k):
        r = orig(*a, **k)
        if r:
            CLAIMS.append((round(time.monotonic() - t0, 1), r["id"], r["url"].rsplit("/", 1)[-1]))
        return r
    store.next_queued_url = spy
    fetch.add_urls(store, [f"{base}/a.json.gz", f"{base}/b.json.gz"])
    t0 = time.monotonic()
    stop = threading.Event()
    th = threading.Thread(target=lambda: fetch.run_queue(cfg, store, stop=stop), daemon=True)
    th.start()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        rows = harness.queue_rows(store)
        if all(r["status"] in ("done", "failed", "skipped") for r in rows):
            break
        time.sleep(0.5)
    stop.set(); th.join(5)
    print("claims (t, id, file):", CLAIMS)
    for r in harness.queue_rows(store):
        print(r["id"], r["status"], r["retries"], (r["error"] or "")[:80])
    gets = [(m, p, h.get("Range")) for m, p, h in harness.LOG]
    print("requests:", gets)
    httpd.shutdown()


if __name__ == "__main__":
    main()
