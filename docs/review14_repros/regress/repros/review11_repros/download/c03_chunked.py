"""Case 3 — chunked transfer, no Content-Length (total unknown):
  a) plain: progress callbacks (what total do they carry?), requests, sha
  b) size limit (confirm_over_gb) on an unknown-length body: when does it trip,
     what is kept
  c) lanes: a small-file slot meeting an unsized file -> handoff? (needs lanes:
     3 slots) — count GETs per file
  d) early dedup: a chunked copy of a loaded gz — is the early check skipped?
  e) chunked + early close (incomplete chunked body): retry/resume behaviour
  f) chunked + server that terminates the chunked body EARLY but cleanly
     (0\\r\\n\\r\\n after fewer bytes): truncated file accepted?"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _harness import Srv, doc, gz, mk_cfg, quiet_logs, sha, sha_file, row, rows  # noqa: E402

from mrfx.fetch import DownloadError, add_urls, download, filename_for, run_queue  # noqa: E402


def direct(name, body, rules, behave=None, max_bytes=None, lane_max=0, **cfgkw):
    cfg, store = mk_cfg("c03_" + name, **cfgkw)
    srv = Srv({"/f.json.gz": body}, {"/f.json.gz": rules})
    if behave:
        srv.behave["/f.json.gz"] = behave
    url = srv.base + "/f.json.gz"
    dest = cfg.downloads_dir / "f.json.gz"
    calls = []
    t0 = time.time()
    try:
        got = download(cfg, url, dest, progress_cb=lambda d, t: calls.append((d, t)),
                       max_bytes=max_bytes, lane_max=lane_max)
        out = f"OK sha_match={got[0] == sha(body)}"
    except DownloadError as e:
        out = (f"DownloadError retryable={e.retryable} oversize={e.oversize} handoff={e.handoff} "
               f"total={e.total}: {str(e)[:120]}")
    except Exception as e:  # noqa: BLE001
        out = f"UNCAUGHT {type(e).__name__}: {str(e)[:140]}"
    finally:
        srv.close()
    part = dest.with_suffix(dest.suffix + ".part")
    on_disk = (f"dest {dest.stat().st_size} B sha_match={sha_file(dest) == sha(body)}" if dest.exists()
               else f"part {part.stat().st_size} B" if part.exists() else "nothing")
    print(f"[{name}] {time.time() - t0:5.1f}s truth={len(body)} B | {out}")
    print(f"    requests: {srv.describe('/f.json.gz')} | progress calls: {len(calls)} "
          f"first={calls[:1]} last={calls[-1:]}")
    print(f"    on disk: {on_disk}")


if __name__ == "__main__":
    quiet_logs()
    body = gz(doc(n=300, pad_mb=3.0))   # ~2.3 MB
    direct("plain", body, {"chunked": True})
    direct("size_limit", body, {"chunked": True}, max_bytes=1 << 20)
    direct("lane_small_slot", body, {"chunked": True}, lane_max=100 << 20)
    direct("early_close", body, {"chunked": True, "cut_at": [800 << 10]})

    # clean early termination: the server ends the chunked body after 800 KB
    def clean_short(h, body_, rng, nth):
        h.send_response(200)
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        piece = body_[:800 << 10]
        h.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n0\r\n\r\n")
        h.wfile.flush()
        h._rec("/f.json.gz", "GET", rng, 200, len(piece))
        return True
    direct("clean_short", body, {"chunked": True}, behave=clean_short)

    # early dedup against a loaded twin, served chunked
    cfg, store = mk_cfg("c03_early_dedup", early_dedup_mb=1)
    srv = Srv({"/a/f.json.gz": body, "/b/f.json.gz": body},
              {"/a/f.json.gz": {}, "/b/f.json.gz": {"chunked": True}})
    ua, ub = srv.base + "/a/f.json.gz", srv.base + "/b/f.json.gz"
    try:
        add_urls(store, [ua]); run_queue(cfg, store, drain=True)
        add_urls(store, [ub]); run_queue(cfg, store, drain=True)
        ra, rb = row(store, ua), row(store, ub)
        print(f"[early_dedup chunked twin] A {ra['status']} | B {rb['status']}/{rb['kind']} "
              f"early={'same first' in (rb['error'] or '')} err={(rb['error'] or '')[:90]!r}")
        print(f"    B requests: {srv.describe('/b/f.json.gz')}  bytes sent {srv.sent_bytes('/b/f.json.gz')} of {len(body)}")
    finally:
        srv.close()

    # stall/park on a chunked body that goes silent after 200 KB
    cfg, store = mk_cfg("c03_park", download_park_seconds=4.0, download_stall_seconds=30.0,
                        download_timeout_seconds=3.0, download_park_minutes=0.05, download_park_limit=2,
                        download_pause_hours=0.0)
    import threading
    gate = threading.Event()
    def silent(h, body_, rng, nth):
        h.send_response(200)
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        piece = body_[:200 << 10]
        h.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
        h.wfile.flush()
        h._rec("/f.json.gz", "GET", rng, 200, len(piece))
        gate.wait(60)       # silence
        return True
    srv = Srv({"/f.json.gz": body}, {"/f.json.gz": {}})
    srv.behave["/f.json.gz"] = silent
    url = srv.base + "/f.json.gz"
    t0 = time.time()
    try:
        add_urls(store, [url])
        run_queue(cfg, store, drain=True)
    finally:
        gate.set(); srv.close()
    r = row(store, url)
    print(f"[chunked silent -> park] {time.time() - t0:.1f}s row status={r['status']} park_count={r.get('park_count')} "
          f"err={(r['error'] or '')[:160]!r}")
    print(f"    requests: {len(srv.hits('/f.json.gz'))}")
