"""A store on a drive that cannot hard-link (exFAT/FAT32 external disk):
backup.py copies every part into <store>/.backup_snapshot UNDER store.write_lock,
and the space guard only checks the BACKUP drive — the store drive needs a
second copy of the whole store, unchecked. Simulated: os.link raises EPERM,
0.4 GB of parts; disk_usage of the store drive reported as 0.2 GB free."""
import errno, os, shutil, tempfile, time, threading, datetime as dt
from pathlib import Path
import h

def main():
    root = Path(tempfile.mkdtemp(dir=Path(__file__).parent, prefix="t12_"))
    cfg, store, A = h.small_store(root, n=20)
    cfg.backup_dir = root / "bk"
    big = store.rates_dir.parent / "tin_zip_place.feedfacefeedface.parquet"   # stands in for 0.4 GB of parts
    with open(big, "wb") as f:
        f.write(os.urandom(1 << 20) * 400)
    from mrfx import backup
    real_link, real_du = os.link, shutil.disk_usage
    def no_link(*a, **k):
        raise OSError(errno.EPERM, "Operation not permitted")
    def du(p):
        r = real_du(p)
        if str(p).startswith(str(store.dir)):
            return r._replace(free=200 * 1024 * 1024)
        return r
    backup.os.link = no_link
    backup.shutil.disk_usage = du
    held = {}
    real_lock = store.write_lock
    orig_take = backup._take
    # time the lock: poll who holds it
    def watch(stop):
        t0 = None
        while not stop.is_set():
            if real_lock.locked() and t0 is None: t0 = time.monotonic()
            if not real_lock.locked() and t0 is not None:
                held.setdefault("spans", []).append(round(time.monotonic() - t0, 2)); t0 = None
            time.sleep(0.005)
    stop = threading.Event(); w = threading.Thread(target=watch, args=(stop,)); w.start()
    called = []
    orig_du = backup.shutil.disk_usage
    def du_spy(p):
        called.append(str(p)); return orig_du(p)
    backup.shutil.disk_usage = du_spy
    r = backup.hot_backup(cfg, store, now=dt.datetime(2026, 11, 20, 9, 0, tzinfo=dt.timezone.utc))
    stop.set(); w.join()
    backup.os.link = real_link; backup.shutil.disk_usage = real_du
    print("backup OK:", r["verdict"])
    print("disk_usage was asked about:", called, "(never the store drive)")
    print("write_lock spans while backing up (s):", held.get("spans"))
    print("manifest linked_parts:", r.get("counts"), "— parts were COPIED into the store drive's .backup_snapshot under the lock")

if __name__ == "__main__":
    main()
