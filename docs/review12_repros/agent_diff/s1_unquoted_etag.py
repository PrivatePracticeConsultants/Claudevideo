"""S1: a server whose ETag is UNQUOTED (non-compliant but seen) and that also
sends Last-Modified, and that cuts every connection after 300 KB. 0.7.4 resumed
across the cuts; does 0.7.5's _same_validator reject every 206 as "the server's
file changed" and restart from 0 each time?"""
import logging
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402

import os as _os
BODY = _os.urandom(1_300_000)   # incompressible 1.3 MB; content is irrelevant to download()
CUT = 300 << 10
ETAG = "5d8c72a5edda8d6a:0"            # IIS-style, unquoted
LM = "Tue, 03 Nov 2026 10:00:00 GMT"
MODE = {"cut_every": True}


def serve(hh, head, entry):
    rng = h.parse_range(hh, len(BODY))
    hdr = {"ETag": ETAG, "Last-Modified": LM, "Accept-Ranges": "bytes"}
    if rng:
        a, b = rng
        hdr["Content-Range"] = f"bytes {a}-{b}/{len(BODY)}"
        h.send(hh, entry, 206, hdr, BODY[a:b + 1], cut=CUT if MODE["cut_every"] else None)
    else:
        h.send(hh, entry, 200, hdr, BODY, cut=CUT)


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    print("mrfx from", h.SRC, "body", len(BODY))
    cfg = h.scratch_cfg("s1_" + Path(h.SRC).name, download_retries=3, download_timeout_seconds=5, download_max_seconds=45,
                        download_park_seconds=0, download_stall_seconds=60)
    h.ROUTES["/f.json.gz"] = serve
    httpd, base = h.start_server()
    from mrfx import fetch
    dest = cfg.downloads_dir / "f.json.gz"
    n = len(h.LOG)
    t = time.monotonic()
    try:
        sha, final = fetch.download(cfg, base + "/f.json.gz", dest)
        print("OK sha", sha[:12], "size", dest.stat().st_size, "truth", len(BODY), "in %.1fs" % (time.monotonic() - t))
    except Exception as e:  # noqa: BLE001
        print("FAILED:", type(e).__name__, str(e)[:300], "in %.1fs" % (time.monotonic() - t))
    reqs = h.requests_since(n)
    print("requests:", len(reqs), "bytes served:", sum(r[3] for r in reqs))
    for r in reqs[:8]:
        print("  ", r)
    if len(reqs) > 8:
        print("   ... last:", reqs[-1])
    print("part left:", [p.name for p in cfg.downloads_dir.iterdir()])
    httpd.shutdown()


if __name__ == "__main__":
    main()
