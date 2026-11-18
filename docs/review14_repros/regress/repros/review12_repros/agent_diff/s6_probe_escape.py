"""S6: the small-lane size probe runs OUTSIDE the downloader's per-row try
(holding[] is set after it). Which exceptions can _server_now let out, and
what happens to the claimed row then? Try the URL shapes add_urls accepts."""
import logging
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    cfg = h.scratch_cfg("s6")
    from mrfx import fetch
    cands = ["http://127.0.0.1:99999/x.json.gz", "http://exa mple.com/x.json.gz",
             "http://[::1/x.json.gz", "http://%zz/x.json.gz", "https://xn--/x.json.gz",
             "http://127.0.0.1:1/x.json.gz"]
    for u in cands:
        norm = fetch._normalize_url(u)
        try:
            r = fetch._server_now(cfg, norm or u)
            print(f"{u!r:45} normalize={norm!r:45} -> {r}")
        except BaseException as e:  # noqa: BLE001
            print(f"{u!r:45} normalize={norm!r:45} -> ESCAPES {type(e).__module__}.{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
