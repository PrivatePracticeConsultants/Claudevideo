"""S3: the 1-byte size probe (_server_now / _probe_ranges, Range: bytes=0-0)
against (a) a server that IGNORES Range and answers 200 with the whole 24 MB
file, (b) a chunked 200, (c) a 416, (d) 200 + Content-Encoding: gzip. How many
body bytes does the client actually pull before it closes? What size is
reported?"""
import logging
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402

BODY = os.urandom(24 << 20)


def ignores_range(hh, head, entry):
    h.send(hh, entry, 200, {"Accept-Ranges": "none"}, BODY, pace=0.002)


def chunked(hh, head, entry):
    h.send(hh, entry, 200, {"Transfer-Encoding": "chunked"}, BODY, pace=0.002)


def r416(hh, head, entry):
    h.send(hh, entry, 416, {"Content-Range": f"bytes */{len(BODY)}"}, b"")


def enc_gzip(hh, head, entry):
    h.send(hh, entry, 200, {"Content-Encoding": "gzip", "Content-Length": str(len(BODY))}, BODY, pace=0.002)


def correct(hh, head, entry):
    rng = h.parse_range(hh, len(BODY))
    a, b = rng
    h.send(hh, entry, 206, {"Content-Range": f"bytes {a}-{b}/{len(BODY)}", "ETag": '"v1"'}, BODY[a:b + 1])


def main():
    logging.basicConfig(level=logging.WARNING)
    cfg = h.scratch_cfg("s3", download_timeout_seconds=10)
    h.ROUTES.update({"/ignores": ignores_range, "/chunked": chunked, "/r416": r416,
                     "/gz": enc_gzip, "/ok": correct})
    httpd, base = h.start_server()
    from mrfx import fetch
    for path in ("/ok", "/ignores", "/chunked", "/r416", "/gz"):
        for fn in (lambda u: fetch._server_now(cfg, u), lambda u: fetch._probe_ranges(cfg, u, None)):
            n = len(h.LOG)
            t = time.monotonic()
            r = fn(base + path)
            dt = time.monotonic() - t
            time.sleep(0.5)      # let the server thread notice the close
            reqs = h.requests_since(n)
            print(f"{path:9s} {fn.__code__.co_consts!s:0.0s}{'server_now  ' if 'server_now' in str(fn.__code__.co_names) else 'probe_ranges'} -> {r!s:60.60s} {dt:5.2f}s server wrote {reqs[0][3]:>10,} B of {len(BODY):,}")
    httpd.shutdown()


if __name__ == "__main__":
    main()
