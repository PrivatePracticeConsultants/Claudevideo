"""Keep a current NPPES monthly file on hand automatically.

Practice names, specialties and addresses come from CMS's NPPES file. Looking
each NPI up over the internet takes hours to days on a big book; the monthly
full file (~1.1 GB zip, ~9.9M providers) resolves the whole book in one local
pass (measured: 164 s to build the lookup table, then sub-second lookups).

Before this module the user had to download that file by hand every month and
edit `enrichment.bulk_csv_path` to its new name. Now:

- `bulk_source(cfg)` — the file to use: an explicit `bulk_csv_path` FILE wins;
  a `bulk_csv_path` FOLDER (or, when unset, `enrichment.nppes_dir`) means "the
  newest NPPES_Data_Dissemination_<Month>_<Year>*.zip in there".
- `ensure_current(cfg)` — with `enrichment.auto_download` on (the default) and
  no explicit file configured, checks CMS's download page at most once a day
  and downloads a newer month's full file into that folder (resumable, disk
  guarded, TLS verified), keeping the newest two.

Everything here is best-effort: any failure is logged and the app carries on
with what it has (an older file, or the NPPES API)."""

from __future__ import annotations

import calendar
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

log = logging.getLogger(__name__)

NPPES_FILES_PAGE = "https://download.cms.gov/nppes/NPI_Files.html"
# the FULL monthly file only — never the weekly incremental or the
# deactivation report, which carry a fraction of the providers
_FULL_RE = re.compile(r"NPPES_Data_Dissemination_([A-Za-z]+)_(\d{4})(?:_V(\d+))?\.zip$", re.I)
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
CHECK_INTERVAL_S = 24 * 3600          # look for a new month at most once a day
RETRY_AFTER_FAILURE_S = 3600          # …or an hour after a failed check
KEEP_FILES = 2                        # the current month + the previous one
_STAMP = ".nppes_last_check"


def _month_key(name: str) -> tuple[int, int, int] | None:
    m = _FULL_RE.search(name)
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    return int(m.group(2)), _MONTHS[m.group(1).lower()], int(m.group(3) or 0)


def _full_files(folder: Path) -> list[Path]:
    try:
        return [p for p in folder.iterdir() if p.is_file() and _month_key(p.name)]
    except OSError:
        return []


def newest_local(folder: Path) -> Path | None:
    """The newest full monthly NPPES zip in `folder` (by the month in its
    name), or None."""
    files = _full_files(Path(folder))
    return max(files, key=lambda p: _month_key(p.name)) if files else None


def bulk_source(cfg) -> Path | None:
    """The NPPES file enrichment should read right now (may not exist yet)."""
    e = cfg.enrichment
    p = getattr(e, "bulk_csv_path", None)
    if p:
        p = Path(p)
        return newest_local(p) if p.is_dir() else p
    d = getattr(e, "nppes_dir", None)
    return newest_local(Path(d)) if d else None


def _auto_folder(cfg) -> Path | None:
    """Where auto-download keeps files, or None when the user manages a
    specific file themselves (or turned the feature off)."""
    e = cfg.enrichment
    if os.environ.get("MRFX_NO_AUTO_DOWNLOAD") or not getattr(e, "auto_download", False) \
            or e.mode == "off":
        return None
    p = getattr(e, "bulk_csv_path", None)
    if p and not Path(p).is_dir():
        return None                     # an explicit file: never second-guess it
    return Path(p) if p else Path(e.nppes_dir)


def latest_remote(cfg) -> tuple[str, str] | None:
    """(url, file name) of the newest full monthly file on CMS's page."""
    import httpx

    from .fetch import ssl_verify

    with httpx.Client(headers={"User-Agent": cfg.user_agent}, verify=ssl_verify(),
                      follow_redirects=True, timeout=30) as c:
        r = c.get(NPPES_FILES_PAGE)
        r.raise_for_status()
        page_url = str(r.url)
        hrefs = re.findall(r"""href\s*=\s*["']?([^"'\s>]+)""", r.text, re.I)
    best = None
    for h in hrefs:
        name = h.split("?")[0].rsplit("/", 1)[-1]
        k = _month_key(name)
        if k and (best is None or k > best[0]):
            best = (k, urljoin(page_url, h), name)
    return (best[1], best[2]) if best else None


# In-memory twin of the stamp file: if the stamp can't be WRITTEN (a
# OneDrive-locked or read-only data folder), _stamp_fresh was always False and
# the enrichment loop asked CMS every ~20 s, all day.
_LAST_CHECK: dict[str, float] = {}
_LOCK = ".nppes_download.lock"
LOCK_STALE_S = 6 * 3600     # a lock older than this is a crashed download's


def _stamp_fresh(folder: Path) -> bool:
    mem = _LAST_CHECK.get(str(folder))
    if mem is not None and time.time() - mem < CHECK_INTERVAL_S:
        return True
    try:
        return time.time() - (folder / _STAMP).stat().st_mtime < CHECK_INTERVAL_S
    except OSError:
        return False


def _take_download_lock(folder: Path) -> bool:
    """Only ONE process downloads the monthly file. Serve's enrichment loop and
    a CLI command could both see a stale stamp and append to the same .part
    (on Windows the second rename fails or adopts an interleaved zip the
    validator throws away — two 1.1 GB downloads wasted). O_EXCL lock with the
    owner's pid; a lock whose owner is gone, or older than LOCK_STALE_S, is a
    crashed download's and is taken over."""
    from .store import _pid_alive
    p = folder / _LOCK
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                owner = int(p.read_text(encoding="utf-8").strip() or 0)
                age = time.time() - p.stat().st_mtime
            except (OSError, ValueError):
                owner, age = 0, LOCK_STALE_S + 1
            if owner and _pid_alive(owner) and age < LOCK_STALE_S:
                return False
            try:
                p.unlink()                       # stale: take it over
            except OSError:
                return False
            continue
        except OSError:
            return True    # can't create a lock at all (read-only): don't block the feature
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    return False


def _release_download_lock(folder: Path) -> None:
    try:
        p = folder / _LOCK
        if p.read_text(encoding="utf-8").strip() == str(os.getpid()):
            p.unlink()
    except OSError:
        pass


def _touch_stamp(folder: Path, retry_in: float | None = None) -> None:
    """Record a check. retry_in (seconds) backdates it so the next check
    happens that soon instead of a day later."""
    p = folder / _STAMP
    _LAST_CHECK[str(folder)] = time.time() - (
        (CHECK_INTERVAL_S - retry_in) if retry_in is not None else 0)
    try:
        p.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        if retry_in is not None:
            t = time.time() - (CHECK_INTERVAL_S - retry_in)
            os.utime(p, (t, t))
    except OSError:
        pass


def _prune(folder: Path, keep: Path) -> None:
    files = sorted(_full_files(folder), key=lambda p: _month_key(p.name), reverse=True)
    for old in files[KEEP_FILES:]:
        if old == keep:
            continue
        try:
            old.unlink()
            log.info("NPPES: removed older monthly file %s", old.name)
        except OSError:
            pass   # in use (being read) — removed on a later check


def ensure_current(cfg, stop=None, force: bool = False) -> Path | None:
    """Download this month's NPPES full file if a newer one than we have is
    published. Returns the file now on hand (new or existing), or None when
    auto-download doesn't apply. Never raises."""
    folder = _auto_folder(cfg)
    if folder is None:
        return None
    have = newest_local(folder)
    if not force and _stamp_fresh(folder):
        return have
    try:
        folder.mkdir(parents=True, exist_ok=True)
        remote = latest_remote(cfg)
    except Exception as e:  # noqa: BLE001 — offline / CMS hiccup: try again later
        log.warning("NPPES: couldn't check CMS for a new monthly provider file (%s) — "
                    "will retry in an hour; names keep coming from %s", e,
                    have.name if have else "the NPPES website lookup")
        _touch_stamp(folder, retry_in=RETRY_AFTER_FAILURE_S)
        return have
    if remote is None:
        log.warning("NPPES: no monthly file link found on %s — will look again tomorrow",
                    NPPES_FILES_PAGE)
        _touch_stamp(folder)
        return have
    url, name = remote
    if have is not None and _month_key(have.name) >= _month_key(name):
        _touch_stamp(folder)
        return have
    if not _take_download_lock(folder):
        log.info("NPPES: another MRF Explorer process is already downloading %s — "
                 "leaving it to that one", name)
        return have
    try:
        return _download_monthly(cfg, folder, url, name, have, stop)
    finally:
        _release_download_lock(folder)


def _download_monthly(cfg, folder: Path, url: str, name: str, have, stop):
    from .fetch import DownloadError, download

    dest = folder / name
    log.info("NPPES: downloading %s (~1.1 GB) — the provider file that fills in "
             "practice names in one fast local pass. This happens once a month, "
             "in the background.", name)
    t0 = time.monotonic()
    try:
        download(cfg, url, dest, max_bytes=None,
                 cancel_check=(stop.is_set if stop is not None else None))
    except DownloadError as e:
        log.warning("NPPES: download of %s stopped (%s) — it resumes on the next "
                    "check; names keep coming from %s", name, e,
                    have.name if have else "the NPPES website lookup")
        _touch_stamp(folder, retry_in=RETRY_AFTER_FAILURE_S)
        return have
    except Exception as e:  # noqa: BLE001
        log.warning("NPPES: download of %s failed (%s) — will retry in an hour", name, e)
        _touch_stamp(folder, retry_in=RETRY_AFTER_FAILURE_S)
        return have
    if not _looks_like_nppes_zip(dest):
        log.warning("NPPES: %s isn't a readable NPPES zip — discarded; will retry", name)
        try:
            dest.unlink()
        except OSError:
            pass
        _touch_stamp(folder, retry_in=RETRY_AFTER_FAILURE_S)
        return have
    log.info("NPPES: %s ready (%.1f GB in %.0fs)", name, dest.stat().st_size / 1e9,
             time.monotonic() - t0)
    _touch_stamp(folder)
    _prune(folder, keep=dest)
    return dest


def _looks_like_nppes_zip(p: Path) -> bool:
    import zipfile
    try:
        with zipfile.ZipFile(p) as z:
            return any("npidata_pfile" in n.lower() and n.lower().endswith(".csv")
                       for n in z.namelist())
    except (OSError, zipfile.BadZipFile):
        return False
