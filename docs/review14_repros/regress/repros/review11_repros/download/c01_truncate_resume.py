"""Case 1 — body truncated with a correct Content-Length, then the resume.
Variants:
  honoured      server closes at 300 KB once, honours Range on the resume
  no_accept_ranges  same, but never sends Accept-Ranges
  ignore_small  server ignores Range (200 from 0) on a small .part
  ignore_big    server ignores Range on a .part >= 64 MB (the keep-200 path)
  cr_start_wrong  206 whose Content-Range says the right total but the body
                starts at byte 0 (server honours total, not offset)
  cr_absent_short  206 with NO Content-Range and a body shorter than the rest
Each prints: requests made (status, Range, bytes sent), bytes on disk, sha vs
truth, and the download() outcome."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _harness import Srv, doc, gz, mk_cfg, quiet_logs, sha, sha_file  # noqa: E402

from mrfx.fetch import DownloadError, download  # noqa: E402


def run(name, body, rules, behave=None, **cfgkw):
    cfg, store = mk_cfg("c01_" + name, **cfgkw)
    srv = Srv({"/f.json.gz": body}, {"/f.json.gz": rules})
    if behave:
        srv.behave["/f.json.gz"] = behave
    url = srv.base + "/f.json.gz"
    dest = cfg.downloads_dir / "f.json.gz"
    t0 = time.time()
    out = None
    try:
        got = download(cfg, url, dest)
        out = f"OK sha_match={got[0] == sha(body)}"
    except DownloadError as e:
        out = f"DownloadError retryable={e.retryable} oversize={e.oversize}: {str(e)[:140]}"
    except Exception as e:  # noqa: BLE001
        out = f"UNCAUGHT {type(e).__name__}: {str(e)[:140]}"
    finally:
        srv.close()
    part = dest.with_suffix(dest.suffix + ".part")
    on_disk = (f"dest {dest.stat().st_size} B sha_match={sha_file(dest) == sha(body)}"
               if dest.exists() else f"part {part.stat().st_size} B" if part.exists() else "nothing")
    print(f"[{name}] {time.time() - t0:5.1f}s truth={len(body)} B | {out}")
    print(f"    requests: {srv.describe('/f.json.gz')}  total sent {srv.sent_bytes('/f.json.gz')} B")
    print(f"    on disk: {on_disk}")
    return srv, dest


if __name__ == "__main__":
    quiet_logs()
    small = gz(doc(n=300, pad_mb=1.2))          # ~1.3 MB
    cut = 300 << 10

    run("honoured", small, {"cut_at": [cut], "etag": '"v1"'})
    run("no_accept_ranges", small, {"cut_at": [cut], "accept_ranges": False})
    run("ignore_small", small, {"cut_at": [cut], "ranges": False})

    # the keep-200 path needs a .part >= 64 MB: 70 MB body cut at 66 MB, Range ignored
    big = bytes(bytearray(70 << 20))  # zeros, incompressible-enough? no: just bytes
    import os
    big = os.urandom(1 << 20) * 70
    run("ignore_big", big, {"cut_at": [66 << 20], "ranges": False})

    # 206 whose body starts at 0 although the client asked for a resume offset
    def cr_start_wrong(h, body, rng, nth):
        if not rng:
            return False
        a = int(rng[6:].split("-")[0])
        h.send_response(206)
        h.send_header("Content-Range", f"bytes {a}-{len(body) - 1}/{len(body)}")
        h.send_header("Content-Length", str(len(body)))     # sends ALL bytes from 0
        h.end_headers()
        h.wfile.write(body)
        srv_log = h.server  # noqa: F841
        return True
    srv, dest = run("cr_start_wrong", small, {"cut_at": [cut]}, behave=cr_start_wrong)

    # 206 with no Content-Range and a short body (consistent Content-Length)
    def cr_absent_short(h, body, rng, nth):
        if not rng:
            return False
        a = int(rng[6:].split("-")[0])
        piece = body[a:a + (100 << 10)]                     # only 100 KB of the rest
        h.send_response(206)
        h.send_header("Content-Length", str(len(piece)))
        h.end_headers()
        h.wfile.write(piece)
        return True
    run("cr_absent_short", small, {"cut_at": [cut]}, behave=cr_absent_short)
