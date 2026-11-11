"""S8: `splices` (resume offsets the server did not vouch for) is not cleared
when the download restarts from byte 0 (the 206-mismatch / over-length
restart paths). Does the finished fresh download still pay the splice sample
requests for offsets that belong to the discarded attempt?"""
import logging
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402

BODY = os.urandom(900_000)
CUT = 300 << 10
STATE = {"n": 0}


def serve(hh, head, entry):
    STATE["n"] += 1
    rng = h.parse_range(hh, len(BODY))
    if rng is None:
        # first full request: cut after 300 KB; later full requests complete
        h.send(hh, entry, 200, {}, BODY, cut=CUT if STATE["n"] == 1 else None)
        return
    a, b = rng
    if b - a + 1 <= 65536:
        # the splice samples: exact
        h.send(hh, entry, 206, {"Content-Range": f"bytes {a}-{b}/{len(BODY)}"}, BODY[a:b + 1])
        return
    # the resume: a wrong 206 — body from byte 0 under the asked header (review 1.4 shape)
    h.send(hh, entry, 206, {"Content-Range": f"bytes {a}-{b}/{len(BODY)}"}, BODY)


def main():
    logging.basicConfig(level=logging.WARNING)
    cfg = h.scratch_cfg("s8", download_retries=4, download_park_seconds=0, download_timeout_seconds=5)
    h.ROUTES["/f.bin"] = serve
    httpd, base = h.start_server()
    from mrfx import fetch
    dest = cfg.downloads_dir / "f.bin"
    try:
        sha, _ = fetch.download(cfg, base + "/f.bin", dest)
        print("OK", dest.stat().st_size == len(BODY))
    except Exception as e:  # noqa: BLE001
        print("FAILED", e)
    for r in h.requests_since(0):
        print("  ", r[0], r[2], "->", r[4], r[3])
    httpd.shutdown()


if __name__ == "__main__":
    main()
