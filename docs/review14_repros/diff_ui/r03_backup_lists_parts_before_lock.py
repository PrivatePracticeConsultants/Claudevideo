"""Hot backup taken while a file is being committed: backup.py lists the store's
files (line 246) BEFORE it takes store.write_lock (line 266). A part that is
swapped in while the backup waits for the lock is in the copied database's
world (its files row, its rows in count(*)) but is never linked into the zip.
The zip verifies OK and claims N rows; a restore has fewer."""
import tempfile, threading, time, zipfile, json, datetime as dt
from pathlib import Path
import h, duckdb

def main():
    root = Path(tempfile.mkdtemp(dir=Path(__file__).parent, prefix="t03_"))
    cfg, store, A = h.small_store(root, n=40)
    cfg.backup_dir = root / "bk"
    from mrfx import backup
    from mrfx.store import Store
    with store.connect() as con:
        before = con.execute("SELECT count(*) FROM rates").fetchone()[0]
    print("rates before:", before, "parts:", len(list(store.rates_dir.glob("*.parquet"))))
    # an ingest whose part swap is slow (Windows AV / the ~1.4 s replace retry)
    orig = store._swap_in_part
    def slow_swap(*a, **k):
        time.sleep(1.0)
        return orig(*a, **k)
    store._swap_in_part = slow_swap
    def ingest():
        h.market(cfg, store, "Delta Health", "72", 50, day="2026-06-01", base=55.0)
    ti = threading.Thread(target=ingest); ti.start()
    # wait until the ingest is inside the write lock, then the weekly backup fires
    for _ in range(200):
        if store.write_lock.locked():
            break
        time.sleep(0.01)
    time.sleep(0.2)
    res = {}
    def bk():
        res["r"] = backup.hot_backup(cfg, store)
    tb = threading.Thread(target=bk); tb.start()
    ti.join(); tb.join()
    with store.connect() as con:
        after = con.execute("SELECT count(*) FROM rates").fetchone()[0]
    print("live store rates after ingest:", after)
    r = res["r"]
    print("backup:", Path(r["path"]).name, "manifest counts:", r["counts"], "verdict:", r["verdict"])
    ok, verdict = backup.verify_zip(Path(r["path"]))
    print("mrfx verify:", ok, verdict)
    # restore into a scratch folder and open it
    rest = root / "restored"
    with zipfile.ZipFile(r["path"]) as z:
        z.extractall(rest)
    rs = Store(rest / "store")
    with rs.connect() as con:
        n = con.execute("SELECT count(*) FROM rates").fetchone()[0]
        done = con.execute("SELECT count(*) FROM files WHERE status='done'").fetchone()[0]
    parts = sorted(p.name for p in (rest / "store" / "rates").glob("*.parquet"))
    print("RESTORED store: rates =", n, "files done =", done, "parts =", len(parts))
    print("live parts:", len(list(store.rates_dir.glob("*.parquet"))))
    print("ROWS LOST IN THE BACKUP:", r["counts"]["rates"] - n)

if __name__ == "__main__":
    main()
