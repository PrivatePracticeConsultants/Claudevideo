"""A backup stopped from the dashboard (or failing mid-zip: a USB drive pulled)
leaves <name>.zip.part on the backup drive; no later run removes it (the
start-of-run sweep only deletes '.mrfx-backup-*.tmp')."""
import tempfile, threading, time, secrets, zipfile
from pathlib import Path
import h
from fastapi.testclient import TestClient

def ls(d):
    return sorted((p.name, p.stat().st_size) for p in Path(d).iterdir())

def main():
    root = Path(tempfile.mkdtemp(dir=Path(__file__).parent, prefix="t04_"))
    cfg, store, A = h.small_store(root, n=40)
    cfg.backup_dir = root / "bk"
    from mrfx import backup
    c = TestClient(h._api.create_app(cfg, store))
    # 1) Stop pressed while the zip is being written (slow drive: 0.5 s a file)
    orig_write = zipfile.ZipFile.write
    def slow_write(self, *a, **k):
        time.sleep(0.5)
        return orig_write(self, *a, **k)
    zipfile.ZipFile.write = slow_write
    tok = secrets.token_hex(12); res = {}
    def run():
        r = c.post("/api/backup", headers={"X-Mrfx-Progress": tok})
        res["r"] = (r.status_code, r.text[:120])
    t = threading.Thread(target=run); t.start()
    for _ in range(100):
        s = c.get(f"/api/progress/{tok}")
        if s.status_code == 200 and s.json().get("stage") == "Writing the backup":
            break
        time.sleep(0.1)
    time.sleep(0.6)
    print("DELETE ->", c.delete(f"/api/progress/{tok}").json())
    t.join()
    zipfile.ZipFile.write = orig_write
    print("stopped backup answered:", res["r"])
    print("backup dir after Stop:", ls(cfg.backup_dir))
    # 2) the drive fails mid-copy (EIO on the 3rd file)
    n = {"i": 0}
    def eio_write(self, *a, **k):
        n["i"] += 1
        if n["i"] == 3:
            raise OSError(5, "Input/output error")
        return orig_write(self, *a, **k)
    zipfile.ZipFile.write = eio_write
    import datetime as dt
    try:
        backup.hot_backup(cfg, store, now=dt.datetime(2026, 11, 13, 9, 0, tzinfo=dt.timezone.utc))
    except backup.BackupRefused as e:
        print("EIO backup refused:", e)
    zipfile.ZipFile.write = orig_write
    print("backup dir after EIO:", ls(cfg.backup_dir))
    # 3) a later good run
    r = backup.hot_backup(cfg, store, now=dt.datetime(2026, 11, 14, 9, 0, tzinfo=dt.timezone.utc))
    print("good backup:", Path(r["path"]).name)
    print("backup dir after a good run:", ls(cfg.backup_dir))
    print("snapshot dir left in store:", (store.dir / ".backup_snapshot").exists())
    print("Files-tab sentence:", backup.backup_status(cfg, store)["sentence"][:160])

if __name__ == "__main__":
    main()
