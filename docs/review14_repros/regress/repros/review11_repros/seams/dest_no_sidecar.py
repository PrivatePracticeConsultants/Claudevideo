"""A kill between `part.replace(dest)` and `_write_meta` leaves a COMPLETE
dest with no .fetchmeta. On restart the row is 'queued': does fetch_url_record
reuse the complete bytes, or download the whole file again?"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness  # noqa: E402


def main():
    root = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/mine/nosidecar")
    cfg = harness.scratch_cfg(root, early_dedup_mb=0)
    store = harness.open_store(cfg)
    data = harness.rate_file_bytes(3000)

    def serve(h, head):
        rng = h.headers.get("Range")
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        h.send_response(206 if rng else 200)
        h.send_header("Content-Length", str(len(data) - start))
        h.send_header("Accept-Ranges", "bytes")
        if rng:
            h.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        h.end_headers()
        if not head:
            h.wfile.write(data[start:])
    harness.ROUTES["/f.json.gz"] = serve
    httpd, base = harness.start_server()
    from mrfx import fetch
    url = f"{base}/f.json.gz"
    fetch.add_urls(store, [url])
    dest = cfg.downloads_dir / fetch.filename_for(url)
    dest.write_bytes(data)             # the complete download, sidecar never written
    rec = store.next_queued_url()
    ok = fetch.fetch_url_record(cfg, store, rec)
    print("fetched:", ok, "requests:", [(m, p, h.get("Range")) for m, p, h in harness.LOG])
    print("bytes served:", sum(len(data) - (int(h["Range"].split("=")[1].split("-")[0]) if h.get("Range") else 0)
                               for m, p, h in harness.LOG if m == "GET"), "of", len(data))
    print(harness.queue_rows(store))
    httpd.shutdown()


if __name__ == "__main__":
    main()
