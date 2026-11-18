"""backup_dir inside the store folder (e.g. data/mrfx_store/backups — 'next
to the data' is a natural choice): store_files() does not exclude it, so each
backup carries every earlier backup; plus retention and the schedule across a
restart (backup_due reads the stored record)."""
import tempfile, zipfile, datetime as dt
from pathlib import Path
import h

def main():
    root = Path(tempfile.mkdtemp(dir=Path(__file__).parent, prefix="t11_"))
    cfg, store, A = h.small_store(root, n=20)
    cfg.backup_dir = store.dir / "backups"; cfg.backup_keep = 2
    from mrfx import backup
    sizes = []
    for day in (1, 2, 3, 4):
        r = backup.hot_backup(cfg, store, now=dt.datetime(2026, 11, day, 9, 0, tzinfo=dt.timezone.utc))
        with zipfile.ZipFile(r["path"]) as z:
            nested = [n for n in z.namelist() if n.startswith("store/backups/")]
        sizes.append(r["bytes"])
        print(f"backup {day}: {Path(r['path']).name} {r['bytes']:,} bytes; carries earlier backups: {nested}; deleted: {r['deleted']}")
    print("listing:", sorted(p.name for p in cfg.backup_dir.iterdir()))
    # the schedule across a restart
    s2 = h.Store(cfg.store_dir)
    now = dt.datetime(2026, 11, 4, 12, 0, tzinfo=dt.timezone.utc)
    print("due 3 h after the last (fresh Store):", backup.backup_due(cfg, s2, now=now),
          "| due 8 days after:", backup.backup_due(cfg, s2, now=now + dt.timedelta(days=8)))

if __name__ == "__main__":
    main()
