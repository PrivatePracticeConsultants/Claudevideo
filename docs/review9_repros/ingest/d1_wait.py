"""Waiting copies (gz, so the early check is live). Scenarios by argv."""
import sys, threading, time
from dl import *
import mrfx.fetch as F
import mrfx.ingest as I


def body():
    return gz(doc(n=300, pad_mb=3.5))     # ~2.7 MB gz, incompressible pad


def start(cfg, store):
    stop = threading.Event()
    t = threading.Thread(target=F.run_queue, args=(cfg, store, stop), daemon=True)
    t.start()
    return stop, t


def wait_for(pred, secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        if pred():
            return time.time() - t0
        time.sleep(0.1)
    return None


def st(store, u):
    r = row(store, u)
    return f"{r['status']}/{r['kind']} waiting_on={r['waiting_on']} err={(r['error'] or '')[:70]!r}"


def scenario(name):
    cfg, store = mk_cfg("d1_" + name)
    cfg.early_dedup_mb = 1
    b = body()
    srv = Srv({"/a/r.json.gz": b, "/b/r.json.gz": b, "/c/r.json.gz": b})
    ua, ub, uc = (srv.base + f"/{x}/r.json.gz" for x in "abc")
    real = F.process_url_record
    HOLD = 12.0
    fail_a = name == "twin_fails"

    def slow(cfg_, store_, rec, *a, **k):
        if rec["url"] == ua:
            time.sleep(HOLD)
            if fail_a:
                orig = I._ingest_file_locked
                I._ingest_file_locked = lambda *aa, **kk: (_ for _ in ()).throw(RuntimeError("disk error"))
                try:
                    return real(cfg_, store_, rec, *a, **k)
                finally:
                    I._ingest_file_locked = orig
        return real(cfg_, store_, rec, *a, **k)
    F.process_url_record = slow
    try:
        if name == "twin_slow_download":
            srv.delay["/a/r.json.gz"] = 0.25      # A takes ~10 s to download
        F.add_urls(store, [ua])
        stop, t = start(cfg, store)
        w = wait_for(lambda: q(store, "SELECT count(*) FROM inflight_fingerprints")[0][0] >= 1, 20)
        print(f"[{name}] A's in-flight fingerprint recorded after {w and round(w,1)} s; A: {st(store, ua)}")
        F.add_urls(store, [ub] + ([uc] if name == "three" else []))
        w = wait_for(lambda: row(store, ub)["status"] == "paused", 20)
        print(f"[{name}] B waiting after {w and round(w,1)} s: {st(store, ub)}")
        print("   counts:", store.url_queue_counts())
        t_wait = time.time()
        if name == "clear_resume":
            print("   clear_queued ->", store.clear_queued(), "| B:", st(store, ub))
            time.sleep(1)
            print("   resume_canceled ->", store.resume_canceled(), "| B:", st(store, ub))
        if name == "retry_all":
            print("   requeue_failed ->", store.requeue_failed())
            time.sleep(2); print("   B 2 s later:", st(store, ub))
        if name == "twin_slow_download":
            time.sleep(1)
            print("   cancel_all_downloading ->", store.cancel_all_downloading(), "| A:", st(store, ua))
        if name == "stop_restart":
            time.sleep(1)
            stop.set(); t.join(60)
            print("   stopped; A:", st(store, ua), "| B:", st(store, ub))
            store2 = store
            n = store2.recover_stuck_urls()
            print("   recover_stuck_urls ->", n, "| A:", st(store, ua), "| B:", st(store, ub))
            F.process_url_record = real
            stop, t = start(cfg, store)
        term = ("done", "skipped", "failed")
        urls = [ua, ub] + ([uc] if name == "three" else [])
        w = wait_for(lambda: all(row(store, u)["status"] in term for u in urls), 150)
        print(f"   all terminal after {w and round(w,1)} s (A's ingest is held {HOLD:.0f} s)")
        for u in urls:
            print("   ", u[-13:], st(store, u))
        stop.set(); t.join(60)
        for p in ("/a/r.json.gz", "/b/r.json.gz", "/c/r.json.gz"):
            h = srv.hits(p)
            if h:
                print(f"    {p}: {len(h)} GET(s) {[(r, s) for _, _, r, s, _ in h][:12]}")
        print("   rates rows:", q(store, "SELECT count(*), count(DISTINCT source_file) FROM rates")[0],
              "| inflight fingerprints left:", q(store, "SELECT count(*) FROM inflight_fingerprints")[0][0],
              "| counts:", store.url_queue_counts())
    finally:
        F.process_url_record = real
        srv.close()


if __name__ == "__main__":
    for n in sys.argv[1:]:
        scenario(n)
