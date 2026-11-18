"""M12: a server that sends the gz file with `Content-Encoding: deflate` and a
Content-Length of the ENCODED body. _body_chunks decodes deflate (iter_bytes),
so `done` counts decoded bytes while `total` is encoded: what happens?"""
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness  # noqa: E402


def main():
    root = Path("/tmp/mrfx_review11/mine/encoding")
    cfg = harness.scratch_cfg(root, download_retries=1, download_stall_seconds=20,
                              download_timeout_seconds=10, download_park_seconds=0, early_dedup_mb=0)
    raw = harness.rate_file_bytes(300)
    enc = zlib.compress(raw)   # deflate (zlib-wrapped), as servers send it

    def deflated(h, head):
        h.send_response(200)
        h.send_header("Content-Length", str(len(enc)))
        h.send_header("Content-Encoding", "deflate")
        h.end_headers()
        if not head:
            h.wfile.write(enc)
    harness.ROUTES["/x.json.gz"] = deflated
    httpd, base = harness.start_server()
    from mrfx.fetch import DownloadError, download
    dest = cfg.downloads_dir / "x.json.gz"
    try:
        sha, final = download(cfg, f"{base}/x.json.gz", dest)
        print("OK", dest.stat().st_size, "vs raw", len(raw), "enc", len(enc), "identical:", dest.read_bytes() == raw)
    except DownloadError as e:
        part = dest.with_suffix(dest.suffix + ".part")
        print("DownloadError:", str(e)[:200])
        print("part exists:", part.exists(), part.stat().st_size if part.exists() else None,
              "raw", len(raw), "enc", len(enc))
    print("requests:", [(m, p, h.get("Range"), h.get("Accept-Encoding")) for m, p, h in harness.LOG])
    httpd.shutdown()


if __name__ == "__main__":
    main()
