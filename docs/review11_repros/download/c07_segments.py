"""Case 7 — segmented download (download_segments=4) on a 210 MB body:
  normal          boundaries requested, assembled sha, requests, bytes vs size
  seg_fails       segment 3 answers 500 forever -> fallback cost (bytes wasted)
  seg_200         segment 2 answers 200 to its Range (probe was fine)
  start_only      server honours only the START of a range (bytes=a-b -> a..EOF)
  holey_restart   a kill left a full-size holey .part: what does the resume do
  reserve_race    _reserve_disk_or_raise under 2 simultaneous reservations
                  (disk_usage monkeypatched in THIS process only)"""
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _harness import Srv, mk_cfg, quiet_logs, sha, sha_file  # noqa: E402

import mrfx.fetch as F  # noqa: E402
from mrfx.fetch import DownloadError, download  # noqa: E402

BODY = os.urandom(1 << 20) * 210     # 210 MB (>= _SEGMENT_MIN_BYTES)


def run(name, body, rules=None, behave=None, pre=None, **over):
    kw = dict(download_segments=4, download_stall_seconds=20.0, download_timeout_seconds=5.0,
              download_retries=1)
    kw.update(over)
    cfg, store = mk_cfg("c07_" + name, **kw)
    srv = Srv({"/f.bin": body}, {"/f.bin": rules or {}})
    if behave:
        srv.behave["/f.bin"] = behave
    url = srv.base + "/f.bin"
    dest = cfg.downloads_dir / "f.bin"
    if pre:
        pre(dest)
    t0 = time.time()
    try:
        got = download(cfg, url, dest)
        out = f"OK sha_match={got[0] == sha(body)}"
    except DownloadError as e:
        out = f"DownloadError retryable={e.retryable}: {str(e)[:120]}"
    except Exception as e:  # noqa: BLE001
        out = f"UNCAUGHT {type(e).__name__}: {str(e)[:140]}"
    finally:
        held = time.time() - t0
        srv.close()
    part = dest.with_suffix(dest.suffix + ".part")
    on_disk = (f"dest {dest.stat().st_size} sha_match={sha_file(dest) == sha(body)}" if dest.exists()
               else f"part {part.stat().st_size}" if part.exists() else "nothing")
    hits = srv.hits("/f.bin")
    print(f"[{name}] {held:5.1f}s | {out}")
    print(f"    requests {len(hits)}: {[(x['status'], x['range'] or '-', x['sent']) for x in hits][:10]}")
    print(f"    bytes sent {srv.sent_bytes('/f.bin') / 1e6:.1f} MB for a {len(body) / 1e6:.1f} MB file | disk: {on_disk}")
    import shutil as _sh
    _sh.rmtree(cfg.downloads_dir, ignore_errors=True)     # 220 MB per run: keep the disk free
    return srv, dest


def seg_index(rng, total, n=4):
    """which segment a Range header belongs to"""
    a = int(rng[6:].split("-")[0])
    return min(a // (total // n), n - 1)


if __name__ == "__main__":
    quiet_logs(level=60)
    only = set(sys.argv[1:])
    def want(n):
        return not only or n in only
    if want("normal"):
        run("normal", BODY)

    def seg3_500(h, body, rng, nth):
        if rng and rng != "bytes=0-0" and seg_index(rng, len(body)) == 2:
            h.send_response(500); h.send_header("Content-Length", "0"); h.end_headers()
            h._rec("/f.bin", "GET", rng, 500); return True
        return False
    if want("seg_fails"):
        run("seg_fails", BODY, behave=seg3_500)

    def seg2_200(h, body, rng, nth):
        if rng and rng != "bytes=0-0" and seg_index(rng, len(body)) == 1:
            h.send_response(200); h.send_header("Content-Length", str(len(body))); h.end_headers()
            h._rec("/f.bin", "GET", rng, 200, 0)
            try:
                h.wfile.write(body)
            except OSError:
                pass
            return True
        return False
    if want("seg_200"):
        run("seg_200", BODY, behave=seg2_200)

    def start_only(h, body, rng, nth):
        if rng and rng != "bytes=0-0":
            a = int(rng[6:].split("-")[0])
            piece = body[a:]                      # ignores the END of the range
            h.send_response(206)
            h.send_header("Content-Range", f"bytes {a}-{len(body) - 1}/{len(body)}")
            h.send_header("Content-Length", str(len(piece))); h.end_headers()
            rec = h._rec("/f.bin", "GET", rng, 206, 0)
            try:
                h.wfile.write(piece); rec["sent"] = len(piece)
            except OSError:
                pass
            return True
        return False
    if want("start_only"):
        run("start_only", BODY, behave=start_only)

    def holey(dest):
        part = dest.with_suffix(dest.suffix + ".part")
        with open(part, "wb") as f:
            f.truncate(len(BODY))          # size == total, content all zero (a kill mid-segments)
    if want("holey_restart_416"):
        run("holey_restart_416", BODY, pre=holey)

    def holey_rangeless(h, body, rng, nth):
        # a server that answers an out-of-range resume with 200 (ignores Range)
        if rng and rng != "bytes=0-0" and int(rng[6:].split("-")[0]) >= len(body):
            h.send_response(200); h.send_header("Content-Length", str(len(body))); h.end_headers()
            rec = h._rec("/f.bin", "GET", rng, 200, 0)
            try:
                h.wfile.write(body); rec["sent"] = len(body)
            except OSError:
                pass
            return True
        return False
    if want("holey_restart_200"):
        run("holey_restart_200", BODY, behave=holey_rangeless, pre=holey, download_retries=4)

    # ---- disk reservation race (no network) ----
    if not want("reserve_race"):
        sys.exit(0)
    import shutil
    real_du = F.shutil.disk_usage
    class DU:  # 5 GB free
        free = 5 << 30
    F.shutil.disk_usage = lambda p: DU
    F._disk_reservations.clear()
    results = []
    def reserve(k):
        try:
            F._reserve_disk_or_raise(k, Path("/tmp"), 2 << 30, 0)   # needs 2 GB + 2 GB headroom
            results.append((k, "ok"))
        except DownloadError as e:
            results.append((k, "refused"))
    oks = 0
    for trial in range(200):
        F._disk_reservations.clear(); results.clear()
        ts = [threading.Thread(target=reserve, args=(k,)) for k in (1, 2)]
        [t.start() for t in ts]; [t.join() for t in ts]
        oks += sum(1 for _, v in results if v == "ok")
    print(f"[reserve_race] 200 trials x 2 simultaneous 2 GB reservations on 5 GB free: "
          f"{oks} accepted in total (expect exactly 200 = one per trial)")
    F.shutil.disk_usage = real_du
    F._disk_reservations.clear()
