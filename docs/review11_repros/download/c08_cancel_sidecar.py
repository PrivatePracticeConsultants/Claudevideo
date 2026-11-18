"""Case 8 — cancel at every stage and the .part / .val / .fetchmeta sidecars:
  cancel_before_first_byte, cancel_mid_stream, cancel_during_backoff (slot
  held how long?), queue: cancel_all_downloading mid-stream then
  resume_canceled (bytes re-downloaded), stale sidecar with WRONG sha
  (trusted? what does the store record? is a true copy later deduped?),
  stale sidecar + short dest, sidecar orphan after a failed row."""
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _harness import Srv, doc, gz, mk_cfg, quiet_logs, sha, sha_file, row, q, dl_files, brief  # noqa: E402

from mrfx.fetch import DownloadError, add_urls, download, filename_for, run_queue, _meta_path  # noqa: E402


def files_of(cfg):
    return [(p.name, p.stat().st_size) for p in sorted(cfg.downloads_dir.iterdir())]


def direct(name, body, rules, cancel_at, **over):
    cfg, store = mk_cfg("c08_" + name, **over)
    srv = Srv({"/f.json.gz": body}, {"/f.json.gz": rules})
    url = srv.base + "/f.json.gz"
    dest = cfg.downloads_dir / "f.json.gz"
    flag = {"c": False}
    threading.Timer(cancel_at, lambda: flag.__setitem__("c", True)).start()
    t0 = time.time()
    try:
        download(cfg, url, dest, cancel_check=lambda: flag["c"])
        out = "OK (finished before the cancel)"
    except DownloadError as e:
        out = f"DownloadError canceled={e.canceled} retryable={e.retryable}: {str(e)[:80]}"
    finally:
        held = time.time() - t0
        srv.close()
    print(f"[{name}] cancel at {cancel_at}s, slot released after {held:.1f}s | {out}")
    print(f"    requests: {srv.describe('/f.json.gz')} | disk: {files_of(cfg)}")


if __name__ == "__main__":
    quiet_logs(level=60)
    body = gz(doc(n=300, pad_mb=2.0))
    direct("cancel_before_first_byte", body, {"delay": 0.5}, cancel_at=0.0,
           download_park_seconds=0.0)
    direct("cancel_mid_stream", body, {"delay": 0.2, "etag": '"v1"'}, cancel_at=1.0,
           download_park_seconds=0.0)
    # zero-progress cuts -> backoff sleeps 2, 4 (+jitter); cancel at 0.5 s
    direct("cancel_during_backoff", body, {"cut_at": [0] * 20}, cancel_at=0.5,
           download_park_seconds=0.0, download_retries=3, download_stall_seconds=60.0)
    # same, with park on (production shape: park 240 caps the sleep) at park=30
    direct("cancel_during_backoff_park30", body, {"cut_at": [0] * 20}, cancel_at=0.5,
           download_park_seconds=30.0, download_retries=4, download_stall_seconds=60.0)

    # ---- queue: stop downloads mid-stream, then resume ----
    cfg, store = mk_cfg("c08_queue_stop", download_park_seconds=0.0)
    srv = Srv({"/f.json.gz": body}, {"/f.json.gz": {"delay": 0.05, "etag": '"v1"'}})   # ~1.3 MB/s
    url = srv.base + "/f.json.gz"
    add_urls(store, [url])
    stop = threading.Event()
    def stopper():
        time.sleep(0.8)
        n = store.cancel_all_downloading()
        print(f"    cancel_all_downloading -> {n} row(s) signalled at t=0.8s")
    threading.Thread(target=stopper).start()
    t0 = time.time()
    run_queue(cfg, store, drain=True)
    r = row(store, url)
    dest = cfg.downloads_dir / filename_for(url)
    print(f"[queue_stop] drain returned after {time.time() - t0:.1f}s | {brief(r)}")
    print(f"    disk after stop: {files_of(cfg)} | requests {srv.describe('/f.json.gz')}")
    n = store.resume_canceled()
    srv.rules["/f.json.gz"]["delay"] = 0.0
    run_queue(cfg, store, drain=True)
    r = row(store, url)
    print(f"    resume_canceled -> {n}; after 2nd drain: {brief(r)}")
    print(f"    requests: {srv.describe('/f.json.gz')} | bytes sent {srv.sent_bytes('/f.json.gz')} of {len(body)}")
    srv.close()

    # ---- stale sidecar with the WRONG sha, dest complete and correct ----
    cfg, store = mk_cfg("c08_wrong_sidecar")
    srv = Srv({"/a/f.json.gz": body, "/b/f.json.gz": body}, {})
    ua, ub = srv.base + "/a/f.json.gz", srv.base + "/b/f.json.gz"
    dest = cfg.downloads_dir / filename_for(ua)
    dest.write_bytes(body)
    _meta_path(dest).write_text(json.dumps({"sha": "0" * 64, "final_url": ua, "trailer_proves": 1}))
    add_urls(store, [ua]); run_queue(cfg, store, drain=True)
    ra = row(store, ua)
    print(f"[wrong_sidecar] A: {brief(ra)} | requests to A: {len(srv.hits('/a/f.json.gz'))} (0 = sidecar trusted)")
    print(f"    sha recorded == truth? {ra['content_sha'] == sha(body)} | content_fingerprints: "
          f"{q(store, 'SELECT filename, full_sha FROM content_fingerprints')}")
    add_urls(store, [ub]); run_queue(cfg, store, drain=True)
    rb = row(store, ub)
    print(f"    B (true copy of A's bytes): {brief(rb)}")
    print(f"    rates rows: {q(store, 'SELECT count(*) FROM rates')[0][0]} (300 expected if deduped, 600 if ingested twice)")
    srv.close()

    # ---- stale sidecar + SHORT dest ----
    cfg, store = mk_cfg("c08_short_dest")
    srv = Srv({"/f.json.gz": body}, {})
    url = srv.base + "/f.json.gz"
    dest = cfg.downloads_dir / filename_for(url)
    dest.write_bytes(body[: len(body) // 2])
    _meta_path(dest).write_text(json.dumps({"sha": sha(body), "final_url": url, "trailer_proves": 1}))
    add_urls(store, [url]); run_queue(cfg, store, drain=True)
    r = row(store, url)
    print(f"[short_dest] {brief(r)} | requests {len(srv.hits('/f.json.gz'))} | disk {files_of(cfg)}")
    # and the retry after that failure
    store.requeue_failed(); run_queue(cfg, store, drain=True)
    r = row(store, url)
    print(f"    after retry: {brief(r)} | requests {len(srv.hits('/f.json.gz'))} | disk {files_of(cfg)}")
    srv.close()
