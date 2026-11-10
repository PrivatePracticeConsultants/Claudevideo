"""kill -9 the queue process while (1) a copy WAITS on a twin that is mid-ingest,
(2) a third file is mid-download; then restart with a drain. usage: d5_kill.py [child <port>]"""
import os, signal, subprocess, sys, threading, time
from dl import *
import mrfx.fetch as F
import mrfx.ingest as I

TAG = "d5"

def child(base):
    cfg, store = mk_cfg(TAG, fresh=False)
    cfg.early_dedup_mb = 1
    ua = base + "/a/r.json.gz"
    real = F.process_url_record
    def slow(cfg_, store_, rec, *a, **k):
        if rec["url"] == ua and not os.path.exists(str(HERE / "runs" / TAG / "second_run")):
            # hold INSIDE the ingest: after the part is written, before 'done'
            orig = I._commit_done
            def held(*aa, **kk):
                open(HERE / "runs" / TAG / "a_part_written", "w").close()
                time.sleep(600)
            I._commit_done = held
        return real(cfg_, store_, rec, *a, **k)
    F.process_url_record = slow
    store.recover_stuck_urls(); store.recover_stuck_files()
    F.run_queue(cfg, store, drain=True)
    print("CHILD drain finished", flush=True)

if __name__ == "__main__":
    if sys.argv[1:2] == ["child"]:
        child(sys.argv[2]); sys.exit()
    cfg, store = mk_cfg(TAG)
    b = gz(doc(n=300, pad_mb=3.5))
    other = gz(doc(n=200, pad_mb=3.5, seed=9, payer="Other Plan"))
    srv = Srv({"/a/r.json.gz": b, "/b/r.json.gz": b, "/c/big.json.gz": other})
    srv.delay["/c/big.json.gz"] = 0.5          # C stays mid-download
    ua, ub, uc = srv.base + "/a/r.json.gz", srv.base + "/b/r.json.gz", srv.base + "/c/big.json.gz"
    F.add_urls(store, [ua, uc]); store.close()
    env = dict(os.environ, PYTHONPATH="/home/user/Claudevideo")
    p = subprocess.Popen([sys.executable, __file__, "child", srv.base], env=env,
                         stdout=open(HERE / "logs" / "d5_child1.log", "w"), stderr=subprocess.STDOUT)
    flag = HERE / "runs" / TAG / "a_part_written"
    t0 = time.time()
    while not flag.exists() and time.time() - t0 < 60: time.sleep(0.2)
    print("A's part is written, its 'done' is held:", flag.exists())
    # second writer on the same store is the child; add B through the child's process is not
    # possible, so B was... -> add B by a short-lived helper process is refused by the DuckDB lock.
    # Instead B is queued up front in a second phase below.
    time.sleep(1)
    os.kill(p.pid, signal.SIGKILL); p.wait()
    print("killed -9 (phase 1: A mid-ingest after its part went live, C mid-download)")
    store = Store(cfg.store_dir)
    print("  after kill:", [(r[0][-12:], r[1], r[2]) for r in q(store, "SELECT url, status, bytes_done FROM url_queue ORDER BY id")])
    print("  files:", q(store, "SELECT filename, status, rows_emitted FROM files"))
    print("  rates rows live with no 'done' file row:", q(store, "SELECT count(*) FROM rates")[0][0])
    print("  tmp leftovers:", sorted(x.name for x in store.rates_dir.iterdir() if not x.name.endswith(".parquet")))
    print("  downloads dir:", sorted(x.name for x in cfg.downloads_dir.iterdir()))
    F.add_urls(store, [ub]); store.close()
    (HERE / "runs" / TAG / "second_run").write_text("1")
    srv.delay["/c/big.json.gz"] = 0.0
    p = subprocess.Popen([sys.executable, __file__, "child", srv.base], env=env,
                         stdout=open(HERE / "logs" / "d5_child2.log", "w"), stderr=subprocess.STDOUT)
    rc = p.wait(timeout=300)
    store = Store(cfg.store_dir)
    print("restart drain exit:", rc)
    for r in q(store, "SELECT url, status, kind, rows_emitted, error FROM url_queue ORDER BY id"):
        print("  ", r[0][-14:], r[1], r[2], r[3], (r[4] or "")[:80])
    print("  files:", q(store, "SELECT filename, status, rows_emitted FROM files ORDER BY 1"))
    print("  rates:", q(store, "SELECT payer, count(*) FROM rates GROUP BY 1 ORDER BY 1"), "(expected Twin Health 300, Other Plan 200)")
    print("  tmp leftovers:", sorted(x.name for x in store.rates_dir.iterdir() if not x.name.endswith(".parquet")))
    print("  requests:", {p_: len(srv.hits(p_)) for p_ in srv.files}, "| C ranges:", [r for _, _, r, s, _ in srv.hits("/c/big.json.gz")])
    srv.close()
