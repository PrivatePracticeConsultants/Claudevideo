"""Case 6 — the four clocks, with small values so each case runs in seconds:
  download_timeout_seconds=3 (silence between bytes), download_park_seconds=6,
  download_stall_seconds=12, download_max_seconds=20, download_retries=2.
For each scenario: wall-clock the slot was held, requests made, verdict, and
whether a clock fired when it should NOT have (steady progress)."""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _harness import Srv, mk_cfg, quiet_logs, sha, sha_file  # noqa: E402

from mrfx.fetch import DownloadError, download  # noqa: E402

import os
BODY = os.urandom(1 << 20) * 3        # 3 MB
BIG = os.urandom(1 << 20) * 8         # 8 MB

CLOCKS = dict(download_timeout_seconds=3.0, download_park_seconds=6.0,
              download_stall_seconds=12.0, download_max_seconds=20.0, download_retries=2)


def run(name, body, rules=None, behave=None, cancel_at=None, **over):
    kw = dict(CLOCKS); kw.update(over)
    cfg, store = mk_cfg("c06_" + name, **kw)
    srv = Srv({"/f.bin": body}, {"/f.bin": rules or {}})
    if behave:
        srv.behave["/f.bin"] = behave
    url = srv.base + "/f.bin"
    dest = cfg.downloads_dir / "f.bin"
    t0 = time.time()
    flag = {"cancel": False}
    if cancel_at is not None:
        threading.Timer(cancel_at, lambda: flag.__setitem__("cancel", True)).start()
    try:
        got = download(cfg, url, dest, cancel_check=lambda: flag["cancel"])
        out = f"OK sha_match={got[0] == sha(body)}"
    except DownloadError as e:
        out = (f"DownloadError retryable={e.retryable} parked={e.parked} set_aside={e.set_aside} "
               f"canceled={e.canceled} progressed={e.progressed}: {str(e)[:110]}")
    except Exception as e:  # noqa: BLE001
        out = f"UNCAUGHT {type(e).__name__}: {str(e)[:140]}"
    finally:
        held = time.time() - t0
        srv.close()
    part = dest.with_suffix(dest.suffix + ".part")
    on_disk = (f"dest {dest.stat().st_size}" if dest.exists() else f"part {part.stat().st_size}"
               if part.exists() else "nothing")
    print(f"[{name}] slot held {held:5.1f}s | {out}")
    print(f"    requests {len(srv.hits('/f.bin'))}: {srv.describe('/f.bin')[:6]} | disk: {on_disk}")


def silent_after(nbytes, forever=True, resume_ok=True):
    gate = threading.Event()

    def b(h, body, rng, nth):
        if rng and resume_ok:
            return False                       # honour the resume normally
        h.send_response(200)
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Accept-Ranges", "bytes")
        h.end_headers()
        h.wfile.write(body[:nbytes]); h.wfile.flush()
        h._rec("/f.bin", "GET", rng, 200, nbytes)
        gate.wait(5 if not forever else 90)
        if not forever:
            try:
                h.wfile.write(body[nbytes:]); h.wfile.flush()
            except OSError:
                pass
        return True
    b.gate = gate
    return b


if __name__ == "__main__":
    quiet_logs(level=60)
    # 1. silence > timeout, then a resume that works: timeout fires at ~3 s, nothing lost
    run("silence_then_resume", BODY, behave=silent_after(300 << 10))
    # 2. steady 250 KB/s (>= 1 MB per park window): no clock may fire
    run("steady_250KBs_no_clock", BODY, {"delay": 0.25})           # 64 KB / 0.25 s = 256 KB/s, 3 MB -> ~12 s
    # 3. steady but < 1 MB per park window (6 s): 100 KB/s
    run("steady_100KBs_parks", BODY, {"delay": 0.64})              # 100 KB/s
    # 4. silent forever after headers + 200 KB: park at 6 s?
    run("silent_forever_park", BODY, behave=silent_after(200 << 10, resume_ok=False))
    # 5. same with park off: the stall deadline at 12 s
    run("silent_forever_stall", BODY, behave=silent_after(200 << 10, resume_ok=False),
        download_park_seconds=0.0)
    # 6. hard cap: steady 256 KB/s on 8 MB (32 s) with max 20 s -> set aside with .part kept
    run("max_cap_set_aside", BIG, {"delay": 0.25}, download_park_seconds=0.0, download_stall_seconds=0.0)
    # 7. retries reset: server cuts every 512 KB (progress every time), retries=2, 3 MB
    run("cut_every_512K_resets", BODY, {"cut_at": [512 << 10] * 5 + [None]})
    # 8. zero-progress cuts: headers then close with 0 bytes, every time
    run("zero_progress_exhausts", BODY, {"cut_at": [0] * 20})
    # 9. cancel during a backoff sleep: zero-progress cuts, cancel at t=1.0 s
    run("cancel_during_backoff", BODY, {"cut_at": [0] * 20}, cancel_at=1.0, download_park_seconds=0.0)
    # 10. headers never arrive (connect ok, server silent before headers)
    def no_headers(h, body, rng, nth):
        h._rec("/f.bin", "GET", rng, 0, 0)
        time.sleep(30)
        return True
    run("no_headers", BODY, behave=no_headers)
