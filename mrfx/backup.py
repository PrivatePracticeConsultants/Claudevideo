"""Store backups: the checksummed zip `mrfx backup` writes, its verification,
and the automatic weekly HOT backup `mrfx serve` takes (review 13 Group 6.3).

A billion-row store takes weeks of downloads to build and lives on one
laptop. `mrfx backup` needs the server stopped, so it was rarely run. The hot
backup runs inside `serve`, without stopping anything:

1. Under the store's write lock (ingest and analytics builds wait; the
   dashboard keeps answering): CHECKPOINT, then DuckDB copies the database
   into a staging file on the backup drive (`ATTACH … COPY FROM DATABASE`,
   DuckDB's own consistent copy — no OS file-lock question on Windows), and
   every other store file (the parquet parts) is HARD-LINKED into
   <store>/.backup_snapshot. A link costs no space and pins the exact bytes
   the copied database refers to, even if a part is replaced a second later.
   (A disk that cannot link — not NTFS/ext4 — copies the parts under the
   lock instead.)
2. The lock is released and the zip is written from the snapshot, STORED
   (parquet is already compressed), with a manifest of sha256 per file and
   the row counts — the same format `mrfx backup` writes.
3. The zip is checked byte-for-byte against its manifest (`mrfx verify`'s
   check, the same function), and only a verified backup lets the oldest
   ones beyond `backup_keep` be deleted (only files this module named).

Never fills a drive: the space needed (the store's size plus the staging
copy of the database plus 1 GB kept free) is checked first, and a shortfall
is a sentence on the Files tab. Off until `backup_dir` is set."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import zipfile
from pathlib import Path

from . import progress as _progress

log = logging.getLogger(__name__)

SNAPSHOT = ".backup_snapshot"
NAME_RE = re.compile(r"^mrfx-backup-\d{4}-\d{2}-\d{2}_\d{4}\.zip$")
RESERVE_BYTES = 1 << 30            # never leave the backup drive below 1 GB free
META_KEY = "last_backup"
_RUN = threading.Lock()


class BackupRefused(RuntimeError):
    """A backup not taken — the message is the user's sentence."""


def store_files(root: Path, *, with_database: bool = True) -> list[Path]:
    """Every file a backup carries: the store minus scratch (spill, temp and
    partial files, diagnostics, a backup snapshot in progress)."""
    db = {"mrfx.duckdb", "mrfx.duckdb.wal"}
    return sorted(p for p in Path(root).rglob("*")
                  if p.is_file() and not p.name.endswith((".tmp", ".part"))
                  and "duckdb_tmp" not in p.parts and "diagnostics" not in p.parts
                  and SNAPSHOT not in p.parts
                  and (with_database or p.relative_to(root).as_posix() not in db))


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_zip(dest: Path, entries: list[tuple[Path, str]], *, store_dir: str,
              counts: dict | None = None, extra: dict | None = None,
              on_file=None) -> dict:
    """The backup zip: every entry under store/<rel>, STORED, and manifest.json
    with sha256 + bytes per file. Written to <dest>.part and renamed, so a
    crash leaves no zip that looks whole."""
    manifest: dict = {"created": dt.datetime.now(dt.timezone.utc).isoformat(),
                      "store_dir": store_dir, "files": {}, **(extra or {})}
    if counts:
        manifest["counts"] = counts
    part = dest.with_name(dest.name + ".part")
    total = 0
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for i, (src, rel) in enumerate(entries):
                _progress.check_cancelled()
                z.write(src, f"store/{rel}")
                size = src.stat().st_size
                manifest["files"][rel] = {"sha256": _sha(src), "bytes": size}
                total += size
                if on_file:
                    on_file(i + 1, len(entries), rel, size)
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
        os.replace(part, dest)
    except BaseException:
        # a stopped or failed backup leaves no store-sized `.zip.part` on the
        # backup drive (review 14 item 2.4, P2-2)
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return {"bytes": total, "files": len(entries), "manifest": manifest}


def _parts_rows(z: zipfile.ZipFile, manifest: dict) -> int | None:
    """Rate rows the zip's own parts hold (store/rates/*.parquet — the
    `rates` view's glob), read from each part's footer without inflating
    it (members are STORED, so the read seeks). None when unreadable."""
    try:
        import pyarrow.parquet as pq
    except Exception:  # noqa: BLE001
        return None
    n = 0
    for rel in manifest.get("files", {}):
        if not re.fullmatch(r"rates/[^/]+\.parquet", rel):
            continue
        try:
            with z.open(f"store/{rel}") as fh:
                n += pq.ParquetFile(fh).metadata.num_rows
        except Exception:  # noqa: BLE001 — the checksum pass reports damage
            return None
    return n


def verify_zip(path: Path, out=None) -> tuple[bool, str]:
    """`mrfx verify <zip>`: every file against its manifest checksum.
    Returns (ok, the verdict sentence); `out` gets the per-file lines."""
    say = out or (lambda _m: None)
    bad = 0
    try:
        with zipfile.ZipFile(path) as z:
            manifest = json.loads(z.read("manifest.json"))
            for rel, meta in manifest["files"].items():
                h = hashlib.sha256()
                try:
                    with z.open(f"store/{rel}") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
                    ok = h.hexdigest() == meta["sha256"]
                except (zipfile.BadZipFile, KeyError, OSError) as e:
                    ok = False
                    say(f"  ({rel}: {e})")
                if not ok:
                    bad += 1
                    say(f"CORRUPT: {rel}")
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as e:
        # a torn central directory / missing manifest is corruption too —
        # report it as the verdict, never a traceback
        return False, (f"this backup is not readable ({e}) — it is damaged; "
                       "take a fresh one.")
    n = len(manifest["files"])
    if bad:
        return False, (f"\n{bad} of {n} files FAILED the checksum — this backup is "
                       "damaged; take a fresh one.")
    rates = manifest.get("counts", {}).get("rates")
    if isinstance(rates, int):
        # the rows a restore WOULD hold, from the parts in the zip — not the
        # number the manifest recorded (review 14 item 2.4: a part committed
        # while the backup waited for the lock was in the database copy and
        # its count, not in the zip; verify said OK and the restore lost it)
        try:
            with zipfile.ZipFile(path) as z:
                held = _parts_rows(z, manifest)
        except (zipfile.BadZipFile, OSError):
            held = None
        if held is not None and held != rates:
            return False, (f"this backup is incomplete: its parts hold {held:,} rate rows "
                           f"but its database recorded {rates:,} — take a fresh one.")
        if held is not None:
            return True, (f"backup OK: {n} files match their checksums; its parts hold the "
                          f"{rates:,} rate rows its database recorded")
    return True, (f"backup OK: {n} files match their checksums"
                  + (f"; recorded {rates:,} rate rows" if isinstance(rates, int) else ""))


def _gb(n: float) -> str:
    return f"{n / 1e9:,.1f} GB" if n >= 1e8 else f"{n / 1e6:,.1f} MB"


def _record(store, rec: dict) -> None:
    import contextvars
    try:
        # in a fresh context: after a Stop the run's own Progress refuses every
        # new connection, and "stopped before it finished" was never recorded
        # (review 14 item 2.4, P2-2)
        contextvars.Context().run(store.meta_set, META_KEY, json.dumps(rec))
    except Exception:  # noqa: BLE001 — the status line is cosmetic (invariant 3)
        log.exception("could not record the backup result")


def last_backup(store) -> dict | None:
    try:
        raw = store.meta_get(META_KEY)
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def backup_due(cfg, store, now: dt.datetime | None = None) -> bool:
    """A backup is due when backup_dir is set and the last good one is older
    than backup_every_days (a failed attempt waits a day before trying again)."""
    if not getattr(cfg, "backup_dir", None):
        return False
    now = now or dt.datetime.now(dt.timezone.utc)
    rec = last_backup(store) or {}

    def age(key):
        try:
            return now - dt.datetime.fromisoformat(rec[key])
        except Exception:  # noqa: BLE001 — never ran / unreadable: due
            return None
    good = age("ok_at")
    tried = age("at")
    if rec.get("error") and tried is not None and tried < dt.timedelta(days=1):
        return False
    return good is None or good >= dt.timedelta(days=int(getattr(cfg, "backup_every_days", 7)))


def backup_status(cfg, store) -> dict:
    """The Files tab's line: off, last good backup (date, size, where), or
    the last attempt's reason."""
    d = getattr(cfg, "backup_dir", None)
    rec = last_backup(store) or {}
    if not d:
        return {"on": False, "sentence": (
            "Automatic backups are off. Set backup_dir in config/mrfx.yaml to a "
            "folder on another drive and the app backs the store up every "
            f"{int(getattr(cfg, 'backup_every_days', 7))} days while it runs.")}
    parts = []
    if rec.get("ok_at"):
        parts.append(f"Last backup: {rec['ok_at'][:16].replace('T', ' ')} UTC, "
                     f"{_gb(rec.get('bytes') or 0)}, {rec.get('path')}"
                     + (" (checked OK)" if rec.get("verified") else ""))
    else:
        parts.append(f"No backup yet in {d}")
    try:
        dbb = sum(p.stat().st_size for p in (Path(store.dir) / "mrfx.duckdb",
                                             Path(store.dir) / "mrfx.duckdb.wal") if p.exists())
        # measured: the writer lock is held about 25 s per GB of database
        # file (review 14 2.4, P2-8); the dashboard keeps answering
        secs = int(dbb / 1e9 * 25)
        if secs >= 10:
            parts.append(f"while a backup copies the database, new files wait about "
                         f"{secs // 60} min {secs % 60:02d} s to be saved (the screens keep answering)"
                         if secs >= 60 else
                         f"while a backup copies the database, new files wait about {secs} s "
                         "to be saved (the screens keep answering)")
    except OSError:
        pass
    if rec.get("error"):
        parts.append(f"the last attempt ({rec.get('at', '')[:16].replace('T', ' ')} UTC) "
                     f"was not taken: {rec['error']}")
    return {"on": True, "dir": str(d), "last": rec or None,
            "sentence": "; ".join(parts) + "."}


def hot_backup(cfg, store, *, now: dt.datetime | None = None) -> dict:
    """Take one backup now. Raises BackupRefused with the sentence (and
    records it for the Files tab) when it is not taken."""
    d = getattr(cfg, "backup_dir", None)
    if not d:
        raise BackupRefused("automatic backups are off — set backup_dir in "
                            "config/mrfx.yaml first")
    if not _RUN.acquire(blocking=False):
        raise BackupRefused("a backup is already being taken")
    now = now or dt.datetime.now(dt.timezone.utc)
    rec = dict(last_backup(store) or {})
    rec["at"] = now.isoformat()
    try:
        res = _take(cfg, store, Path(d), now)
        rec.update({"ok_at": now.isoformat(), "path": res["path"], "bytes": res["bytes"],
                    "files": res["files"], "verified": True, "error": None,
                    "deleted": res["deleted"]})
        _record(store, rec)
        return res
    except _progress.Stopped:
        rec["error"] = "stopped before it finished"
        _record(store, rec)
        raise
    except BackupRefused as e:
        rec["error"] = str(e)
        _record(store, rec)
        raise
    except Exception as e:  # noqa: BLE001 — a backup never takes the server down
        log.exception("backup failed")
        rec["error"] = f"it failed ({e.__class__.__name__}: {e})"
        _record(store, rec)
        raise BackupRefused(rec["error"]) from e
    finally:
        _RUN.release()


def _take(cfg, store, dest_dir: Path, now: dt.datetime) -> dict:
    prog = _progress.current()
    prog.stage("Checking the backup drive")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BackupRefused(f"the backup folder {dest_dir} cannot be used ({e})") from e
    root = Path(store.dir)
    if _inside(dest_dir, root):
        # every backup would carry all the earlier ones (review 14 2.4, P2-5)
        raise BackupRefused(
            f"backup_dir ({dest_dir}) is inside the data folder, so every backup would "
            "contain all the earlier ones — point it at a folder outside the data "
            "folder, ideally on another drive")
    snap = root / SNAPSHOT
    shutil.rmtree(snap, ignore_errors=True)        # a crash's leftover links
    for stale in list(dest_dir.glob(".mrfx-backup-*.tmp")) + list(
            dest_dir.glob("mrfx-backup-*.zip.part")):
        stale.unlink(missing_ok=True)              # a stopped or killed run's leftovers
    # a size ESTIMATE only — the parts a backup carries are listed under the
    # write lock below (review 14 item 2.4)
    files = store_files(root, with_database=False)
    db_size = sum(p.stat().st_size for p in (root / "mrfx.duckdb", root / "mrfx.duckdb.wal")
                  if p.exists())
    parts_size = sum(p.stat().st_size for p in files if p.exists())
    est = parts_size + db_size
    need = est + db_size + RESERVE_BYTES
    free = shutil.disk_usage(dest_dir).free
    if free < need:
        raise BackupRefused(
            f"not enough space: the backup drive has {_gb(free)} free and this "
            f"backup needs about {_gb(need)} (the store, a working copy of its "
            "database, and 1 GB kept free) — free some space there or point "
            "backup_dir at a bigger drive")
    can_link = _can_hard_link(root, snap)
    if not can_link:
        # the parts are copied into the store's own folder first (review 14
        # 2.4, P2-6): that drive needs the room too
        sfree = shutil.disk_usage(root).free
        if sfree < parts_size + RESERVE_BYTES:
            raise BackupRefused(
                f"not enough space: this drive cannot hard-link, so the backup copies the "
                f"store's files next to it first — that needs about "
                f"{_gb(parts_size + RESERVE_BYTES)} free on the data drive and it has "
                f"{_gb(sfree)}")
    name = f"mrfx-backup-{now.strftime('%Y-%m-%d_%H%M')}.zip"
    dest = dest_dir / name
    if dest.exists():
        raise BackupRefused(f"{name} already exists (backups are never overwritten)")
    staging = dest_dir / f".{name}.duckdb.tmp"
    prog.stage("Copying the database (new data waits a moment)")
    linked = 0
    to_copy: list[tuple[Path, tuple]] = []
    try:
        with store.write_lock:
            with store.connect() as con:
                con.execute("CHECKPOINT")
                db = con.execute("SELECT current_database()").fetchone()[0]
                con.execute(f"ATTACH '{str(staging).replace(chr(39), chr(39) * 2)}' AS mrfx_bk")
                try:
                    con.execute(f'COPY FROM DATABASE "{db}" TO mrfx_bk')
                finally:
                    con.execute("DETACH mrfx_bk")
                counts = {
                    "rates": con.execute("SELECT count(*) FROM rates").fetchone()[0],
                    "files_done": con.execute(
                        "SELECT count(*) FROM files WHERE status = 'done'").fetchone()[0]}
            # the parts the copied database refers to — listed HERE, under the
            # same lock (review 14 item 2.4, P0: listed before it, a part
            # swapped in while the backup waited for the lock was in the
            # database copy and its count but not in the zip: 200 of 840 rows
            # lost on restore, and verify said OK) — pinned by hard links
            files = store_files(root, with_database=False)
            for p in files:
                if not p.exists():
                    continue
                t = snap / p.relative_to(root)
                t.parent.mkdir(parents=True, exist_ok=True)
                if can_link:
                    try:
                        os.link(p, t)
                        linked += 1
                        continue
                    except OSError:
                        pass
                st = p.stat()
                to_copy.append((p, (st.st_size, st.st_mtime_ns)))
        if to_copy:
            # a drive that cannot link: the copy runs OUTSIDE the lock (a USB
            # disk copying a 100 GB store held every writer for minutes —
            # 2.4 P2-6), then the parts are checked unchanged under the lock
            prog.stage("Copying the store's files (this drive cannot hard-link)")
            for p, _sig in to_copy:
                _progress.check_cancelled()
                shutil.copy2(p, snap / p.relative_to(root))
            with store.write_lock:
                changed = [p.name for p, sig in to_copy if not p.exists()
                           or (p.stat().st_size, p.stat().st_mtime_ns) != sig]
                now_parts = {q.relative_to(root) for q in store_files(root, with_database=False)}
            if changed or now_parts != {p.relative_to(root) for p in files}:
                raise BackupRefused(
                    "the store changed while the backup copied it (this drive cannot "
                    "hard-link, so the copy runs while files keep loading) — it is "
                    "tried again later; nothing was kept from this attempt")
        entries = [(staging, "mrfx.duckdb")] + [
            (snap / p.relative_to(root), p.relative_to(root).as_posix())
            for p in files if (snap / p.relative_to(root)).exists()]
        prog.stage("Writing the backup")
        size_all = sum(s.stat().st_size for s, _ in entries) or 1
        done = [0]

        def on_file(i, n, rel, size):
            done[0] += size
            prog.tick(done[0] / size_all)
        res = write_zip(dest, entries, store_dir=str(root), counts=counts,
                        extra={"kind": "hot", "linked_parts": linked}, on_file=on_file)
    finally:
        shutil.rmtree(snap, ignore_errors=True)
        staging.unlink(missing_ok=True)
    prog.stage("Checking the backup")
    ok, verdict = verify_zip(dest)
    if not ok:
        dest.unlink(missing_ok=True)
        raise BackupRefused(f"the backup failed its own check ({verdict.strip()}) and "
                            "was removed; the older backups are kept")
    # retention: only a VERIFIED new backup lets the oldest go; only files
    # this module named are ever deleted
    keep = max(1, int(getattr(cfg, "backup_keep", 4)))
    ours = sorted(p for p in dest_dir.iterdir() if NAME_RE.match(p.name))
    deleted = []
    for old in ours[:-keep]:
        if old != dest:
            old.unlink(missing_ok=True)
            deleted.append(old.name)
    log.info("backup written: %s (%s, %d files, verified)", dest, _gb(res["bytes"]),
             res["files"])
    return {"path": str(dest), "bytes": res["bytes"], "files": res["files"],
            "counts": counts, "verdict": verdict, "deleted": deleted}


def _inside(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False


def _can_hard_link(root: Path, snap: Path) -> bool:
    """One probe link inside the store folder (a FAT/exFAT drive refuses)."""
    probe_src = root / ".backup_link_probe"
    probe_dst = snap / ".backup_link_probe"
    try:
        snap.mkdir(parents=True, exist_ok=True)
        probe_src.write_bytes(b"x")
        os.link(probe_src, probe_dst)
        return True
    except OSError:
        return False
    finally:
        for q in (probe_dst, probe_src):
            try:
                q.unlink(missing_ok=True)
            except OSError:
                pass


def backup_loop(cfg, store, stop: threading.Event, first_wait: float = 600.0,
                every: float = 3600.0) -> None:
    """`serve`'s thread: an hourly look; a backup when one is due."""
    if stop.wait(first_wait):
        return
    while not stop.is_set():
        try:
            if backup_due(cfg, store):
                hot_backup(cfg, store)
        except BackupRefused as e:
            log.warning("automatic backup not taken: %s", e)
        except Exception:  # noqa: BLE001 — never kills the server
            log.exception("automatic backup failed; will retry")
        if stop.wait(every):
            return
