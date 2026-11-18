"""Cost of a hot backup of a store whose database file is ~0.7 GB (a scratch
copy of the live store + a filler table standing in for big rollup tables):
how long store.write_lock is held (every ingest status write, claim and part
swap waits that long), and what a dashboard read costs meanwhile."""
import shutil, threading, time, tempfile, statistics
from pathlib import Path
import h

HERE = Path(__file__).parent

def main():
    root = Path(tempfile.mkdtemp(dir=HERE, prefix="t10_"))
    shutil.copytree(HERE / "live" / "store", root / "store")
    cfg = h.mkcfg(root); cfg.backup_dir = root / "bk"
    store = h.Store(cfg.store_dir)
    with store.write_lock, store.connect() as con:
        con.execute("CREATE TABLE filler AS SELECT i, md5(i::VARCHAR) AS a, md5((i*7)::VARCHAR) AS b "
                    "FROM range(9000000) t(i)")
        con.execute("CHECKPOINT")
    print("database file:", round((root / "store" / "mrfx.duckdb").stat().st_size / 1e9, 2), "GB")
    from mrfx import backup
    # instrument the lock: record how long the backup holds it
    held = {}
    real = store.write_lock
    class Spy:
        def __enter__(self):
            t = time.monotonic(); real.acquire()
            if threading.current_thread().name == "bk":
                held["t0"] = time.monotonic()
            return self
        def __exit__(self, *a):
            if threading.current_thread().name == "bk" and "t0" in held:
                held.setdefault("spans", []).append(time.monotonic() - held.pop("t0"))
            real.release()
        def acquire(self, *a, **k): return real.acquire(*a, **k)
        def release(self): return real.release()
        def locked(self): return real.locked()
    store.write_lock = Spy()
    lat_r, lat_w, stop = [], [], threading.Event()
    def reader():
        while not stop.is_set():
            t = time.monotonic()
            with store.connect() as con:
                con.execute("SELECT payer, count(*) FROM rates_by_tin GROUP BY payer ORDER BY payer").fetchall()
            lat_r.append(time.monotonic() - t); time.sleep(0.05)
    def writer():
        while not stop.is_set():
            t = time.monotonic(); store.meta_set("audit_probe", str(t))
            lat_w.append(time.monotonic() - t); time.sleep(0.05)
    for f in (reader, writer):
        threading.Thread(target=f, daemon=True).start()
    time.sleep(1.0)
    base_r = list(lat_r); base_w = list(lat_w)
    t = time.monotonic(); res = {}
    tb = threading.Thread(target=lambda: res.update(r=backup.hot_backup(cfg, store)), name="bk")
    tb.start(); tb.join(); total = time.monotonic() - t
    stop.set(); time.sleep(0.3)
    print(f"backup took {total:.1f} s; write_lock held by it for {[round(s,2) for s in held.get('spans', [])]} s")
    print("status-write latency: before median %.3f s; during backup max %.2f s" % (
        statistics.median(base_w), max(lat_w[len(base_w):] or [0])))
    print("dashboard read latency: before median %.3f s; during backup median %.3f s, max %.2f s" % (
        statistics.median(base_r), statistics.median(lat_r[len(base_r):] or [0]), max(lat_r[len(base_r):] or [0])))
    print("zip:", round(Path(res["r"]["path"]).stat().st_size / 1e9, 2), "GB;", res["r"]["verdict"])
    shutil.rmtree(root)

if __name__ == "__main__":
    main()
