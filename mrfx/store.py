"""DuckDB-backed Parquet store — V4 TIN-grain data model.

- `rates`: raw rows (one per extracted price × provider), parquet part per
  source file (re-ingest overwrites its part; idempotent).
- `rates_by_tin` (materialized): the app's spine — one row per
  (payer, tin_value, billing_code, modifier_set, billing_class,
  service_code_set, file_month) with npi_count / source_count /
  rate_variants. NPI rows stay available for drill-down (`rates_dedup`).
- `tin_directory` (derived): TIN -> display name (rolled up from the NPPES
  org names of its NPIs), entity kind, npi_count, states, discipline.
- `entity_map`: user-defined TIN -> entity grouping (config/entity_map.yaml).
- `files`, `npi_directory`, `provider_refs`, `peer_sets`, `mpfs`.

Writes are serialized behind a lock; readers never touch the DuckDB catalog
(views are registered only at init and inside locked write paths).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from . import progress as _progress
from .catalog import (THERAPY_CLINIC_STRONG_CODES, THERAPY_MIN_SHARE_PCT,
                      THERAPY_SMALL_PRACTICE_NPIS,
                      excluded_facility_taxonomy_sql, hospital_taxonomy_sql,
                      therapy_taxonomy_sql)

# Parquet codec for the rate parts. zstd is ~30-50% smaller than the pyarrow
# default (snappy) on real payer data. Decode is marginally slower than snappy
# but the store is on local disk and queries are I/O-bound on part SIZE, so the
# smaller files are a net win in practice; DuckDB reads zstd natively. Mixed
# snappy/zstd parts coexist fine (compression is per-file metadata), so old
# parts need no migration; new ingests just get smaller.
PARQUET_COMPRESSION = "zstd"

RATES_SCHEMA = pa.schema(
    [
        pa.field("payer", pa.string(), nullable=False),
        pa.field("source_file", pa.string(), nullable=False),
        pa.field("file_month", pa.string()),
        pa.field("schema_version", pa.string()),
        pa.field("last_updated_on", pa.string()),
        pa.field("tin_value", pa.string()),
        pa.field("tin_type", pa.string()),
        pa.field("tin_is_really_npi", pa.bool_()),
        # nullable: a provider group can carry a TIN but no NPIs (TIN-only
        # rate). Such a row keeps the rate at the TIN grain with npi = NULL;
        # it is filtered out of the NPI grain (DEDUP_QUERY) so it never shows
        # as a phantom NPI.
        pa.field("npi", pa.string(), nullable=True),
        pa.field("billing_code", pa.string(), nullable=False),
        pa.field("billing_code_type", pa.string()),
        pa.field("discipline", pa.string()),
        pa.field("is_timed", pa.bool_()),
        pa.field("billing_code_modifier", pa.list_(pa.string())),
        pa.field("negotiated_rate", pa.float64()),
        pa.field("negotiated_type", pa.string()),
        pa.field("is_dollar_rate", pa.bool_()),
        pa.field("billing_class", pa.string()),
        pa.field("service_code", pa.list_(pa.string())),
        pa.field("expiration_date", pa.string()),
        pa.field("ingested_at", pa.string()),
    ]
)


def file_key(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()[:16]


def sql_path(p) -> str:
    """Quote a filesystem path for inlining in SQL: DuckDB has no parameter
    binding for SET/DDL statements, and a directory named O'Brien would
    otherwise break the statement."""
    return str(p).replace("'", "''")


# The analytics pool chosen automatically (duckdb_memory_gb unset) and the
# parser workers share one machine: the pool takes 40 % of RAM (2..12 GB) but
# never so much that the OS and two parser workers no longer fit beside it
# (review 12 item 2.6 — the shipped 8 GB sat beside the workers' budget on a
# 16 GB laptop). fetch._ram_worker_cap sizes the workers from the same pool.
PARSER_WORKER_RAM_GB = 2.0
OS_RESERVE_GB = 3.0
MIN_PARSER_WORKERS_RESERVED = 2


def auto_pool_gb(total_bytes: int) -> int:
    total_gb = total_bytes / 1e9
    room = total_gb - OS_RESERVE_GB - MIN_PARSER_WORKERS_RESERVED * PARSER_WORKER_RAM_GB
    return max(2, min(12, int(total_gb * 0.4), int(room)))


def _total_ram_bytes() -> int:
    """Physical RAM, cross-platform, 0 if unknown. os.sysconf doesn't exist on
    Windows — without this branch the documented '40% of RAM, clamped [2,12] GB'
    auto memory cap silently fell back to a flat 4 GB on the target platform."""
    if os.name == "nt":
        try:
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_uint64),
                            ("ullAvailPhys", ctypes.c_uint64),
                            ("ullTotalPageFile", ctypes.c_uint64),
                            ("ullAvailPageFile", ctypes.c_uint64),
                            ("ullTotalVirtual", ctypes.c_uint64),
                            ("ullAvailVirtual", ctypes.c_uint64),
                            ("ullAvailExtendedVirtual", ctypes.c_uint64)]

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(st)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys)
        except Exception:  # noqa: BLE001
            return 0
        return 0
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


# --- automatic fast-disk spill selection (Windows HDD store → SSD scratch) ----
def _avail_ram_bytes() -> int:
    """Physical RAM currently AVAILABLE, cross-platform, 0 if unknown. The auto
    memory cap must respect this, not just total RAM: on a 32 GB machine with
    20 GB already in use by other apps, a 12 GB DuckDB cap overcommits what is
    actually free — observed as a silent NATIVE process death (no traceback,
    prompt just returns) partway through a big rollup aggregation."""
    if os.name == "nt":
        try:
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_uint64),
                            ("ullAvailPhys", ctypes.c_uint64),
                            ("ullTotalPageFile", ctypes.c_uint64),
                            ("ullAvailPageFile", ctypes.c_uint64),
                            ("ullTotalVirtual", ctypes.c_uint64),
                            ("ullAvailVirtual", ctypes.c_uint64),
                            ("ullAvailExtendedVirtual", ctypes.c_uint64)]

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(st)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullAvailPhys)
        except Exception:  # noqa: BLE001
            return 0
        return 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


# DuckDB spills big rollups to disk; on a spinning HDD that stalls every parser
# worker. When the store is on a confirmed HDD and a roomy SSD is present, the
# spill is auto-routed to the SSD. Every step is best-effort: any failure leaves
# the spill in the store (the prior behavior), never breaks opening the store.
_AUTO_SPILL_MIN_FREE_BYTES = 20 * 10**9  # don't relocate onto a near-full drive


def _parse_media_output(text: str) -> dict[str, str]:
    """Parse `DRIVELETTER=MediaType` lines (from the PowerShell probe below)
    into {'C': 'SSD', 'E': 'HDD', …}. Tolerant of blank/garbage lines."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        dl, mt = line.split("=", 1)
        dl = dl.strip().rstrip(":").upper()
        mt = mt.strip()
        if len(dl) == 1 and dl.isalpha() and mt:
            out[dl] = mt
    return out


@lru_cache(maxsize=1)
def _windows_drive_media() -> dict[str, str]:
    """Map drive letter → media type ('SSD'/'HDD'/'Unspecified'/…) on Windows,
    via one PowerShell call (cached for the process). Empty on non-Windows or
    ANY failure/timeout, so callers fall back to in-store spill. Joins physical
    disks (which carry MediaType) to partitions (which carry drive letters)."""
    if os.name != "nt":
        return {}
    ps = (
        "$m=@{}; Get-PhysicalDisk | ForEach-Object { $m[[int]$_.DeviceId]=$_.MediaType }; "
        "Get-Partition | Where-Object { $_.DriveLetter } | ForEach-Object "
        "{ \"$($_.DriveLetter)=$($m[[int]$_.DiskNumber])\" }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=10)
        return _parse_media_output(proc.stdout or "")
    except Exception:  # noqa: BLE001 — detection is best-effort, never fatal
        return {}


def _choose_spill_drive(store_drive: str, media: dict[str, str], free_bytes,
                        min_free: float = _AUTO_SPILL_MIN_FREE_BYTES) -> str | None:
    """Pure decision (unit-testable off Windows): given the store's drive letter,
    a {letter: media_type} map, and a free-bytes(letter)->int probe, return the
    letter of the roomiest SSD to spill onto — or None to keep spill in-store.
    Relocate ONLY off a confirmed HDD onto a confirmed SSD with ≥ min_free free."""
    sd = (store_drive or "").upper()
    if media.get(sd, "").upper() != "HDD":
        return None  # store already fast (or type unknown): don't touch spill
    best: tuple[str, int] | None = None
    for dl, mt in media.items():
        dl = dl.upper()
        if dl == sd or mt.upper() != "SSD":
            continue
        try:
            free = free_bytes(dl)
        except OSError:
            continue
        if free >= min_free and (best is None or free > best[1]):
            best = (dl, free)
    return best[0] if best else None


def _auto_spill_base(store_dir: Path) -> Path | None:
    """Windows-only: if the store lives on a spinning HDD and a roomy SSD exists,
    return `<SSD>:\\mrfx_spill` for rollup scratch. None whenever we can't
    clearly improve things — a pure heuristic that is always safe to ignore."""
    media = _windows_drive_media()
    if not media:
        return None
    drive = os.path.splitdrive(os.path.abspath(str(store_dir)))[0].rstrip(":").upper()
    if len(drive) != 1 or not drive.isalpha():
        return None
    chosen = _choose_spill_drive(drive, media, lambda dl: shutil.disk_usage(f"{dl}:\\").free)
    return Path(f"{chosen}:\\mrfx_spill") if chosen else None


def replace_with_retry(src: Path, dst: Path, attempts: int = 12) -> None:
    """`src.replace(dst)` with retries. On Windows, replace/unlink raise
    PermissionError while ANY handle is open on the target without
    FILE_SHARE_DELETE — a dashboard query scanning the parquet glob, a running
    full-store CSV export (minutes-long!), or antivirus touching the fresh
    file. Failing a multi-hour parse at its very last step over a lock held by
    the app's OWN export is unacceptable, so the backoff budget (~90s total)
    is sized to outlive a big export scan, not just a 100ms AV probe.
    Re-raises after the final attempt — the caller's error handling applies."""
    for i in range(attempts):
        try:
            Path(src).replace(dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(min(0.2 * (2 ** i), 15.0))


def _fsync_path(p: Path) -> None:
    """Flush a finished file's bytes to the disk before a rename makes it
    live: a power loss (not a process kill) could leave a truncated part under
    a 'done' row — the app never fsynced anything (review 11 item 4.4b).
    Best effort: a failure here never fails the file."""
    try:
        fd = os.open(str(p), os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def unlink_with_retry(p: Path, attempts: int = 4) -> bool:
    """missing_ok unlink with the same Windows transient-lock tolerance as
    replace_with_retry. Returns False when the file is STILL THERE after the
    final attempt (a long reader holds it open) instead of raising — but
    callers for whom the deletion is load-bearing (forget: the honesty
    contract says removed data must actually be removed) MUST check the
    return value; a tmp-cleanup caller may ignore it (cosmetic leak)."""
    for i in range(attempts):
        try:
            Path(p).unlink(missing_ok=True)
            return True
        except PermissionError:
            if i == attempts - 1:
                return False
            time.sleep(0.2 * (i + 1))
    return True


def _nt_alive_without_handle(last_error: int) -> bool:
    """OpenProcess gave no handle: only ERROR_INVALID_PARAMETER (87) means
    "no such process". ACCESS_DENIED (5) — antivirus/EDR, an elevation
    mismatch, a protected process — means it EXISTS. Treating every failure as
    dead made a parse worker under EDR conclude its parent was gone at the
    first progress tick and abandon every file."""
    return last_error != 87


def _pid_alive(pid: int) -> bool:
    """Is `pid` a live process? Platform-aware: on Windows `os.kill(pid, 0)` is
    NOT a liveness probe — signal 0 is CTRL_C_EVENT, so it either delivers a
    real Ctrl-C to an unlucky recycled-pid console group or raises a plain
    OSError for a dead pid (which used to escape the orphan sweep and brick
    EVERY command after an interrupted parse). Unsure -> True: keeping a
    stranger's temp file is a cosmetic leak; deleting a live writer's temp is
    corruption."""
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            SYNCHRONIZE = 0x00100000
            h = k32.OpenProcess(SYNCHRONIZE, False, pid)
            if not h:
                return _nt_alive_without_handle(ctypes.get_last_error())
            try:
                # WAIT_TIMEOUT (0x102) = still running; WAIT_OBJECT_0 = exited
                return k32.WaitForSingleObject(h, 0) == 0x102
            finally:
                k32.CloseHandle(h)
        except Exception:  # noqa: BLE001 — can't tell: keep the file
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by another user
    except OSError:
        return True  # can't tell: keep the file
    # signal 0 succeeds on a ZOMBIE (exited, not yet reaped by its parent) —
    # it can't be writing anything, so its temp files are fair to sweep
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            state = f.read().rsplit(b")", 1)[1].split()[0]
        return state not in (b"Z", b"X")
    except (OSError, IndexError):
        return True


# --- SSN masking (§7A.7b) ----------------------------------------------------
# EINs and SSNs are both 9 digits. A 9-digit number is masked when its 2-digit
# prefix is not a valid IRS EIN campus prefix: it cannot be an EIN, so it is
# an SSN, an ITIN (9xx: as personal as an SSN) or junk, and none of those is
# shown raw. ONE rule for the screen (SQL) and the exports (Python): the
# Python rule used to also demand SSN structure (no 000/666/9xx area, no 00
# group, no 0000 serial), so 96x/97x/000-style numbers were masked on screen
# and printed raw in every CSV (round 6 P3).
_VALID_EIN_PREFIXES = {
    "01","02","03","04","05","06","10","11","12","13","14","15","16",
    "20","21","22","23","24","25","26","27","30","31","32","33","34","35",
    "36","37","38","39","40","41","42","43","44","45","46","47","48",
    "50","51","52","53","54","55","56","57","58","59","60","61","62","63",
    "64","65","66","67","68","71","72","73","74","75","76","77",
    "80","81","82","83","84","85","86","87","88","90","91","92","93","94",
    "95","98","99",
}
# the complement, for the SQL twins (api._mask_tin_sql, the no-name label)
SSN_PREFIXES = tuple(f"{i:02d}" for i in range(100) if f"{i:02d}" not in _VALID_EIN_PREFIXES)


def looks_like_ssn(tin: str | None) -> bool:
    if not tin or not re.fullmatch(r"\d{9}", tin):
        return False
    return tin[:2] not in _VALID_EIN_PREFIXES


def mask_tin(tin: str | None) -> str | None:
    """UI/export-safe TIN: SSN-pattern numbers are never shown raw."""
    if tin is None:
        return None
    return "MASKED-SSN" if looks_like_ssn(tin) else tin


def looks_like_ssn_text(text: str | None) -> bool:
    """A typed subject that is an SSN-pattern tax ID (with or without the
    dashes / spaces people type)."""
    t = re.sub(r"[\s-]", "", str(text or ""))
    return looks_like_ssn(t)


# a 9-digit run (or NNN-NN-NNNN) standing alone — never inside a word, a
# number or a base64 data URI (the brand logo)
_SSN_RUN = re.compile(r"(?<![\w+/=.,-])(\d{3}-\d{2}-\d{4}|\d{9})(?![\w+/=]|[.,]\d)")


def mask_ssn_text(text: str | None) -> str | None:
    """Invariant 4 at the last step of a document: every SSN-pattern tax ID
    in `text` (a report's HTML before it is sent, saved or printed to PDF, a
    folder name, a log line) becomes MASKED-SSN. An SSN prefix is never a
    valid EIN prefix, so a real EIN is never touched (review 14 item 2.5)."""
    if not text:
        return text

    def sub(m):
        d = m.group(1).replace("-", "")
        return "MASKED-SSN" if looks_like_ssn(d) else m.group(1)
    return _SSN_RUN.sub(sub, text)


def defuse_csv(v):
    """Excel/Sheets execute a cell starting with = + - @ (or tab/CR) as a
    formula. Org names, payer names, and descriptions come from third-party
    MRF/NPPES data and these CSVs are built to be opened in a spreadsheet, so
    prefix a risky leading character with a quote — it renders as text, never
    executes. (Shared twin of outreach._defuse / api._defuse_sql.)"""
    if isinstance(v, str) and v and v[0] in "=+-@\t\r":
        return "'" + v
    return v


# --- dedup / rollup queries ----------------------------------------------------

# NPI-grain dedup (drill-down + npi grain toggle)
# materialized rollups rebuild in hash-partitioned passes above this many raw
# rows — identical output, bounded memory/temp-disk per pass. This is the
# CEILING; the effective slice is also capped to fit the memory limit (see
# Store._rollup_partition_rows), so a small-RAM machine slices finer on its own.
ROLLUP_PARTITION_ROWS = 15_000_000
# The rollup's median/DISTINCT/list aggregates can't spill to disk, so peak RAM
# tracks rows-per-slice. Budget this many raw rows per GB of memory limit: keeps
# a slice's in-memory hash tables comfortably under the cap (≈15M rows at an
# 8 GB limit, matching the validated default; ≈8M at 4 GB). This is the
# SINGLE-THREAD budget — DuckDB keeps one hash table per thread, so the slice
# size is divided by the rebuild's thread count (see _rollup_partition_rows).
ROLLUP_ROWS_PER_GB = 2_000_000
# Parallelism cap for a rollup rebuild. The un-spillable per-thread hash tables
# mean peak RAM ≈ threads × slice-size, so unbounded threads on a many-core box
# multiply memory past the limit (→ OOM → the slow single-threaded fallback) AND
# over-partition (with only ~21 distinct billing codes, extra slices are wasted
# full scans). A moderate cap keeps the FIRST attempt both parallel (fast) and
# within budget: slices are sized for exactly this many threads, so the build
# fits on the first try instead of falling back. Kept low enough that
# threads × (slice ≈ memory_limit/threads) leaves comfortable headroom.
ROLLUP_MAX_THREADS = 4

DEDUP_QUERY = """
    SELECT payer, npi, billing_code,
           min(billing_code_type)                                    AS billing_code_type,
           min(discipline)                                           AS discipline,
           min(is_timed)                                             AS is_timed,
           coalesce(array_to_string(billing_code_modifier, '|'), '') AS modifier_set,
           negotiated_rate, billing_class,
           coalesce(array_to_string(service_code, '|'), '')          AS service_code_set,
           file_month,
           -- tin_value / tin_is_really_npi / is_dollar_rate are part of the
           -- GROUP BY key, NOT any_value/bool_or folds: an NPI billing the
           -- same rate under TWO practices is two facts (folding attributed
           -- the rate to an arbitrary TIN and hid the other association from
           -- drill-down and TIN search), and a dollar row must never merge
           -- with a same-numeric percentage row and swallow it
           tin_value, tin_is_really_npi, is_dollar_rate,
           min(negotiated_type)                                      AS negotiated_type,
           min(schema_version)                                       AS schema_version,
           max(last_updated_on)                                      AS last_updated_on,
           min(expiration_date)                                      AS expiration_date,
           count(DISTINCT source_file)                               AS source_count,
           -- min(), not any_value(), for every representative pick here: a
           -- re-dropped twin made any_value flip the shown file between two
           -- runs, so the CSV differed from the screen (invariant 7)
           -- representative file, not the full list: string_agg(DISTINCT)
           -- over tens of millions of near-unique groups cannot spill and
           -- explodes memory/temp disk; source_count carries the cardinality
           -- and the raw rates table has the complete list on demand
           min(source_file)                                          AS source_files,
           1                                                         AS rate_variants,
           1                                                         AS npi_count
    FROM rates
    -- TIN-only rows (npi IS NULL) live at the TIN grain only; excluding them
    -- here keeps the NPI grain from showing a phantom "no-NPI" unit.
    WHERE npi IS NOT NULL
      -- a non-finite rate is not a rate (round 7 P0.1: parts ingested before
      -- clean_rate refused "nan"/"Infinity" strings may still hold them)
      AND (negotiated_rate IS NULL OR isfinite(negotiated_rate))
    GROUP BY payer, npi, billing_code,
             coalesce(array_to_string(billing_code_modifier, '|'), ''),
             negotiated_rate, billing_class,
             coalesce(array_to_string(service_code, '|'), ''), file_month,
             tin_value, tin_is_really_npi, is_dollar_rate
"""

# TIN-grain spine (§4). Distinct rates within the tuple become rate_variants;
# negotiated_rate is the median of the distinct values (min/max kept).
# {part} is a partition predicate (TRUE for a single-shot build): billing_code
# is part of the GROUP BY key, so building the table in hash(billing_code)
# slices yields byte-identical results at a fraction of the memory/temp-disk
# peak — the DISTINCT/median aggregates are what exhausted a 23 GB temp dir
# on a 64M-row store when run in one shot.
# bump when a RATE-SPINE (rates_by_tin) GROUP BY / column-semantics change must
# reach EXISTING stores (same columns, different grain -> the missing-column
# probe can't see it). v2: tin_is_really_npi joined the BY_TIN grain (identity
# blending fix). Bumping this forces the hours-long WHOLE-STORE pass, so only
# bump it for a real spine change — a directory-only change uses
# DIRECTORY_SCHEMA_VERSION below.
ROLLUP_SCHEMA_VERSION = 2

# bump when only the DIRECTORY's column semantics change (is_therapy rules,
# name/geo derivation). Rebuilding tin_directory alone is minutes, not hours, so
# this must NOT ride on ROLLUP_SCHEMA_VERSION: doing that made a therapy-filter
# tweak re-aggregate the entire 300M-row rate spine and blocked serve (and even
# `mrfx status`) inside Store.__init__ until it finished.
# v2: is_therapy tightened — share measured over identified PROVIDERS (non-therapy
# org NPIs no longer dilute), 75% threshold, enrichment-coverage test whose
# small-clinic escape needs the unambiguous PT-clinic code, and the facility veto
# widened past hospitals (SNF/home health/hospice/residential/schools/agencies).
# v3: clinic taxonomy codes corrected against the authoritative NUCC set —
# 261QP2300X is PRIMARY CARE, not Physical Therapy (every primary-care clinic was
# counting as a therapy practice); the real PT code 261QP2000X replaces it, and
# Hearing-and-Speech / Developmental-Disabilities / CORF clinics were added so
# speech and pediatric therapy clinics stop being excluded.
# v5: a therapy taxonomy in ANY of NPPES's 15 slots now counts, not just the
# primary/slot-1 one. is_therapy is MATERIALIZED, so without this bump an
# existing store would keep serving the old primary-only verdicts (measured on
# the live registry: 9% of real therapy providers misclassified) until some
# unrelated ingest happened to rebuild the directory.
# v6: deterministic practice names / primary discipline / tin_type (ties broken
# alphabetically instead of by thread arrival) — invariant 7; + primary_state
# (the entity grain's organization key).
DIRECTORY_SCHEMA_VERSION = 6

# Sentinel for upsert_file(finished_at=...): "stamp the completion time INSIDE
# the write lock". Callers must never pre-compute a done-timestamp in Python —
# the lock wait between computing it and committing can put the value behind
# the rollup coverage marker (see upsert_file).
FINISHED_NOW = object()

# Rules that decide what a STORED rate row looks like. Bumping this makes every
# part written under an older rule be rewritten once at open (_migrate_row_rules)
# — exactly as a re-ingest would write it, no re-download. 1: modifier field
# (mrfx/modifiers.py). 2: + a header dated after the file was read is not its
# month (parser.file_month_for). 3: negotiated_type spelling ("fee_schedule"
# is "fee schedule", a dollar rate — parser.normalize_negotiated_type), and
# files that lost items as "bundled" under the old arrangement rule are
# flagged for a re-ingest (those items are not in the part to rewrite).
# 4: rule 3 VERIFIED — 0.7.2 stamped a part 3 even when its scan result could
# not be matched back to it (review 9 item 1.1), so every store it touched is
# re-checked once; a part is stamped only after its own types were read.
ROW_RULES_VERSION = 4

# parts per query of the stored-row type check: small enough that the
# progress line moves on a slow drive, large enough to amortise the query
_TYPE_SCAN_BATCH = 40
# files per budget check of the one-time stored-row update (review 12 item 2.9)
_MIGRATE_SLICE = 200
# how long `mrfx serve` lets that update run before the dashboard opens
MIGRATION_BUDGET_S = 30.0


# a paused row that is NOT a copy waiting on an earlier row still in flight
# (early duplicate check): only those may be woken
_NOT_WAITING = ("(waiting_on IS NULL OR waiting_on NOT IN (SELECT q.id FROM url_queue q "
                "WHERE q.status IN ('downloading', 'fetched', 'ingesting')))")


def _utcnow() -> dt.datetime:
    """THE timestamp for anything stored: naive UTC. An AWARE datetime lands in
    a DuckDB TIMESTAMP column as the PC's LOCAL wall-clock time (06:30Z ->
    01:30 under New York), and local time runs backwards once a year — on the
    fall-back night a file finishing 30 real minutes after the rollup marker
    got an EARLIER stamp and never reached the analytics. Naive UTC never
    goes backwards (and the instance runs SET TimeZone='UTC' as well)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

# Returned by a skip_if_busy rebuild that found another rebuild already running.
# The periodic triggers (ingest rollup, name refresh) pass skip_if_busy=True so
# they DON'T queue behind a multi-hour rebuild and then each run their own
# multi-hour pass in turn (the observed "waiting 388 min" pile-up that reads as
# an infinite loop). The running rebuild covers the current data; the next
# scheduled trigger catches anything that landed after it started.
ROLLUP_SKIPPED = -1.0


class RollupNeedsFull(RuntimeError):
    """The incremental update's PRECONDITION does not hold (no prior full
    build, rule versions moved, a removal that could not be scoped, rows with
    no payer …): a full build is the right answer, and the message says which
    precondition failed (review 14 item 1.1d). Anything ELSE an incremental
    raises — out of memory at its finest slicing, a disk error — is a failure
    to retry, never a reason to rebuild the whole store."""


class RollupPlanPending(RollupNeedsFull):
    """A durable build plan is part-way: it must finish first (a `names` plan
    resumes as a names plan — review 14 Group 3 — a `full` one as full)."""

    def __init__(self, kind: str):
        super().__init__(f"an analytics build ({kind}) is part-way — it finishes first")
        self.kind = kind


class RollupPaused(Exception):
    """The user paused the analytics (review 14 item 1.2): a running build
    stops between two committed parts and continues when resumed. Not a
    failure — nothing is noted, nothing backs off."""


def _run_cancelled() -> bool:
    """Was the current run stopped (its Progress cancelled)? THE rule for
    every cache (review 14 item 2.9): a result computed under a Stop is never
    stored. Never raises."""
    try:
        from . import progress
        return bool(getattr(progress.current(), "cancelled", False))
    except Exception:  # noqa: BLE001
        return False


def _needs_full_set(token: str | None) -> bool:
    """`rollup_needs_full` holds '1' (written before 0.8.0) or '1:<id>' — a
    fresh id per raise, so a plan clears only the raise it started with
    (review 14 item 2.6)."""
    return bool(token) and str(token).startswith("1")


def _needs_full_token() -> str:
    return "1:" + os.urandom(6).hex()


# a loaded twin counts only in the candidate's own month (review 14 Group 3,
# decided): the same document under a name that gives ANOTHER month is that
# month's publication. Params: (month, month); NULL on either side matches.
_SAME_MONTH = "(? IS NULL OR f.file_month IS NULL OR f.file_month = ?)"


def _directory_query(src: str = "rates") -> str:
    """TIN_DIRECTORY_QUERY reading `src` instead of the whole `rates` view —
    the same rows for the TINs its {part} predicate selects, as long as `src`
    holds every part carrying those TINs (review 14 item 1.1e)."""
    if src == "rates":
        return TIN_DIRECTORY_QUERY
    return re.sub(r"\bFROM rates\b(?=\s)", f"FROM {src}", TIN_DIRECTORY_QUERY)

BY_TIN_QUERY = """
    SELECT payer, tin_value, billing_code,
           min(tin_type)                                             AS tin_type,
           tin_is_really_npi,
           min(billing_code_type)                                    AS billing_code_type,
           min(discipline)                                           AS discipline,
           min(is_timed)                                             AS is_timed,
           coalesce(array_to_string(billing_code_modifier, '|'), '') AS modifier_set,
           billing_class,
           coalesce(array_to_string(service_code, '|'), '')          AS service_code_set,
           file_month, is_dollar_rate,
           -- round the aggregate to 4 dp: kills float-median noise
           -- (86.835000000001) while preserving any legitimate sub-cent median.
           -- Exclude $0/$0.01/negative DOLLAR placeholders from the median so a
           -- TIN that published both a placeholder AND a real rate for the same
           -- code isn't dragged to their midpoint (median of 0.01 and 85 = 42.5).
           -- is_dollar_rate is a GROUP BY key, so the FILTER is a no-op for
           -- non-dollar groups. coalesce keeps a placeholder-ONLY group showing
           -- its value (so /api/rates still lists it and the hide-outliers
           -- toggle still masks it) rather than becoming NULL.
           round(coalesce(
               median(DISTINCT negotiated_rate) FILTER (NOT is_dollar_rate OR negotiated_rate > 0.01),
               median(DISTINCT negotiated_rate)
           ), 4)                                                     AS negotiated_rate,
           round(min(negotiated_rate), 4)                            AS rate_min,
           round(max(negotiated_rate), 4)                            AS rate_max,
           count(DISTINCT negotiated_rate)                           AS rate_variants,
           count(DISTINCT npi)                                       AS npi_count,
           count(DISTINCT source_file)                               AS source_count,
           min(source_file)                                          AS source_files,
           min(negotiated_type)                                      AS negotiated_type,
           min(schema_version)                                       AS schema_version,
           max(last_updated_on)                                      AS last_updated_on,
           min(expiration_date)                                      AS expiration_date
    FROM rates
    WHERE {part}
      -- a non-finite rate is not a rate: DuckDB orders NaN above every
      -- number, so the median FILTER below kept it and the practice's
      -- median became NaN (round 7 P0.1; clean_rate refuses them at ingest)
      AND (negotiated_rate IS NULL OR isfinite(negotiated_rate))
    GROUP BY payer, tin_value, billing_code,
             coalesce(array_to_string(billing_code_modifier, '|'), ''),
             billing_class,
             coalesce(array_to_string(service_code, '|'), ''),
             file_month, is_dollar_rate,
             -- part of the GRAIN, not bool_or-folded: a 9-digit value typed
             -- 'npi' colliding with a real EIN string must stay a SEPARATE
             -- row, or the blend's median matches neither identity and the
             -- NOT tin_is_really_npi market filter hides both (the EIN
             -- practice silently vanished from every benchmark surface)
             tin_is_really_npi
    -- CLUSTER the materialized spine by (billing_code, tin_value): DuckDB keeps
    -- per-row-group min/max zonemaps, so a code-filtered query (summary,
    -- benchmark, leads, /api/code — nearly every real dashboard query carries a
    -- billing_code IN filter) skips the row groups that can't match instead of
    -- scanning the whole spine. Measured 20-60x on a code-filtered aggregate
    -- over 30M rows (80ms -> 2ms). ORDER BY is a SPILLABLE operator over the
    -- (already grouped, smaller) output, so it never breaks the bounded-memory
    -- slice build; each hash slice sorts independently and still prunes because
    -- pruning is per-row-group, not global. Rows are byte-identical as a SET —
    -- only their physical order changes, and every reader re-aggregates.
    ORDER BY billing_code, tin_value
"""

# The well-formed NPIs a rate relation references (an NPI in the TIN slot
# included) — the population name enrichment works through
# (Store.enrichment_progress). {src} is `rates` or one part's read_parquet().
_RATE_NPIS_SQL_T = """
    SELECT DISTINCT npi FROM (
        SELECT npi FROM {src}
        UNION
        SELECT tin_value AS npi FROM {src}
        WHERE tin_is_really_npi AND tin_value IS NOT NULL
    ) WHERE regexp_full_match(npi, '[0-9]{{10}}')"""
_RATE_NPIS_SQL = _RATE_NPIS_SQL_T.format(src="rates")

# "Latest" = each CONTRACT's newest month, a contract being a payer x provider x
# code line (benchmark._rates_relation explains the grain). tin_is_really_npi is
# part of the identity: an EIN and a same-digits npi-typed TIN never supersede
# each other.
LATEST_PARTITION = "payer, tin_value, tin_is_really_npi, billing_code"

# The MATERIALIZED spine = BY_TIN_QUERY + `is_latest`: TRUE on the rows of each
# contract's newest month. Every "latest" screen used to work this out per
# request with `QUALIFY file_month = max(file_month) OVER (PARTITION BY ...)` —
# a window over the WHOLE spine (plus the name joins under it) on every click:
# 4.4 s of WINDOW + 1.6 s of projection on the real 1.41M-row spine; on a
# 99M-rate store the Explorer's default view ran out of memory. Stored,
# "latest" is `WHERE is_latest`, a scan filter.
# How the build sets it: each slice statement derives its contracts' newest
# month from its OWN aggregated rows (a CTE: GROUP BY max + hash join) — exact
# because no slice ever splits a contract (the full build slices by
# hash(billing_code || tin_value), a delta takes whole payers, and every row of
# a contract shares payer, code and TIN). Measured at 99M rates: full rebuild
# 577 s -> 619 s, payer delta 49 s either way. Rejected after measuring: a
# window per slice (+31% on the full rebuild), a join to one store-wide
# newest-month table (+53% per slice), and an UPDATE after the inserts — it
# marked 0 of 79M rows: in DuckDB 1.5.5/1.5.6, after the build's (then) empty
# CTAS, rows an INSERT … ORDER BY adds are invisible to EVERY statement until a
# CHECKPOINT (round 5 found the mechanism and fixed the build itself; see
# _build_rollup_tables and tests/mrfx/test_rollup_visibility.py).
# NOT part of the GROUP BY grain, so no ROLLUP_SCHEMA_VERSION bump.
_BY_TIN_BODY = BY_TIN_QUERY.rsplit("ORDER BY billing_code, tin_value", 1)[0]
_NEWEST_JOIN = " AND ".join(f"n.{c} IS NOT DISTINCT FROM spine.{c}"
                            for c in LATEST_PARTITION.split(", "))
SPINE_QUERY = (
    # the newline before ')' is load-bearing: the body's last line is a comment
    f"WITH spine AS ({_BY_TIN_BODY}\n),\n"
    f"newest AS (SELECT {LATEST_PARTITION}, max(file_month) AS newest\n"
    f"           FROM spine GROUP BY {LATEST_PARTITION})\n"
    "SELECT spine.*, coalesce(spine.file_month = n.newest, FALSE) AS is_latest\n"
    f"FROM spine LEFT JOIN newest n ON {_NEWEST_JOIN}\n"
    "ORDER BY spine.billing_code, spine.tin_value\n")
# A store with no materialized spine yet serves this live view: the flag is
# the window itself (tiny stores only — the first build materializes).
SPINE_LIVE_QUERY = (
    "SELECT spine.*, coalesce(file_month = max(file_month) OVER "
    f"(PARTITION BY {LATEST_PARTITION}), FALSE) AS is_latest\n"
    f"FROM ({_BY_TIN_BODY}\n) spine\n")


# TIN directory (§3.3a): display name from NPPES org names of the TIN's
# Type-2 NPIs (mode); individual-billed TINs labeled by dominant person name.
# {part} partitions on tin_value (every group key below is a single TIN, so
# hash(tin_value) slices rebuild to identical results) — see BY_TIN_QUERY.
TIN_DIRECTORY_QUERY = """
    WITH tin_npis AS (
        SELECT DISTINCT tin_value, npi
        FROM rates
        WHERE tin_value IS NOT NULL AND NOT tin_is_really_npi AND {part}
    ),
    joined AS (
        SELECT t.tin_value, t.npi, d.entity_type, d.org_name, d.state, d.city,
               d.taxonomy_code, d.taxonomy_codes
        FROM tin_npis t LEFT JOIN npi_directory d USING (npi)
    ),
    -- The practice NAME is the most common organization name among the TIN's
    -- NPIs — with ties broken ALPHABETICALLY. mode() broke ties by thread
    -- arrival: a TIN whose NPI-2s split evenly between a parent name and a
    -- per-site entity got a different name on each rebuild (a reviewer's
    -- probe: 5 different results in 6 runs) — on a rate card that is sold.
    name_counts AS (
        SELECT tin_value, org_name,
               count(*) FILTER (entity_type = 'NPI-2') AS c_org,
               count(*)                                AS c_any
        FROM joined WHERE org_name IS NOT NULL
        GROUP BY tin_value, org_name
    ),
    names AS (
        SELECT o.tin_value, o.n_orgs, nm.org_mode, nm.any_mode
        FROM (SELECT tin_value, count(*) FILTER (entity_type = 'NPI-2') AS n_orgs
              FROM joined GROUP BY tin_value) o
        LEFT JOIN (
            SELECT tin_value,
                   first(org_name ORDER BY c_org DESC, org_name) FILTER (c_org > 0) AS org_mode,
                   first(org_name ORDER BY c_any DESC, org_name)                    AS any_mode
            FROM name_counts GROUP BY tin_value) nm USING (tin_value)
    ),
    -- the practice's MAIN state: where most of its NPIs are (ties
    -- alphabetical). The entity grain keys an organization on name + this
    -- state — the full state list split one org whose TINs' NPI lists
    -- reached different neighbouring states.
    pstate AS (
        SELECT tin_value, first(state ORDER BY c DESC, state) AS primary_state
        FROM (SELECT tin_value, state, count(*) AS c FROM joined
              WHERE state IS NOT NULL GROUP BY tin_value, state)
        GROUP BY tin_value
    ),
    disc AS (
        -- same rule: most rows, ties alphabetical
        SELECT tin_value, first(discipline ORDER BY c DESC, discipline) AS primary_discipline
        FROM (SELECT tin_value, discipline, count(*) AS c
              FROM rates WHERE tin_value IS NOT NULL AND discipline != 'unspecified' AND {part}
              GROUP BY tin_value, discipline)
        GROUP BY tin_value
    )
    SELECT j.tin_value,
           -- min, not any_value: a TIN published as both 'ein' and another
           -- type must read the same on every rebuild
           min(r.tin_type)                                          AS tin_type,
           -- the no-name fallback label must never surface a raw SSN-pattern
           -- TIN: display_name renders in the UI and every export
           coalesce(any_value(n.org_mode), any_value(n.any_mode),
                    'TIN ' || CASE WHEN regexp_full_match(j.tin_value, '[0-9]{{9}}')
                                        AND substr(j.tin_value, 1, 2) IN
                                        ('00','07','08','09','17','18','19','28','29',
                                         '49','69','70','78','79','89','96','97')
                                   THEN 'MASKED-SSN' ELSE j.tin_value END) AS display_name,
           CASE WHEN any_value(n.n_orgs) > 0 THEN 'org'
                ELSE 'individual-billed' END                        AS entity_kind,
           count(DISTINCT j.npi)                                    AS npi_count,
           list_sort(list_distinct(list(j.state)
                     FILTER (j.state IS NOT NULL)))                 AS states,
           any_value(ps.primary_state)                              AS primary_state,
           list_sort(list_distinct(list(j.city)
                     FILTER (j.city IS NOT NULL)))                  AS cities,
           any_value(d2.primary_discipline)                         AS primary_discipline,
           -- STRICT outpatient-therapy-practice test. A TIN qualifies only when
           -- ALL of these hold:
           --   (a) SHARE: at least {min_share}% of its taxonomy-IDENTIFIED NPIs are
           --       PT/OT/SLP (incl. PTA/OTA) or a therapy-clinic org NPI. A bare
           --       majority (the old rule) admitted genuinely mixed groups — a
           --       chiropractic or physician office with two therapists on staff
           --       read as a "therapy practice".
           --   (b) COVERAGE: at least half its NPIs are identified, OR it carries a
           --       therapy-CLINIC org NPI. Without this, a 50-provider physician
           --       group with 49 not-yet-identified NPIs and ONE identified PT
           --       scored 100% therapy and sailed through — the single biggest
           --       leak, and worst early in enrichment. The clinic-org escape
           --       keeps a genuinely small therapy clinic visible while its
           --       individual NPIs are still being identified.
           --   (c) NOT a hospital/health system, and NOT a non-outpatient facility
           --       (SNF, home health, residential, hospice…): those employ
           --       therapists but are not outpatient practices, so ANY such member
           --       NPI disqualifies the TIN.
           -- The therapy-clinic org code no longer BLANKET-qualifies a TIN (it
           -- still counts toward the share numerator): physiatry and multispecialty
           -- rehab groups carry 261QR0400X too, and the old bool_or bypass let
           -- every one of them in regardless of how MD-heavy they were.
           -- Enrichment-dependent by design: NULL taxonomies count toward neither
           -- side of the share, so the flag sharpens as identification lands.
           -- Counted on CLINICIANS (NPI-1 / unknown), never on org NPIs. A group
           -- that registers one org NPI per location would otherwise buy itself a
           -- "therapist" per site: measured live, 3 PT-clinic site orgs + 3 PTs +
           -- 5 MDs passed, and so did 3 site orgs + 1 MD with ZERO therapists at
           -- all. Org NPIs contribute as EVIDENCE (the strong-clinic signal and
           -- the org-only-practice case below), not as headcount.
           count(DISTINCT j.npi) FILTER (
               coalesce(j.entity_type, 'NPI-1') <> 'NPI-2'
               AND {therapy})                                       AS therapy_npi_count,
           count(DISTINCT j.npi) FILTER (
               coalesce(j.entity_type, 'NPI-1') <> 'NPI-2'
               AND j.taxonomy_code IS NOT NULL)                     AS classified_npi_count,
           coalesce(bool_or({hospital}), FALSE)                     AS has_hospital,
           coalesce(bool_or({excluded}), FALSE)                     AS has_facility,
           -- ONE population, measured consistently: the TIN's CLINICIANS, i.e.
           -- every NPI that isn't a known organization. An org (NPI-2) code —
           -- "Multi-Specialty Group", a DME/orthotics arm, a per-location billing
           -- shell — is the practice's paperwork, not a person, so it belongs on
           -- NEITHER side of the share NOR in the coverage denominator.
           -- Mixing populations was itself the bug: counting org NPIs in the
           -- numerator handed multi-site groups a phantom therapist per location,
           -- and measuring coverage over ALL NPIs while the share ignored orgs
           -- meant each added shell bought one more unidentified NPI (measured:
           -- 1 PT + 100 shells + 100 unknowns qualified).
           -- coalesce(entity_type,'NPI-1'): an UNKNOWN entity type must count as a
           -- clinician (dilute), not as a shell. Without it, `NULL = 'NPI-2'` is
           -- NULL, the FILTER drops the row, and an MD whose entity type wasn't
           -- recorded stopped diluting — quietly re-loosening the rule.
           (
               -- (a) SHARE: {min_share}% of identified clinicians are PT/OT/SLP
               (count(DISTINCT j.npi) FILTER (
                        coalesce(j.entity_type, 'NPI-1') <> 'NPI-2'
                        AND j.taxonomy_code IS NOT NULL) > 0
                AND (count(DISTINCT j.npi) FILTER (
                        coalesce(j.entity_type, 'NPI-1') <> 'NPI-2' AND {therapy})) * 100
                    >= (count(DISTINCT j.npi) FILTER (
                        coalesce(j.entity_type, 'NPI-1') <> 'NPI-2'
                        AND j.taxonomy_code IS NOT NULL)) * {min_share})
               -- (b) a DEFINITE therapy clinic ("Clinic/Center - Physical Therapy"
               --     and friends, never the ambiguous rehab code) needs only HALF
               --     its clinicians: a two-therapist clinic with an NP on staff is
               --     still a therapy clinic.
               OR (coalesce(bool_or({strong_clinic}), FALSE)
                   AND count(DISTINCT j.npi) FILTER (
                       coalesce(j.entity_type, 'NPI-1') <> 'NPI-2'
                       AND j.taxonomy_code IS NOT NULL) > 0
                   AND (count(DISTINCT j.npi) FILTER (
                       coalesce(j.entity_type, 'NPI-1') <> 'NPI-2' AND {therapy})) * 2
                       >= count(DISTINCT j.npi) FILTER (
                           coalesce(j.entity_type, 'NPI-1') <> 'NPI-2'
                           AND j.taxonomy_code IS NOT NULL))
               -- (c) a SMALL practice with NO identified clinician yet, known only
               --     by an org NPI that is either an unambiguous therapy CLINIC or
               --     an incorporated sole proprietor still carrying their
               --     PRACTITIONER taxonomy. This is the ordinary early-enrichment
               --     state of a real one-site clinic. Bounded in size, and it must
               --     be a strong code — the ambiguous "Clinic/Center -
               --     Rehabilitation" org NPI standing alone is exactly the
               --     physiatry / hospital-outpatient population we exclude — and
               --     nothing identified may contradict it.
               OR ((coalesce(bool_or({strong_clinic}), FALSE)
                    OR coalesce(bool_or({therapy_practitioner}), FALSE))
                   AND count(DISTINCT j.npi) FILTER (
                       coalesce(j.entity_type, 'NPI-1') <> 'NPI-2'
                       AND j.taxonomy_code IS NOT NULL) = 0
                   AND count(DISTINCT j.npi) FILTER (
                       j.taxonomy_code IS NOT NULL AND NOT {therapy}) = 0
                   AND count(DISTINCT j.npi) <= {small_practice})
           )
           -- COVERAGE: half the TIN's CLINICIAN slots identified, OR it is a SMALL
           -- practice carrying an unambiguous therapy-clinic org code. The escape
           -- MUST use the strong code and MUST be size-bounded: allowing the
           -- ambiguous rehab-clinic code here (or any size) let a physiatry group /
           -- hospital outpatient rehab dept whose ONLY identified NPI was its
           -- "Clinic/Center - Rehabilitation" org NPI score 100% therapy and skip
           -- coverage entirely — reopening the exact leak this rule exists to close.
           AND ((count(DISTINCT j.npi) FILTER (
                        coalesce(j.entity_type, 'NPI-1') <> 'NPI-2'
                        AND j.taxonomy_code IS NOT NULL)) * 2
                    >= count(DISTINCT j.npi) FILTER (
                        coalesce(j.entity_type, 'NPI-1') <> 'NPI-2')
                OR (coalesce(bool_or({strong_clinic}), FALSE)
                    AND count(DISTINCT j.npi) <= {small_practice}))
           AND NOT coalesce(bool_or({excluded}), FALSE)             AS is_therapy
    FROM joined j
    LEFT JOIN names n ON n.tin_value = j.tin_value
    LEFT JOIN disc d2 ON d2.tin_value = j.tin_value
    LEFT JOIN pstate ps ON ps.tin_value = j.tin_value
    LEFT JOIN (SELECT DISTINCT tin_value, tin_type FROM rates WHERE {part}) r
           ON r.tin_value = j.tin_value
    GROUP BY j.tin_value
    -- CLUSTER the directory by tin_value so the point lookups that dominate it
    -- (entity-detail WHERE tin_value=?, the per-row LEFT JOINs from rates_by_tin /
    -- monitor / outreach / benchmark, resolve_subject_tins) prune to a single
    -- row group via the zonemap instead of scanning the whole directory — the
    -- same free pruning an ART index would give, but carried in the build query
    -- so it survives every CREATE OR REPLACE with no index to maintain. Spillable
    -- sort over the grouped output; rows are byte-identical as a set.
    ORDER BY j.tin_value
"""
# inline the taxonomy tests now (their identifiers are constants); {part}
# stays for the per-partition .format() at rebuild time.
TIN_DIRECTORY_QUERY = (
    TIN_DIRECTORY_QUERY
    .replace("{therapy}", therapy_taxonomy_sql("j.taxonomy_code"))
    .replace("{hospital}", hospital_taxonomy_sql("j.taxonomy_code"))
    # hospital + every other non-outpatient facility class: any member NPI
    # carrying one disqualifies the TIN from the strict practice filter
    .replace("{excluded}", excluded_facility_taxonomy_sql("j.taxonomy_code"))
    # clinic-org codes alone (no PT/OT/SLP prefixes): satisfies the COVERAGE
    # test for a small therapy clinic whose individual NPIs aren't identified
    # yet — it no longer bypasses the share test
    # unambiguous PT-clinic org code only (never the ambiguous rehab-clinic one).
    # Used for BOTH escapes — the share escape and the coverage escape; wiring
    # the coverage escape to the broader {clinic} set let a rehab-clinic org NPI
    # alone qualify a whole physiatry/hospital-outpatient TIN.
    .replace("{strong_clinic}", therapy_taxonomy_sql(
        "j.taxonomy_code", prefixes=(), codes=THERAPY_CLINIC_STRONG_CODES))
    # PRACTITIONER therapy taxonomies only (Physical Therapist, OTA, SLP… — a
    # person's credential), never the Clinic/Center org codes. An incorporated
    # sole proprietor gets a Type-2 NPI that still carries their PRACTITIONER
    # taxonomy — that is a therapist, not a billing shell — so it must be able to
    # stand in for a clinician at a one-person practice.
    .replace("{therapy_practitioner}", therapy_taxonomy_sql(
        "j.taxonomy_code", codes=()))
    .replace("{min_share}", str(THERAPY_MIN_SHARE_PCT))
    .replace("{small_practice}", str(THERAPY_SMALL_PRACTICE_NPIS))
)


# Every path that puts a finished / stopped / paused row back in the queue
# resets the row's per-attempt bookkeeping through THIS fragment (review 11
# item 4.1: each path kept a different subset — a spent auto_resumes made
# every later stall an instant failure (2.6), a kept park_count shortened the
# next park budget, a kept 'duplicate' kind made a user-skipped row eligible
# for automatic revival, a kept force_size outlived its one shot). The error
# text, `retries` and `force_size` (download anyway) are set by the caller.
REQUEUE_RESET = (
    "progress = 0, bytes_done = 0, retry_after = NULL, park_count = 0, "
    "waiting_on = NULL, auto_resumes = 0, big_lane = false, finished_at = NULL, "
    "duplicate_of = NULL, kind = CASE WHEN kind IN ('duplicate', 'canceled') "
    "THEN 'unknown' ELSE kind END")


class Store:
    def __init__(self, store_dir: Path, memory_limit_gb: int | None = None,
                 keep_warm: bool = False, temp_dir: Path | None = None,
                 auto_spill: bool = False, migrate_budget_s: float | None = None):
        self.dir = Path(store_dir)
        # the one-time stored-row update (_migrate_row_rules): None = all of
        # it before the store is handed over (every CLI command); a number of
        # seconds = at most that long before the dashboard opens, the rest in
        # the background (`mrfx serve` — review 12 item 2.9)
        self._migrate_budget_s = migrate_budget_s
        self.rows_update: dict | None = None
        self.rates_dir = self.dir / "rates"
        self.rates_dir.mkdir(parents=True, exist_ok=True)
        # DuckDB spill directory. Defaults inside the store; the spill is what
        # stalls parser workers during a rollup, so on a slow-HDD store it is
        # routed to a fast disk — either an explicit duckdb_temp_dir (temp_dir)
        # or, when that's unset, an auto-detected roomy SSD (Windows only).
        # self.spill_is_relocated lets the serve banner report where it landed.
        self.spill_is_relocated = False
        self.spill_is_auto = False
        self.spill_fallback_from: str | None = None  # set when an explicit dir was unusable
        spill_base = temp_dir
        # auto_spill only for the rollup-heavy entry points (serve, ingest): the
        # detection probe (a PowerShell call on Windows) isn't worth paying on
        # every quick read-only command like `status`/`export`.
        if spill_base is None and auto_spill:
            try:
                spill_base = _auto_spill_base(self.dir)
                self.spill_is_auto = spill_base is not None
            except Exception:  # noqa: BLE001 — auto-detection must never be fatal
                spill_base = None
        if spill_base is not None:
            try:
                # per-store subfolder: two stores (the real one and a trial)
                # sharing ONE spill base must not share a spill directory —
                # DuckDB spill file names are per-instance counters, not unique.
                tag = hashlib.sha1(str(self.dir.resolve()).encode()).hexdigest()[:10]
                cand = Path(spill_base) / f"spill-{tag}"
                cand.mkdir(parents=True, exist_ok=True)
                self._tmp_dir = cand
                self.spill_is_relocated = True
            except OSError:
                logging.getLogger(__name__).warning(
                    "spill dir %r is not usable — falling back to the in-store "
                    "spill folder", str(spill_base))
                self._tmp_dir = self.dir / "duckdb_tmp"
                if not self.spill_is_auto:
                    # an EXPLICIT duckdb_temp_dir silently falling back would
                    # leave the user believing their config took — surface it
                    self.spill_fallback_from = str(spill_base)
                self.spill_is_auto = False
        else:
            self._tmp_dir = self.dir / "duckdb_tmp"
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "mrfx.duckdb"
        self.write_lock = threading.Lock()
        # "the URL queue changed": the download/parse worker threads sleep on
        # this instead of re-querying the queue every half second (round 5:
        # 9 idle threads made ~30 DuckDB queries and 18 lock grabs a second,
        # 7% of a core, for weeks). Bumped by every queue writer
        # (_QUEUE_WRITERS below); waiters re-check with a long fallback.
        self._queue_cond = threading.Condition()
        self._queue_epoch = 0
        # url_ids the user asked to cancel mid-download ("stop downloads"): the
        # in-flight downloader thread polls is_download_canceled() and aborts.
        # In-memory (same process as the workers); cleared once the download
        # sees it and stops.
        self._canceled_downloads: set[int] = set()
        self._cancel_lock = threading.Lock()
        # filenames the user asked to STOP mid-parse (the Files tab's "stop"):
        # the in-process parse checks it on every read; for a worker process
        # the parent's wait loop turns it into a sentinel file the worker
        # polls. Cleared once the parse has stopped.
        self._stopped_parses: set[str] = set()
        # content (sha256) -> filename of an INBOX file being loaded right now
        # (review 11 item 1.1). The link path's in-flight claim is the
        # url_queue row ('ingesting' + content_sha); an inbox load has no
        # queue row, so its claim lives here — read and written ONLY under
        # write_lock, by claim_inbox_content and claim_content_ingest, so a
        # link and an inbox copy of the same bytes can never both win. In
        # memory on purpose: a crash ends every load it could describe.
        self._inbox_claims: dict[str, str] = {}
        # CONTENT sha (inflated) -> name of a file being loaded right now
        # (review 13 item 5.2.1), beside the raw-byte claims above
        self._content_claims: dict[str, str] = {}
        # content sha -> the url_queue id that holds the claim (a LINK's); an
        # inbox file's claim maps to None. A claim whose link is not loading
        # in THIS process any more is no owner (review 14 item 2.1).
        self._content_claim_owner: dict[str, int | None] = {}
        # payer -> how many provider-reference files finished in THIS process
        # (review 13 item 5.2.7): a rate file that started before its payer's
        # list landed compares it at its end and is read again when it moved
        self._refs_epoch: dict[str, int] = {}
        # url_queue ids THIS process set to 'ingesting' (a processor claim or a
        # won content claim). A row 'ingesting' that is not here was left by a
        # process that died: no load is running for it, so an inbox copy of
        # its bytes must not wait on it (review 12 item 1.3 — `mrfx ingest`
        # waited forever). Read/written under write_lock.
        self._live_url_ingests: set[int] = set()
        # Rollup rebuilds serialize against EACH OTHER here — NOT against the
        # write_lock. Holding write_lock for a rebuild froze the whole
        # pipeline: claiming a queue row (downloaders AND parsers) needs
        # write_lock, so a slow 86M-row directory rebuild stopped every
        # download and every parse dispatch for its full duration (seen live:
        # 12 threads parked on write_lock behind one enrichment rebuild, CPU
        # burning, zero extraction). Rebuild transactions touch only the
        # rollup tables; the small writers touch url_queue/files/etc — DuckDB
        # commits disjoint-table transactions concurrently without conflict.
        self.rollup_lock = threading.Lock()
        # True while any rollup (full/incremental/names) is aggregating. The
        # enrichment loop reads it to YIELD its own whole-store NPI scan while a
        # rollup runs — two heavy scans at once thrash the 8 GB pool and the
        # HDD, slowing extraction (a plain bool; GIL makes the read/write
        # atomic, no lock needed).
        self._rollup_active = False
        # the running analytics build's progress (rollup_job) and the last
        # failed attempt's sentence (rollup_failure; _MISSING = not read yet)
        self._rollup_job: dict | None = None
        self._rollup_failure = _MISSING
        # review 14 item 1.2: the user's "Pause analytics" switch (persisted
        # in meta; _MISSING = not read yet) and the fold-in scheduler that
        # owns every analytics update of a serving process (mrfx/foldin.py)
        self._analytics_paused = _MISSING
        self.foldin = None
        self._sweep_orphan_tmps()
        if memory_limit_gb is not None:
            # explicit override from config (duckdb_memory_gb): trust the user
            # who knows their machine, but never below 1 GB (DuckDB can't do
            # useful work under that and would fail every rollup).
            want = max(1, int(memory_limit_gb))
            # …and never so high it risks a NATIVE OOM kill (uncatchable — the
            # console just closes). DuckDB's cap + parser workers + OS + browser
            # must coexist; hold the analytics pool to ~70% of TOTAL RAM. The
            # config comment tells users to raise this when a view OOMs, so an
            # over-eager value on a 32 GB box is a real hazard, not paranoia.
            total = _total_ram_bytes()
            if total:
                safe = max(2, int(total * 0.7 / 1e9))
                if want > safe:
                    logging.getLogger(__name__).warning(
                        "duckdb_memory_gb=%d is close to this machine's total RAM "
                        "(%.0f GB) — capping the analytics pool at %d GB so the "
                        "parser workers, Windows, and your browser aren't starved "
                        "(too high risks an uncatchable out-of-memory crash)",
                        want, total / 1e9, safe)
                    want = safe
            self._memory_limit_gb = want
        else:
            total = _total_ram_bytes()
            if total:
                cap = auto_pool_gb(total)
                avail = _avail_ram_bytes()
                if avail:
                    # never plan to use more than ~60% of what is FREE right
                    # now — a cap sized off total RAM alone overcommitted a
                    # machine already 2/3 used and died natively mid-rollup
                    cap = max(2, min(cap, int(avail * 0.6 / 1e9)))
                self._memory_limit_gb = cap
            else:
                self._memory_limit_gb = 4  # RAM size unknown: safe flat default
        # State the budget at open. DuckDB reports OOM in GiB ("7.4 GiB/7.4 GiB
        # used"), which does NOT visibly match the GB number in the config
        # (8 GB == 7.45 GiB), so a user hitting the ceiling had no way to connect
        # the error to the setting that causes it — or to know whether the value
        # came from their config or was chosen automatically.
        logging.getLogger(__name__).info(
            "analytics memory budget: %d GB (%.1f GiB, as DuckDB reports it) — "
            "%s; raise duckdb_memory_gb in config/mrfx.yaml if rebuilds run out",
            self._memory_limit_gb, self._memory_limit_gb * 1e9 / (1 << 30),
            "set by duckdb_memory_gb" if memory_limit_gb is not None
            else "chosen automatically from this machine's RAM")
        # Global DuckDB thread count, set ONCE at open (see _apply_settings):
        # capped so rollup slices size as validated AND concurrent-query
        # contention stays bounded. Never below 1, never above the machine.
        self._rollup_threads = max(1, min(ROLLUP_MAX_THREADS, os.cpu_count() or 4))
        self._temp_cap_gb: int | None = None  # computed once, in connect()
        # bumped whenever the analytics tables change (rebuild/reset) so the API's
        # row-count cache can key on it and never serve a stale total across an
        # ingest — see api.rates(). Cheap monotonic int, not a data hash.
        self.data_generation = 0
        # bumped only when the TIN spine itself is rewritten (a rollup, a
        # reset) — NOT by a directory name refresh, which changes no rate:
        # the per-spine materialised files (spine_materialised) key on it
        self.spine_generation = 0
        self._spine_mat: dict = {}
        self._spine_mat_locks: dict = {}
        self._spine_mat_guard = threading.Lock()
        self._instance_tag = os.urandom(4).hex()
        # user edits a cached answer shows (a verified website, the entity
        # map): they invalidate Store.cached answers WITHOUT counting as a new
        # data generation — the materialised per-generation files (placement,
        # practice medians) are rates-only and must not rebuild on a click
        self.user_edit_generation = 0
        self._placement_lock = threading.Lock()
        # results that change only when the analytics tables do (see cached())
        self._gen_cache: dict = {}
        self._gen_cache_lock = threading.Lock()
        self._generation_lock = threading.Lock()  # leaf lock: bump sites hold
        # DIFFERENT outer locks (rollup_lock vs write_lock), so a bare += 1
        # read-modify-write could lose an increment and let the API's
        # generation-keyed caches serve pre-rebuild numbers for one TTL
        # enrichment_progress scans the full rates view (DISTINCT npi over a
        # UNION) — far too heavy to run on every 15s dashboard poll of a big
        # store; cache it briefly ((monotonic, result), see enrichment_progress)
        self._enrich_progress_cache: tuple[float, dict] | None = None
        self._enrich_progress_lock = threading.Lock()  # single-flight for the scan
        # SAME hazard, SAME fix for the header row/tin/payer counts and the
        # state-filter list: both scan the full store (count(DISTINCT ...) over
        # 86M raw rows; DISTINCT-unnest over the directories) and BOTH are on
        # the dashboard's 15s poll. Uncached, each poll stacked another
        # whole-store HDD scan behind the last, saturating the disk the parser
        # workers stream from — the "starts strong, stalls after a few minutes,
        # CPU busy, chunks frozen, recovers when I close the dashboard" report.
        self._store_stats_cache: tuple[float, dict] | None = None
        self._spine_counts: tuple[int, tuple[int, int]] | None = None  # (generation, counts)
        self._store_stats_lock = threading.Lock()
        self._states_cache: tuple[float, list[str]] | None = None
        self._states_lock = threading.Lock()
        # `keep_warm` (serve only) pins ONE connection open for the process's
        # lifetime. DuckDB's memory/temp settings are GLOBAL to the in-process
        # database instance and persist while any connection holds it, so with a
        # pin we apply them ONCE and every short-lived per-request connection
        # inherits them for free instead of re-running four SET pragmas each time
        # (~14ms/call — the dominant floor on a busy dashboard). NOT used by the
        # one-shot CLI: holding the file open would block a concurrent
        # `mrfx status`/`export`/BI tool (DuckDB is single-writer per process),
        # and the CLI's brief runs don't benefit anyway.
        self._pin: duckdb.DuckDBPyConnection | None = None
        self._local_tz: str | None = None   # the PC's zone, read before SET TimeZone
        if keep_warm:
            try:
                # same retry budget as connect(): a one-second external reader
                # (BI tool, `mrfx status` from another window) racing serve's
                # boot used to SILENTLY downgrade the whole session to pin-less
                # mode — instance-teardown churn plus a residual SET-threads
                # hazard — with nothing logged (audit F2)
                self._pin = self._connect_with_retry()
                self._apply_settings(self._pin, include_threads=True)
            except duckdb.Error as e:
                self._pin = None  # per-connection settings still applied
                logging.getLogger(__name__).warning(
                    "could not pin a database connection (%s) — running "
                    "un-pinned; performance may dip and settings re-apply "
                    "per connection", e)
        with self.write_lock, self.connect() as con:
            if self._pin is None:
                # first-ever settings application happens HERE, still
                # single-threaded (no worker threads exist yet) — the ONLY
                # place threads may be set in pin-less mode (audit F1)
                self._apply_settings(con, include_threads=True)
            # holding a live connection means DuckDB's file lock is OURS — no
            # other process is using this store, so every duckdb_temp_* file in
            # the spill dir is a dead leftover (crash/power-loss mid-rollup).
            # DuckDB never cleans pre-existing temp files itself (verified on
            # 1.3): unswept, one power loss could strand tens of GB — on the
            # auto-spill SSD that's the user's system drive filling up with no
            # visible cause. Sweep BEFORE _init_tables (nothing has spilled yet).
            self._sweep_stale_spill()
            quarantined = self._quarantine_corrupt_parts()
            self._init_tables(con)
            self._flag_quarantined_files(con, quarantined)
            self._migrate_timestamps_to_utc(con)
            self._register_views(con)
        # parts a locked file kept from being deleted / swapped in last time
        self._retry_pending_part_ops()
        # BEFORE the rollup check: a full rebuild it may trigger must see the
        # fixed rows, and the payers this queues ride the next incremental
        self._migrate_row_rules(budget_s=self._migrate_budget_s)
        self._migrate_stale_rollups()
        self._migrate_latest_flag()

    def _quarantine_corrupt_parts(self) -> None:
        """Move any UNREADABLE .parquet part out of the rates glob before the
        views bind to it.

        The store's own writes are atomic (write .tmp, then rename), so a
        kill mid-write leaves a .parquet.tmp — never a corrupt .parquet — and
        those are swept elsewhere. But corruption from OUTSIDE the app is real:
        disk bit-rot, a half-copied backup restore, an antivirus quarantine
        that truncates a file, a rename target lost to a full disk on a
        filesystem without atomic rename. read_parquet() over the glob fails
        ENTIRELY on one bad file, so a single damaged part would blind the
        whole dashboard with a cryptic "No magic bytes" error — the opposite
        of the fault-isolation the rest of the pipeline guarantees.

        A valid parquet begins and ends with the 4-byte magic 'PAR1'. Checking
        those 8 bytes is O(1) per file and catches truncation and header
        damage; anything failing it is moved to rates/corrupt/ (kept, never
        deleted — the raw MRF may be gone) with a plain-language warning, so
        the rest of the store still serves. Best-effort: a file we cannot even
        stat/move is skipped, never fatal."""
        try:
            parts = list(self.rates_dir.glob("*.parquet"))
        except OSError:
            return []
        # Parts verified at an earlier start and unchanged since (same size
        # and mtime) are not opened again: 2 reads per part at EVERY start
        # was 4,004 read calls for 2,000 parts before the first log line —
        # tens of silent seconds on a spinning drive (review 9 item 2.2)
        seen_p = self.rates_dir / ".verified_parts.json"
        try:
            verified = json.loads(seen_p.read_text())
            if not isinstance(verified, dict):
                verified = {}
        except (OSError, ValueError):
            verified = {}
        now_ok: dict[str, list] = {}
        to_check = []
        for p in parts:
            try:
                st = p.stat()
            except OSError:
                continue
            if verified.get(p.name) == [st.st_size, st.st_mtime_ns]:
                now_ok[p.name] = [st.st_size, st.st_mtime_ns]
            else:
                to_check.append((p, st))
        if len(to_check) > 200:
            logging.getLogger(__name__).info(
                "checking %d stored rate file(s) that are new or changed since the last "
                "start…", len(to_check))
        bad = []
        for p, st in to_check:
            try:
                size = st.st_size
                if size < 8:
                    bad.append(p)
                    continue
                with open(p, "rb") as fh:
                    head = fh.read(4)
                    fh.seek(-4, 2)
                    tail = fh.read(4)
                if head != b"PAR1" or tail != b"PAR1":
                    bad.append(p)
                else:
                    now_ok[p.name] = [st.st_size, st.st_mtime_ns]
            except OSError:
                continue  # can't read it now (locked/vanishing) — leave it
        if now_ok != verified:
            try:
                tmp = seen_p.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(now_ok))
                tmp.replace(seen_p)
            except OSError:
                pass       # cosmetic: the next start checks again
        if not bad:
            return []
        quar = self.rates_dir / "corrupt"
        moved = []
        for p in bad:
            try:
                quar.mkdir(exist_ok=True)
                replace_with_retry(p, quar / p.name)
                moved.append(p.stem)
            except OSError:
                continue
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "quarantined %d unreadable rate part(s) to %s so the rest of the "
            "store still loads — re-ingest the source file(s) to restore them: %s",
            len(bad), quar, ", ".join(p.name for p in bad))
        return moved

    def _flag_quarantined_files(self, con: duckdb.DuckDBPyConnection,
                                stems: list[str]) -> None:
        """A quarantined part's rows left the raw store, but its files row
        still said 'done' (older than the rollup marker), so the analytics kept
        serving its rows while the NPI view (live over the raw parts) didn't,
        and nothing looked stale. Flip the row to failed with the reason and
        ask for ONE full rebuild (the damaged part can't be read to scope it)."""
        if not stems:
            return
        want = set(stems)
        names = [r[0] for r in con.execute("SELECT filename FROM files").fetchall()
                 if file_key(r[0]) in want]
        for n in names:
            con.execute("UPDATE files SET status = 'failed', error = ? WHERE filename = ?",
                        ["its stored rates were damaged on disk and were set aside "
                         "(data/mrfx_store/rates/corrupt) — re-add this file or link "
                         "to restore them", n])
        if con.execute("SELECT count(*) FROM information_schema.tables "
                       "WHERE table_name = 'rates_by_tin_tbl'").fetchone()[0]:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_needs_full', ?)", [_needs_full_token()])

    def _sweep_stale_spill(self) -> None:
        """Best-effort removal of dead DuckDB spill files in our spill dir.
        Only call while holding a live connection (the DuckDB file lock proves
        sole ownership). Cosmetic-tier: a locked/vanishing file never fails
        the open."""
        try:
            for p in self._tmp_dir.glob("duckdb_temp*"):
                try:
                    if p.is_file():
                        p.unlink()
                except OSError:
                    continue
        except OSError:
            pass

    def _migrate_stale_rollups(self) -> None:
        """A prior version may have materialized the rollup tables WITHOUT a
        column this version's queries now reference (e.g. is_therapy), or with
        an older GROUP-BY grain (the meta version below). Reopening such a
        store would serve wrong/failing queries until an ingest happened to
        trigger a rebuild. Detect drift and rebuild once now (best-effort; a
        rebuild failure must not stop the app from opening)."""
        try:
            with self.connect() as con:
                if self._meta_get(con, "rollup_plan"):
                    # a sliced build is part-way: it resumes in the background
                    # (serve / the queue), never synchronously here at open
                    return
                if not con.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name = 'tin_directory_tbl'").fetchone()[0]:
                    return  # no materialized table -> views use the current-schema query
                cols = {r[0] for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'tin_directory_tbl'").fetchall()}
                ver = self._meta_get(con, "rollup_schema_version")
                dver = self._meta_get(con, "directory_schema_version")
            if ("is_therapy" not in cols or "has_hospital" not in cols
                    or ver != str(ROLLUP_SCHEMA_VERSION)):
                import logging as _logging
                _logging.getLogger(__name__).info(
                    "migrating store: rollup tables were built by an older "
                    "version (schema v%s -> v%d) — rebuilding once",
                    ver or "?", ROLLUP_SCHEMA_VERSION)
                self.rebuild_rollups()
                return
            if ("has_facility" not in cols or "primary_state" not in cols
                    or dver != str(DIRECTORY_SCHEMA_VERSION)):
                # DIRECTORY-ONLY drift (e.g. the therapy-practice rules changed):
                # rebuild just tin_directory — minutes — instead of dragging the
                # whole rate spine through an hours-long pass on every such tweak.
                import logging as _logging
                _logging.getLogger(__name__).info(
                    "migrating store: the practice directory was built by an "
                    "older version (directory v%s -> v%d) — rebuilding just the "
                    "directory (the rate data is untouched)",
                    dver or "?", DIRECTORY_SCHEMA_VERSION)
                # names_only=True IS the directory-only selection (see
                # rebuild_rollups) — passing tables= raised a TypeError that the
                # broad except below swallowed as a "skipped" warning, so the
                # marker never advanced, the check re-fired on every launch, and
                # rule changes never reached an existing store.
                self.rebuild_rollups(names_only=True)
                return
            with self.connect() as con:
                if (self._meta_get(con, "rollup_covered_through") is not None
                        and self._meta_get(con, "rollup_covered_names") is None):
                    # The version marker matches but the covered-names key —
                    # written by every build of THIS version — is absent. That
                    # means the tables were last rebuilt either by a pre-tie-
                    # break build or by an OLDER app run against this store
                    # after a rollback (older builds rebuild the tables in
                    # their old grain but never touch these keys, so the
                    # version marker lies). Provenance unverifiable -> queue
                    # ONE background full rebuild; incremental updates refuse
                    # to run (rollup_needs_full) until it completes.
                    import logging as _logging
                    _logging.getLogger(__name__).info(
                        "analytics provenance can't be verified (the tables may "
                        "have been rebuilt by an older app version) — scheduling "
                        "one full refresh in the background")
                    con.execute(
                        "INSERT OR REPLACE INTO meta VALUES ('rollup_needs_full', ?)", [_needs_full_token()])
        except Exception as e:  # noqa: BLE001
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "rollup schema-migration check skipped (%s)", e)

    def _migrate_latest_flag(self) -> None:
        """Give a spine built before `is_latest` existed (SPINE_QUERY) the
        column IN PLACE: _mark_latest over the whole spine — 5.6 s on 79M rows
        — instead of the hour-long rebuild a spine-grain change costs at 300M
        rates. The rate rows themselves are untouched. One transaction: a failure leaves the store exactly as it
        was, and until the column exists the rates_by_tin VIEW supplies it per
        query (_register_views) — the old cost, never a wrong or failing
        screen. Retried at every open; any full or payer-slice rebuild writes
        the column as well."""
        import logging as _logging
        log = _logging.getLogger(__name__)
        try:
            with self.write_lock, self.connect() as con:
                cols = {r[0] for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'rates_by_tin_tbl'").fetchall()}
                if not cols or "is_latest" in cols:
                    return
                t0 = time.monotonic()
                con.execute("BEGIN TRANSACTION")
                try:
                    con.execute("ALTER TABLE rates_by_tin_tbl "
                                "ADD COLUMN is_latest BOOLEAN DEFAULT FALSE")
                    self._mark_latest(con)
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('spine_token', ?)", [os.urandom(8).hex()])
                    con.execute("COMMIT")
                except Exception:
                    try:
                        con.execute("ROLLBACK")
                    except duckdb.Error:
                        pass
                    raise
                self._register_views(con)
                try:
                    con.execute("CHECKPOINT")
                except duckdb.Error:
                    pass
                log.info("analytics: marked each contract's newest month on the "
                         "rate summary in %.1fs (one time; the rates are untouched)",
                         time.monotonic() - t0)
        except Exception as e:  # noqa: BLE001 — never stops the store opening
            log.warning("could not mark the newest months on the rate summary (%s) — "
                        "'latest' views work it out per request until the next "
                        "analytics rebuild", e)

    @staticmethod
    def _mark_latest(con: duckdb.DuckDBPyConnection, payer_pred: str = "TRUE") -> None:
        """Set rates_by_tin_tbl.is_latest on the rows of each contract's newest
        month, for the contracts of the payers `payer_pred` selects, on a spine
        whose rows are COMMITTED and FALSE there (the in-place migration; a
        build sets the flag inside each slice statement — see SPINE_QUERY). The SAME rule as the per-request window it replaced —
        `file_month = max(file_month) OVER (PARTITION BY LATEST_PARTITION)` —
        written as a GROUP BY + hash join, which spills and is ~200x cheaper
        than windowing every build slice. A whole payer is always in or out
        (payer is part of the partition), so a payer-scoped mark is exact.
        NOT DISTINCT FROM: a NULL tin_value is one contract to PARTITION BY too;
        a row with no month never equals the max and stays FALSE, as QUALIFY
        dropped it. Runs inside the caller's transaction."""
        con.execute(f"""
            UPDATE rates_by_tin_tbl AS t SET is_latest = TRUE
            FROM (SELECT {LATEST_PARTITION}, max(file_month) AS newest
                  FROM rates_by_tin_tbl WHERE {payer_pred}
                  GROUP BY {LATEST_PARTITION}) k
            WHERE t.payer IS NOT DISTINCT FROM k.payer
              AND t.tin_value IS NOT DISTINCT FROM k.tin_value
              AND t.tin_is_really_npi IS NOT DISTINCT FROM k.tin_is_really_npi
              AND t.billing_code IS NOT DISTINCT FROM k.billing_code
              AND t.file_month = k.newest""")

    # (table, column) pairs every writer stamped with an AWARE datetime or SQL
    # current_timestamp before round 3 — i.e. LOCAL wall-clock time, exactly
    # convertible. npi_directory.enriched_at is NOT here: the bulk path wrote
    # naive UTC and the API path local time, so its marker is shifted instead.
    _LOCAL_TIME_COLUMNS = (
        ("files", "started_at"), ("files", "finished_at"),
        ("url_queue", "added_at"), ("url_queue", "finished_at"),
        ("peer_sets", "created_at"), ("hospital_files", "imported_at"),
        ("inflation_index", "added_at"), ("client_watchlist", "added_at"),
        ("engagement_baselines", "created_at"),
    )

    def _migrate_timestamps_to_utc(self, con: duckdb.DuckDBPyConnection) -> None:
        """Once per store (meta timestamps_utc): convert the local-time stamps
        older versions wrote to UTC, in the PC's own zone, together with the
        rollup marker copied from them — so old and new stamps (and the
        marker) share one clock. The names marker is compared against a mixed
        column, so it moves back 14 h (the widest UTC offset): at worst a few
        practices' names are re-checked once. Caller holds write_lock."""
        if self._meta_get(con, "timestamps_utc") == "1":
            return
        tz = self._local_tz or "UTC"
        try:
            con.execute("BEGIN TRANSACTION")
            if tz.upper() not in ("UTC", "GMT", "ETC/UTC", "Z"):
                have = {(t, c) for t, c in con.execute(
                    "SELECT table_name, column_name FROM information_schema.columns").fetchall()}
                for t, c in self._LOCAL_TIME_COLUMNS:
                    if (t, c) in have:
                        con.execute(f"UPDATE {t} SET {c} = timezone(?, {c})::TIMESTAMP "
                                    f"WHERE {c} IS NOT NULL", [tz])
                cov = self._meta_get(con, "rollup_covered_through")
                if cov:
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_covered_through', "
                                "(SELECT timezone(?, ?::TIMESTAMP)::TIMESTAMP::VARCHAR))", [tz, cov])
                names = self._meta_get(con, "directory_names_through")
                if names:
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('directory_names_through', "
                                "(SELECT (?::TIMESTAMP - INTERVAL 14 HOUR)::VARCHAR))", [names])
            con.execute("INSERT OR REPLACE INTO meta VALUES ('timestamps_utc', '1')")
            con.execute("COMMIT")
            if tz.upper() not in ("UTC", "GMT", "ETC/UTC", "Z"):
                logging.getLogger(__name__).info(
                    "stored times converted once from this PC's time zone (%s) to UTC "
                    "— the dashboard shows them in your local time", tz)
        except Exception as e:  # noqa: BLE001 — never block the store from opening
            try:
                con.execute("ROLLBACK")
            except duckdb.Error:
                pass
            logging.getLogger(__name__).warning(
                "could not convert stored times to UTC (%s) — will retry at next start", e)

    def _migrate_row_rules(self, budget_s: float | None = None, hold=None) -> list[str]:
        """Rewrite, once, the rate parts written under older ROW rules
        (files.row_rules < ROW_RULES_VERSION) so they read exactly as a
        re-ingest by this version would write them — no re-download.

        Every rule is a pure function of stored columns, run through the SAME
        Python function the parser calls: the modifier rule (mrfx/modifiers.py)
        over (billing_code, billing_code_modifier, service_code), the month
        rule (parser.file_month_for) over (last_updated_on, source_file, the
        month of ingested_at), the negotiated-type spelling
        (parser.normalize_negotiated_type). Only rows whose inputs change are
        rewritten, via a hash join; each part is swapped atomically through
        finalize_rates_part, which queues its payers and TINs for the next
        incremental rollup. A part that can't be read or written is left as
        it was and retried at the next start.

        It runs BEFORE the dashboard opens, so it must be fast and must say
        what it is doing: 0.7.1 ran every rule over every part one by one and
        logged nothing until the end — on a store of many files on an external
        drive the program sat silent after "analytics memory budget" for many
        minutes, which reads as a hang. Files already at rules 2 now need only
        the rule-3 check, answered for ALL of them by one narrow scan of two
        columns; only parts that actually change are rewritten (each logged),
        and progress is logged every 15 s.

        THE RULE FOR EVERY STORED-ROW UPDATE (review 12 item 2.9, AI_HANDOFF
        Round 12): the dashboard opens first. With `budget_s` (serve) the
        update works for at most that long before the store is handed over;
        the files it did not reach are returned and finished by
        finish_row_rules_in_background, file by file under the same per-file
        claim an ingest takes (`hold`), with `rows_update` describing the
        progress for the Files tab. Without a budget (every CLI command: an
        export or a report must never mix rule versions) it runs to the end.
        Returns the names left for the background."""
        log = logging.getLogger(__name__)
        try:
            with self.connect() as con:
                todo = con.execute(
                    "SELECT filename, coalesce(row_rules, 0) FROM files "
                    "WHERE coalesce(row_rules, 0) < ? ORDER BY filename",
                    [ROW_RULES_VERSION]).fetchall()
        except Exception as e:  # noqa: BLE001 — never block the app from opening
            log.warning("row-rules check skipped (%s)", e)
            return []
        # a file whose newer rates are PARKED (its old part was held open when
        # they finished) waits: rewriting the old part swaps it in through
        # _swap_in_part, which discards the park — the newer rates would be
        # lost for the old ones (review 9 item 5.2). Its park is adopted when
        # the old part is free; the next start updates that part.
        try:
            with self.connect() as con:
                parked = set(json.loads(self._meta_get(con, "parked_parts") or "{}"))
        except Exception:  # noqa: BLE001
            parked = set()
        if parked:
            held = [n for n, _r in todo if f".{file_key(n)}.parked" in parked]
            if held:
                log.info("%d file(s) with newer rates waiting to be swapped in are "
                         "updated at a later start", len(held))
                todo = [(n, r) for n, r in todo if n not in set(held)]
        if not todo:
            return []
        t0 = time.monotonic()
        deadline = None if budget_s is None else t0 + max(0.0, float(budget_s))
        log.info("one-time update of %d stored file(s) to this version's rules — %s "
                 "(progress below)", len(todo),
                 "the dashboard opens as soon as it is done" if deadline is None else
                 f"the dashboard opens within {budget_s:.0f} s and the rest continues "
                 "behind it" if hold is None else "continuing behind the dashboard")
        tick = [t0]
        if hold is not None:
            self.rows_update = {"total": len(todo), "done": 0, "running": True}

        def progress(done: int) -> None:
            if self.rows_update is not None and hold is not None:
                self.rows_update["done"] = done
            if time.monotonic() - tick[0] >= 15 or (done == len(todo) and done > 500):
                tick[0] = time.monotonic()
                log.info("  … %d of %d files checked (%.0f s)", done, len(todo),
                         tick[0] - t0)

        def over() -> bool:
            return deadline is not None and time.monotonic() > deadline

        def held_names(names: list[str]) -> list[str]:
            # in the background a file being (re)loaded right now is skipped:
            # its new part is written under the current rules anyway
            return [n for n in names if hold is None or hold[0](n)]

        fixed = rows_fixed = done = 0
        left: list[str] = []
        deferred: list[str] = []
        # files at rules 2: only the type-spelling rule is new for them —
        # in slices, so a budget can stop between two of them
        recent = [n for n, r in todo if r >= 2]
        for i in range(0, len(recent), _MIGRATE_SLICE):
            chunk = recent[i:i + _MIGRATE_SLICE]
            if over():
                deferred += chunk
                continue
            mine = held_names(chunk)
            try:
                f, n_rows, left_recent = self._migrate_types_only(mine, progress) \
                    if mine else (0, 0, [])
                fixed += f
                rows_fixed += n_rows
                left += left_recent
            except Exception as e:  # noqa: BLE001 — never block the app from opening
                log.warning("could not update the stored rows to the current rules "
                            "(%s) — they are unchanged; will retry at the next start", e)
                left += mine
            finally:
                if hold is not None:
                    for n in mine:
                        hold[1](n)
            done += len(chunk)
            progress(done)
        for name, _r in todo:
            if _r >= 2:
                continue
            if over():
                deferred.append(name)
                continue
            if not held_names([name]):
                done += 1
                continue
            try:
                n = self._normalize_part(name)
            except Exception as e:  # noqa: BLE001 — one bad part never blocks the rest
                log.warning("could not update the stored rows of %s to the current rules "
                            "(%s) — its rows are unchanged; will retry at the next start",
                            name, e)
                left.append(name)
                continue
            finally:
                if hold is not None:
                    hold[1](name)
                done += 1
                progress(done)
            if n:
                fixed += 1
                rows_fixed += n
        if deferred:
            log.info("stored rows: %d of %d file(s) updated before the dashboard opened "
                     "(%.0f s); the other %d are updated behind it — the Files tab shows "
                     "the progress", len(todo) - len(deferred), len(todo),
                     time.monotonic() - t0, len(deferred))
            self.rows_update = {"total": len(todo), "done": len(todo) - len(deferred),
                                "running": False, "pending": len(deferred)}
            return deferred
        if hold is not None:
            self.rows_update = None
        if left:
            # never "up to date" while a file is left (review 9 item 1.1b)
            log.warning("stored rows: %d of %d file(s) checked, %s row(s) in %d file(s) "
                        "rewritten in %.0f s; %d file(s) could not be read and keep their "
                        "stored rows — they are checked again at the next start",
                        len(todo) - len(left), len(todo), f"{rows_fixed:,}", fixed,
                        time.monotonic() - t0, len(left))
        else:
            log.info("stored rows are up to date: %d file(s) checked, %s row(s) in %d file(s) "
                     "rewritten (modifier / place-of-service fields, future-dated months, "
                     "'fee_schedule' spelling) in %.0f s — one time",
                     len(todo), f"{rows_fixed:,}", fixed, time.monotonic() - t0)
        return []

    def finish_row_rules_in_background(self, on_done=None) -> threading.Thread | None:
        """The files the start-up budget did not reach (review 12 item 2.9):
        updated behind the dashboard, one at a time under the per-file claim
        an ingest takes, then the analytics catch up on the payers they
        touched. `rows_update` says how far it is ({"total", "done",
        "running"}; None once done). None when nothing was left."""
        if not self.rows_update or not self.rows_update.get("pending"):
            return None
        from .ingest import _claim_ingest, _release_ingest

        def run() -> None:
            log = logging.getLogger(__name__)
            try:
                self._migrate_row_rules(budget_s=None, hold=(_claim_ingest, _release_ingest))
            except Exception as e:  # noqa: BLE001 — the next start retries
                log.warning("background update of stored rows stopped (%s) — it "
                            "continues at the next start", e)
            finally:
                self.rows_update = None
            try:
                if self.rollups_stale():
                    try:
                        if self.update_rollups_incremental(skip_if_busy=True) == ROLLUP_SKIPPED:
                            return
                    except Exception:  # noqa: BLE001 — the full build is always correct
                        self.rebuild_rollups(skip_if_busy=True)
            except Exception as e:  # noqa: BLE001
                log.warning("analytics after the stored-row update: %s", e)
            if on_done is not None:
                on_done()
        t = threading.Thread(target=run, name="mrfx-row-rules", daemon=True)
        t.start()
        return t

    def _migrate_types_only(self, names: list[str], progress) -> tuple[int, int, list[str]]:
        """Rules 3-4 for files already at rules 2: read (negotiated_type,
        is_dollar_rate) of their parts in batches of _TYPE_SCAN_BATCH (one
        progress tick per batch), rewrite only parts whose stored spelling
        changes, then stamp — in one transaction — ONLY the files whose part
        was actually read (or has no part at all). Results are mapped back by
        the part's BASENAME: 0.7.2 compared whole path strings and stamped
        every name, so a path DuckDB spelled differently (letter case, a
        prefix) left a part unfixed but marked done (review 9 item 1.1). A
        batch that fails is retried part by part; a part that cannot be read
        keeps its old rules and is named once. Returns (files rewritten, rows
        rewritten, names left for the next start)."""
        log = logging.getLogger(__name__)
        by_base: dict[str, str] = {}
        stamp: list[str] = []
        for name in names:
            path = self.rates_dir / f"{file_key(name)}.parquet"
            if path.exists():
                by_base[path.name] = name
            else:
                stamp.append(name)          # nothing stored: nothing to fix
        types: dict[str, list] = {}
        seen: set[str] = set()
        bases = sorted(by_base)
        failed: list[str] = []

        def scan(chunk: list[str]) -> None:
            paths = [str(self.rates_dir / b) for b in chunk]
            with self.connect() as con:
                rows = con.execute(
                    "SELECT DISTINCT filename, negotiated_type, is_dollar_rate "
                    "FROM read_parquet(?, filename = true)", [paths]).fetchall()
            for f, t, d in rows:
                base = Path(str(f).replace("\\", "/")).name
                if base not in by_base:
                    # same file, spelled differently: match case-insensitively
                    low = {b.lower(): b for b in chunk}
                    base = low.get(base.lower(), base)
                if base in by_base:
                    types.setdefault(by_base[base], []).append((t, d))
                    seen.add(base)

        done = 0
        for i in range(0, len(bases), _TYPE_SCAN_BATCH):
            chunk = bases[i:i + _TYPE_SCAN_BATCH]
            try:
                scan(chunk)
            except Exception:  # noqa: BLE001 — one bad part must not block the batch
                for b in chunk:
                    try:
                        scan([b])
                    except Exception as e:  # noqa: BLE001
                        failed.append(by_base[b])
                        log.warning("could not read the stored rows of %s (%s) — left "
                                    "as they are; checked again at the next start",
                                    by_base[b], e)
            done += len(chunk)
            progress(done)
        # a part that exists and was read but returned no row has no type to
        # fix (an empty part); a part that was NOT seen and did not fail is a
        # mapping we could not make — never stamp it on faith
        failed_set = set(failed)
        for b, name in by_base.items():
            if name in failed_set:
                continue
            if b in seen:
                stamp.append(name)
                continue
            try:
                with self.connect() as con:
                    n = con.execute(
                        f"SELECT count(*) FROM read_parquet('{sql_path(self.rates_dir / b)}')"
                    ).fetchone()[0]
            except Exception as e:  # noqa: BLE001
                n = None
                log.warning("could not read the stored rows of %s (%s) — left as they "
                            "are; checked again at the next start", name, e)
            if n == 0:
                stamp.append(name)
            else:
                failed.append(name)
                if n is not None:
                    log.warning("the type check of %s returned no rows for a part of %d "
                                "rows — left as it is; checked again at the next start",
                                name, n)
        fixed = rows_fixed = 0
        ok: list[str] = []
        stamp_set = set(stamp)
        for name in names:
            if name not in stamp_set:
                continue
            type_map = self._type_map(types.get(name, []))
            if type_map:
                path = self.rates_dir / f"{file_key(name)}.parquet"
                log.info("  rewriting %s (%s)", name, ", ".join(
                    f"{o!r} -> {n!r}" for o, _od, n, _d in type_map))
                try:
                    n = self._rewrite_part(name, f"read_parquet('{sql_path(path)}')",
                                           [], [], "", type_map, keep_qa=True)
                except Exception as e:  # noqa: BLE001 — one bad part never blocks the rest
                    log.warning("could not update the stored rows of %s to the current "
                                "rules (%s) — its rows are unchanged; will retry at the "
                                "next start", name, e)
                    failed.append(name)
                    continue
                fixed += 1
                rows_fixed += n
            ok.append(name)
        self._finish_rules(ok)
        return fixed, rows_fixed, sorted(set(failed))

    @staticmethod
    def _type_map(types: list) -> list:
        """[(old type, old is_dollar, new type, new is_dollar)] for the stored
        (type, is_dollar) pairs whose spelling or dollar flag changes. Keyed on
        BOTH old values, so a spelling stored with both flags maps each row
        once (review 9 item 1.1c: the join on the type alone doubled them)."""
        from .config import DOLLAR_TYPES
        from .parser import normalize_negotiated_type
        out, seen = [], set()
        for old_t, old_d in types:
            if old_t is None:
                continue
            new_t = normalize_negotiated_type(old_t)
            new_d = new_t in DOLLAR_TYPES
            key = (old_t, bool(old_d) if old_d is not None else None)
            if key in seen:
                continue
            seen.add(key)
            if new_t != old_t or (old_d is None or bool(old_d) != new_d):
                out.append((old_t, key[1], new_t, new_d))
        return out

    def _finish_rules(self, names: list[str]) -> None:
        """Stamp files as on the current rules, in ONE transaction, and flag
        those an older arrangement rule may have trimmed: items excluded as
        "bundled" before rule 3 may have been "fee for service" spelled out —
        they were never stored, so only a re-ingest can tell (the Files tab
        says so)."""
        if not names:
            return
        with self.write_lock, self.connect() as con:
            rows = con.execute(
                "SELECT filename, qa FROM files WHERE coalesce(row_rules, 0) < 3 "
                "AND filename IN (SELECT unnest(?::VARCHAR[])) AND qa LIKE '%bundled_items%'",
                [names]).fetchall()
            flags = []
            for name, raw in rows:
                try:
                    qa = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(qa, dict) and (qa.get("bundled_items") or 0) > 0:
                    qa["reingest_suggested"] = (
                        f"{qa['bundled_items']:,} item(s) were left out as bundle/capitation "
                        "by an older version, which also read a spelled-out "
                        "\"fee for service\" that way — re-ingest this file to check")
                    flags.append((json.dumps(qa), name))
            con.execute("BEGIN")
            try:
                if flags:
                    con.executemany("UPDATE files SET qa = ? WHERE filename = ?", flags)
                con.execute("UPDATE files SET row_rules = ? "
                            "WHERE filename IN (SELECT unnest(?::VARCHAR[]))",
                            [ROW_RULES_VERSION, names])
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise

    def _normalize_part(self, name: str) -> int:
        """One file's part -> the current row rules. Returns rows changed."""
        from .catalog import resolve_discipline
        from .modifiers import normalize
        from .parser import file_month_for, published_month_of
        path = self.rates_dir / f"{file_key(name)}.parquet"
        changed_rows = 0
        months: dict = {}
        type_map: list = []   # (old type, old is_dollar, new type, new is_dollar)
        if path.exists():
            src = f"read_parquet('{sql_path(path)}')"
            with self.connect() as con:
                combos = con.execute(
                    f"SELECT DISTINCT billing_code, billing_code_modifier, service_code "
                    f"FROM {src}").fetchall()
                mrows = con.execute(
                    f"SELECT DISTINCT last_updated_on, source_file, file_month, "
                    f"substr(ingested_at, 1, 7) FROM {src}").fetchall()
                types = con.execute(
                    f"SELECT DISTINCT negotiated_type, is_dollar_rate FROM {src}").fetchall()
            type_map = self._type_map(types)
            mod_map = []   # (code, old mods, old pos, new mods, new pos, discipline, split, moved)
            for code, mods, pos in combos:
                mods, pos = list(mods or []), list(pos or [])
                new_mods, new_pos, split, moved = normalize(mods, pos)
                if new_mods != mods or new_pos != pos:
                    mod_map.append((code, mods, pos, new_mods, new_pos,
                                    resolve_discipline(code, new_mods), split, moved))
            month_map = []  # (header, source_file, old month, ingest month, new month)
            for hdr, sf, fm, im in mrows:
                new_fm, future = file_month_for(hdr, sf, im or fm)
                if future and new_fm != fm:
                    month_map.append((hdr, sf, fm, im, new_fm))
                months = {"file_month": new_fm,
                          "published_month": published_month_of(sf or name, im or fm),
                          "future_header_date": str(hdr) if future else ""}
            if mod_map or month_map or type_map:
                changed_rows = self._rewrite_part(name, src, mod_map, month_map,
                                                  months.get("future_header_date", ""),
                                                  type_map)
        self._finish_rules([name])
        with self.write_lock, self.connect() as con:
            if months:
                con.execute("UPDATE files SET file_month = coalesce(file_month, ?), "
                            "published_month = coalesce(published_month, ?) "
                            "WHERE filename = ?",
                            [months["file_month"], months["published_month"], name])
        return changed_rows

    def _rewrite_part(self, name: str, src: str, mod_map: list, month_map: list,
                      future_header: str, type_map: list | None = None,
                      keep_qa: bool = False) -> int:
        tmp = self.rates_dir / f".{file_key(name)}.{os.getpid()}.parquet.tmp"
        # hash joins on the rules' inputs — measured 12 s for a 13.5M-row part
        # vs 50 s for a per-row map lookup
        join = (f"FROM {src} r LEFT JOIN _modfix f ON r.billing_code = f.c "
                "AND r.billing_code_modifier = f.m AND r.service_code = f.s "
                "LEFT JOIN _monthfix m ON r.file_month = m.fm "
                "AND r.last_updated_on IS NOT DISTINCT FROM m.h "
                "AND r.source_file IS NOT DISTINCT FROM m.sf "
                "AND substr(r.ingested_at, 1, 7) IS NOT DISTINCT FROM m.im "
                "LEFT JOIN _typefix t ON r.negotiated_type = t.o "
                "AND r.is_dollar_rate IS NOT DISTINCT FROM t.od")
        with self.connect() as con:
            con.execute("CREATE OR REPLACE TEMP TABLE _typefix "
                        "(o VARCHAR, od BOOLEAN, n VARCHAR, d BOOLEAN)")
            if type_map:
                con.executemany("INSERT INTO _typefix VALUES (?, ?, ?, ?)", type_map)
            con.execute("CREATE OR REPLACE TEMP TABLE _modfix (c VARCHAR, m VARCHAR[], "
                        "s VARCHAR[], nm VARCHAR[], ns VARCHAR[], nd VARCHAR, "
                        "split BOOLEAN, moved BOOLEAN)")
            con.execute("CREATE OR REPLACE TEMP TABLE _monthfix (h VARCHAR, sf VARCHAR, "
                        "fm VARCHAR, im VARCHAR, nfm VARCHAR)")
            if mod_map:
                con.executemany("INSERT INTO _modfix VALUES (?, ?, ?, ?, ?, ?, ?, ?)", mod_map)
            if month_map:
                con.executemany("INSERT INTO _monthfix VALUES (?, ?, ?, ?, ?)", month_map)
            changed, split_rows, moved_rows = con.execute(
                f"SELECT count(*) FILTER (WHERE f.c IS NOT NULL OR m.nfm IS NOT NULL "
                f"OR t.o IS NOT NULL), "
                f"count(*) FILTER (WHERE f.split), "
                f"count(*) FILTER (WHERE f.moved) {join}").fetchone()
            reader = con.execute(
                f"""SELECT r.* REPLACE (
                        coalesce(f.nm, r.billing_code_modifier) AS billing_code_modifier,
                        coalesce(f.ns, r.service_code) AS service_code,
                        coalesce(f.nd, r.discipline) AS discipline,
                        coalesce(m.nfm, r.file_month) AS file_month,
                        coalesce(t.n, r.negotiated_type) AS negotiated_type,
                        coalesce(t.d, r.is_dollar_rate) AS is_dollar_rate) {join}"""
            ).to_arrow_reader(200_000)
            rows = 0
            tmp.unlink(missing_ok=True)
            try:
                with pq.ParquetWriter(tmp, RATES_SCHEMA, compression=PARQUET_COMPRESSION) as w:
                    for batch in reader:
                        w.write_table(pa.Table.from_batches([batch]).select(
                            RATES_SCHEMA.names).cast(RATES_SCHEMA))
                        rows += batch.num_rows
                con.execute("DROP TABLE IF EXISTS _modfix")
                con.execute("DROP TABLE IF EXISTS _monthfix")
                con.execute("DROP TABLE IF EXISTS _typefix")
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        self.finalize_rates_part(name, tmp, rows)
        # the Files tab's data-quality line: same counters a re-ingest reports
        with self.write_lock, self.connect() as con:
            raw = con.execute("SELECT qa FROM files WHERE filename = ?", [name]).fetchone()
            try:
                qa = json.loads(raw[0]) if raw and raw[0] else {}
            except ValueError:
                qa = {}
            if isinstance(qa, dict) and not keep_qa:
                qa["modifier_split_rows"] = int(split_rows)
                qa["pos_in_modifier_rows"] = int(moved_rows)
                qa["future_header_date"] = future_header
                con.execute("UPDATE files SET qa = ? WHERE filename = ?",
                            [json.dumps(qa), name])
        return int(changed)

    def _meta_get(self, con: duckdb.DuckDBPyConnection, key: str) -> str | None:
        try:
            row = con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
            return row[0] if row else None
        except duckdb.Error:
            return None

    def meta_get(self, key: str) -> str | None:
        """Small durable key/value for scheduling state (e.g. the last month
        the auto-refresh ran) — survives restarts, unlike anything in memory."""
        with self.connect() as con:
            return self._meta_get(con, key)

    def meta_set(self, key: str, value: str) -> None:
        with self.write_lock, self.connect() as con:
            con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, str(value)])

    def directory_ready(self) -> bool:
        """True when a materialized tin_directory table exists — the boot-time
        forced name refresh has nothing to add unless new names arrived."""
        try:
            with self.connect() as con:
                return bool(con.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name = 'tin_directory_tbl'").fetchone()[0])
        except Exception:  # noqa: BLE001
            return False

    def rollups_stale(self) -> bool:
        """True when a done rate file finished AFTER the last committed rollup
        covered it — the crash window: URL-queue ingests defer rollups and
        batch them; a kill during/before the batched rebuild leaves files
        'done' (with row counts in the Files tab) whose rates are silently
        missing from every dashboard number, and nothing on restart noticed."""
        try:
            with self.connect() as con:
                if _needs_full_set(self._meta_get(con, "rollup_needs_full")):
                    return True
                if self._meta_get(con, "rollup_plan"):
                    return True  # a sliced build is part-way: it resumes
                if self._read_pending_payers(con)[1]:
                    return True  # a removed part's slices await recompute
                covered = self._meta_get(con, "rollup_covered_through") or ""
                names = set(json.loads(
                    self._meta_get(con, "rollup_covered_names") or "[]"))
                cand = con.execute(
                    "SELECT filename, finished_at::VARCHAR FROM files "
                    "WHERE status = 'done' AND rows_emitted > 0 "
                    "AND finished_at::VARCHAR >= ?", [covered]).fetchall()
                # tie-break by filename: same-microsecond twins of the marker
                # are stale unless the marker explicitly names them
                return any(t > covered or f not in names for f, t in cand)
        except Exception:  # noqa: BLE001 — a probe failure must not block startup
            return False

    def rollup_stale_reason(self) -> str:
        """Plain-language reason the rollups are stale — logged at serve start so
        a slow FULL rebuild isn't a silent mystery. 'needs full rebuild' is the
        one that forces the hours-long whole-store pass; 'N new file(s)' takes
        the fast incremental path."""
        try:
            with self.connect() as con:
                plan = self._plan_get(con)
                if plan is not None:
                    return (f"an analytics build is part-way ({plan.get('next', 0)} of "
                            f"{len(plan['slices'])} parts saved) — it continues where it "
                            "stopped")
                if _needs_full_set(self._meta_get(con, "rollup_needs_full")):
                    return ("a full rebuild is FLAGGED (rollup_needs_full) — a "
                            "removal that couldn't be scoped, or a prior full "
                            "rebuild that didn't finish; this forces the slow "
                            "whole-store pass until it completes once")
                pend = self._read_pending_payers(con)[1]
                if pend:
                    return (f"{len(pend)} payer slice(s) from a removal/replace "
                            "await recompute (incremental)")
                covered = self._meta_get(con, "rollup_covered_through") or ""
                names = set(json.loads(
                    self._meta_get(con, "rollup_covered_names") or "[]"))
                cand = con.execute(
                    "SELECT filename, finished_at::VARCHAR FROM files "
                    "WHERE status = 'done' AND rows_emitted > 0 "
                    "AND finished_at::VARCHAR >= ?", [covered]).fetchall()
                n_new = sum(1 for f, t in cand if t > covered or f not in names)
                if n_new:
                    return f"{n_new} newly ingested file(s) not yet rolled up (incremental)"
                return "not stale"
        except Exception as e:  # noqa: BLE001
            return f"unknown ({e})"

    def _sweep_orphan_tmps(self) -> None:
        """Remove half-written `.{key}.{pid}.parquet.tmp` parts (and
        `.{key}.{pid}.progress` sidecars) whose writing process is gone
        (crash/kill mid-ingest). Temps owned by a LIVE process are left
        alone — that's the whole point of the pid in the name."""
        for p in [*self.rates_dir.glob(".*.parquet.tmp"),
                  *self.rates_dir.glob(".*.spool.tmp"),
                  *self.rates_dir.glob(".*.progress"),
                  *self.rates_dir.glob(".*.progress.tmp"),
                  *self.rates_dir.glob(".*.progress.stop"),
                  *self.rates_dir.glob(".*.qa-spill")]:
            # every name shape carries the pid right after the file key:
            # .{key}.{pid}.parquet.tmp / .spool.tmp (big-file target-item
            # spool, can be large) / .progress[.tmp]
            m = re.match(r"^\.(.+)\.(\d+)\.(parquet\.tmp|spool\.tmp|qa-spill"
                         r"|progress(\.tmp|\.stop)?)$", p.name)
            pid = m.group(2) if m else None
            if pid is not None and _pid_alive(int(pid)):
                continue  # owner is alive — in-progress write, keep it
            try:
                if p.is_dir():   # a dead worker's QA spill folder
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            except OSError:
                pass  # locked by AV/indexer — a leftover tmp is cosmetic, not fatal

    # -- connections --------------------------------------------------------

    def connect(self) -> duckdb.DuckDBPyConnection:
        """Plain connection. Readers never touch the catalog; views are
        (re)registered only at init and inside locked write paths.

        Each connection is told to spill to a temp dir under the store so the
        big rollup GROUP BYs over national files (millions of rows) never OOM —
        DuckDB streams to disk under memory pressure instead."""
        p = _progress.current()
        if p is not _progress._NOOP and getattr(p, "cancelled", False):
            # a stopped run opens no new connection (review 13 Group 2)
            raise _progress.Stopped(getattr(p, "cancel_reason", "") or "stopped")
        con = self._connect_with_retry()
        # The settings below are GLOBAL to the in-process DuckDB instance
        # and are applied once on the pinned connection (see __init__), so a
        # normal per-request connection inherits them and skips the re-SET cost.
        # Only apply here if the pin is absent (older duckdb / open failed) —
        # and NEVER the thread count: in pin-less mode the instance can be torn
        # down between connections, and re-SETting threads on a fresh instance
        # while another thread's query is mid-flight is the deadlock class this
        # store just eliminated. A fresh pin-less instance simply runs DuckDB's
        # default thread count; rollup slice sizing reads the ACTUAL count, so
        # memory stays bounded either way (audit F1).
        if self._pin is None:
            self._apply_settings(con, include_threads=False)
        # review 13 Group 2: a run with a progress bar sees DuckDB's own
        # progress on every connection it opens, and its Stop reaches the
        # connection actually running the statement; a stopped run opens no
        # new one. Nothing at all happens without a token (the no-op).
        if p is not _progress._NOOP:
            try:
                p.attach(con)
            except Exception:  # noqa: BLE001 — a progress bar never fails a query
                pass
        return con

    def _connect_with_retry(self) -> duckdb.DuckDBPyConnection:
        # Transient lock conflicts are a fact of life: the user may open the
        # .duckdb file read-only (CLI, a BI tool) while the app runs, and even
        # a millisecond-held external lock lands exactly between our
        # short-lived connections sometimes. Retry briefly instead of letting
        # one unlucky race kill an hours-long ingest run.
        # duckdb 1.5.x added a second transient: "Unique file handle conflict"
        # (a BinderException), raised when connect() races another in-process
        # connection that is closing the same file — the instance cache is
        # mid-teardown for a few ms and refuses the attach. 1.3.x tolerated
        # this; under 1.5.5 a loaded machine hits it and, unretried, the queue
        # marked a FILE as failed over a wobble that clears in milliseconds
        # (caught by the audit's concurrent-suite run, in three fetch tests).
        # A real binder error (bad SQL, missing table) still raises at once.
        def _transient(e: Exception) -> bool:
            msg = str(e).lower()
            if isinstance(e, duckdb.IOException):
                return "lock" in msg
            return "unique file handle conflict" in msg

        last_exc: Exception | None = None
        for attempt in range(6):
            try:
                return duckdb.connect(str(self.db_path))
            except (duckdb.IOException, duckdb.BinderException) as e:
                if not _transient(e):
                    raise
                last_exc = e
                if attempt < 5:  # no pointless sleep after the final attempt
                    time.sleep(min(0.2 * (2 ** attempt), 3.0))
        raise last_exc  # 6 attempts over ~6s — something genuinely holds it

    def _apply_settings(self, con: duckdb.DuckDBPyConnection,
                        include_threads: bool = False) -> None:
        """Apply the store's spill/memory settings to a connection. GLOBAL to the
        DuckDB instance, so setting them on any one live connection covers all.

        include_threads=True is allowed ONLY from __init__, before any worker
        thread exists: `SET threads` resizes the instance's global task
        scheduler and DEADLOCKS if any other connection has a query in flight
        (it waits for them to quiesce; they wait for the scheduler it seized —
        observed live as a total freeze). The other settings here are
        config/buffer-pool writes that do not quiesce the scheduler."""
        try:
            # every TIMESTAMP this app writes is UTC (_utcnow); make the
            # instance agree, so an aware stamp from any module and SQL
            # current_timestamp land as UTC too. The PC's own zone is read
            # FIRST — the one-time conversion of an older store needs it.
            if self._local_tz is None:
                self._local_tz = str(con.execute(
                    "SELECT current_setting('TimeZone')").fetchone()[0] or "UTC")
            con.execute("SET GLOBAL TimeZone = 'UTC'")
        except duckdb.Error:   # no ICU in this build: naive-UTC stamps still hold
            self._local_tz = self._local_tz or "UTC"
        try:
            con.execute(f"SET temp_directory = '{sql_path(self._tmp_dir)}'")
            con.execute("SET preserve_insertion_order = false")
            # DuckDB's default memory limit is ~80% of system RAM; a rollup
            # rebuild over tens of millions of rows will happily balloon to
            # that before spilling — enough to get the process OOM-killed
            # when anything else is running. But too tight a cap fails the
            # rollup outright (its DISTINCT aggregates cannot spill), so use
            # 40% of RAM clamped to [2GB, 12GB]: kernel-safe headroom AND
            # room for stores with tens of millions of rows.
            con.execute(f"SET memory_limit = '{self._memory_limit_gb}GB'")
            # cap spill so one huge rollup can NEVER fill the disk and take
            # down unrelated work: leave ~20% of free space untouched. The cap
            # is computed ONCE per Store — recomputing per connection let every
            # dashboard poll shrink the cap to 80% of the free space a RUNNING
            # rebuild had already spilled into, strangling the rebuild it was
            # meant to protect.
            if self._temp_cap_gb is None:
                try:
                    free_gb = shutil.disk_usage(self._tmp_dir).free / 1e9
                    self._temp_cap_gb = max(1, int(free_gb * 0.8))
                except OSError:
                    # spill drive vanished between mkdir and first connect
                    # (unplugged USB SSD, dropped share) — a conservative flat
                    # cap keeps the store OPENING; actual spill writes to a
                    # dead drive will surface their own error if it comes to it
                    self._temp_cap_gb = 8
            con.execute(f"SET max_temp_directory_size = '{self._temp_cap_gb}GB'")
            # Thread count: set ONCE, from __init__ only (single-threaded by
            # construction — no worker threads exist yet), and NEVER again for
            # the process lifetime. Capping to ROLLUP_MAX_THREADS keeps rollup
            # slices sized as validated (_rollup_partition_rows divides the
            # budget by the running count) AND bounds concurrent-query CPU/RAM
            # contention. Parser workers are SEPARATE processes, unaffected.
            # FAIL CLOSED (audit F3): if the probe can't confirm the current
            # value differs, do NOT execute the dangerous SET.
            if include_threads:
                try:
                    cur = int(con.execute(
                        "SELECT current_setting('threads')").fetchone()[0])
                except Exception:  # noqa: BLE001
                    cur = None
                if cur is not None and cur != self._rollup_threads:
                    con.execute(f"SET threads = {self._rollup_threads}")
        except duckdb.Error:  # older duckdb without these knobs
            pass

    def close(self) -> None:
        """Release the pinned connection. Safe to call more than once."""
        if self._pin is not None:
            try:
                self._pin.close()
            except duckdb.Error:
                pass
            self._pin = None

    def __del__(self):
        self.close()

    def _init_tables(self, con: duckdb.DuckDBPyConnection) -> None:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                filename VARCHAR PRIMARY KEY,
                payer VARCHAR,
                file_type VARCHAR,
                status VARCHAR,
                schema_version VARCHAR,
                last_updated_on VARCHAR,
                size_bytes BIGINT,
                rows_emitted BIGINT DEFAULT 0,
                ref_groups_skipped BIGINT DEFAULT 0,
                error VARCHAR,
                preflight VARCHAR,
                qa VARCHAR,
                progress DOUBLE DEFAULT 0,
                chunks_done BIGINT DEFAULT 0,
                chunks_total BIGINT DEFAULT 0,
                started_at TIMESTAMP,
                finished_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS npi_directory (
                npi VARCHAR PRIMARY KEY,
                entity_type VARCHAR,
                org_name VARCHAR,
                taxonomy_code VARCHAR,
                taxonomy_desc VARCHAR,
                -- every taxonomy the provider carries, pipe-joined. NPPES
                -- allows 15 and flags only one primary, so a real therapy
                -- clinic can hold its therapy code in a secondary slot
                -- (measured: 9% of therapy providers) — the therapy filter
                -- tests this column as well as the primary.
                taxonomy_codes VARCHAR,
                city VARCHAR,
                state VARCHAR,
                address VARCHAR,
                zip VARCHAR,
                phone VARCHAR,
                enriched_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS provider_refs (
                payer VARCHAR,
                source_file VARCHAR,
                last_updated_on VARCHAR,
                ref_id BIGINT,
                tin_value VARCHAR,
                tin_type VARCHAR,
                npis VARCHAR[]
            );
            CREATE TABLE IF NOT EXISTS entity_map (
                tin_value VARCHAR PRIMARY KEY,
                entity_name VARCHAR
            );
            -- user-verified practice website per tax id (NPPES has no URL field,
            -- so these are the ONLY confirmed URLs — hand-checked, never guessed)
            CREATE TABLE IF NOT EXISTS org_websites (
                tin_value VARCHAR PRIMARY KEY,
                url VARCHAR
            );
            CREATE TABLE IF NOT EXISTS peer_sets (
                name VARCHAR PRIMARY KEY,
                definition VARCHAR,      -- JSON: mode, filters or tin list
                created_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS meta (
                key VARCHAR PRIMARY KEY,
                value VARCHAR
            );
            -- TINs of removed/replaced rate parts, queued (with the payers in
            -- meta 'rollup_pending_payers') for the next incremental rollup:
            -- a directory row depends only on its own TIN's rows, so these are
            -- the ONLY directory rows a removal can change
            CREATE TABLE IF NOT EXISTS rollup_pending_tins (tin_value VARCHAR);
            -- The NPIs each rate PART references (Store.enrichment_progress),
            -- kept per part with the part's size|mtime: a new file costs one
            -- scan of THAT file, never of the whole store
            CREATE TABLE IF NOT EXISTS rate_npi_parts (part VARCHAR PRIMARY KEY, sig VARCHAR);
            -- size + first/last bytes of every ingested source file, so a
            -- download can be recognised as a copy BEFORE it finishes
            CREATE TABLE IF NOT EXISTS content_fingerprints (
                filename VARCHAR PRIMARY KEY,
                size BIGINT,
                head_bytes BIGINT,
                head_sha VARCHAR,
                tail_bytes BIGINT,
                tail_sha VARCHAR,
                recorded_at TIMESTAMP
            );
            -- (legacy column; loaded fingerprints are no longer tied to a row)
            ALTER TABLE content_fingerprints ADD COLUMN IF NOT EXISTS url_id BIGINT;
            -- sha of 8 x 64 KB windows spread through the file, and the
            -- container ('gz' / 'zip' / 'other'): only a compressed file,
            -- whose tail carries a CRC of its whole content, can be proven a
            -- copy early (audit 8 F3)
            ALTER TABLE content_fingerprints ADD COLUMN IF NOT EXISTS win_sha VARCHAR;
            ALTER TABLE content_fingerprints ADD COLUMN IF NOT EXISTS magic VARCHAR;
            -- the IDENTITY of the bytes that were loaded under this name
            -- (review 9 item 1.2): their full sha256 when known, and the
            -- size + mtime the file had when it was read, so the very same
            -- file left in place is recognised without reading it again
            ALTER TABLE content_fingerprints ADD COLUMN IF NOT EXISTS full_sha VARCHAR;
            ALTER TABLE content_fingerprints ADD COLUMN IF NOT EXISTS mtime_ns BIGINT;
            -- 1 = the file's last bytes vouch for its whole content (a gzip
            -- of ONE member: its trailer has the CRC of everything; a zip
            -- whose whole central directory lies in the compared tail),
            -- 0 = they do not (a multi-member gzip), NULL = not known
            -- (review 9 item 1.5). Only 1 lets a copy be skipped early.
            ALTER TABLE content_fingerprints ADD COLUMN IF NOT EXISTS trailer_proves INTEGER;
            -- the CONTENT a parser reads (gzip inflated, the zip member read,
            -- the BOM dropped) and its length: the same document under other
            -- bytes is the same file (review 13 item 5.2.1)
            ALTER TABLE content_fingerprints ADD COLUMN IF NOT EXISTS content_sha VARCHAR;
            ALTER TABLE content_fingerprints ADD COLUMN IF NOT EXISTS content_size BIGINT;
            -- fingerprints of downloads IN FLIGHT, one per queue row. Kept
            -- apart from the loaded ones: a re-fetch of a file under its old
            -- name must never pass for the file already loaded (audit 8 F2)
            CREATE TABLE IF NOT EXISTS inflight_fingerprints (
                url_id BIGINT PRIMARY KEY,
                filename VARCHAR,
                size BIGINT,
                head_bytes BIGINT,
                head_sha VARCHAR,
                tail_bytes BIGINT,
                tail_sha VARCHAR,
                win_sha VARCHAR,
                magic VARCHAR,
                recorded_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS rate_npi_set (part VARCHAR, npi VARCHAR);
            -- What each rate PART holds, per payer and month (rows) and its
            -- TINs — kept per part like rate_npi_set (one scan of a new part,
            -- never of the store): the payers and months LOADED, for the
            -- coverage line and the pickers (review 13 item 1.2), and the
            -- header's counts while the analytics are behind (a raw
            -- count(DISTINCT) over a billion rows every minute is gone)
            CREATE TABLE IF NOT EXISTS part_payer_set (part VARCHAR, payer VARCHAR,
                                                       file_month VARCHAR, n BIGINT);
            CREATE TABLE IF NOT EXISTS part_tin_set (part VARCHAR, tin_value VARCHAR);
            CREATE TABLE IF NOT EXISTS part_set_parts (part VARCHAR PRIMARY KEY, sig VARCHAR);
            -- Which health PLANS a rate file serves, harvested from the payer's
            -- table-of-contents (the rate file itself never names a plan).
            -- Keyed by the URL's dedup_key so it joins to url_queue, which
            -- carries the local filename that rates rows are stamped with —
            -- no column on the 300M-row spine and no re-ingest.
            -- Many-to-many on purpose: payers publish ONE file shared by many
            -- plans, so a rate legitimately belongs to all of them.
            CREATE TABLE IF NOT EXISTS plan_index (
                dedup_key VARCHAR,
                plan_name VARCHAR,
                plan_id VARCHAR,
                plan_id_type VARCHAR,
                market_type VARCHAR,
                PRIMARY KEY (dedup_key, plan_name, plan_id)
            );
            -- Annual price-index values used to restate rates in constant
            -- dollars. The shipped CPI-U table lives in inflation.py; this
            -- holds what the USER added (newer years, or a different basket),
            -- which wins for the same year. Never auto-populated: an index
            -- value this app did not read somewhere must not exist.
            -- One row per imported hospital price-transparency file. The rates
            -- themselves live in hospital/*.parquet behind the hospital_rates
            -- view — deliberately NOT in `rates`: a hospital outpatient rate is
            -- a facility payment under a different mandate, and mixing it into
            -- the spine would corrupt every practice median in the app.
            CREATE TABLE IF NOT EXISTS hospital_files (
                hospital_name VARCHAR PRIMARY KEY,
                state VARCHAR,
                city VARCHAR,
                source_file VARCHAR,
                rows BIGINT,
                last_updated_on VARCHAR,
                imported_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS inflation_index (
                year INTEGER PRIMARY KEY,
                value DOUBLE,
                index_name VARCHAR,
                source VARCHAR,
                added_at TIMESTAMP
            );
            -- State Medicaid / workers'-comp fee schedules the USER loaded.
            -- Never fetched or generated: each state publishes its own document
            -- on its own cadence, and the label the user gives a schedule IS
            -- its provenance.
            CREATE TABLE IF NOT EXISTS floor_schedules (
                kind VARCHAR,          -- medicaid | workers_comp | other
                state VARCHAR,
                year VARCHAR,
                label VARCHAR,
                code VARCHAR,
                rate DOUBLE
            );
            CREATE TABLE IF NOT EXISTS mpfs (
                code VARCHAR,
                locality VARCHAR,
                non_facility_rate DOUBLE,
                source VARCHAR
            );
            CREATE TABLE IF NOT EXISTS url_queue (
                id BIGINT PRIMARY KEY,
                url VARCHAR,
                dedup_key VARCHAR,          -- url without volatile query (signed URLs)
                kind VARCHAR,               -- unknown | in_network | provider_reference | toc
                status VARCHAR,             -- queued | downloading | expanding | ingesting | done | failed | skipped
                parent_id BIGINT,           -- the TOC this url was discovered from
                filename VARCHAR,           -- local filename once downloaded/ingested
                bytes_total BIGINT,
                bytes_done BIGINT,
                progress DOUBLE DEFAULT 0,
                rows_emitted BIGINT DEFAULT 0,
                child_count BIGINT DEFAULT 0,
                error VARCHAR,
                content_sha VARCHAR,        -- sha256 of downloaded bytes (cross-host dupe detection)
                force_size BOOLEAN DEFAULT false,  -- user chose "download anyway" past confirm_over_gb
                added_at TIMESTAMP,
                finished_at TIMESTAMP
            );
            """
        )
        # migrate pre-existing stores created before the outreach columns
        for col in ("address", "zip", "phone", "taxonomy_codes"):
            con.execute(f"ALTER TABLE npi_directory ADD COLUMN IF NOT EXISTS {col} VARCHAR")
        # migrate stores created before the chunked-progress columns
        for col, typ in (("progress", "DOUBLE"), ("chunks_done", "BIGINT"), ("chunks_total", "BIGINT")):
            con.execute(f"ALTER TABLE files ADD COLUMN IF NOT EXISTS {col} {typ}")
        # migrate stores created before content-hash duplicate detection
        con.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS content_sha VARCHAR")
        # migrate stores created before the "download anyway" size override
        con.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS force_size BOOLEAN DEFAULT false")
        # the row rules (ROW_RULES_VERSION) a file's rows were written under;
        # older parts are rewritten once at open (_migrate_row_rules) — plus
        # the file's month (header rule) and its published month (file name)
        con.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS row_rules INTEGER")
        # the billing codes the file was read for ("ALL", or the sorted list
        # joined by ","): after the user ADDS codes, the files read without
        # them can be read again (review 13 item 5.2.4)
        con.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS code_set VARCHAR")
        # the size / mtime / sha of the bytes read, written IN the 'done'
        # commit: a kill after the commit and before the identity row (the
        # rollup runs in between) must not turn the very file still in the
        # inbox into "a different file with the same name" (round-11 kill
        # harness s4, after_commit window, on 0.7.7 before this column)
        con.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS read_identity VARCHAR")
        # the state a Medicare fee-schedule row prices (None: the file didn't say)
        con.execute("ALTER TABLE mpfs ADD COLUMN IF NOT EXISTS state VARCHAR")
        con.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS file_month VARCHAR")
        con.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS published_month VARCHAR")
        # bulk "retry failed" rounds a row has been through: _claim_next serves
        # never-retried rows first (fair scheduling)
        con.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS retries INTEGER DEFAULT 0")
        # a PAUSED download (its payer's server stalled): when it wakes, and
        # how many times it has already been paused (fetch.download_pause_hours)
        con.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS retry_after TIMESTAMP")
        con.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS auto_resumes INTEGER DEFAULT 0")
        # parks in a row with no progress (fetch: download_park_seconds); a
        # paused row with park_count > 0 is PARKED (back within minutes), one
        # with 0 is on the long pause
        con.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS park_count INTEGER DEFAULT 0")
        # the earlier queue row a "waiting" copy waits on (early duplicate
        # check); the loaded file an early-skipped copy duplicated; and the
        # size lane a small-file slot handed the row to
        con.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS waiting_on BIGINT")
        con.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS duplicate_of VARCHAR")
        con.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS big_lane BOOLEAN DEFAULT false")
        # what the payer's server said about the file when it was downloaded
        # (strong ETag, else Last-Modified; and its size), and whether a
        # re-queued row must ask the server again before it reuses or skips
        # anything: 1 = a refresh (unchanged -> done again without a load),
        # 2 = a reload (the bytes must be loaded again; a kept download is
        # reused only if the server still serves it). Review 11 item 1.7.
        con.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS server_validator VARCHAR")
        con.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS server_size BIGINT")
        con.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS revalidate INTEGER DEFAULT 0")

    def _register_views(self, con: duckdb.DuckDBPyConnection) -> None:
        """(Re)point the derived views. Callers must hold write_lock."""
        glob = sql_path(self.rates_dir / "*.parquet")
        if any(self.rates_dir.glob("*.parquet")):
            con.execute(f"CREATE OR REPLACE VIEW rates AS SELECT * FROM read_parquet('{glob}')")
        else:
            cols = ", ".join(
                f"CAST(NULL AS {t}) AS {f.name}"
                for f, t in zip(RATES_SCHEMA, _duck_types())
            )
            con.execute(f"CREATE OR REPLACE VIEW rates AS SELECT * FROM (SELECT {cols}) WHERE FALSE")
        for view, tbl, q in (
            ("rates_dedup", "rates_dedup_tbl", DEDUP_QUERY),
            ("rates_by_tin", "rates_by_tin_tbl", SPINE_LIVE_QUERY.format(part="TRUE")),
            ("tin_directory", "tin_directory_tbl", TIN_DIRECTORY_QUERY.format(part="TRUE")),
        ):
            cols = {r[0] for r in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                [tbl]).fetchall()}
            if not cols:
                con.execute(f"CREATE OR REPLACE VIEW {view} AS {q}")
            elif tbl == "rates_by_tin_tbl" and "is_latest" not in cols:
                # a spine not yet given its stored flag (_migrate_latest_flag
                # failed or has not run): supply it per query — the old cost,
                # the same rows
                con.execute(
                    f"CREATE OR REPLACE VIEW {view} AS SELECT *, coalesce(file_month = "
                    f"max(file_month) OVER (PARTITION BY {LATEST_PARTITION}), FALSE) "
                    f"AS is_latest FROM {tbl}")
            else:
                con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM {tbl}")

    # -- rates parts ---------------------------------------------------------
    # (the old write_rates_part, which wrote DIRECTLY to the live part path with
    # no tmp+rename, is deleted: it had zero callers and a kill mid-write would
    # have left a corrupt half-parquet inside the rates view glob. Every live
    # path goes through RatesPartWriter / finalize_rates_part, which are atomic.)

    def finalize_rates_part(self, source_file: str, tmp_path: Path, rows_written: int) -> None:
        """Adopt a parquet part written OUTSIDE this store (a parallel parse
        worker writes to a temp path with no DB access; only this process
        touches DuckDB). Mirrors RatesPartWriter's atomic finish."""
        path = self.rates_dir / f"{file_key(source_file)}.parquet"
        tmp = Path(tmp_path)
        # scan the OLD part's payers before replacing/removing it (outside the
        # lock): a replacement can carry different per-row payers than the old
        # version, and a 0-row outcome removes the part entirely — either way
        # the old payers' slices must be queued for the incremental rollup
        doomed = self._part_payers_for_removal(path)
        _fsync_path(tmp)            # before the rename that makes it live (4.4b)
        with self.write_lock:
            with self.connect() as con:
                self._queue_removed_payers(con, doomed)
            self._swap_in_part(source_file, tmp, path, rows_written)
            with self.connect() as con:
                self._register_views(con)
        self._invalidate_scan_caches()

    def _swap_in_part(self, source_file: str, tmp: Path, path: Path, rows_written: int) -> None:
        """Under write_lock: the finished part replaces the old one (atomic
        replace — never an unlink-first window for lock-free readers); a
        0-row outcome removes the old one. A Windows lock on the old part
        (a long export) no longer fails a finished, possibly hours-long parse:
        the complete part is PARKED under a name the sweep leaves alone and
        swapped in at the next open or rollup; an old part that can't be
        deleted is retried the same way (_retry_pending_part_ops)."""
        # Whatever an EARLIER run of this file left pending is superseded by
        # this outcome: an older parked replacement must not be adopted over
        # it, and an older "delete this part" must not delete the part this
        # run just wrote (audit 8 P0-2: v1 parked, v2 swapped in, the next
        # rollup adopted v1 over v2; v1 doomed at 0 rows, v2 swapped in, the
        # next rollup deleted v2's part under a 'done' files row)
        with self.connect() as con:
            self._discard_park(con, source_file)
            doomed = json.loads(self._meta_get(con, "doomed_parts") or "[]")
            if path.name in doomed:
                doomed.remove(path.name)
                con.execute("INSERT OR REPLACE INTO meta VALUES ('doomed_parts', ?)",
                            [json.dumps(doomed)])
        if rows_written:
            try:
                # this runs under write_lock: a lock held by an export or
                # antivirus kept EVERY claim and status write waiting up to the
                # full ~90 s retry budget (review 11 item 3.5). Try for ~1.4 s,
                # then park the finished part — _retry_pending_part_ops swaps
                # it in at the next rollup / open
                replace_with_retry(tmp, path, attempts=4)
            except PermissionError:
                park = f".{file_key(source_file)}.parked"
                replace_with_retry(tmp, self.rates_dir / park)
                with self.connect() as con:
                    parked = json.loads(self._meta_get(con, "parked_parts") or "{}")
                    parked[park] = path.name
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('parked_parts', ?)",
                                [json.dumps(parked)])
                logging.getLogger(__name__).warning(
                    "%s: its finished rates couldn't replace the old ones (the old "
                    "file is held open by another program) — kept, and swapped in "
                    "automatically as soon as it is free", source_file)
        else:
            unlink_with_retry(tmp)
            if not unlink_with_retry(path) and path.exists():
                with self.connect() as con:
                    self._note_doomed(con, path.name)

    def rates_part_writer(self, source_file: str) -> "RatesPartWriter":
        """Streaming writer: accept row batches and flush them to the part
        incrementally, so a huge file never holds all its rows in memory.
        Writes to a temp file, then atomically swaps it into place on close."""
        return RatesPartWriter(self, source_file)

    def update_progress(self, filename: str, progress: float,
                        chunks_done: int | None = None, chunks_total: int | None = None) -> None:
        """Lightweight progress write for the dashboard bar (no view churn).
        BEST-EFFORT by contract: it is called from inside multi-hour parse
        loops, and a transient store error here (user opened the .duckdb in a
        BI tool) must never fail the parse it decorates."""
        sets = ["progress = ?"]
        params: list = [round(progress, 3)]
        if chunks_done is not None:
            sets.append("chunks_done = ?")
            params.append(chunks_done)
        if chunks_total is not None:
            sets.append("chunks_total = ?")
            params.append(chunks_total)
        params.append(filename)
        # non-blocking (see url_progress): never wait on a long rollup rebuild
        # holding write_lock just to repaint a progress bar.
        if not self.write_lock.acquire(timeout=1.0):
            return
        try:
            with self.connect() as con:
                con.execute(f"UPDATE files SET {', '.join(sets)} WHERE filename = ?", params)
        except Exception as e:  # noqa: BLE001
            import logging as _logging

            _logging.getLogger(__name__).debug(
                "progress write for %s skipped (%s) — cosmetic only", filename, e)
        finally:
            self.write_lock.release()

    # -- URL queue ----------------------------------------------------------

    def enqueue_url(self, url: str, dedup_key: str, parent_id: int | None = None) -> int | None:
        """Add a URL to the download queue. Deduped by dedup_key (URL without
        volatile signed-query params). Returns the new row id, or None if it's
        already queued/done."""
        with self.write_lock, self.connect() as con:
            return self._enqueue_one(con, url, dedup_key, parent_id)

    def enqueue_urls(self, pairs: list[tuple[str, str]], parent_id: int | None = None,
                     oversize_limit: int = 0, only_if_expanding: bool = False) -> int | None:
        """Batch enqueue under ONE lock + connection — a 2,000-child index
        expansion must not pay a connection/lock cycle per child. Same
        semantics as enqueue_url per row. Returns how many were newly queued
        (or revived). `only_if_expanding`: the children of an index are
        queued only while the index is still 'expanding' — checked in the
        same locked step, so "Clear queue" pressed mid-expansion sticks
        (review 11 item 2.3); None when it no longer is."""
        added = 0
        with self.write_lock, self.connect() as con:
            if only_if_expanding and parent_id is not None:
                st = con.execute("SELECT status FROM url_queue WHERE id = ?",
                                 [parent_id]).fetchone()
                if not st or st[0] != "expanding":
                    return None
            for url, dk in pairs:
                if self._enqueue_one(con, url, dk, parent_id, oversize_limit) is not None:
                    added += 1
        return added

    def save_plan_index(self, rows: list[dict]) -> int:
        """Record which plans each rate-file URL serves (from a payer's TOC).

        Keyed on dedup_key so it survives signed-URL churn and joins to
        url_queue. INSERT OR REPLACE: re-expanding a TOC refreshes the plan
        list rather than duplicating it."""
        if not rows:
            return 0
        # callers pass dedup_key (fetch.py owns that function; the store must
        # not depend on the fetch layer)
        payload = []
        for r in rows:
            dk = r.get("dedup_key")
            if not dk or not r.get("plan_name"):
                continue
            payload.append([dk, r.get("plan_name"), r.get("plan_id") or "",
                            r.get("plan_id_type"), r.get("market_type")])
        if not payload:
            return 0
        # one bulk INSERT OR REPLACE instead of executemany (row-at-a-time,
        # ~420 rows/s, minutes under the write lock for a 200k-plan TOC). A
        # bulk upsert refuses two rows with the same key, which executemany
        # resolved as "last one wins" — so dedupe the same way first.
        last: dict = {}
        for row in payload:
            last[(row[0], row[1], row[2])] = row
        rows_out = list(last.values())
        tbl = pa.table({  # noqa: F841 — scanned by name below
            "dedup_key": pa.array([r[0] for r in rows_out], pa.string()),
            "plan_name": pa.array([r[1] for r in rows_out], pa.string()),
            "plan_id": pa.array([r[2] for r in rows_out], pa.string()),
            "plan_id_type": pa.array([r[3] for r in rows_out], pa.string()),
            "market_type": pa.array([r[4] for r in rows_out], pa.string()),
        })
        with self.write_lock, self.connect() as con:
            con.register("__plans_in", tbl)
            try:
                con.execute(
                    "INSERT OR REPLACE INTO plan_index "
                    "(dedup_key, plan_name, plan_id, plan_id_type, market_type) "
                    "SELECT dedup_key, plan_name, plan_id, plan_id_type, market_type FROM __plans_in")
            finally:
                con.unregister("__plans_in")
        return len(payload)

    def plans_for_payer(self, payer: str | None = None) -> list[dict]:
        """Plans that resolve to INGESTED files, with how many files each
        covers. Only meaningful once a TOC has been expanded — a store built
        from directly-pasted rate links has no plan metadata to show, which
        the caller reports as 'not published' rather than 'no plans'."""
        # plan_index is keyed by URL; url_queue turns that into the local
        # filename; `files` is the only place the payer name lives per file.
        q = """
            SELECT f.payer, p.plan_name, min(p.market_type) AS market_type,
                   count(DISTINCT f.filename) AS files
            FROM plan_index p
            JOIN url_queue q ON q.dedup_key = p.dedup_key
            JOIN files f ON f.filename = q.filename AND f.status = 'done'
        """
        args: list = []
        if payer:
            q += " WHERE f.payer = ?"
            args.append(payer)
        q += " GROUP BY 1, 2 ORDER BY 4 DESC, 2"
        with self.connect() as con:
            try:
                rows = con.execute(q, args).fetchall()
            except duckdb.Error as e:
                # An empty list reads to the caller as "this payer publishes no
                # plan metadata". Never let a query fault masquerade as that.
                logging.getLogger(__name__).warning("plans_for_payer failed: %s", e)
                return []
        return [{"payer": p, "plan_name": n, "market_type": m, "files": f}
                for p, n, m, f in rows]

    def files_for_plan(self, plan_name: str) -> list[str]:
        """The ingested source_file names a plan's rates live in — the join a
        plan-scoped market filter uses."""
        with self.connect() as con:
            try:
                return [r[0] for r in con.execute(
                    "SELECT DISTINCT f.filename FROM plan_index p "
                    "JOIN url_queue q ON q.dedup_key = p.dedup_key "
                    "JOIN files f ON f.filename = q.filename AND f.status = 'done' "
                    "WHERE p.plan_name = ?",
                    [plan_name]).fetchall() if r[0]]
            except duckdb.Error as e:
                logging.getLogger(__name__).warning("files_for_plan failed: %s", e)
                return []

    def _enqueue_one(self, con: duckdb.DuckDBPyConnection, url: str, dedup_key: str,
                     parent_id: int | None, oversize_limit: int = 0) -> int | None:
        exists = con.execute(
            "SELECT id, status, kind, bytes_total FROM url_queue WHERE dedup_key = ? LIMIT 1",
            [dedup_key]).fetchone()
        if exists:
            # Re-adding a failed/over-size link retries it (a fresh URL may
            # carry a new signature; or the user raised confirm_over_gb). A
            # 'skipped' row is only revived by a DIRECT paste (parent_id
            # None): re-expanding a TOC must not undo the user's explicit
            # skip or re-download a known duplicate. force_size is reset so a
            # re-paste re-checks the size ceiling rather than silently
            # inheriting an old "download anyway".
            retryable = (("failed", "oversize") if parent_id is not None
                         else ("failed", "skipped", "oversize"))
            revive = exists[1] in retryable
            if revive and exists[1] == "skipped" and exists[2] == "duplicate":
                # a known COPY pasted again: answered from what is loaded,
                # without a byte (review 13 item 5.2 P2 — it downloaded the
                # file, or 15 MB + 1.5 MB of ranges, to skip it again). The
                # row's retry button still downloads it on purpose.
                twin = con.execute(
                    "SELECT coalesce(q.duplicate_of, t.filename, c.filename) FROM url_queue q "
                    "LEFT JOIN files fd ON fd.filename = q.duplicate_of AND fd.status = 'done' "
                    "LEFT JOIN url_queue t ON t.content_sha = q.content_sha AND t.id <> q.id "
                    "     AND t.status = 'done' "
                    "LEFT JOIN (SELECT c.filename, c.full_sha FROM content_fingerprints c "
                    "           JOIN files f USING (filename) WHERE f.status = 'done') c "
                    "     ON c.full_sha = q.content_sha "
                    "WHERE q.id = ? AND (fd.filename IS NOT NULL OR t.id IS NOT NULL "
                    "                    OR c.filename IS NOT NULL) LIMIT 1",
                    [exists[0]]).fetchone()
                if twin:
                    con.execute(
                        "UPDATE url_queue SET error = ? WHERE id = ?",
                        [f"pasted again: still a copy of {str(twin[0]).split('?')[0]}, which is "
                         "loaded — nothing downloaded (press retry to download it anyway)",
                         exists[0]])
                    return None
            if revive and exists[1] == "oversize" and parent_id is not None:
                # a re-expansion revives an over-size child only when its
                # known size now fits: it re-checked the size (and a chunked
                # one downloaded up to the limit again) every month (review
                # 11 item 2.9)
                revive = bool(oversize_limit) and 0 < int(exists[3] or 0) <= oversize_limit
            if not revive and exists[1] == "done" and parent_id is None \
                    and exists[2] == "in_network" and self.current_code_set:
                # a loaded rate link pasted again after the user ADDED billing
                # codes: it is read again for them (review 13 item 5.2.4 — the
                # guide promised this and it was "0 added, 1 already known")
                had = con.execute(
                    "SELECT f.code_set FROM url_queue q JOIN files f ON f.filename = q.filename "
                    "WHERE q.id = ?", [exists[0]]).fetchone()
                had = had[0] if had else None
                cur = self.current_code_set
                if had and had != "ALL" and had != cur and (
                        cur == "ALL" or set(cur.split(",")) - set(had.split(","))):
                    con.execute(
                        "UPDATE url_queue SET url = ?, status = 'queued', revalidate = 2, "
                        "error = 'reading it again: billing codes were added to your list "
                        "since it was read', force_size = false, "
                        f"{REQUEUE_RESET} WHERE id = ?", [url, exists[0]])
                    return exists[0]
            if not revive and exists[1] == "done" and parent_id is None \
                    and exists[2] in ("toc", "page"):
                # a pasted index / page again: it carries no rates (it anchors
                # no duplicate check), so it is fetched and expanded again —
                # how a capped index takes the files past the cap once
                # max_toc_files is raised (review 11 item 2.1)
                revive = True
            if revive:
                con.execute(
                    "UPDATE url_queue SET url = ?, status = 'queued', error = NULL, "
                    f"force_size = false, {REQUEUE_RESET} WHERE id = ?",
                    [url, exists[0]],
                )
                return exists[0]
            if exists[1] not in ("downloading", "fetched", "ingesting", "expanding"):
                # the same file under a FRESH link (a new signature, a re-pasted
                # list, the index's current child link): keep the newest one so
                # a queued / paused row and a loaded row's next re-check use a
                # link that still works (review 12 items 1.4 and 2.4). A row in
                # flight finishes with the link it started with.
                con.execute("UPDATE url_queue SET url = ? WHERE id = ? AND url IS DISTINCT FROM ?",
                            [url, exists[0], url])
            return None  # already queued / in flight / done
        nid = (con.execute("SELECT coalesce(max(id), 0) + 1 FROM url_queue").fetchone()[0])
        con.execute(
            "INSERT INTO url_queue (id, url, dedup_key, kind, status, parent_id, "
            "bytes_total, bytes_done, progress, added_at) "
            "VALUES (?, ?, ?, 'unknown', 'queued', ?, 0, 0, 0, current_timestamp)",
            [nid, url, dedup_key, parent_id],
        )
        return nid

    def done_download_filenames(self) -> list[str]:
        """Filenames of fully-ingested queue rows — their raw downloads are
        reclaimable (parquet is durable). Feeds the startup sweep that reaps
        files stranded by a kill between the 'done' write and _cleanup_raw."""
        with self.connect() as con:
            rows = con.execute(
                "SELECT DISTINCT filename FROM url_queue "
                "WHERE status = 'done' AND filename IS NOT NULL"
            ).fetchall()
        return [r[0] for r in rows if r[0]]

    def next_queued_url(self, lane: str | None = None, lane_bytes: int = 0) -> dict | None:
        """Claim the next queued download. `lane` "small": only files known
        to be <= lane_bytes, or not yet sized (known-small first); "large":
        files known to be bigger first, then anything (a big-file slot never
        idles while small files wait). None: the plain order."""
        big = (f"(coalesce(big_lane, false) OR coalesce(bytes_total, 0) > {int(lane_bytes)})")
        if lane in ("small", "small_only"):
            # a provider reference list first by its name: rate files loaded
            # before it skip their groups until it lands (review 11 item 2.8)
            rec = self._claim_next(
                "queued", "downloading", where=f"NOT {big}",
                order="(NOT regexp_matches(lower(url), 'provider[-_ ]?ref|reference')), "
                      "(coalesce(bytes_total, 0) = 0), ")
            if rec is not None:
                return rec
            # nothing small (or unsized) is waiting: help with the big files
            # rather than leave the slot idle (with lanes on, a queue of big
            # files ran on half the slots — audit 8 F4). Slot 0 too: keeping
            # it idle for a small file that may never come cost a queue of big
            # files a third of its throughput (review 9 item 5.1)
            return self._claim_next("queued", "downloading", order=f"{big} DESC, ")
        if lane == "large":
            return self._claim_next("queued", "downloading", order=f"{big} DESC, ")
        return self._claim_next("queued", "downloading")

    def next_fetched_url(self) -> dict | None:
        """Claim the next prefetched row (file already on disk) for processing."""
        return self._claim_next("fetched", "ingesting")

    def _claim_next(self, from_status: str, to_status: str, where: str = "",
                    order: str = "") -> dict | None:
        with self.write_lock, self.connect() as con:
            row = con.execute(
                # FAIR order: rows a bulk "retry failed" sent back (flaky or
                # dead links, usually low ids) go after fresh files instead of
                # re-burning the download slots first; id within each tier
                f"SELECT * FROM url_queue WHERE status = '{from_status}' "
                + (f"AND {where} " if where else "")
                + f"ORDER BY {order}coalesce(retries, 0), id LIMIT 1"
            ).fetchall()
            if not row:
                return None
            cols = [d[0] for d in con.description]
            rec = dict(zip(cols, row[0]))
            con.execute("UPDATE url_queue SET status = ? WHERE id = ?", [to_status, rec["id"]])
            if to_status == "ingesting":
                self._live_url_ingests.add(rec["id"])
            return rec

    def url_of(self, url_id: int | None) -> str | None:
        """The stored URL of one queue row (None if absent)."""
        if url_id is None:
            return None
        with self.connect() as con:
            row = con.execute("SELECT url FROM url_queue WHERE id = ?", [url_id]).fetchone()
        return row[0] if row else None

    def update_url(self, url_id: int, only_if_status: str | None = None, **fields) -> bool:
        """Write these fields on one queue row. `only_if_status`: only while
        the row still has that status, checked in the same locked step (an
        index's final 'done' must not overwrite a "Clear queue" pressed during
        its expansion — review 12 item 1.5). True when the row was written."""
        allowed = {"kind", "status", "filename", "bytes_total", "bytes_done",
                   "progress", "rows_emitted", "child_count", "error", "parent_id",
                   "content_sha", "park_count", "duplicate_of", "big_lane",
                   "server_validator", "server_size", "revalidate", "retries"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        terminal = fields.get("status") in ("done", "failed", "skipped", "oversize")
        if terminal:
            fields["finished_at"] = _utcnow()
        if not fields:
            return False
        sets = ", ".join(f"{k} = ?" for k in fields)
        cond = " AND status = ?" if only_if_status else ""
        args = [*fields.values(), url_id] + ([only_if_status] if only_if_status else [])
        with self.write_lock, self.connect() as con:
            hit = con.execute(f"UPDATE url_queue SET {sets} WHERE id = ?{cond} RETURNING id",
                              args).fetchall()
            if terminal and hit:
                # its in-flight fingerprint has done its job (audit 8 F10)
                con.execute("DELETE FROM inflight_fingerprints WHERE url_id = ?", [url_id])
            if hit and fields.get("status") not in (None, "ingesting", "expanding"):
                # no longer loading in this process: it owns no claim any more
                # (review 14 item 2.1)
                self._live_url_ingests.discard(url_id)
        return bool(hit)

    def url_status(self, url_id: int) -> str | None:
        with self.connect() as con:
            r = con.execute("SELECT status FROM url_queue WHERE id = ?", [url_id]).fetchone()
        return r[0] if r else None

    def url_progress(self, url_id: int, bytes_done: int, bytes_total: int | None) -> None:
        """BEST-EFFORT by contract: called from inside download stream loops —
        a transient store error must never fail the download it decorates.

        NON-BLOCKING on the write lock: this runs inside the socket read loop,
        and anything holding write_lock for long (a part swap waiting on a
        Windows lock; claims and status writes from many threads) would stop
        the download reading bytes and let an idle-timeout server drop the
        connection (invariant 3: a cosmetic helper must never break real work).
        Rollups do NOT hold it — they serialize on rollup_lock and take
        write_lock only around view DDL (measured 0.07-1.85 s per rebuild,
        review 11 item 4.7). If the lock is busy we just skip this tick; the
        next one repaints the bar."""
        pct = (100.0 * bytes_done / bytes_total) if bytes_total else 0.0
        if not self.write_lock.acquire(timeout=1.0):
            return  # lock busy (e.g. rollup rebuild) — drop this cosmetic update
        try:
            with self.connect() as con:
                con.execute(
                    # a known size is never overwritten by "unknown" (a
                    # resume from a server without Content-Length reported 0
                    # and sent a big file back to a small-file slot)
                    "UPDATE url_queue SET bytes_done = ?, "
                    "bytes_total = CASE WHEN ? > 0 THEN ? ELSE bytes_total END, "
                    "progress = ? WHERE id = ?",
                    [bytes_done, bytes_total or 0, bytes_total or 0, round(pct, 2), url_id],
                )
        except Exception as e:  # noqa: BLE001
            import logging as _logging

            _logging.getLogger(__name__).debug(
                "download-progress write for url %s skipped (%s) — cosmetic only", url_id, e)
        finally:
            self.write_lock.release()

    def queue_epoch(self) -> int:
        """A counter that moves whenever a URL-queue row was added or changed
        status. Read it BEFORE looking for work, then wait_queue_change(it):
        a change in between returns at once (no lost wake-up)."""
        return self._queue_epoch

    def queue_changed(self) -> None:
        """Wake every thread waiting for queue work (also used on shutdown)."""
        with self._queue_cond:
            self._queue_epoch += 1
            self._queue_cond.notify_all()

    def wait_queue_change(self, seen: int, timeout: float) -> bool:
        """Sleep until the queue changes after `seen`, or `timeout` seconds."""
        with self._queue_cond:
            return self._queue_cond.wait_for(lambda: self._queue_epoch != seen, timeout)

    # Files & imports lists the queue in this order — what is moving now,
    # then what needs the user, then what is finished, then what waits:
    # downloading (and reading an index), processing, downloaded-and-waiting-
    # to-process, too big, paused, failed, done, skipped, queued.
    URL_LIST_ORDER = ("downloading", "expanding", "ingesting", "fetched", "oversize",
                      "paused", "failed", "done", "skipped", "queued")
    _URL_IN_PROGRESS = ("downloading", "expanding", "ingesting", "fetched")

    def list_urls(self, limit: int = 500) -> list[dict]:
        """The queue as Files & imports shows it, in URL_LIST_ORDER. Within a
        status: links the user pasted first (a 764-file index expansion must
        not bury the row they are watching), then in-progress rows in the
        order they started, queued rows in the order they will be downloaded
        (the claim order: retries, then id), and finished rows newest first.

        Which rows are SENT stays bounded: every in-progress row (as many as
        there are download/parse slots), every pasted link, the actionable
        children (failed/skipped/too big/paused: the only rows with retry or
        "download anyway" buttons — a file forgotten mid-grind on a 2,000-row
        queue must keep its button reachable), the newest done children and
        the next queued ones — each slice capped at `limit`. (Taking the
        "newest `limit`" of everything else used to fill the window with
        queued rows and could leave the files downloading RIGHT NOW out of
        it on a big index.)"""
        actionable = "('failed', 'skipped', 'oversize', 'paused')"
        slices = (
            ("status IN ('downloading', 'expanding', 'ingesting', 'fetched')", "id"),
            ("parent_id IS NULL", "id DESC"),
            # actionable rows WHATEVER their parent: past 500 pasted links a
            # failed / too-big / parked pasted row had no button (review 11 2.7)
            (f"status IN {actionable}", "id DESC"),
            ("parent_id IS NOT NULL AND status = 'done'",
             "finished_at DESC NULLS LAST, id DESC"),
            ("parent_id IS NOT NULL AND status = 'queued'", "coalesce(retries, 0), id"),
            # anything else (a status this list does not know yet) still shows
            ("parent_id IS NOT NULL AND status NOT IN ('downloading', 'expanding', "
             f"'ingesting', 'fetched', 'done', 'queued') AND status NOT IN {actionable}",
             "id DESC"),
        )
        with self.connect() as con:
            rows = []
            for where, order in slices:
                rows += con.execute(f"SELECT * FROM url_queue WHERE {where} "
                                    f"ORDER BY {order} LIMIT ?", [limit]).fetchall()
            cols = [d[0] for d in con.description]
        out, seen = [], set()
        for r in rows:
            d = dict(zip(cols, r))
            if d["id"] in seen:
                continue  # slices run as separate queries; a row changing
                # status between them must not render twice in one poll
            seen.add(d["id"])
            out.append(d)
        rank = {s: i for i, s in enumerate(self.URL_LIST_ORDER)}

        def key(d: dict) -> tuple:
            st = d.get("status")
            pasted = 0 if d.get("parent_id") is None else 1
            if st in self._URL_IN_PROGRESS:
                within: tuple = (d["id"],)
            elif st == "queued":
                within = (d.get("retries") or 0, d["id"])
            else:                       # finished / needs the user: newest first
                fin = d.get("finished_at")
                within = (fin is None, -(fin.timestamp() if fin else 0), -d["id"])
            return (rank.get(st, len(rank)), pasted, *within)
        out.sort(key=key)
        for d in out:
            for k in ("added_at", "finished_at", "retry_after"):
                if d.get(k) is not None:
                    d[k] = str(d[k])
        return out

    def url_queue_counts(self) -> dict:
        """Per status — except that a paused copy WAITING on an identical-
        looking download (waiting_on) is counted as 'waiting', not with the
        stalled ones: it is the app saving a download, not a problem
        (audit 8 P2-E)."""
        with self.connect() as con:
            rows = con.execute(
                "SELECT CASE WHEN status = 'paused' AND waiting_on IS NOT NULL "
                "THEN 'waiting' ELSE status END, count(*) FROM url_queue GROUP BY 1").fetchall()
        return {s: n for s, n in rows}

    def record_fingerprint(self, filename: str, fp: dict) -> None:
        """The fingerprint of a file that has been LOADED (written by the
        ingest's success path, or backfilled from an identical copy)."""
        with self.write_lock, self.connect() as con:
            # an upsert: the identity columns (record_identity) survive a
            # fingerprint backfill
            con.execute(
                "INSERT INTO content_fingerprints (filename, size, head_bytes, "
                "head_sha, tail_bytes, tail_sha, recorded_at, url_id, win_sha, magic) "
                "VALUES (?, ?, ?, ?, ?, ?, current_timestamp, NULL, ?, ?) "
                "ON CONFLICT (filename) DO UPDATE SET size = excluded.size, "
                "head_bytes = excluded.head_bytes, head_sha = excluded.head_sha, "
                "tail_bytes = excluded.tail_bytes, tail_sha = excluded.tail_sha, "
                "recorded_at = excluded.recorded_at, win_sha = excluded.win_sha, "
                "magic = excluded.magic",
                [filename, fp["size"], fp["head_bytes"], fp["head_sha"], fp["tail_bytes"],
                 fp["tail_sha"], fp["win_sha"], fp["magic"]])

    def record_identity(self, filename: str, size: int, mtime_ns: int | None,
                        full_sha: str | None, trailer_proves: int | None = None,
                        content_sha: str | None = None,
                        content_size: int | None = None) -> None:
        """What was loaded under `filename`: its size, the mtime it had when
        it was read, its full sha256 (None when not computed) and whether
        its last bytes vouch for its whole content (see the column)."""
        with self.write_lock, self.connect() as con:
            old = con.execute("SELECT size, full_sha FROM content_fingerprints "
                              "WHERE filename = ?", [filename]).fetchone()
            if old and (old[0] != size or (old[1] and full_sha and old[1] != full_sha)):
                # the file under this name now holds DIFFERENT content (its
                # link was re-fetched, or the user chose Replace): copies that
                # were skipped as identical to the OLD content are not
                # identical to anything loaded any more — their content would
                # be nowhere in the store. Queue them again (review 9 5.2)
                n = con.execute(
                    "UPDATE url_queue SET status = 'queued', kind = 'unknown', "
                    "duplicate_of = NULL, content_sha = NULL, retry_after = NULL, "
                    "waiting_on = NULL, progress = 0, bytes_done = 0, "
                    "error = 'the file this was skipped as a copy of was replaced "
                    "by different content — downloading it again' "
                    "WHERE status = 'skipped' AND kind = 'duplicate' AND "
                    "(duplicate_of = ? OR (? IS NOT NULL AND content_sha = ?)) "
                    "RETURNING id", [filename, old[1], old[1]]).fetchall()
                if n:
                    import logging as _logging
                    _logging.getLogger(__name__).info(
                        "%s was replaced by different content: %d copy link(s) skipped "
                        "as identical to the old content are queued again", filename, len(n))
            con.execute(
                "INSERT INTO content_fingerprints (filename, size, recorded_at, "
                "full_sha, mtime_ns, trailer_proves, content_sha, content_size) "
                "VALUES (?, ?, current_timestamp, ?, ?, ?, ?, ?) "
                "ON CONFLICT (filename) DO UPDATE SET full_sha = excluded.full_sha, "
                "mtime_ns = excluded.mtime_ns, trailer_proves = excluded.trailer_proves, "
                "size = excluded.size, content_sha = coalesce(excluded.content_sha, "
                "CASE WHEN content_fingerprints.full_sha IS NOT DISTINCT FROM excluded.full_sha "
                "THEN content_fingerprints.content_sha END), "
                "content_size = coalesce(excluded.content_size, CASE WHEN "
                "content_fingerprints.full_sha IS NOT DISTINCT FROM excluded.full_sha "
                "THEN content_fingerprints.content_size END)",
                [filename, size, full_sha, mtime_ns, trailer_proves, content_sha, content_size])

    def loaded_identity(self, filename: str) -> dict | None:
        """What was loaded under `filename`: the identity row, else — a kill
        between the 'done' commit and the identity row — the read identity
        the commit itself carried (files.read_identity)."""
        with self.connect() as con:
            row = con.execute(
                "SELECT size, mtime_ns, full_sha, trailer_proves FROM content_fingerprints "
                "WHERE filename = ?", [filename]).fetchone()
            if not row:
                try:
                    ri = con.execute("SELECT read_identity FROM files WHERE filename = ? "
                                     "AND status = 'done'", [filename]).fetchone()
                except duckdb.Error:
                    ri = None
                if ri and ri[0]:
                    try:
                        d = json.loads(ri[0])
                        return {"size": d.get("size"), "mtime_ns": d.get("mtime_ns"),
                                "full_sha": d.get("sha"), "trailer_proves": None}
                    except (ValueError, AttributeError):
                        return None
        if not row:
            return None
        return dict(zip(("size", "mtime_ns", "full_sha", "trailer_proves"), row))

    def drop_file_row(self, filename: str) -> None:
        """Remove a files row that never had rates (a held same-name
        candidate the user resolved)."""
        with self.write_lock, self.connect() as con:
            con.execute("DELETE FROM files WHERE filename = ? AND coalesce(rows_emitted, 0) = 0 "
                        "AND status IN ('needs_choice', 'queued')", [filename])

    def done_file_with_sha(self, full_sha: str) -> str | None:
        """A loaded file whose bytes have this sha256."""
        if not full_sha:
            return None
        with self.connect() as con:
            row = con.execute(
                "SELECT c.filename FROM content_fingerprints c JOIN files f "
                "ON f.filename = c.filename WHERE c.full_sha = ? AND f.status = 'done' "
                "ORDER BY c.filename LIMIT 1", [full_sha]).fetchone()
        return row[0] if row else None

    def loaded_fingerprint(self, filename: str) -> dict | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT size, head_bytes, head_sha, tail_bytes, tail_sha, win_sha, magic "
                "FROM content_fingerprints WHERE filename = ?", [filename]).fetchone()
        if not row:
            return None
        return dict(zip(("size", "head_bytes", "head_sha", "tail_bytes", "tail_sha",
                         "win_sha", "magic"), row))

    def drop_fingerprint(self, filename: str) -> None:
        with self.write_lock, self.connect() as con:
            con.execute("DELETE FROM content_fingerprints WHERE filename = ?", [filename])

    def record_inflight(self, url_id: int, filename: str, fp: dict,
                        wait: float | None = None) -> bool:
        """A download's own fingerprint while it is in flight (head only, or
        head + tail + windows). `wait`: give up after this many seconds when
        the writer lock is busy — this runs INSIDE a download stream, which
        must never sit on an idle payer connection behind an index expansion
        (audit 8 F7). Returns whether it was written."""
        if wait is not None:
            if not self.write_lock.acquire(timeout=wait):
                return False
        else:
            self.write_lock.acquire()
        try:
            with self.connect() as con:
                con.execute(
                    "INSERT OR REPLACE INTO inflight_fingerprints VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)",
                    [url_id, filename, fp["size"], fp["head_bytes"], fp["head_sha"],
                     fp.get("tail_bytes"), fp.get("tail_sha"), fp.get("win_sha"),
                     fp.get("magic")])
            return True
        finally:
            self.write_lock.release()

    def inflight_fingerprint(self, url_id: int) -> dict | None:
        with self.connect() as con:
            cur = con.execute("SELECT * FROM inflight_fingerprints WHERE url_id = ?", [url_id])
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row)) if row else None

    def fingerprint_candidates(self, size: int, own_url_id: int | None,
                               own_filename: str | None = None) -> list[dict]:
        """Possible twins of a download of exactly `size` bytes:
        kind 'loaded' — a file whose files row is 'done' (never this row's
        own file name: a re-fetch is not a copy of its old self);
        kind 'inflight' — an EARLIER queue row (lower id: two copies never
        wait on each other) still downloading, downloaded or loading."""
        with self.connect() as con:
            cur = con.execute(
                "SELECT 'loaded' AS kind, c.filename, NULL::BIGINT AS url_id, c.head_bytes, "
                "       c.head_sha, c.tail_bytes, c.tail_sha, c.win_sha, c.magic, "
                "       c.trailer_proves "
                "FROM content_fingerprints c JOIN files f ON f.filename = c.filename "
                "WHERE c.size = ? AND f.status = 'done' AND c.filename IS DISTINCT FROM ? "
                "AND c.head_sha IS NOT NULL "
                "UNION ALL "
                "SELECT 'inflight', i.filename, i.url_id, i.head_bytes, i.head_sha, "
                "       i.tail_bytes, i.tail_sha, i.win_sha, i.magic, NULL::INTEGER "
                "FROM inflight_fingerprints i JOIN url_queue u ON u.id = i.url_id "
                "WHERE i.size = ? AND u.status IN ('downloading', 'fetched', 'ingesting') "
                "  AND i.url_id < ? "
                "ORDER BY 1, 2", [size, own_filename, size, own_url_id if own_url_id is not None else -1])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def park_waiting(self, url_id: int, twin_id: int, minutes: float, error: str) -> None:
        """A copy waits for an EARLIER row with the same beginning: parked,
        tied to that row. It is NOT woken while that row is still in flight
        (no reconnect-and-re-park loop against the payer, audit 8 F1); the
        wake functions let it go once the earlier row is loaded, fails or
        stops, or when release_mismatched_waiters finds it differs."""
        with self.write_lock, self.connect() as con:
            con.execute(
                "UPDATE url_queue SET status = 'paused', waiting_on = ?, error = ?, "
                "retry_after = ?, park_count = 1 WHERE id = ?",
                [twin_id, error, _utcnow() + dt.timedelta(minutes=minutes), url_id])

    def release_mismatched_waiters(self, twin_id: int) -> int:
        """The earlier row finished downloading: rows waiting on it whose own
        full fingerprint DIFFERS from its full fingerprint are not copies —
        let them go now instead of after its (maybe hours-long) load."""
        with self.write_lock, self.connect() as con:
            rows = con.execute(
                "SELECT u.id FROM url_queue u "
                "JOIN inflight_fingerprints me ON me.url_id = u.id "
                "JOIN inflight_fingerprints tw ON tw.url_id = ? "
                "WHERE u.waiting_on = ? AND u.status = 'paused' "
                "  AND me.tail_sha IS NOT NULL AND tw.tail_sha IS NOT NULL "
                "  AND (me.tail_sha <> tw.tail_sha OR coalesce(me.win_sha, '') <> "
                "       coalesce(tw.win_sha, '') OR me.size <> tw.size)",
                [twin_id, twin_id]).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                con.execute("UPDATE url_queue SET waiting_on = NULL, retry_after = NULL "
                            "WHERE id IN (SELECT unnest(?::BIGINT[]))", [ids])
        return len(ids)

    def url_for_filename(self, filename: str) -> str | None:
        """The link a loaded file came from (None for an inbox drop)."""
        with self.connect() as con:
            row = con.execute("SELECT url FROM url_queue WHERE filename = ? "
                              "ORDER BY (status = 'done') DESC, id LIMIT 1", [filename]).fetchone()
        return row[0] if row else None

    def done_filename_with_content(self, content_sha: str, exclude_id: int) -> str | None:
        """The local filename of a DONE queue row with these exact bytes."""
        if not content_sha:
            return None
        with self.connect() as con:
            row = con.execute(
                "SELECT filename FROM url_queue WHERE content_sha = ? AND id != ? "
                "AND status = 'done' AND filename IS NOT NULL ORDER BY id LIMIT 1",
                [content_sha, exclude_id]).fetchone()
        return row[0] if row else None

    def find_url_with_same_content(self, content_sha: str, exclude_id: int) -> tuple[str, str] | None:
        """(url, status) of a DONE queue row whose downloaded bytes were
        identical (Blue plans host copies of each other's national files, so
        the same file arrives under many domains). None if no match.

        Deliberately a done-only FAST PATH: it lets a mirror copy skip before
        wasting a preflight. In-flight ('ingesting') twins are NOT arbitrated
        here — any unlocked skip decision against an in-flight twin can race
        that twin's own skip decision and end with BOTH rows skipped and the
        content never ingested. All in-flight arbitration happens atomically
        in `claim_content_ingest` right before ingest, which is the
        authoritative guard."""
        if not content_sha:
            return None
        with self.connect() as con:
            row = con.execute(
                "SELECT url, status FROM url_queue WHERE content_sha = ? AND id != ? "
                "AND status = 'done' ORDER BY id LIMIT 1",
                [content_sha, exclude_id],
            ).fetchone()
        return (row[0], row[1]) if row else None

    def claim_content_ingest(self, content_sha: str, url_id: int,
                             kind: str | None, filename: str | None,
                             content_sha2: str | None = None,
                             month: str | None = None) -> tuple[str, str] | None:
        """Atomically decide whether THIS row may ingest its content, done
        right before the ingest under the write lock so the twin-check and the
        status flip are ONE indivisible step.

        Returns (twin_url, twin_status) when a byte-identical twin is already
        'done' or 'ingesting' — in that case THIS row is flipped to
        skipped/duplicate (with the explanatory message) INSIDE the same
        locked step. Returns None after winning the claim (this row's status
        is set to 'ingesting' with the given kind/filename).

        Both outcomes must land inside the lock: if the loser were skipped by
        the caller afterwards, two racing twins could each see the other still
        'ingesting' and MUTUALLY defer — both skipped, nobody ingests, and
        revive_skipped_duplicates never fires because neither twin fails.
        With the defer atomic, the second racer sees the first one 'skipped'
        (no longer in-flight) and proceeds: exactly one twin per content
        ingests no matter the order or timing. This also closes the id-order
        hole where a retried lower-id row slips past a higher-id twin already
        ingesting and the same bytes land twice (inflating source_count)."""
        with self.write_lock, self.connect() as con:
            if content_sha:
                row = con.execute(
                    "SELECT url, status FROM url_queue WHERE content_sha = ? AND id != ? "
                    "AND status IN ('done', 'ingesting') AND (status <> 'done' OR ? IS NULL "
                    "OR NOT EXISTS (SELECT 1 FROM files f WHERE f.filename = url_queue.filename "
                    "AND f.file_month IS NOT NULL AND f.file_month <> ?)) "
                    "ORDER BY (status = 'done') DESC, id LIMIT 1",
                    [content_sha, url_id, month, month],
                ).fetchone()
                if row:
                    twin_url, twin_status = row
                    plain = twin_url.split("?")[0]
                    msg = (
                        "identical to a file already ingested (payers host copies "
                        f"of each other's files) — skipped as duplicate of {plain}"
                        if twin_status == "done" else
                        "identical to a link currently being processed "
                        f"({plain}) — will retry automatically from the kept "
                        "download if that one fails"
                    )
                    con.execute(
                        "UPDATE url_queue SET status = 'skipped', kind = 'duplicate', "
                        "error = ?, finished_at = ? WHERE id = ?",
                        [msg, _utcnow(), url_id],
                    )
                    # a terminal row keeps no in-flight fingerprint (review 9 3.5)
                    con.execute("DELETE FROM inflight_fingerprints WHERE url_id = ?", [url_id])
                    return (twin_url, twin_status)
                # the same bytes loaded from the INBOX (or loading from it right
                # now) are the same content too (review 11 item 1.1): one
                # content claim for every path
                other = self._loaded_or_claimed_locked(con, content_sha, filename, month)
                if other:
                    twin_name, twin_status = other
                    msg = (f"identical to {twin_name}, which is already loaded — skipped "
                           "as a duplicate (it is not counted twice)"
                           if twin_status == "done" else
                           f"identical to {twin_name}, which is being loaded from the inbox "
                           "right now — will retry automatically from the kept download "
                           "if that one fails")
                    con.execute(
                        "UPDATE url_queue SET status = 'skipped', kind = 'duplicate', "
                        "error = ?, finished_at = ?, duplicate_of = ? WHERE id = ?",
                        [msg, _utcnow(), twin_name, url_id])
                    con.execute("DELETE FROM inflight_fingerprints WHERE url_id = ?", [url_id])
                    return (twin_name, twin_status)
            # the same CONTENT under other bytes (review 13 item 5.2.1)
            same = self._same_content_locked(con, content_sha2, filename, month)
            if same:
                twin_name, twin_status = same
                msg = (f"the same content as {twin_name}, which is already loaded — the "
                       "same document compressed differently (or saved decompressed); "
                       "skipped so it is not counted twice"
                       if twin_status == "done" else
                       f"the same content as {twin_name}, which is being loaded right now "
                       "— will retry automatically from the kept download if that one fails")
                con.execute(
                    "UPDATE url_queue SET status = 'skipped', kind = 'duplicate', "
                    "error = ?, finished_at = ?, duplicate_of = ? WHERE id = ?",
                    [msg, _utcnow(), twin_name, url_id])
                con.execute("DELETE FROM inflight_fingerprints WHERE url_id = ?", [url_id])
                return (twin_name, twin_status)
            con.execute(
                "UPDATE url_queue SET status = 'ingesting', kind = ?, filename = ? WHERE id = ?",
                [kind, filename, url_id],
            )
            self._live_url_ingests.add(url_id)
            if content_sha2:
                self._content_claims[content_sha2] = filename or str(url_id)
                self._content_claim_owner[content_sha2] = url_id
        return None

    def _same_content_locked(self, con, content_sha2: str | None,
                             own_name: str | None, month: str | None = None
                             ) -> tuple[str, str] | None:
        """Under write_lock: a LOADED file (or one being loaded now) whose
        CONTENT — not its bytes — equals `content_sha2` (review 13 item 5.2.1)
        and whose month is `month` (None: any). The same document published
        for another month is that month's file, not a copy (review 14,
        decided: the month a file name gives was silently dropped)."""
        if not content_sha2:
            return None
        row = con.execute(
            "SELECT c.filename FROM content_fingerprints c JOIN files f "
            "ON f.filename = c.filename WHERE c.content_sha = ? AND f.status = 'done' "
            "AND c.filename IS DISTINCT FROM ? AND " + _SAME_MONTH +
            " ORDER BY c.filename LIMIT 1",
            [content_sha2, own_name, month, month]).fetchone()
        if row:
            return (row[0], "done")
        # a kill between the 'done' commit and its identity row: the commit
        # carried what was read (review 14 item 2.11 — every claim path reads it)
        row = self._read_identity_twin(con, "csha", content_sha2, own_name, month)
        if row:
            return (row, "done")
        claimant = self._content_claims.get(content_sha2)
        if claimant and claimant != own_name:
            uid = self._content_claim_owner.get(content_sha2)
            if uid is not None and uid not in self._live_url_ingests:
                # its link stopped loading without releasing (any exit path
                # the release missed): no owner — never wait on it
                self._content_claims.pop(content_sha2, None)
                self._content_claim_owner.pop(content_sha2, None)
                return None
            return (claimant, "ingesting")
        return None

    def _read_identity_twin(self, con, key: str, value: str | None,
                            own_name: str | None, month: str | None = None) -> str | None:
        """A done file with no identity row whose 'done' commit recorded
        `key` ('sha' or 'csha') = value in files.read_identity (review 14
        item 2.11). Never raises."""
        if not value:
            return None
        try:
            row = con.execute(
                "SELECT f.filename FROM files f LEFT JOIN content_fingerprints c "
                "USING (filename) WHERE f.status = 'done' AND f.read_identity IS NOT NULL "
                "AND (c.filename IS NULL OR (c.full_sha IS NULL AND c.content_sha IS NULL)) "
                f"AND json_extract_string(f.read_identity, '$.{key}') = ? "
                "AND f.filename IS DISTINCT FROM ? AND " + _SAME_MONTH +
                " ORDER BY f.filename LIMIT 1",
                [value, own_name, month, month]).fetchone()
        except duckdb.Error:
            return None
        return row[0] if row else None

    def release_content_claim(self, content_sha2: str | None, name: str | None) -> None:
        if not content_sha2:
            return
        with self.write_lock:
            if self._content_claims.get(content_sha2) == name:
                del self._content_claims[content_sha2]
                self._content_claim_owner.pop(content_sha2, None)

    def _loaded_or_claimed_locked(self, con, content_sha: str,
                                  own_name: str | None, month: str | None = None
                                  ) -> tuple[str, str] | None:
        """Under write_lock: (filename, 'done') of a LOADED file with these
        bytes under another name (of the same month — see
        _same_content_locked), else (filename, 'ingesting') of an inbox file
        with these bytes being loaded right now, else None."""
        row = con.execute(
            "SELECT c.filename FROM content_fingerprints c JOIN files f "
            "ON f.filename = c.filename WHERE c.full_sha = ? AND f.status = 'done' "
            "AND c.filename IS DISTINCT FROM ? AND " + _SAME_MONTH +
            " ORDER BY c.filename LIMIT 1",
            [content_sha, own_name, month, month]).fetchone()
        if row:
            return (row[0], "done")
        row = self._read_identity_twin(con, "sha", content_sha, own_name, month)
        if row:
            return (row, "done")
        claimant = self._inbox_claims.get(content_sha)
        if claimant and claimant != own_name:
            return (claimant, "ingesting")
        return None

    def claim_inbox_content(self, content_sha: str, filename: str,
                            content_sha2: str | None = None,
                            month: str | None = None) -> tuple[str, str] | None:
        """The inbox twin of claim_content_ingest (review 11 item 1.1): may
        `filename` (no queue row — dropped in the inbox, uploaded, chosen on
        the Files tab) load these bytes? Under the SAME write_lock as the link
        path's claim, so the two can never both win. Returns None after
        winning (released by release_inbox_claim), else (twin, status):
        status 'done' — the bytes are already loaded under another name or
        link; 'ingesting' — a link or another inbox file with these bytes is
        being loaded right now (the caller waits and asks again)."""
        if not content_sha:
            return None
        with self.write_lock, self.connect() as con:
            for uid, nm, st in con.execute(
                    "SELECT id, coalesce(filename, url), status FROM url_queue "
                    "WHERE content_sha = ? AND status IN ('done', 'ingesting') "
                    "AND filename IS DISTINCT FROM ? AND (status <> 'done' OR ? IS NULL "
                    "OR NOT EXISTS (SELECT 1 FROM files f WHERE f.filename = url_queue.filename "
                    "AND f.file_month IS NOT NULL AND f.file_month <> ?)) "
                    "ORDER BY (status = 'done') DESC, id",
                    [content_sha, filename, month, month]).fetchall():
                # an 'ingesting' row no thread of THIS process holds is an
                # orphan of a killed run: nothing is loading it, so it is no
                # owner (review 12 item 1.3)
                if st == "done" or uid in self._live_url_ingests:
                    # the loaded file's name (what the Files tab lists), else its link
                    return (nm.split("?", 1)[0], st)
            other = self._loaded_or_claimed_locked(con, content_sha, filename, month)
            if other:
                return other
            same = self._same_content_locked(con, content_sha2, filename, month)
            if same:
                return same
            self._inbox_claims[content_sha] = filename
            if content_sha2:
                self._content_claims[content_sha2] = filename
                self._content_claim_owner[content_sha2] = None
        return None

    def release_inbox_claim(self, content_sha: str | None, filename: str) -> None:
        if not content_sha:
            return
        with self.write_lock:
            if self._inbox_claims.get(content_sha) == filename:
                del self._inbox_claims[content_sha]

    def revive_skipped_duplicates(self, content_sha: str, failed_id: int,
                                  twin_name: str | None = None) -> int:
        """The in-flight twin this sha's duplicates deferred to has FAILED:
        re-queue them. Their downloads were kept on disk, so they ingest
        without re-downloading (expired signed URLs don't matter).

        `twin_name`: also the rows that deferred to that file's CONTENT
        (the same document under other bytes — review 14 item 2.1: they
        match by `duplicate_of`, never by the raw sha, and were never revived)
        while it was loading ("will retry automatically"), not rows skipped as
        copies of a loaded file of that name.

        Only kind='duplicate' rows qualify — those are the ones the DEDUP
        deferred. Rows the USER skipped or forgot share the sha but must
        never be resurrected behind their back."""
        if not content_sha and not twin_name:
            return 0
        where = ("status = 'skipped' AND kind = 'duplicate' AND id != ? AND ("
                 "(? <> '' AND content_sha = ?) OR (? IS NOT NULL AND duplicate_of = ? "
                 "AND error LIKE '%will retry automatically%'))")
        args = [failed_id, content_sha or "", content_sha or "", twin_name, twin_name]
        with self.write_lock, self.connect() as con:
            n = con.execute(f"SELECT count(*) FROM url_queue WHERE {where}", args).fetchone()[0]
            if n:
                con.execute(
                    "UPDATE url_queue SET status = 'queued', "
                    "error = 'the identical link this deferred to failed — "
                    f"retrying (from the kept download when still present)', {REQUEUE_RESET} "
                    f"WHERE {where}", args)
        return n

    def skipped_duplicate_urls(self, content_sha: str, done_id: int) -> list[str]:
        """URLs of dedup-deferred duplicates of this sha (for cleaning their
        kept downloads once a twin has ingested successfully)."""
        if not content_sha:
            return []
        with self.connect() as con:
            rows = con.execute(
                "SELECT url FROM url_queue WHERE content_sha = ? "
                "AND status = 'skipped' AND kind = 'duplicate' AND id != ?",
                [content_sha, done_id],
            ).fetchall()
        return [r[0] for r in rows]

    def url_inflight_for_filename(self, filename: str) -> bool:
        """True if any queue row that previously produced this file is being
        re-processed right now (user pressed retry; the files row keeps its
        old terminal status for the whole download, so forget must ask the
        QUEUE, not just the files table)."""
        with self.connect() as con:
            return con.execute(
                # 'paused' too: a paused (or parked) row wakes by itself,
                # re-downloads and re-ingests the file the user just removed
                "SELECT 1 FROM url_queue WHERE filename = ? AND status IN "
                "('queued', 'downloading', 'fetched', 'expanding', 'ingesting', 'paused') "
                "LIMIT 1",
                [filename],
            ).fetchone() is not None

    def recover_stuck_files(self) -> int:
        """Files-table twin of recover_stuck_urls: an inbox ingest killed
        mid-parse leaves its row at 'processing', and scan_inbox skips
        'processing' files forever. Flip them to failed with a plain reason —
        the next inbox scan re-ingests them. Called when a worker starts
        (same single-owner assumption as recover_stuck_urls)."""
        with self.write_lock, self.connect() as con:
            names = [r[0] for r in con.execute(
                "SELECT filename FROM files WHERE status = 'processing'").fetchall()]
            if names:
                con.execute(
                    "UPDATE files SET status = 'failed', "
                    "error = 'interrupted by a restart — will re-ingest on the next scan' "
                    "WHERE status = 'processing'"
                )
            # 'queued' is a promise of a load in THIS process ("Keep both" /
            # Replace / "ingest anyway" start a thread; a pool restart hands the
            # file back): at start nothing owns it any more. Left 'queued' it
            # could never be removed, retried or chosen again (review 11 item
            # 1.3). A row whose rates are live goes back to 'done' (a chosen
            # replacement still loads from inbox/.replacing at the next scan);
            # any other becomes 'failed' — the next scan loads it when the file
            # is still in the inbox (or inbox/.held), and it can be removed.
            for (qn,) in con.execute(
                    "SELECT filename FROM files WHERE status = 'queued'").fetchall():
                live = (self.rates_dir / f"{file_key(qn)}.parquet").exists()
                con.execute(
                    "UPDATE files SET status = ?, error = ? WHERE filename = ?",
                    ["done" if live else "failed",
                     None if live else ("interrupted by a restart before it loaded — it "
                                        "loads at the next scan if it is still in the "
                                        "inbox; otherwise drop it in again"), qn])
        for n in names:          # an interrupted RE-ingest leaves the old part live
            self.requeue_part_payers(n)
        return len(names)

    def release_claim(self, url_id: int) -> bool:
        """A worker thread died holding this row: put it back in the queue
        NOW (round 7 P1.5), the same way recover_stuck_urls does at the next
        start — a 'fetched' download whose sidecar survived is reused, and
        the content hash prevents a double ingest. Only an in-flight row is
        touched: one that already reached done/failed stays as it is."""
        with self.write_lock, self.connect() as con:
            r = con.execute("SELECT status FROM url_queue WHERE id = ?", [url_id]).fetchone()
            if not r or r[0] not in ("downloading", "fetched", "expanding", "ingesting"):
                return False
            con.execute("UPDATE url_queue SET status = 'queued', progress = 0, bytes_done = 0 "
                        "WHERE id = ?", [url_id])
            self._live_url_ingests.discard(url_id)
            return True

    def recover_stuck_urls(self) -> int:
        """Rows left mid-flight by a crash/Ctrl-C go back to queued so the
        next run resumes them ('fetched' rows whose download + sidecar
        survived are reused as-is; otherwise they re-download — either way
        the content hash prevents any double ingest). Called when a queue
        worker starts."""
        with self.write_lock, self.connect() as con:
            n = con.execute(
                "SELECT count(*) FROM url_queue WHERE status IN "
                "('downloading', 'fetched', 'expanding', 'ingesting')"
            ).fetchone()[0]
            if n:
                con.execute(
                    "UPDATE url_queue SET status = 'queued', progress = 0, bytes_done = 0 "
                    "WHERE status IN ('downloading', 'fetched', 'expanding', 'ingesting')"
                )
            # Stranded dedup twins: a duplicate defers to its in-flight twin
            # with the promise "will retry automatically if that one FAILS" —
            # but the failure write and the twin revival are two separate store
            # calls, so a crash exactly between them leaves the duplicate
            # 'skipped' forever (its twin is terminally 'failed', which the
            # in-flight sweep above never touches). Honor the promise at startup.
            #
            # The trigger is specifically a FAILED twin. A twin that is merely
            # 'skipped' — because the user forgot the ingested file or skipped
            # it by hand — is NOT a failure: reviving off it would silently
            # re-download and re-ingest data the user deliberately erased (the
            # forget path flips the 'done' anchor to 'skipped', which used to
            # satisfy the old "no live/done twin" predicate). So require an
            # actually-failed twin, AND still no live/done twin to defer to.
            revive_pred = (
                "status = 'skipped' AND kind = 'duplicate' AND content_sha IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM url_queue t "
                "  WHERE t.content_sha = url_queue.content_sha AND t.id != url_queue.id "
                "  AND t.status = 'failed') "
                "AND NOT EXISTS (SELECT 1 FROM url_queue t "
                "  WHERE t.content_sha = url_queue.content_sha AND t.id != url_queue.id "
                "  AND t.status IN ('done', 'queued', 'downloading', 'fetched', "
                "                   'expanding', 'ingesting'))")
            revived = con.execute(
                f"SELECT count(*) FROM url_queue WHERE {revive_pred}").fetchone()[0]
            if revived:
                con.execute(
                    "UPDATE url_queue SET status = 'queued', "
                    "error = 'the identical link this deferred to failed — retrying "
                    f"(from the kept download when still present)' WHERE {revive_pred}")
                n += revived
            # A link that deferred to an INBOX load ("being loaded from the
            # inbox right now") is revived only by that load's own `finally` —
            # which a crash never reaches. At start no inbox load can be
            # running (the claim lived in memory), so a deferral whose inbox
            # file did not load (recover_stuck_files made it 'failed') gets its
            # promised retry here (review 12 item 2.2). Not when that name
            # still has a live part (a failed RE-load leaves the old rates):
            # the link would load the same bytes beside them.
            for uid, dup in con.execute(
                    "SELECT q.id, q.duplicate_of FROM url_queue q JOIN files f "
                    "ON f.filename = q.duplicate_of WHERE q.status = 'skipped' "
                    "AND q.kind = 'duplicate' AND f.status = 'failed' ORDER BY q.id").fetchall():
                if (self.rates_dir / f"{file_key(dup)}.parquet").exists():
                    continue
                con.execute(
                    "UPDATE url_queue SET status = 'queued', error = ?, "
                    f"{REQUEUE_RESET} WHERE id = ?",
                    [f"the inbox load of {dup} this deferred to did not finish — retrying "
                     "(from the kept download when still present)", uid])
                n += 1
        return n

    # legal transitions for user actions: retry revives dead rows; skip
    # cancels rows not yet claimed by the worker. Nothing may touch 'done'
    # (its content_sha anchors duplicate detection) or in-flight rows (the
    # worker's final write would silently overwrite the change anyway).
    _USER_TRANSITIONS = {
        # 'oversize' is a too-big-needs-OK terminal state: retry (→queued)
        # works once the user has raised confirm_over_gb, and skip dismisses it
        # 'paused' -> queued is "try now"; -> skipped is "forget it"
        "queued": ("failed", "skipped", "oversize", "paused"),
        "skipped": ("queued", "failed", "oversize", "paused"),
    }

    def set_url_status_by_id(self, url_id: int, status: str) -> bool:
        allowed_from = self._USER_TRANSITIONS.get(status)
        if allowed_from is None:
            return False
        with self.write_lock, self.connect() as con:
            r = con.execute("SELECT status, kind FROM url_queue WHERE id = ?",
                            [url_id]).fetchone()
            # a done INDEX / page may be fetched again (it anchors no duplicate
            # check): its "press retry on this row" is a real button now
            # (review 11 item 2.1)
            if not r or not (r[0] in allowed_from or (
                    status == "queued" and r[0] == "done" and r[1] in ("toc", "page"))):
                return False
            if status == "queued":
                # plain retry must NOT inherit a prior "download anyway" — that
                # override is one-shot; only force_size_requeue re-sets it
                con.execute(
                    "UPDATE url_queue SET status = 'queued', error = NULL, force_size = false, "
                    f"{REQUEUE_RESET} WHERE id = ?", [url_id])
            else:
                con.execute(
                    "UPDATE url_queue SET status = ?, error = NULL, progress = 0, "
                    "bytes_done = 0, force_size = false, retry_after = NULL, park_count = 0, "
                    # a row the USER skipped is never revived automatically —
                    # only kind 'duplicate' rows are (review 11 item 4.1)
                    "kind = CASE WHEN kind = 'duplicate' THEN 'unknown' ELSE kind END, "
                    "waiting_on = NULL, finished_at = ? WHERE id = ?",
                    [status, _utcnow(), url_id])
            return True

    def note_unlisted_children(self, parent_id: int, keys: list[str], note: str) -> int:
        """Loaded children of `parent_id` whose link the index no longer lists
        get `note` on their row; returns how many."""
        with self.write_lock, self.connect() as con:
            got = con.execute(
                "UPDATE url_queue SET error = ? WHERE parent_id = ? AND status = 'done' "
                "AND dedup_key NOT IN (SELECT unnest(?::VARCHAR[])) RETURNING id",
                [note, parent_id, list(keys)]).fetchall()
        return len(got)

    def done_rate_rows(self, dedup_keys: list[str]) -> list[dict]:
        """Loaded rate / reference rows among these links (an index's children
        on a re-expansion): id, url, what the server said when each was
        downloaded, and whether a raw copy name is known."""
        if not dedup_keys:
            return []
        with self.connect() as con:
            # loaded_size: the size the LOAD recorded (content_fingerprints,
            # written for every load): the "then" size of a row a 0.7.4 load
            # left without server_validator / server_size (review 12 item 2.3)
            cur = con.execute(
                "SELECT q.id, q.url, q.dedup_key, q.server_validator, q.server_size, q.filename, "
                "c.size AS loaded_size FROM url_queue q "
                "LEFT JOIN content_fingerprints c ON c.filename = q.filename "
                "WHERE q.dedup_key IN (SELECT unnest(?::VARCHAR[])) AND q.status = 'done' "
                "AND q.kind IN ('in_network', 'provider_reference') ORDER BY q.id",
                [list(dedup_keys)])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def requeue_republished(self, ids: list[int], revalidate: int, reason: str,
                            parent_id: int | None = None,
                            urls: dict | None = None) -> int | None:
        """Loaded rows whose server now serves something else (or can't say):
        back in the queue to be downloaded and loaded again (review 11 1.7).
        With `parent_id`: only while that index is still 'expanding', checked
        in the same locked step (None when it no longer is — the user cleared
        it; review 12 item 1.5). `urls`: id -> the link the index handed over
        now, which the re-download uses (review 12 item 1.4)."""
        if not ids:
            return 0
        with self.write_lock, self.connect() as con:
            if parent_id is not None:
                st = con.execute("SELECT status FROM url_queue WHERE id = ?",
                                 [parent_id]).fetchone()
                if not st or st[0] != "expanding":
                    return None
            for i in ids:
                if urls and urls.get(i):
                    con.execute("UPDATE url_queue SET url = ? WHERE id = ? AND status = 'done'",
                                [urls[i], i])
            n = con.execute(
                "UPDATE url_queue SET status = 'queued', error = ?, force_size = false, "
                f"revalidate = ?, {REQUEUE_RESET} "
                "WHERE id IN (SELECT unnest(?::BIGINT[])) AND status = 'done' RETURNING id",
                [reason, int(revalidate), list(ids)]).fetchall()
        return len(n)

    def requeue_url_for_file(self, filename: str, reason: str) -> bool:
        """Download a loaded file's link again (its raw copy is gone) — the
        payer's provider list for it arrived after it was loaded (review 9
        item 3.2). Only a finished row; True when one was re-queued."""
        with self.write_lock, self.connect() as con:
            row = con.execute(
                "SELECT id FROM url_queue WHERE filename = ? AND status IN ('done', 'skipped') "
                "ORDER BY (status = 'done') DESC, id DESC LIMIT 1", [filename]).fetchone()
            if not row:
                return False
            con.execute(
                "UPDATE url_queue SET status = 'queued', error = ?, force_size = false, "
                f"revalidate = 2, {REQUEUE_RESET} WHERE id = ?", [reason, row[0]])
            return True

    def force_size_requeue(self, url_id: int) -> bool:
        """User pressed 'download anyway' on a row the confirm_over_gb guard
        stopped: set the per-row override and re-queue it. Only a failed or
        skipped row qualifies (same rule as retry); the disk-space guard
        still applies, so this can't fill the drive — it only lifts the
        are-you-sure size ceiling for THIS file."""
        with self.write_lock, self.connect() as con:
            r = con.execute("SELECT status FROM url_queue WHERE id = ?", [url_id]).fetchone()
            if not r or r[0] not in ("failed", "skipped", "oversize"):
                return False
            con.execute(
                "UPDATE url_queue SET status = 'queued', force_size = true, error = NULL, "
                f"{REQUEUE_RESET} WHERE id = ?", [url_id])
            return True

    def known_dedup_keys(self, keys: list[str]) -> set:
        """Which of these dedup keys already have a queue row."""
        if not keys:
            return set()
        with self.connect() as con:
            return {r[0] for r in con.execute(
                "SELECT dedup_key FROM url_queue WHERE dedup_key IN "
                "(SELECT unnest(?::VARCHAR[]))", [list(keys)]).fetchall()}

    def requeue_urls(self, urls: list[str]) -> int:
        """Back to 'queued' (from done/failed/skipped) for these exact URLs —
        the monthly refresh's re-fetch of an index updated in place, and its
        daily retry of a not-yet-posted monthly file. Returns rows changed."""
        if not urls:
            return 0
        with self.write_lock, self.connect() as con:
            n = con.execute(
                "SELECT count(*) FROM url_queue WHERE url IN (SELECT unnest(?::VARCHAR[])) "
                "AND status IN ('done', 'failed', 'skipped')", [list(urls)]).fetchone()[0]
            if n:
                con.execute(
                    "UPDATE url_queue SET status = 'queued', error = NULL, "
                    "retries = coalesce(retries, 0) + 1, force_size = false, "
                    "revalidate = CASE WHEN status = 'done' THEN 1 ELSE 0 END, "
                    f"{REQUEUE_RESET} WHERE url IN (SELECT unnest(?::VARCHAR[])) "
                    "AND status IN ('done', 'failed', 'skipped')", [list(urls)])
        return n

    def pause_url(self, url_id: int, hours: float, error: str) -> None:
        """Set a stalled download aside for `hours`: it holds no slot, isn't
        counted as in flight (a CLI drain finishes), keeps its partial bytes,
        and wake_paused_urls() puts it back in the queue when the time comes."""
        with self.write_lock, self.connect() as con:
            con.execute(
                "UPDATE url_queue SET status = 'paused', error = ?, "
                "retry_after = ? + to_seconds(CAST(? AS DOUBLE)), "
                "auto_resumes = coalesce(auto_resumes, 0) + 1, park_count = 0 WHERE id = ?",
                [error, _utcnow(), float(hours) * 3600.0, url_id])

    def park_url(self, url_id: int, minutes: float, park_count: int, error: str) -> None:
        """Step a download aside for a few minutes (its server sent nothing for
        download_park_seconds): it holds no slot, keeps its partial bytes, and
        rejoins the queue when the cool-down ends — or at once, through
        wake_parked_urls, when no other file is waiting. Not a long pause: it
        does not spend one of the download_auto_resumes."""
        with self.write_lock, self.connect() as con:
            con.execute(
                "UPDATE url_queue SET status = 'paused', error = ?, "
                "retry_after = ? + to_seconds(CAST(? AS DOUBLE)), park_count = ?, "
                "waiting_on = NULL WHERE id = ?",
                [error, _utcnow(), float(minutes) * 60.0, int(park_count), url_id])

    def wake_parked_urls(self, only_if_idle: bool = False) -> int:
        """Every PARKED download back in the queue now, oldest park first in
        the claim order (they keep their ids). `only_if_idle`: only when no
        fresh file is queued — a downloader with nothing else to do goes back
        to the files it stepped away from instead of sitting idle."""
        with self.write_lock, self.connect() as con:
            if only_if_idle and con.execute(
                    "SELECT count(*) FROM url_queue WHERE status = 'queued'").fetchone()[0]:
                return 0
            n = con.execute("SELECT count(*) FROM url_queue WHERE status = 'paused' "
                            f"AND coalesce(park_count, 0) > 0 AND {_NOT_WAITING}").fetchone()[0]
            if n:
                # error cleared like wake_paused_urls: a queued row must not
                # keep reading "parked: ..." (round-7 audit)
                con.execute("UPDATE url_queue SET status = 'queued', retry_after = NULL, "
                            "error = NULL, waiting_on = NULL WHERE status = 'paused' "
                            f"AND coalesce(park_count, 0) > 0 AND {_NOT_WAITING}")
        return n

    def parked_count(self) -> int:
        with self.connect() as con:
            return con.execute("SELECT count(*) FROM url_queue WHERE status = 'paused' "
                               "AND coalesce(park_count, 0) > 0").fetchone()[0]

    def wake_paused_urls(self) -> int:
        """Paused downloads whose time has come go back in the queue (they
        resume from their kept bytes). Returns how many woke."""
        with self.write_lock, self.connect() as con:
            now = _utcnow()
            # a waiting copy's own timer does not wake it while the row it
            # waits on is still in flight; once that row is loaded / failed /
            # stopped it wakes at the next pass (the coordinator calls this
            # every minute), its timer notwithstanding
            due = (f"((retry_after IS NULL OR retry_after <= ?) AND {_NOT_WAITING}) "
                   f"OR (waiting_on IS NOT NULL AND {_NOT_WAITING})")
            n = con.execute(f"SELECT count(*) FROM url_queue WHERE status = 'paused' "
                            f"AND ({due})", [now]).fetchone()[0]
            if n:
                con.execute("UPDATE url_queue SET status = 'queued', error = NULL, "
                            "retry_after = NULL, waiting_on = NULL WHERE status = 'paused' "
                            f"AND ({due})", [now])
        return n

    def requeue_failed(self) -> int:
        """Bulk retry: every failed row back to queued in ONE statement — the
        per-row path capped at list_urls' window silently stranded failures
        beyond the newest 500. Returns rows re-queued."""
        with self.write_lock, self.connect() as con:
            n = con.execute("SELECT count(*) FROM url_queue WHERE status IN "
                            "('failed', 'paused')").fetchone()[0]
            if n:
                # paused rows too: pressing retry means "now", not tomorrow
                con.execute(
                    "UPDATE url_queue SET status = 'queued', error = NULL, "
                    "retries = coalesce(retries, 0) + 1, "
                    # bulk retry never inherits a one-shot "download anyway"
                    f"force_size = false, {REQUEUE_RESET} "
                    "WHERE status IN ('failed', 'paused')"
                )
        return n

    def clear_queued(self) -> int:
        """Bulk-cancel every not-yet-started row (queued / fetched / expanding)
        so a runaway backlog (e.g. a whole national portal) stops feeding the
        parsers. Marks them 'skipped' (retryable — they show a retry button), so
        nothing is destroyed; already-downloaded and in-flight rows are left
        alone. Returns the count cleared."""
        with self.write_lock, self.connect() as con:
            n = con.execute(
                "SELECT count(*) FROM url_queue WHERE status IN "
                "('queued', 'fetched', 'expanding', 'paused')").fetchone()[0]
            if n:
                con.execute(
                    "UPDATE url_queue SET status = 'skipped', kind = 'canceled', "
                    "error = 'cleared from the queue', retry_after = NULL, waiting_on = NULL "
                    "WHERE status IN "
                    "('queued', 'fetched', 'expanding', 'paused')")
        return n

    def cancel_all_downloading(self) -> int:
        """Stop every in-flight download so the freed slots (and the parsers) can
        work through what's already downloaded. Flips 'downloading' rows to
        'skipped' AND records their ids so the running downloader threads abort
        mid-stream (is_download_canceled). Partial .part files are kept — a later
        retry resumes them. Returns the count signalled."""
        with self.write_lock, self.connect() as con:
            ids = [r[0] for r in con.execute(
                "SELECT id FROM url_queue WHERE status = 'downloading'").fetchall()]
            # signal the downloader threads BEFORE the commit: a download
            # finishing in the window after it overwrote the user's skip, and
            # its id lingered in the set to abort that row's NEXT retry
            with self._cancel_lock:
                self._canceled_downloads.update(ids)
            if ids:
                con.execute(
                    "UPDATE url_queue SET status = 'skipped', kind = 'canceled', "
                    "error = 'download stopped by user' WHERE id IN "
                    "(SELECT unnest(?::BIGINT[]))", [ids])
                con.execute("DELETE FROM inflight_fingerprints WHERE url_id IN "
                            "(SELECT unnest(?::BIGINT[]))", [ids])
        return len(ids)

    def resume_canceled(self) -> int:
        """Undo "Clear queue" / "Stop downloads" in one go: every row they
        parked (skipped, kind 'canceled') back to 'queued'. Partial downloads
        resume. Returns rows resumed."""
        with self.write_lock, self.connect() as con:
            n = con.execute("SELECT count(*) FROM url_queue WHERE status = 'skipped' "
                            "AND kind = 'canceled'").fetchone()[0]
            if n:
                con.execute("UPDATE url_queue SET status = 'queued', error = NULL, "
                            f"force_size = false, {REQUEUE_RESET} "
                            "WHERE status = 'skipped' AND kind = 'canceled'")
        return n

    def is_download_canceled(self, url_id: int) -> bool:
        with self._cancel_lock:
            return url_id in self._canceled_downloads

    def clear_download_cancel(self, url_id: int) -> None:
        """The downloader calls this once it has honored a cancel, so a later
        retry of the SAME row isn't instantly canceled again."""
        with self._cancel_lock:
            self._canceled_downloads.discard(url_id)

    def request_parse_stop(self, filename: str) -> bool:
        """Ask the in-flight parse of `filename` to stop. False when the file
        isn't being processed right now (nothing to stop)."""
        with self.connect() as con:
            row = con.execute("SELECT status FROM files WHERE filename = ?",
                              [filename]).fetchone()
        if not row or row[0] != "processing":
            return False
        with self._cancel_lock:
            self._stopped_parses.add(filename)
        return True

    def is_parse_stopped(self, filename: str) -> bool:
        with self._cancel_lock:
            return filename in self._stopped_parses

    def clear_parse_stop(self, filename: str) -> None:
        """Called once the stop has been honored, so a retry of the same file
        isn't stopped again at its first read."""
        with self._cancel_lock:
            self._stopped_parses.discard(filename)

    def requeue_oversize_within(self, limit_bytes: int) -> int:
        """Re-queue 'oversize' rows whose known size fits the CURRENT
        confirm_over_gb — the user raised the limit and restarted, which the
        FAQ presents as the fix, but nothing re-queued the stopped rows (and
        past the UI's row window they had no reachable button at all)."""
        if limit_bytes <= 0:
            return 0
        with self.write_lock, self.connect() as con:
            n = con.execute(
                "SELECT count(*) FROM url_queue WHERE status = 'oversize' "
                "AND bytes_total > 0 AND bytes_total <= ?",
                [limit_bytes]).fetchone()[0]
            if n:
                # bytes_total > 0: a chunked transfer (length unknown) records
                # 0, which "fit" every limit — an 8 GB chunked file re-
                # downloaded 5 GB and tripped oversize on every restart
                con.execute(
                    "UPDATE url_queue SET status = 'queued', error = NULL, force_size = false, "
                    f"{REQUEUE_RESET} WHERE status = 'oversize' AND bytes_total > 0 "
                    "AND bytes_total <= ?", [limit_bytes])
        return n

    def _part_payers_for_removal(self, path: Path) -> tuple[list[str], list[str]] | None:
        """(distinct payers, distinct TINs) of a live part that is about to be
        removed or replaced — the rollup slices the removal can change: the
        rate spine by payer (a grain key), the directory by TIN (each row is
        computed from its own TIN's rows only). Deliberately LOCK-FREE — a
        multi-GB part scan under write_lock would stall every claim/progress/
        upsert for seconds; the per-filename ingest claim already serializes
        writers of one part. Returns ([], []) when no part exists; None when
        the part is unreadable (caller must then force the full-rebuild flag).

        RETRIES a transient read failure before giving up: returning None latches
        `rollup_needs_full`, which forces the hours-long whole-store rebuild — and
        a part briefly held open by an antivirus scan, the Windows indexer, or a
        live reader is a transient condition, not a corrupt file. A permanent
        full-rebuild demand from a 200ms lock is exactly the "why is it doing a
        full rebuild again" trap, so read up to 3 times with short backoffs."""
        if not path.exists():
            return [], []
        last: Exception | None = None
        for i in range(3):
            try:
                with self.connect() as con:
                    src = f"read_parquet('{sql_path(path)}')"
                    payers = [r[0] for r in con.execute(
                        f"SELECT DISTINCT payer FROM {src} WHERE payer IS NOT NULL").fetchall()]
                    tins = [r[0] for r in con.execute(
                        f"SELECT DISTINCT tin_value FROM {src} "
                        "WHERE tin_value IS NOT NULL").fetchall()]
                    return payers, tins
            except Exception as e:  # noqa: BLE001
                last = e
                if i < 2:
                    time.sleep(0.5 * (i + 1))  # 0.5s, 1s — ride out a transient lock
        logging.getLogger(__name__).warning(
            "could not read %s to scope its removal after 3 tries (%s) — forcing "
            "a full analytics rebuild to stay correct", path.name, last)
        return None

    def _read_pending_payers(self, con: duckdb.DuckDBPyConnection) -> tuple[int, list[str]]:
        """(generation, payers) of the queued-removal note. The generation
        bumps on every append so a consumer can tell whether the note changed
        while it worked."""
        raw = self._meta_get(con, "rollup_pending_payers")
        if not raw:
            return 0, []
        try:
            obj = json.loads(raw)
        except ValueError:
            return 0, []
        if isinstance(obj, list):  # tolerate a bare-list shape
            return 0, [p for p in obj if isinstance(p, str)]
        return int(obj.get("gen", 0)), [p for p in obj.get("payers", [])
                                        if isinstance(p, str)]

    def _pending_tins_tracked(self, con: duckdb.DuckDBPyConnection) -> bool:
        """True when every queued removal also recorded its TINs (so the
        directory can be refreshed for just those). A note written by an older
        version, or by any removal whose TINs weren't captured, says False —
        and the consumer then refreshes the whole directory, as it always did."""
        raw = self._meta_get(con, "rollup_pending_payers")
        try:
            obj = json.loads(raw) if raw else {}
        except ValueError:
            return False
        return isinstance(obj, dict) and obj.get("tins_tracked") is True

    def _consume_pending_payers(self, con: duckdb.DuckDBPyConnection,
                                gen0: int, consumed: list[str]) -> None:
        """Under write_lock: drop the consumed payers from the pending note —
        but ONLY if no appender bumped the generation meanwhile. A concurrent
        append (another part removed mid-rollup) may have RE-queued a payer
        whose slice we computed from pre-removal data; leaving the whole note
        intact costs one redundant recompute and never loses a slice."""
        with self.write_lock:
            gen, cur = self._read_pending_payers(con)
            if gen != gen0:
                return
            remain = sorted(set(cur) - set(consumed))
            if remain:
                # (unreachable while gen is unchanged — kept conservative: the
                # TIN list stays, and tins_tracked is dropped so the next
                # consumer refreshes the whole directory)
                con.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('rollup_pending_payers', ?)",
                    [json.dumps({"gen": gen, "payers": remain})])
            else:
                con.execute("DELETE FROM meta WHERE key = 'rollup_pending_payers'")
                con.execute("DELETE FROM rollup_pending_tins")

    def _queue_removed_payers(self, con: duckdb.DuckDBPyConnection,
                              scope: tuple[list[str], list[str]] | None) -> None:
        """Under write_lock: queue removed/replaced slices for the next
        incremental rollup — the part's payers (rate spine) and TINs (name
        directory), as returned by _part_payers_for_removal. A removed part's
        rows vanish from the raw store, but its files row leaves the
        rows_emitted>0 delta — without this note the orphaned rollup slice
        would keep serving the removed rows with rollups_stale() False.
        scope=None (unreadable part) forces the full-rebuild flag instead."""
        try:
            if scope is not None and not scope[0] and not scope[1]:
                return
            if scope is None:
                con.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('rollup_needs_full', ?)", [_needs_full_token()])
                return
            payers, tins = scope
            gen, cur = self._read_pending_payers(con)
            # TINs are tracked only if EVERY queued removal recorded them: an
            # existing note without the flag (older version) stays untracked
            tracked = (not cur) or self._pending_tins_tracked(con)
            if tins:
                con.execute("INSERT INTO rollup_pending_tins "
                            "SELECT unnest(?::VARCHAR[])", [list(tins)])
            con.execute(
                "INSERT OR REPLACE INTO meta VALUES ('rollup_pending_payers', ?)",
                [json.dumps({"gen": gen + 1, "tins_tracked": tracked,
                             "payers": sorted(set(cur) | set(payers))})])
        except Exception:  # noqa: BLE001 — never block the removal itself
            try:
                con.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('rollup_needs_full', ?)", [_needs_full_token()])
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "could not record removed-part payers — rollups may lag "
                    "until the next full rebuild")

    def _discard_park(self, con: duckdb.DuckDBPyConnection, source_file: str) -> None:
        """Forget a PARKED replacement of this file's part (a finished parse
        that could not swap in because the old part was held open): the file
        is being removed, so the park must not be adopted at the next open
        (round 7 P1.9: forget / reset, then a restart, brought the "removed"
        rates back with no files row — and forget could not remove them
        again, because it reads `files`). Caller holds write_lock."""
        park = f".{file_key(source_file)}.parked"
        p = self.rates_dir / park
        if p.exists() and not unlink_with_retry(p) and p.exists():
            logging.getLogger(__name__).warning(
                "%s could not be deleted yet; it will not be adopted", park)
        parked = json.loads(self._meta_get(con, "parked_parts") or "{}")
        if parked.pop(park, None) is not None:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('parked_parts', ?)",
                        [json.dumps(parked)])

    def drop_rates_part(self, source_file: str) -> None:
        path = self.rates_dir / f"{file_key(source_file)}.parquet"
        doomed = self._part_payers_for_removal(path)  # scan OUTSIDE the lock
        with self.write_lock:
            with self.connect() as con:
                self._discard_park(con, source_file)
                self._queue_removed_payers(con, doomed)
                if not unlink_with_retry(path) and path.exists():
                    self._note_doomed(con, path.name)
                self._register_views(con)
        self._invalidate_scan_caches()

    def _note_doomed(self, con: duckdb.DuckDBPyConnection, part_name: str) -> None:
        """A part that should be gone but couldn't be deleted (a long export or
        antivirus held it open on Windows) — remembered in meta and retried at
        every open and before every rollup (_retry_pending_part_ops); the sweep
        ignores *.parquet, so it would otherwise stay in the glob for good while
        the files row says it is gone."""
        cur = json.loads(self._meta_get(con, "doomed_parts") or "[]")
        if part_name not in cur:
            cur.append(part_name)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('doomed_parts', ?)", [json.dumps(cur)])
        logging.getLogger(__name__).warning(
            "%s could not be deleted (held open by another program) — it keeps "
            "being served until it can be; retried automatically", part_name)

    def _retry_pending_part_ops(self) -> None:
        """Finish part operations a locked file blocked: delete doomed parts
        (then queue their payers — the analytics must drop their rows) and
        adopt parked parts (then queue their payers — the analytics must add
        them). Never raises."""
        log = logging.getLogger(__name__)
        try:
            with self.connect() as con:
                doomed = json.loads(self._meta_get(con, "doomed_parts") or "[]")
                parked = json.loads(self._meta_get(con, "parked_parts") or "{}")
        except Exception:  # noqa: BLE001
            return
        if not doomed and not parked:
            return
        still_doomed, still_parked = [], {}
        live_parts: set[str] = set()
        if parked:
            try:
                with self.connect() as con:
                    live_parts = {f"{file_key(r[0])}.parquet" for r in con.execute(
                        "SELECT filename FROM files").fetchall()}
            except Exception:  # noqa: BLE001
                return
        for name in doomed:
            path = self.rates_dir / name
            if not path.exists():
                continue
            scope = self._part_payers_for_removal(path)
            with self.write_lock, self.connect() as con:
                if unlink_with_retry(path) or not path.exists():
                    self._queue_removed_payers(con, scope)
                    self._register_views(con)
                    log.info("removed %s (it was held open earlier)", name)
                else:
                    still_doomed.append(name)
        for park, name in parked.items():
            src, dst = self.rates_dir / park, self.rates_dir / name
            if not src.exists():
                continue
            if name not in live_parts:
                # its file was forgotten / the store reset since it parked:
                # adopting it would bring back rates nothing owns (P1.9)
                if not unlink_with_retry(src) and src.exists():
                    still_parked[park] = name   # retried — never adopted
                continue
            old = self._part_payers_for_removal(dst)
            try:
                # a short try: this runs at every rollup and open, and a part
                # still held open must not stall the rollup ~90 s each time
                replace_with_retry(src, dst, attempts=3)
            except OSError:
                still_parked[park] = name
                continue
            new = self._part_payers_for_removal(dst)
            with self.write_lock, self.connect() as con:
                self._queue_removed_payers(con, old)
                self._queue_removed_payers(con, new)
                self._register_views(con)
            log.info("adopted %s (its file was held open when the parse finished)", name)
        with self.write_lock, self.connect() as con:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('doomed_parts', ?)",
                        [json.dumps(still_doomed)])
            con.execute("INSERT OR REPLACE INTO meta VALUES ('parked_parts', ?)",
                        [json.dumps(still_parked)])
        self._invalidate_scan_caches()

    def requeue_part_payers(self, source_file: str) -> None:
        """A re-ingest that failed / stopped / crashed leaves the OLD part live
        while the files row is not 'done' — invisible to the incremental
        delta. If the rollup marker moved past the file meanwhile, those live
        rows never reached the analytics. Queue the surviving part's payers
        and TINs so the next incremental recomputes them from the raw parts."""
        path = self.rates_dir / f"{file_key(source_file)}.parquet"
        if not path.exists():
            return
        scope = self._part_payers_for_removal(path)
        try:
            with self.write_lock, self.connect() as con:
                self._queue_removed_payers(con, scope)
        except Exception:  # noqa: BLE001 — a failure path must not fail again
            logging.getLogger(__name__).warning(
                "could not queue %s's surviving rates for the analytics", source_file)

    def forget_file(self, filename: str) -> dict:
        """User-driven per-file erasure: delete this file's rates (its parquet
        part), its provider references, and its files-table row, so the user
        can trim the store without a full reset. Any url_queue row that
        produced the file flips to 'skipped' (never deleted: a surviving
        'done' row would dedup-away the same bytes forever with no data behind
        the claim) — its retry button re-downloads if the user wants it back.
        Returns {rows, bytes} removed. Rollups are the caller's job — but the
        removed payers' slices are queued here (rollup_pending_payers), so
        even if the caller's rollup FAILS, rollups_stale() stays True and the
        serve-start catch-up recomputes the slice instead of the "forgotten"
        rows silently living on in every dashboard number."""
        path = self.rates_dir / f"{file_key(filename)}.parquet"
        freed = path.stat().st_size if path.exists() else 0
        doomed = self._part_payers_for_removal(path)  # scan OUTSIDE the lock
        with self.write_lock, self.connect() as con:
            self._discard_park(con, filename)
            self._queue_removed_payers(con, doomed)
            row = con.execute(
                "SELECT rows_emitted FROM files WHERE filename = ?", [filename]
            ).fetchone()
            rows = int(row[0] or 0) if row else 0
            # ORDER MATTERS for retryability: the parquet part goes first
            # (with the view immediately re-registered so readers never see a
            # dangling glob entry), and the files row is deleted LAST inside
            # one transaction. If anything fails part-way, the files row
            # survives — so `forget` can simply be run again — instead of
            # leaving orphan rates that a missing files row makes unreachable.
            if not unlink_with_retry(path) and path.exists():
                # Windows: a long reader (a running full-store export) held the
                # part open past the retry budget. Reporting "forgotten" while
                # the rates survive — and get rebuilt into the rollups — would
                # break the honesty contract; fail honestly and retryably.
                raise RuntimeError(
                    "this file's data is held open by a running query or "
                    "export — let it finish (or close other tools reading the "
                    "store) and press remove again")
            self._register_views(con)
            con.execute("BEGIN")
            try:
                con.execute("DELETE FROM provider_refs WHERE source_file = ?", [filename])
                # Neutralize duplicate twins of the erased content FIRST (while
                # the anchor is still 'done', so the subquery finds its sha):
                # clearing their content_sha makes them permanently ineligible
                # for the failed-twin auto-revive — otherwise a THIRD twin of
                # the same bytes that failed long ago would satisfy the revive
                # predicate on the next restart and silently re-download and
                # re-ingest the data the user just erased. Their retry button
                # still works (retry re-downloads by URL, sha is recomputed).
                con.execute(
                    "UPDATE url_queue SET content_sha = NULL, "
                    "error = 'the identical file this deferred to was removed by "
                    "the user — press retry to re-download it' "
                    "WHERE kind = 'duplicate' AND status = 'skipped' "
                    "AND content_sha IS NOT NULL AND content_sha IN "
                    "  (SELECT content_sha FROM url_queue "
                    "   WHERE filename = ? AND status = 'done' "
                    "   AND content_sha IS NOT NULL)", [filename])
                con.execute(
                    "UPDATE url_queue SET status = 'skipped', "
                    "error = 'data removed by user — press retry to re-download and re-ingest' "
                    "WHERE filename = ? AND status = 'done'", [filename])
                # copies skipped EARLY as duplicates of this file no longer
                # duplicate anything loaded (audit 8 F11)
                con.execute(
                    "UPDATE url_queue SET duplicate_of = NULL, "
                    "error = 'the identical file this was skipped for was removed by "
                    "the user — press retry to download it' "
                    "WHERE duplicate_of = ? AND status = 'skipped'", [filename])
                con.execute("DELETE FROM content_fingerprints WHERE filename = ?", [filename])
                con.execute("DELETE FROM files WHERE filename = ?", [filename])
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except duckdb.Error:
                    pass  # a failed COMMIT already aborted the transaction —
                    # a ROLLBACK error here must not mask the original failure
                raise
        self._invalidate_scan_caches()
        return {"rows": rows, "bytes": freed}

    def _rollup_partition_rows(self, split: int = 1, threads: int = 1) -> int:
        """Rows per hash-partition slice for a rollup rebuild. Capped both by the
        module ceiling (ROLLUP_PARTITION_ROWS, honored so tests can force many
        slices) AND by what fits the memory limit (ROLLUP_ROWS_PER_GB), so a
        small-RAM machine slices finer automatically instead of OOM-ing. `threads`
        divides the budget because DuckDB keeps one un-spillable hash table per
        thread, so a parallel build peaks at ≈threads × slice-size — sizing for
        the thread count lets the fast multi-threaded pass fit on the first try.
        `split` is the retry escalator: each doubling subdivides further when a
        build still hit the limit."""
        mem_scaled = max(1_000_000, self._memory_limit_gb * ROLLUP_ROWS_PER_GB)
        mem_scaled = max(1_000_000, mem_scaled // max(1, threads))
        base = min(ROLLUP_PARTITION_ROWS, mem_scaled)
        return max(1, base // max(1, split))

    def rebuild_rollups(self, names_only: bool = False,
                        skip_if_busy: bool = False) -> float:
        """Materialize the dedup/by-TIN/tin-directory rollups after ingest or
        enrichment so page queries stay fast. The DISTINCT/median/list aggregates
        can't spill to disk, so if a build still hits the memory limit we retry
        single-threaded (per-thread hash tables are the dominant cost) and, if
        that still OOMs, subdivide into progressively smaller hash-partition
        slices until it fits — identical output, lower peak RAM each pass.

        names_only=True rebuilds ONLY the name/geo directory (tin_directory),
        skipping the rate spine (rates_by_tin). The rate spine is built purely
        FROM rates and does NOT depend on NPPES names, so an ENRICHMENT cycle
        (which only resolves names) has no reason to re-run its expensive
        median(DISTINCT) aggregation over every raw row. Doing so on a big store
        rebuilt the whole spine every ~4 minutes during a long identification,
        starving concurrent searches. Ingest (new rates) still does a full
        rebuild."""
        self._retry_pending_part_ops()   # a held-open part delete / swap-in
        import logging as _logging

        log = _logging.getLogger(__name__)
        self._enrich_progress_cache = None  # new rates change the NPI population
        self._store_stats_cache = None
        self._states_cache = None
        # NOTE: data_generation is bumped by _build_rollup_tables AFTER its
        # transaction commits — bumping here (pre-build) let a concurrent poll
        # cache a count computed from the OLD tables under the NEW generation
        # and serve that stale total for the cache TTL after the swap.
        if not self._acquire_rollup_lock(log, skip_if_busy=skip_if_busy):
            return ROLLUP_SKIPPED
        try:
            self._rollup_active = True
            # review 13 item 1.3: a durable plan of slices, each committed on
            # its own — resumed after a kill / restart / failure, queryable
            # while it runs, an out-of-memory slice halved and retried
            took = self._run_plan("names" if names_only else "full", log)
            self._clear_rollup_failure()
            return took
        except RollupPaused:
            raise
        except Exception as e:
            self._note_rollup_failure(e)
            raise
        finally:
            self._rollup_active = False
            self._rollup_job = None
            self.rollup_lock.release()

    def _acquire_rollup_lock(self, log, skip_if_busy: bool = False) -> bool:
        # instrumented lock wait: the stretch between a caller's "updating
        # analytics rollups…" line and the first partition banner had ZERO
        # narration — five silent hours read as a dead app when another
        # rebuild (or a finishing ingest) held this lock. Say so, per minute.
        #
        # skip_if_busy=True (the PERIODIC triggers — ingest rollup, name
        # refresh): if a rebuild is already running, do NOT queue behind it.
        # Blocking-queueing let a second (and third) periodic trigger stack up
        # over a multi-hour rebuild and then each run its OWN multi-hour pass in
        # turn — the "waiting 293 min / 388 min" pile-up that reads as an
        # infinite loop. The running rebuild already covers the current data,
        # and the next scheduled trigger picks up anything that lands after it.
        if skip_if_busy:
            if self.rollup_lock.acquire(blocking=False):
                return True
            log.info("analytics rebuild already running — skipping this refresh "
                     "so it can't stack another multi-hour pass behind it (the "
                     "running rebuild covers current data; the next cycle catches "
                     "the rest)")
            return False
        t_wait = time.monotonic()
        while not self.rollup_lock.acquire(timeout=60.0):
            log.info("analytics rebuild: waiting for the previous rebuild to "
                     "finish (%.0f min so far — this is a queue, not a hang; "
                     "downloads and parsing continue meanwhile)",
                     (time.monotonic() - t_wait) / 60)
        return True

    def rollups_ever_built(self) -> bool:
        """Has a full rollup build ever completed on this store (the
        incremental update's precondition)?"""
        try:
            with self.connect() as con:
                return bool(self._meta_get(con, "rollup_schema_version"))
        except duckdb.Error:
            return True     # can't tell: let the incremental decide

    def update_rollups_incremental(self, skip_if_busy: bool = False) -> float:
        """Incremental alternative to rebuild_rollups(): recompute ONLY the
        slices of the materialized rollups that new files can affect, so the
        cost scales with the ingested payer's data instead of the whole store
        (a full rebuild over ~86M rows is an hour on a hard drive; a
        one-payer slice is minutes).

        Correct by the shape of the grain, not by merging aggregates:
        - rates_by_tin's GROUP BY includes `payer`, so a new file can only
          change groups belonging to its own payer(s). The affected payers'
          slices are DELETEd and re-aggregated exactly from raw rows —
          median(DISTINCT) is not mathematically mergeable, so we recompute
          it on the slice; results are bit-identical to a full rebuild
          (regression-tested). Payer-scoping (not payer+month) also heals a
          re-ingested file whose file_month changed between versions.
        - tin_directory's grain is `tin_value`; only TINs present in the new
          files are recomputed (across all payers, exactly).
        The delta is derived from the store's own coverage marker — every
        done file newer than rollup_covered_through — so nothing can be
        silently skipped, and the marker only advances to the delta's own
        max(finished_at), never past files that finish mid-update.

        Removals (forget, a part re-ingested to 0 rows, a replaced part whose
        old version carried other payers) are handled via the
        `rollup_pending_payers` meta note queued by the removal itself — see
        _queue_removed_payers.

        Raises (RuntimeError/duckdb.Error) whenever the precondition for an
        exact delta doesn't hold — no prior full build, rollup schema
        migration pending, a file with no recorded payer, an unscopable
        removal (`rollup_needs_full`), OOM at max split — and callers fall
        back to rebuild_rollups(), which is always correct."""
        self._retry_pending_part_ops()   # a held-open part delete / swap-in
        import logging as _logging

        log = _logging.getLogger(__name__)
        if not self._acquire_rollup_lock(log, skip_if_busy=skip_if_busy):
            return ROLLUP_SKIPPED
        try:
            self._rollup_active = True
            self._rollup_job = {"kind": "update", "slice": 0, "slices": 1, "work_done": 0,
                                "work_total": 1, "rate": None, "rows_done": 0,
                                "label": "", "started": time.time(), "files": [],
                                "payers": [], "con": None}
            with self.connect() as con:
                t_locked = time.monotonic()
                self._watch_job_con(con)
                with self.write_lock:
                    # view (re)registration is a catalog write — racing another
                    # connection's _register_views raises TransactionException,
                    # and in the worst caller (a finishing multi-hour parse's
                    # finalize) that marked the DONE file failed
                    self._register_views(con)
                if self._meta_get(con, "rollup_schema_version") != str(ROLLUP_SCHEMA_VERSION):
                    raise RollupNeedsFull("the analytics tables were built by an "
                                          "older version (or never built)")
                for tbl in ("rates_by_tin_tbl", "tin_directory_tbl"):
                    if not con.execute(
                            "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_name = ?", [tbl]).fetchone()[0]:
                        raise RollupNeedsFull(f"{tbl} is not materialized yet")
                if _needs_full_set(self._meta_get(con, "rollup_needs_full")):
                    raise RollupNeedsFull("a data removal could not be scoped — "
                                          "full rebuild required")
                plan = self._plan_get(con)
                if plan is not None:
                    raise RollupPlanPending(str(plan.get("kind") or "full"))
                covered = self._meta_get(con, "rollup_covered_through") or ""
                # filenames AT the covered timestamp: a strict `>` alone would
                # permanently skip a file whose done-upsert committed after the
                # marker was stamped but with an identical (same-microsecond)
                # finished_at — silent missing rows with rollups_stale() False
                covered_names = set(json.loads(
                    self._meta_get(con, "rollup_covered_names") or "[]"))
                cand = con.execute(
                    "SELECT filename, finished_at::VARCHAR, coalesce(file_type, '') "
                    "FROM files WHERE status = 'done' AND rows_emitted > 0 "
                    "AND finished_at::VARCHAR >= ?", [covered]).fetchall()
                delta = [(f, t, ft) for f, t, ft in cand
                         if t > covered or f not in covered_names]
                # provider-reference files legitimately have rows_emitted>0
                # but contribute NO rates rows — they advance the marker but
                # must not trip the "no rows found" anomaly check
                rate_delta = [(f, t) for f, t, ft in delta
                              if ft != "provider_reference"]
                # payer slices queued by a removed or replaced part (a file
                # re-ingested to 0 rows leaves the rows_emitted>0 delta; a
                # replaced part may have carried payers its new version
                # doesn't) — the orphaned slices are found via this note
                pend_gen, pending = self._read_pending_payers(con)
                if not delta and not pending:
                    return 0.0
                q = lambda s: "'" + s.replace("'", "''") + "'"  # noqa: E731
                files_pred = ("source_file IN ("
                              + ", ".join(q(f) for f, _t in rate_delta) + ")") if rate_delta else "FALSE"
                # affected payers come from the delta files' ROWS, never from
                # files.payer: multi-licensee books (Highmark et al.) stamp a
                # per-row payer that differs from the file-level label, and
                # scoping by the wrong name would recompute the wrong slice
                # while the new rows silently never landed (caught by test).
                payers = [r[0] for r in con.execute(
                    f"SELECT DISTINCT payer FROM rates WHERE {files_pred}").fetchall()]
                if any(p is None for p in payers):
                    raise RollupNeedsFull("a new file has rows with no payer")
                if rate_delta and not payers and not pending:
                    # the delta files contributed no rows after all (rows_emitted
                    # said otherwise — trust the parts); full rebuild sorts it out
                    raise RollupNeedsFull("no rows found for the new files")
                delta_payers = sorted(set(payers))
                payers = sorted(set(payers) | set(pending))
                self._rollup_job.update(
                    kind="update" if rate_delta else ("remove" if pending else "update"),
                    files=[f for f, _t in rate_delta], payers=payers)
                # A REMOVAL (forget / re-ingest-to-fewer-rows / replaced part)
                # queues pending payers. It can change the tin_directory row of
                # any TIN that lost a SUBSET of its NPIs but still has rates
                # from surviving files (a shared TIN) — and those TINs are NOT
                # in the delta files, while the directory has no payer column to
                # scope by. So on a removal the rate spine stays payer-scoped
                # (fast; payer is a grain key) but the directory is rebuilt in
                # full — correct, and rare (removals don't happen in a normal
                # first-time grind).
                removal = bool(pending)
                # a removal whose TINs were all recorded refreshes ONLY those
                # directory rows (each row depends on its own TIN's rows alone):
                # measured 30+ min -> the slice at 300M rows. An untracked note
                # (older version) keeps the old full-directory refresh.
                dir_scoped = (not removal) or self._pending_tins_tracked(con)
                if payers:
                    payer_pred = "payer IN (" + ", ".join(q(p) for p in payers) + ")"
                    log.info("analytics update: %d new file(s), %d queued slice(s) — "
                             "recomputing %d payer slice(s) instead of the whole store",
                             len(delta), len(pending), len(payers))
                    self._apply_rollup_delta(con, payer_pred, files_pred, log,
                                             directory_scoped=dir_scoped,
                                             with_pending_tins=removal,
                                             narrow_payers=sorted(set(delta_payers)
                                                                  - set(pending)),
                                             wide_payers=sorted(set(pending)))
                else:
                    log.info("analytics update: %d new reference file(s) — "
                             "no rate slices to recompute", len(delta))
                if removal and not dir_scoped:
                    log.info("analytics update: a removal queued by an older "
                             "version — refreshing the full name directory "
                             "(rate slices stayed scoped and fast)")
                    # size slices for the ACTUAL thread count and give this
                    # build the same OOM ladder as every other — the default
                    # (threads=1, no ladder) under-partitioned by ~4x and one
                    # OOM aborted the whole incremental into the hour-long
                    # full-rebuild fallback (verification audit)
                    try:
                        thr = max(1, int(con.execute(
                            "SELECT current_setting('threads')").fetchone()[0]))
                    except Exception:  # noqa: BLE001
                        thr = 1
                    for split in (1, 2, 4, 8, 16, 32, 64):
                        try:
                            self._build_rollup_tables(
                                con, split=split, threads=thr,
                                tables=("tin_directory_tbl",))
                            break
                        except duckdb.OutOfMemoryException:
                            if split == 64:
                                raise
                            log.warning("directory refresh over the memory "
                                        "limit at split=%d; subdividing", split)
                if delta:
                    new_covered = max(t for _f, t, _ft in delta)
                    names = {f for f, t, _ft in delta if t == new_covered}
                    if new_covered == covered:
                        names |= covered_names  # boundary-only delta: keep prior names
                    con.execute(
                        "INSERT OR REPLACE INTO meta VALUES ('rollup_covered_through', ?)",
                        [new_covered])
                    con.execute(
                        "INSERT OR REPLACE INTO meta VALUES ('rollup_covered_names', ?)",
                        [json.dumps(sorted(names))])
                # consume ONLY the pending payers this run actually recomputed,
                # and only if no removal re-queued anything meanwhile — a
                # wholesale DELETE would destroy concurrent notes and their
                # slices would never be recomputed
                self._consume_pending_payers(con, pend_gen, pending)
                self._enrich_progress_cache = None
                self._store_stats_cache = None
                self._states_cache = None
                self._bump_generation()
                # the files this update made visible, each with its time
                # (review 14 item 1.2: the panel's "Added recently")
                self._note_fold_ins("payer update" if rate_delta else "removal")
                took = time.monotonic() - t_locked
                self.last_rollup_build_seconds = took
                log.info("analytics update finished in %.0fs (payer slice, not "
                         "a full rebuild)", took)
                if self.rollup_failure():
                    self._clear_rollup_failure()
                return took
        except RollupNeedsFull:
            raise                   # a precondition, not a failure: the caller builds in full
        except Exception as e:
            # out of memory at the finest slicing, a disk error: noted with its
            # sentence (the header and the panel say it) and retried as THIS
            # update after a back-off — never escalated (review 14 item 1.1d)
            self._note_rollup_failure(e)
            raise
        finally:
            self._rollup_active = False
            self._rollup_job = None
            self.rollup_lock.release()

    def refresh_directory_incremental(self, skip_if_busy: bool = False) -> float:
        """Names-only sibling of update_rollups_incremental: recompute
        tin_directory rows ONLY for TINs touched by NPIs whose NPPES record
        landed since the last refresh (meta `directory_names_through`, keyed
        on npi_directory.enriched_at). The full names_only rebuild
        re-aggregated the ENTIRE store every enrichment cycle — the last
        recurring whole-store scan, and on a big HDD store the thing that
        flattened extraction throughput minutes into every run. Crash-safe:
        enriched_at is durable, so names saved just before a kill are still
        newer than the marker and materialize on the next refresh (>= keeps
        boundary NPIs re-included rather than ever missing one — a redundant
        TIN recompute is harmless). Raises when preconditions fail (no
        materialized tables, schema drift, affected set too large to be
        worth scoping) — callers fall back to
        rebuild_rollups(names_only=True)."""
        import logging as _logging

        log = _logging.getLogger(__name__)
        if not self._acquire_rollup_lock(log, skip_if_busy=skip_if_busy):
            return ROLLUP_SKIPPED
        try:
            self._rollup_active = True
            # the header says what this is (review 14 item 1.1f: it said
            # "Adding the newly loaded files" during a name refresh)
            self._rollup_job = {"kind": "names_refresh", "slice": 0, "slices": 1,
                                "work_done": 0, "work_total": 1, "rate": None,
                                "label": "practice names", "started": time.time(),
                                "files": [], "payers": [], "con": None}
            with self.connect() as con:
                t0 = time.monotonic()
                with self.write_lock:
                    self._register_views(con)
                if self._meta_get(con, "rollup_schema_version") != str(ROLLUP_SCHEMA_VERSION):
                    raise RuntimeError("rollup tables were built by an older "
                                       "version (or never built)")
                # The DIRECTORY version moves independently of the rollup one, and
                # this INSERT feeds TIN_DIRECTORY_QUERY's CURRENT column list into
                # the EXISTING table. Against a directory built before a column was
                # added that's a raw column-count BinderException; only the caller's
                # fallback-to-full-rebuild happened to repair it. Refuse up front so
                # the intended path (full names rebuild) runs deliberately.
                if self._meta_get(con, "directory_schema_version") != str(
                        DIRECTORY_SCHEMA_VERSION):
                    raise RuntimeError("the practice directory was built by an "
                                       "older version — a full names rebuild "
                                       "must run first")
                if not con.execute(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_name = 'tin_directory_tbl'").fetchone()[0]:
                    raise RuntimeError("tin_directory is not materialized yet")
                covered = self._meta_get(con, "directory_names_through")
                if covered is None:
                    # never stamped -> a full names build must run once first
                    raise RuntimeError("no names-coverage marker yet")
                new_max = con.execute(
                    "SELECT coalesce(max(enriched_at)::VARCHAR, '') "
                    "FROM npi_directory").fetchone()[0]
                if not new_max:
                    return 0.0
                con.execute("DROP TABLE IF EXISTS __delta_tins")
                con.execute(
                    "CREATE TEMP TABLE __delta_tins AS "
                    "SELECT DISTINCT r.tin_value FROM rates r "
                    "JOIN npi_directory d ON d.npi = r.npi "
                    # the directory holds no NPI-in-the-TIN-slot rows: they
                    # made the log say "4,007 of 3,511 TINs" (review 14 G3)
                    "WHERE r.tin_value IS NOT NULL AND NOT r.tin_is_really_npi "
                    "AND d.enriched_at::VARCHAR >= ?",
                    [covered])
                n_new = con.execute("SELECT count(*) FROM __delta_tins").fetchone()[0]
                if n_new:
                    total = con.execute(
                        "SELECT count(*) FROM tin_directory_tbl").fetchone()[0] or 0
                    if total and n_new > max(1000, total // 4):
                        raise RollupNeedsFull(
                            f"{n_new:,} of {total:,} TINs affected — the full "
                            "directory rebuild is cheaper")
                    tin_pred = "tin_value IN (SELECT tin_value FROM __delta_tins)"
                    con.execute("BEGIN TRANSACTION")
                    try:
                        con.execute(f"DELETE FROM tin_directory_tbl WHERE {tin_pred}")
                        con.execute("INSERT INTO tin_directory_tbl "
                                    + TIN_DIRECTORY_QUERY.format(part=tin_pred))
                        con.execute("COMMIT")
                    except Exception:
                        try:
                            con.execute("ROLLBACK")
                        except duckdb.Error:
                            pass
                        raise
                con.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('directory_names_through', ?)",
                    [new_max])
                self._bump_generation(spine=False)
                took = time.monotonic() - t0
                log.info("name refresh: %d TIN(s) updated in %.0fs (scoped to "
                         "newly identified NPIs, not the whole store)", n_new, took)
                return took
        finally:
            self._rollup_active = False
            self._rollup_job = None
            self.rollup_lock.release()

    def rollup_in_progress(self) -> bool:
        """True while a rollup is aggregating — the enrichment loop yields its
        own whole-store NPI scan to it (see enrich._wait_out_rollup)."""
        return self._rollup_active

    def _apply_rollup_delta(self, con: duckdb.DuckDBPyConnection, payer_pred: str,
                            files_pred: str, log, directory_scoped: bool = True,
                            with_pending_tins: bool = False,
                            narrow_payers: list[str] | None = None,
                            wide_payers: list[str] | None = None) -> None:
        """Delete-and-recompute the affected rollup slices. Same OOM ladder as
        the full build (thread cap first, then finer hash partitions); same
        one-transaction-per-table + CHECKPOINT discipline (a mid-slice failure
        rolls back to that table's previous complete version).

        What is recomputed (review 14 item 1.1e):
        - the SPINE: a payer with a queued removal (`wide_payers`) in full; a
          payer that only gained files (`narrow_payers`) only for the TINs
          those files carry — every spine row and its `is_latest` contract are
          inside one (payer, TIN), so the rows of a TIN the new files do not
          touch cannot change. A new 2 GB file of a 300M-row payer used to
          re-aggregate all 300M rows.
        - the DIRECTORY: the delta TINs, read only from the parts that hold
          them (the kept per-part TIN sets) instead of every part of the
          store (10.3 s of a 11.6 s fold-in on the 99M store).

        directory_scoped=False rebuilds ONLY the rate spine here and leaves the
        directory to the caller (an untracked removal needs the FULL directory —
        see update_rollups_incremental). with_pending_tins adds the TINs that
        queued removals recorded (rollup_pending_tins) to the directory slice.
        Called without narrow/wide payers it keeps the old payer-wide rule."""
        # READ the current thread count for partition sizing — NEVER `SET
        # threads`. SET threads reconfigures the shared instance's GLOBAL task
        # scheduler; on a live store with other connections mid-query (the
        # dashboard, the enrichment scan, a files UPDATE) it DEADLOCKS — SET
        # threads waits for those queries to quiesce while they wait for the
        # scheduler it seized (observed live: every DuckDB thread frozen at its
        # query line, disk 0%, for 20+ minutes). Memory is bounded purely by
        # hash-partition slicing, sized for the running thread count
        # (_rollup_partition_rows divides the budget by threads, so more
        # threads just yields more, smaller slices).
        try:
            threads = int(con.execute("SELECT current_setting('threads')").fetchone()[0])
        except Exception:  # noqa: BLE001 — older duckdb / odd value
            threads = 1
        threads = max(1, threads)
        q = lambda s: "'" + s.replace("'", "''") + "'"  # noqa: E731
        # affected TINs: computed ONCE from the delta files' parts (per-file
        # parquet stats prune every other file's part from this scan)
        con.execute("DROP TABLE IF EXISTS __delta_file_tins")
        con.execute("CREATE TEMP TABLE __delta_file_tins AS "
                    f"SELECT DISTINCT tin_value FROM rates WHERE {files_pred}")
        null_tin = bool(con.execute(
            "SELECT count(*) FROM __delta_file_tins WHERE tin_value IS NULL").fetchone()[0])
        con.execute("DROP TABLE IF EXISTS __delta_tins")
        pending_tins = ("UNION SELECT tin_value FROM rollup_pending_tins "
                        "WHERE tin_value IS NOT NULL") if with_pending_tins else ""
        con.execute("CREATE TEMP TABLE __delta_tins AS "
                    "SELECT tin_value FROM __delta_file_tins WHERE tin_value IS NOT NULL "
                    f"{pending_tins}")
        tin_pred = "tin_value IN (SELECT tin_value FROM __delta_tins)"
        spine_pred = payer_pred
        if narrow_payers is not None or wide_payers is not None:
            ors = []
            if wide_payers:
                ors.append("payer IN (" + ", ".join(q(p) for p in wide_payers) + ")")
            if narrow_payers:
                tins = "tin_value IN (SELECT tin_value FROM __delta_file_tins)"
                if null_tin:
                    tins = f"({tins} OR tin_value IS NULL)"
                ors.append("(payer IN (" + ", ".join(q(p) for p in narrow_payers)
                           + f") AND {tins})")
            spine_pred = "(" + " OR ".join(ors) + ")" if ors else "FALSE"
        # where the directory reads: only the parts holding a delta TIN when
        # the kept part sets can say so exactly (else every part, as before)
        dir_src = "rates"
        n_rows = None
        try:
            if self._part_sets_current(con, 60.0):
                live = {p.name for p in self.rates_dir.glob("*.parquet")}
                holding = sorted(r[0] for r in con.execute(
                    "SELECT DISTINCT part FROM part_tin_set WHERE tin_value IN "
                    "(SELECT tin_value FROM __delta_tins)").fetchall() if r[0] in live)
                if directory_scoped and holding and len(holding) < 0.9 * len(live):
                    files = ", ".join("'" + sql_path(self.rates_dir / h) + "'" for h in holding)
                    con.execute("CREATE OR REPLACE TEMP VIEW __rates_dir AS "
                                f"SELECT * FROM read_parquet([{files}])")
                    dir_src = "__rates_dir"
                    log.info("analytics update: the name directory reads %d of %d "
                             "parts (the ones holding the new files' TINs)",
                             len(holding), len(live))
                # rows the payer slices cover, from the kept per-part counts
                # (an upper bound for the TIN-scoped ones: only sizes slices)
                if payer_pred != "FALSE":
                    n_rows = con.execute(
                        f"SELECT coalesce(sum(n), 0) FROM part_payer_set WHERE {payer_pred} "
                        "AND part IN (SELECT part FROM part_set_parts)").fetchone()[0]
        except Exception as e:  # noqa: BLE001 — the unscoped read is always right
            log.debug("part sets unavailable for the delta (%s) — reading every part", e)
            dir_src = "rates"
        if n_rows is None:
            n_rows = con.execute(
                f"SELECT count(*) FROM rates WHERE {payer_pred}").fetchone()[0] or 0
        # escalate PARTITION fineness on OOM (never thread count) until it fits
        for split in (1, 2, 4, 8, 16, 32, 64):
            try:
                self._apply_rollup_delta_sliced(con, spine_pred, tin_pred, int(n_rows or 0),
                                                split=split, threads=threads, log=log,
                                                directory_scoped=directory_scoped,
                                                dir_src=dir_src)
                return
            except duckdb.OutOfMemoryException:
                if split == 64:
                    log.error(
                        "analytics update cannot fit in the current memory "
                        "limit even at the finest slicing — raise "
                        "duckdb_memory_gb in config/mrfx.yaml and restart")
                    raise  # the scheduler retries THIS update after a back-off
                log.warning("analytics update over the memory limit at split=%d; "
                            "subdividing into finer partitions", split)

    def _apply_rollup_delta_sliced(self, con: duckdb.DuckDBPyConnection, payer_pred: str,
                                   tin_pred: str, n_rows: int, split: int,
                                   threads: int, log, directory_scoped: bool = True,
                                   dir_src: str = "rates") -> None:
        rows_per_part = self._rollup_partition_rows(split, threads)
        parts = max(1, -(-n_rows // rows_per_part))
        specs = [("rates_by_tin_tbl", SPINE_QUERY, "billing_code || coalesce(tin_value, '')",
                  payer_pred, "the payer rates")]
        if directory_scoped:
            # a removal skips this and rebuilds the directory in full afterward
            specs.append(("tin_directory_tbl", _directory_query(dir_src), "tin_value",
                          tin_pred, "the practice directory"))
        job = self._rollup_job if isinstance(self._rollup_job, dict) else {}
        job.update(work_total=parts * len(specs), work_done=0, slice=0,
                   slices=parts * len(specs), rows_done=0, rate=None,
                   unit_t0=time.monotonic(), rows_total=n_rows)
        t_rows0 = time.monotonic()
        unit = 0
        for tbl, query, key, pred, words in specs:
            con.execute("BEGIN TRANSACTION")
            try:
                # the slice is deleted and re-aggregated inside ONE txn: a
                # reader never sees the slice missing, and a failure rolls
                # back to the previous complete table
                con.execute(f"DELETE FROM {tbl} WHERE {pred}")
                for i in range(parts):
                    part = pred if parts == 1 else f"({pred}) AND hash({key}) % {parts} = {i}"
                    if parts > 1:
                        log.info("analytics update %s: partition %d/%d", tbl, i + 1, parts)
                    job.update(slice=unit, label=words + (f", part {i + 1} of {parts}"
                                                          if parts > 1 else ""),
                               unit_t0=time.monotonic())
                    # BY NAME: a spine that gained is_latest in place
                    # (_migrate_latest_flag) has it as its LAST column
                    con.execute(f"INSERT INTO {tbl} BY NAME {query.format(part=part)}")
                    unit += 1
                    if tbl == "rates_by_tin_tbl":
                        job["rows_done"] = int(job.get("rows_done") or 0) + n_rows // parts
                    el = time.monotonic() - t_rows0
                    job.update(work_done=unit,
                               rate=(job["rows_done"] / el) if el > 0 and job.get("rows_done") else None)
                # (no whole-store ghost prune here: every TIN whose rows could
                # have vanished — a removed or shrunk part's — is in tin_pred
                # via rollup_pending_tins, so the DELETE above drops its row
                # and the INSERT re-creates it only if rates remain. The old
                # anti-join scanned all of `rates` on every update.)
                if tbl == "rates_by_tin_tbl":
                    # in the spine's own transaction: a crash can never leave
                    # a new spine under the old token (spine_key)
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('spine_token', ?)", [os.urandom(8).hex()])
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except duckdb.Error:
                    pass  # connection already aborted the transaction
                raise
            try:
                con.execute("CHECKPOINT")
            except duckdb.Error:
                pass  # another connection's txn can block it; next one catches up

    def _rebuild_rollups_locked(self, tables: tuple[str, ...], log) -> float:
        with self.connect() as con:
            # BUILD time measured from lock ACQUISITION, not from the caller's
            # request: a refresh that queued 40 min behind an ingest rollup did
            # not "take" 40 min. The enrichment loop's adaptive cadence reads
            # this — timing from outside the lock inflated its interval to
            # hours and the steady name-refresh log churn went silent (read
            # live as "the app stopped running").
            t_locked = time.monotonic()
            self.last_rollup_build_seconds: float | None = None
            with self.write_lock:
                # catalog write — must not race another connection's
                # _register_views (see update_rollups_incremental)
                self._register_views(con)  # rates view must see current parts first
            # READ the running thread count for partition sizing — NEVER `SET
            # threads`. Changing DuckDB's GLOBAL thread count on the shared,
            # concurrently-queried instance DEADLOCKS: SET threads waits for
            # in-flight queries (dashboard/enrichment/UPDATE) to quiesce while
            # they wait for the scheduler it seized (seen live: all DuckDB
            # threads frozen at their query lines, disk 0%, 20+ minutes).
            # Memory is bounded by hash-partition slicing alone — sized for the
            # thread count (_rollup_partition_rows divides the budget by
            # threads → more threads yields more, smaller slices).
            try:
                build_threads = int(con.execute(
                    "SELECT current_setting('threads')").fetchone()[0])
            except Exception:  # noqa: BLE001 — older duckdb / odd value
                build_threads = 1
            build_threads = max(1, build_threads)
            try:
                # escalate PARTITION fineness on OOM (never thread count)
                for split in (1, 2, 4, 8, 16, 32, 64):
                    try:
                        self._build_rollup_tables(con, split=split,
                                                  threads=build_threads, tables=tables)
                        return time.monotonic() - t_locked
                    except duckdb.OutOfMemoryException:
                        if split == 64:
                            log.error(
                                "analytics rebuild cannot fit in the current "
                                "memory limit even at the finest slicing — "
                                "raise duckdb_memory_gb in config/mrfx.yaml "
                                "and restart (raw data is safe; dashboards "
                                "lag until a rebuild succeeds)")
                            raise  # genuinely can't fit at this memory limit
                        log.warning("rollup over the memory limit at split=%d; "
                                    "subdividing into finer partitions", split)
            finally:
                self.last_rollup_build_seconds = time.monotonic() - t_locked

    def _build_rollup_tables(self, con: duckdb.DuckDBPyConnection, split: int = 1,
                             threads: int = 1, tables: tuple[str, ...] | None = None) -> None:
        # rates_dedup stays a LIVE VIEW: at NPI×rate grain its groups are
        # nearly one-per-row (a 30M-row store means a ~30M-group hash
        # aggregation whose spill can exceed any reasonable disk). Drill-down
        # queries always hit it WITH filters, which DuckDB pushes into the
        # parquet scan — milliseconds, no materialization. The TIN-grain
        # spine and directory have few groups and stay materialized.
        #
        # Big stores build the materialized rollups in HASH PARTITIONS: the
        # partition column is part of each GROUP BY key, so N slices produce
        # identical rows to a single shot while peaking at ~1/N the
        # memory/temp-disk (a one-shot build over 64M rows exhausted a 23 GB
        # temp dir; the same build in 15M-row slices fits comfortably).
        import logging as _logging

        log = _logging.getLogger(__name__)
        # COVERAGE SNAPSHOT taken BEFORE any table build reads the rates glob:
        # stamping a fresh max(finished_at) AFTER an hour-long build claimed
        # files that finished mid-build — files whose rows the earlier hash
        # partitions never saw (a part landing mid-build is torn across
        # slices). With the snapshot, anything finishing after this instant
        # stays newer than the marker; the next incremental picks it up and
        # its payer-slice recompute also heals any torn partial inclusion.
        # Same rule for the removal notes: only clear what existed NOW.
        cover_ts = con.execute(
            "SELECT coalesce(max(finished_at)::VARCHAR, '') FROM files "
            "WHERE status = 'done' AND rows_emitted > 0").fetchone()[0]
        cover_names = [] if not cover_ts else [r[0] for r in con.execute(
            "SELECT filename FROM files WHERE status = 'done' "
            "AND rows_emitted > 0 AND finished_at::VARCHAR = ?", [cover_ts]).fetchall()]
        pend_gen, pending_snapshot = self._read_pending_payers(con)
        # the TOKEN, not a bool: only the raise this build started with is
        # cleared at its end (review 14 item 2.6)
        needs_full_snapshot = self._meta_get(con, "rollup_needs_full")
        if not _needs_full_set(needs_full_snapshot):
            needs_full_snapshot = None
        dir_names_ts = con.execute(
            "SELECT coalesce(max(enriched_at)::VARCHAR, '') "
            "FROM npi_directory").fetchone()[0]
        with self.write_lock:
            # catalog writes — must not race a writer's _register_views
            con.execute("DROP TABLE IF EXISTS rates_dedup_tbl")
            con.execute(f"CREATE OR REPLACE VIEW rates_dedup AS {DEDUP_QUERY}")
        if tables is None or "rates_by_tin_tbl" in tables:
            log.info("full analytics rebuild: the slow whole-store pass (one-time "
                     "after an upgrade, or when a shortcut wasn't safe). Extraction "
                     "continues but shares the disk; once this completes, updates "
                     "are payer-sized and fast.")
        log.info("rollup: preparing (counting source rows)…")
        n_rows = con.execute("SELECT count(*) FROM rates").fetchone()[0] or 0
        rows_per_part = self._rollup_partition_rows(split, threads)
        parts = max(1, -(-n_rows // rows_per_part))
        if parts > 1:
            log.info("rollup rebuild: %d partitions (%d rows/slice, %d threads, split=%d)",
                     parts, rows_per_part, threads, split)
        # ONE TRANSACTION PER TABLE: a mid-slice failure must roll back to that
        # table's PREVIOUS complete version (a partially-filled rollup served
        # as truth silently loses billing codes). Per-table — not one mega-
        # transaction around both — on purpose: a 15-minute two-table
        # transaction on an 85M-row store accumulated enough native state to
        # die with STATUS_HEAP_CORRUPTION (0xC0000374) partway through table
        # two, taking BOTH tables' work with it (observed live, exit code
        # confirmed). Each table commits alone, a CHECKPOINT between tables
        # flushes the WAL and releases memory, and the coverage markers land
        # in their own tiny transaction only after EVERY table committed — so
        # a crash between tables re-triggers the rebuild on next start instead
        # of masking it. Brief mixed-generation window between commits is the
        # accepted cost (each table is internally consistent at all times).
        # (table, query, hash-partition key, view). `tables` selects a subset —
        # a names-only enrichment refresh rebuilds just tin_directory_tbl and
        # leaves the (unchanged, expensive) rate spine alone.
        specs = {
            "rates_by_tin_tbl": (SPINE_QUERY, "billing_code || coalesce(tin_value, '')", "rates_by_tin"),
            "tin_directory_tbl": (TIN_DIRECTORY_QUERY, "tin_value", "tin_directory"),
        }
        selected = tables if tables is not None else tuple(specs)
        t_build = time.monotonic()
        for tbl in selected:
            query, key, _view = specs[tbl]
            con.execute("BEGIN TRANSACTION")
            try:
                if parts == 1:
                    con.execute(f"CREATE OR REPLACE TABLE {tbl} AS {query.format(part='TRUE')}")
                else:
                    # schema only, as EXPLICIT columns — never an empty
                    # `CREATE TABLE … AS (… WHERE FALSE)`. DuckDB 1.5.5: after an
                    # empty CTAS, rows that a later INSERT … ORDER BY of more than
                    # one row group (~123k rows) adds in the same transaction stay
                    # invisible to EVERY connection, even after COMMIT, until a
                    # CHECKPOINT. The CHECKPOINT below fails whenever another
                    # connection has a write open (a queue update, a name save:
                    # "Cannot CHECKPOINT: there are other write transactions
                    # active"), and the rebuilt spine then read as EMPTY on every
                    # screen until a later checkpoint. Explicit DDL + the same
                    # INSERTs: visible at COMMIT (round 5, P1.2; reproduced in
                    # tests/mrfx/test_rollup_visibility.py).
                    cols = con.execute(f"DESCRIBE {query.format(part='FALSE')}").fetchall()
                    ddl = ", ".join(f'"{name}" {typ}' for name, typ, *_ in cols)
                    con.execute(f"CREATE OR REPLACE TABLE {tbl} ({ddl})")
                    for i in range(parts):
                        log.info("rollup %s: partition %d/%d (%d rows total)", tbl, i + 1, parts, n_rows)
                        pred = f"hash({key}) % {parts} = {i}"
                        con.execute(f"INSERT INTO {tbl} {query.format(part=pred)}")
                    log.info("rollup %s: writing to the database file (can take "
                             "minutes on a hard drive; not stuck)", tbl)
                if tbl == "rates_by_tin_tbl":
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('spine_token', ?)", [os.urandom(8).hex()])
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except duckdb.Error:
                    pass  # connection already aborted the transaction
                raise
            try:
                # flush the WAL and release transaction memory before the next
                # table — the accumulation across tables is what crashed
                con.execute("CHECKPOINT")
            except duckdb.Error:
                pass  # another connection's txn can block it; next one catches up
        # markers AFTER every table committed, in ONE transaction: which schema
        # built these tables, and through WHEN they cover done files (restart
        # uses them to catch a kill before a batched rollup). Atomic so a kill
        # between marker writes can't leave a torn set (e.g. covered_through
        # advanced but covered_names not) that would mis-scope the next
        # incremental. FULL rebuilds stamp coverage; a names-only refresh
        # leaves the rate spine untouched and stamps only the names marker.
        # The pending-payers consume takes write_lock, so it runs AFTER this
        # transaction commits (nesting a write_lock section inside an open
        # txn is fine, but keeping the consume separate avoids holding the
        # meta txn across a lock wait).
        con.execute("BEGIN TRANSACTION")
        try:
            if "rates_by_tin_tbl" in selected:
                con.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('rollup_schema_version', ?)",
                    [str(ROLLUP_SCHEMA_VERSION)])
                # SNAPSHOT values from before the build read anything — files
                # finishing mid-build stay newer than the marker (the names
                # list tie-breaks same-microsecond twins so none can hide)
                con.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('rollup_covered_through', ?)",
                    [cover_ts])
                con.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('rollup_covered_names', ?)",
                    [json.dumps(sorted(cover_names))])
                if needs_full_snapshot:
                    # set before we started -> satisfied; raised again mid-build
                    # (a new token) -> kept
                    con.execute("DELETE FROM meta WHERE key = 'rollup_needs_full' "
                                "AND value = ?", [needs_full_snapshot])
            if "tin_directory_tbl" in selected:
                con.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('directory_names_through', ?)",
                    [dir_names_ts])
                # stamp the DIRECTORY semantics version here (not with the spine's
                # marker): a directory-only rebuild must be able to clear its own
                # migration, or the drift check would re-fire every startup
                con.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('directory_schema_version', ?)",
                    [str(DIRECTORY_SCHEMA_VERSION)])
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except duckdb.Error:
                pass
            raise
        if "rates_by_tin_tbl" in selected:
            # clear ONLY the removal notes that existed before the build began
            # (this build's scans cover those removals); notes appended
            # mid-build stay queued for the next incremental. Takes write_lock,
            # so kept out of the meta transaction above.
            self._consume_pending_payers(con, pend_gen, pending_snapshot)
        if parts > 1:
            log.info("rollup rebuild finished in %.0fs", time.monotonic() - t_build)
        # bump AFTER the commit: a count computed from the old tables during the
        # (minutes-long) build stays keyed to the old generation, and the first
        # post-swap request misses the cache and recounts against the new tables.
        self._bump_generation()
        with self.write_lock:
            # catalog writes — must not race a writer's _register_views
            for tbl in selected:
                _query, _key, view = specs[tbl]
                con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM {tbl}")

    # -- the durable sliced build (review 13 item 1.3; mrfx/rollplan.py) -------

    def _plan_versions(self) -> list:
        """What a plan's slices were computed under: a plan made by another
        version of the rules is not resumed (its committed slices are redone)."""
        return [ROLLUP_SCHEMA_VERSION, DIRECTORY_SCHEMA_VERSION, ROW_RULES_VERSION]

    def _plan_get(self, con: duckdb.DuckDBPyConnection) -> dict | None:
        raw = self._meta_get(con, "rollup_plan")
        if not raw:
            return None
        try:
            plan = json.loads(raw)
        except ValueError:
            return None
        return plan if isinstance(plan, dict) and isinstance(plan.get("slices"), list) else None

    def payers_part_way(self) -> set:
        """Payers a part-way FULL build has started but not finished: some of
        their rows are in the spine, not all. Never raises."""
        plan = self.rollup_plan()
        if not plan or plan.get("kind") != "full":
            return set()
        last: dict = {}
        for i, sl in enumerate(plan["slices"]):
            if sl.get("t") == "spine":
                last[sl.get("payer")] = i
        nxt = int(plan.get("next", 0))
        first = {}
        for i, sl in enumerate(plan["slices"]):
            if sl.get("t") == "spine":
                first.setdefault(sl.get("payer"), i)
        return {p for p, i in last.items() if i >= nxt and first[p] < nxt}

    def rollup_plan(self) -> dict | None:
        """The outstanding analytics build plan, if one is part-way (None
        otherwise). Never raises."""
        try:
            with self.connect() as con:
                return self._plan_get(con)
        except Exception:  # noqa: BLE001
            return None

    def _table_columns(self, con: duckdb.DuckDBPyConnection, tbl: str) -> list[str]:
        return [r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ? "
            "ORDER BY ordinal_position", [tbl]).fetchall()]

    def _new_plan(self, con: duckdb.DuckDBPyConnection, kind: str, log) -> dict:
        """Snapshot the coverage markers, size the slices, make sure the
        target tables exist, and store the plan. Nothing is aggregated yet."""
        from . import rollplan
        # the same SNAPSHOT rule as the one-shot build had: taken BEFORE any
        # slice reads the rates glob, so a file finishing mid-build stays
        # newer than the marker and the next incremental heals its payer
        cover_ts = con.execute(
            "SELECT coalesce(max(finished_at)::VARCHAR, '') FROM files "
            "WHERE status = 'done' AND rows_emitted > 0").fetchone()[0]
        cover_names = [] if not cover_ts else [r[0] for r in con.execute(
            "SELECT filename FROM files WHERE status = 'done' "
            "AND rows_emitted > 0 AND finished_at::VARCHAR = ?", [cover_ts]).fetchall()]
        pend_gen, pending = self._read_pending_payers(con)
        # the token of the raise this plan answers (review 14 item 2.6): a
        # needs-full raised WHILE the plan runs (an unscopable forget) has a
        # new token and survives the plan's end
        needs_full = self._meta_get(con, "rollup_needs_full")
        needs_full = needs_full if _needs_full_set(needs_full) else None
        dir_names_ts = con.execute(
            "SELECT coalesce(max(enriched_at)::VARCHAR, '') FROM npi_directory").fetchone()[0]
        try:
            threads = max(1, int(con.execute(
                "SELECT current_setting('threads')").fetchone()[0]))
        except Exception:  # noqa: BLE001
            threads = 1
        rpp = self._rollup_partition_rows(1, threads)
        log.info("analytics build: planning (counting rows per payer and code)…")
        n_rows = con.execute("SELECT count(*) FROM rates").fetchone()[0] or 0
        slices = list(rollplan.dir_slices(n_rows, rpp))
        payers: list = []
        if kind == "full":
            counts = con.execute(
                "SELECT payer, billing_code, count(*) FROM rates "
                "GROUP BY payer, billing_code").fetchall()
            spine = rollplan.spine_slices(counts, rpp)
            payers = sorted({s["payer"] for s in spine if s["payer"] is not None})
            has_null = any(s["payer"] is None for s in spine)
            slices.append({"t": "prune", "payers": payers, "null_payer": has_null,
                           "k": 1, "i": 0, "rows": 0, "work": 1})
            slices.extend(spine)
        # where each table is built: in place (the live table, queryable as it
        # fills) when its columns are what this version writes; a fresh
        # `<table>__next` swapped in at the end when they are not (a schema
        # change — the old table keeps serving meanwhile)
        targets = {}
        specs = {"tin_directory_tbl": TIN_DIRECTORY_QUERY}
        if kind == "full":
            specs["rates_by_tin_tbl"] = SPINE_QUERY
        with self.write_lock:
            con.execute("BEGIN TRANSACTION")
            try:
                for tbl, query in specs.items():
                    cols = con.execute(f"DESCRIBE {query.format(part='FALSE')}").fetchall()
                    want = [c[0] for c in cols]
                    have = self._table_columns(con, tbl)
                    ddl = ", ".join(f'"{name}" {typ}' for name, typ, *_ in cols)
                    if not have:
                        # explicit DDL, never an empty CTAS (round 5's DuckDB
                        # visibility bug — tests/mrfx/test_rollup_visibility.py)
                        con.execute(f"CREATE TABLE {tbl} ({ddl})")
                        targets[tbl] = tbl
                    elif sorted(have) == sorted(want):
                        targets[tbl] = tbl
                    if targets.get(tbl) == tbl:
                        # a dropped plan of another rules version may have
                        # left its shadow table (review 14 Group 3, P3)
                        con.execute(f"DROP TABLE IF EXISTS {tbl}__next")
                    else:
                        con.execute(f"CREATE OR REPLACE TABLE {tbl}__next ({ddl})")
                        targets[tbl] = f"{tbl}__next"
                plan = {
                    "id": os.urandom(6).hex(), "kind": kind,
                    "versions": self._plan_versions(),
                    "cover_ts": cover_ts, "cover_names": sorted(cover_names),
                    "pend_gen": pend_gen, "pending": pending, "needs_full": needs_full,
                    "dir_names_ts": dir_names_ts, "targets": targets,
                    "rows": int(n_rows), "slices": slices, "next": 0, "work_done": 0,
                    "created": _utcnow().isoformat(timespec="seconds"),
                }
                con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_plan', ?)",
                            [json.dumps(plan)])
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except duckdb.Error:
                    pass
                raise
            self._register_views(con)
        log.info("analytics build planned: %d slice(s) over %s rows (%d payer(s))",
                 len(slices), f"{n_rows:,}", len(payers))
        return plan

    def _run_plan(self, kind: str, log) -> float:
        """Create (or resume) the plan and run its slices to the end. Each
        slice commits with the plan's pointer in ONE transaction, so a kill
        between any two statements loses at most the running slice. An
        out-of-memory slice is halved (rollplan.halve) and retried; any other
        error propagates with the plan kept for the next attempt."""
        from . import rollplan
        with self.connect() as con:
            t_locked = time.monotonic()
            self.last_rollup_build_seconds = None
            with self.write_lock:
                self._register_views(con)
            plan = self._plan_get(con)
            if plan is not None and plan.get("versions") != self._plan_versions():
                log.info("analytics build: the part-way build was made by another "
                         "version of the rules — starting it again")
                plan = None
            if plan is not None and kind == "full" and plan.get("kind") == "names":
                plan = None      # a full build includes the names; it replaces it
            if plan is None:
                if kind == "full":
                    log.info("full analytics build: payer by payer, each part saved "
                             "as it finishes — the dashboard shows each payer as it "
                             "lands, and a stop or restart continues where it left off")
                plan = self._new_plan(con, kind, log)
            else:
                log.info("analytics build: resuming at part %d of %d (%s)",
                         plan["next"] + 1, len(plan["slices"]),
                         rollplan.slice_label(plan["slices"][min(plan["next"], len(plan["slices"]) - 1)])
                         if plan["slices"] else "—")
            total_work = sum(int(s.get("work", 1)) for s in plan["slices"]) or 1
            session_work, session_t0 = 0, time.monotonic()
            rows_left = sum(int(s.get("rows", 0) or 0) for s in plan["slices"][plan["next"]:])
            self._rollup_job = {"kind": plan["kind"], "slice": plan["next"],
                                "slices": len(plan["slices"]),
                                "work_done": int(plan.get("work_done", 0)),
                                "work_total": total_work, "rate": None,
                                "label": "", "started": time.time(), "rows_done": 0,
                                "rows_total": rows_left, "files": [], "payers": [],
                                "con": None}
            self._watch_job_con(con)
            while plan["next"] < len(plan["slices"]):
                idx = plan["next"]
                sl = plan["slices"][idx]
                if idx > 0 and self.analytics_paused():
                    # the user paused the analytics (review 14 item 1.2): stop
                    # between two committed parts; the plan continues on resume
                    log.info("analytics paused — the build stops after part %d of %d "
                             "and continues when the analytics are resumed",
                             idx, len(plan["slices"]))
                    raise RollupPaused()
                self._rollup_job.update(slice=idx, slices=len(plan["slices"]),
                                        label=rollplan.slice_label(sl),
                                        slice_work=int(sl.get("work", 1)),
                                        payers=[sl["payer"]] if sl.get("payer") else [])
                t_slice = time.monotonic()
                try:
                    self._run_slice(con, plan, idx, log)
                except duckdb.OutOfMemoryException:
                    halves = rollplan.halve(sl) if sl["t"] != "prune" else None
                    if not halves:
                        raise
                    log.warning("analytics build: %s ran out of memory — cutting it "
                                "in two and trying again", rollplan.slice_label(sl))
                    plan["slices"][idx:idx + 1] = halves
                    with self.write_lock:
                        con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_plan', ?)",
                                    [json.dumps(plan)])
                    total_work = sum(int(s.get("work", 1)) for s in plan["slices"]) or 1
                    self._rollup_job["work_total"] = total_work
                    continue
                w = int(sl.get("work", 1))
                session_work += w
                elapsed = time.monotonic() - session_t0
                self._rollup_job.update(
                    work_done=int(plan.get("work_done", 0)), slice=plan["next"],
                    rate=(session_work / elapsed) if elapsed > 0 else None,
                    work_total=total_work, _q_slice=None, _q_frac=0.0,
                    rows_done=int(self._rollup_job.get("rows_done") or 0)
                    + int(sl.get("rows", 0) or 0))
                if sl["t"] == "spine" and plan["kind"] == "full":
                    # a payer whose last slice just committed is now visible:
                    # its files get their "added" time (review 14 item 1.2)
                    self._note_fold_ins("full build part")
                if len(plan["slices"]) > 1:
                    log.info("analytics build: part %d of %d done (%s, %.0fs)",
                             idx + 1, len(plan["slices"]), rollplan.slice_label(sl),
                             time.monotonic() - t_slice)
            self._finish_plan(con, plan, log)
            self._note_fold_ins("full build" if plan["kind"] == "full" else "name refresh")
            took = time.monotonic() - t_locked
            self.last_rollup_build_seconds = took
            log.info("analytics build finished in %.0fs", took)
            return took

    def _run_slice(self, con: duckdb.DuckDBPyConnection, plan: dict, idx: int, log) -> None:
        """One slice: delete what it recomputes, insert it, advance the plan —
        one transaction. A spine slice writes a new spine token in the same
        transaction (spine_key), so a cached screen never outlives it."""
        from . import rollplan
        sl = plan["slices"][idx]
        nxt = dict(plan, next=idx + 1, work_done=int(plan.get("work_done", 0)) + int(sl.get("work", 1)))
        spine = plan["targets"].get("rates_by_tin_tbl", "rates_by_tin_tbl")
        directory = plan["targets"].get("tin_directory_tbl", "tin_directory_tbl")
        con.execute("BEGIN TRANSACTION")
        try:
            if sl["t"] == "dir":
                pred = rollplan.slice_pred(sl)
                con.execute(f"DELETE FROM {directory} WHERE {pred}")
                con.execute(f"INSERT INTO {directory} BY NAME "
                            + TIN_DIRECTORY_QUERY.format(part=pred))
            elif sl["t"] == "prune":
                keep = list(sl.get("payers") or [])
                null_clause = "FALSE" if sl.get("null_payer") else "payer IS NULL"
                con.execute(f"DELETE FROM {spine} WHERE {null_clause} OR (payer IS NOT NULL "
                            "AND payer NOT IN (SELECT unnest(?::VARCHAR[])))", [keep])
                con.execute("INSERT OR REPLACE INTO meta VALUES ('spine_token', ?)",
                            [os.urandom(8).hex()])
            else:
                pred = rollplan.slice_pred(sl)
                con.execute(f"DELETE FROM {spine} WHERE {pred}")
                con.execute(f"INSERT INTO {spine} BY NAME " + SPINE_QUERY.format(part=pred))
                con.execute("INSERT OR REPLACE INTO meta VALUES ('spine_token', ?)",
                            [os.urandom(8).hex()])
            con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_plan', ?)",
                        [json.dumps(nxt)])
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except duckdb.Error:
                pass
            raise
        plan.update(next=nxt["next"], work_done=nxt["work_done"])
        try:
            con.execute("CHECKPOINT")
        except duckdb.Error:
            pass  # another connection's write blocks it; the next one catches up
        if sl["t"] != "dir" and spine == "rates_by_tin_tbl":
            # the spine changed and is LIVE: caches keyed on it go (coverage
            # moves on screen as each payer lands)
            self._store_stats_cache = None
            self._bump_generation()
        elif sl["t"] == "dir" and directory == "tin_directory_tbl":
            self._states_cache = None
            self._bump_generation(spine=False)

    def _finish_plan(self, con: duckdb.DuckDBPyConnection, plan: dict, log) -> None:
        """Every slice committed: swap in any `__next` table, stamp the
        coverage markers from the plan's snapshot, clear the plan — in ONE
        transaction — then consume the removal notes the snapshot covered."""
        full = plan["kind"] == "full"
        with self.write_lock:
            con.execute("BEGIN TRANSACTION")
            try:
                for live, target in plan["targets"].items():
                    if target != live:
                        con.execute(f"DROP TABLE IF EXISTS {live}")
                        con.execute(f"ALTER TABLE {target} RENAME TO {live}")
                if full:
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_schema_version', ?)",
                                [str(ROLLUP_SCHEMA_VERSION)])
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_covered_through', ?)",
                                [plan["cover_ts"]])
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_covered_names', ?)",
                                [json.dumps(sorted(plan["cover_names"]))])
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('spine_token', ?)",
                                [os.urandom(8).hex()])
                    nf = plan.get("needs_full")
                    if nf:
                        # set before the plan was made -> satisfied; raised
                        # since (another token) -> kept (review 14 item 2.6).
                        # A plan made by 0.7.7 recorded True: it answers the
                        # legacy '1' only.
                        con.execute("DELETE FROM meta WHERE key = 'rollup_needs_full' "
                                    "AND value = ?", ["1" if nf is True else str(nf)])
                con.execute("INSERT OR REPLACE INTO meta VALUES ('directory_names_through', ?)",
                            [plan["dir_names_ts"]])
                con.execute("INSERT OR REPLACE INTO meta VALUES ('directory_schema_version', ?)",
                            [str(DIRECTORY_SCHEMA_VERSION)])
                con.execute("DELETE FROM meta WHERE key = 'rollup_plan'")
                con.execute("DROP TABLE IF EXISTS rates_dedup_tbl")
                con.execute(f"CREATE OR REPLACE VIEW rates_dedup AS {DEDUP_QUERY}")
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except duckdb.Error:
                    pass
                raise
            self._register_views(con)
        if full:
            self._consume_pending_payers(con, int(plan.get("pend_gen", 0)),
                                         list(plan.get("pending") or []))
        try:
            con.execute("CHECKPOINT")
        except duckdb.Error:
            pass
        self._enrich_progress_cache = None
        self._store_stats_cache = None
        self._states_cache = None
        self._bump_generation(spine=full)

    # -- the build's state, for the dashboard (review 13 item 1.2) --------------

    def _note_rollup_failure(self, exc: BaseException) -> None:
        """A rollup attempt failed: keep the sentence (the header line and the
        Files tab say it) until a later attempt succeeds. Never raises."""
        from . import rollplan
        try:
            sentence = rollplan.failure_sentence(exc, self._memory_limit_gb)
            self._rollup_failure = {"at": _utcnow().isoformat(timespec="seconds"),
                                    "sentence": sentence}
            self._coverage_cache = None
            with self.write_lock, self.connect() as con:
                con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_failure', ?)",
                            [json.dumps(self._rollup_failure)])
        except Exception:  # noqa: BLE001
            pass

    def _clear_rollup_failure(self) -> None:
        try:
            self._rollup_failure = None
            self._coverage_cache = None
            with self.write_lock, self.connect() as con:
                con.execute("DELETE FROM meta WHERE key = 'rollup_failure'")
        except Exception:  # noqa: BLE001
            pass

    def rollup_failure(self) -> dict | None:
        """{at, sentence} of the last failed analytics attempt, or None."""
        if self._rollup_failure is not _MISSING:
            return self._rollup_failure
        try:
            with self.connect() as con:
                raw = self._meta_get(con, "rollup_failure")
            self._rollup_failure = json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            return None
        return self._rollup_failure

    def _watch_job_con(self, con: duckdb.DuckDBPyConnection) -> None:
        """Let the activity panel read DuckDB's own progress of the running
        statement (the round-13 progress module's method, on the rollup's own
        connection only). Cosmetic: never raises."""
        try:
            con.execute("SET enable_progress_bar = true")
            con.execute("SET enable_progress_bar_print = false")
            con.execute("SET progress_bar_time = 0")
            if isinstance(self._rollup_job, dict):
                self._rollup_job["con"] = con
        except Exception:  # noqa: BLE001
            pass

    def rollup_job(self) -> dict | None:
        """The running build's progress — None when nothing is building.
        {kind, pct, slice, slices, label, eta_s, rate_rows_s, rows_done,
        rows_total, files, payers, started}. Percent = work of committed units
        over the total (a plan's slices weighted by the raw rows they read; an
        update's partitions) plus DuckDB's own progress of the running
        statement inside the current unit; never backwards. The ETA and the
        rate appear only after one unit was measured (review 14 item 1.2:
        "about", never a guess). Never raises."""
        job = self._rollup_job
        if not job or not self._rollup_active:
            return None
        try:
            total = max(1, int(job.get("work_total") or 1))
            done = min(total, int(job.get("work_done") or 0))
            frac = 0.0
            con = job.get("con")
            if con is not None and done < total:
                try:
                    qp = con.query_progress()
                    if qp is not None and 0 <= qp <= 100:
                        cur = int(job.get("slice") or 0)
                        w = 1
                        sl = job.get("slice_work")
                        if sl:
                            w = max(1, int(sl))
                        frac = (qp / 100.0) * w
                        if job.get("_q_slice") == cur:
                            frac = max(frac, float(job.get("_q_frac") or 0.0))
                        job["_q_slice"], job["_q_frac"] = cur, frac
                except Exception:  # noqa: BLE001 — a closed handle
                    frac = 0.0
            pct = min(99, int(100 * (done + frac) / total))
            pct = max(pct, int(job.get("_pct_max") or 0))
            job["_pct_max"] = pct
            rate = job.get("rate")
            eta = (total - done) / rate if rate and job.get("kind") in ("full", "names") else None
            rrate = None
            el = time.time() - float(job.get("started") or time.time())
            if job.get("rows_done") and el > 0 and done > 0:
                rrate = int(job["rows_done"]) / el
                if job.get("kind") not in ("full", "names") and job.get("rows_total"):
                    left = max(0, int(job["rows_total"]) - int(job["rows_done"]))
                    eta = left / rrate if rrate else None
            return {"kind": job.get("kind"), "pct": pct,
                    "slice": int(job.get("slice") or 0), "slices": int(job.get("slices") or 0),
                    "label": job.get("label") or "", "eta_s": eta if done > 0 else None,
                    "rate_rows_s": rrate, "rows_done": int(job.get("rows_done") or 0),
                    "rows_total": int(job.get("rows_total") or 0),
                    "files": list(job.get("files") or []),
                    "payers": list(job.get("payers") or []),
                    "started": job.get("started")}
        except Exception:  # noqa: BLE001
            return None

    def cached(self, name: str, key, compute):
        """`compute()`, remembered until the next data generation.

        For whole-store answers that change only when a rollup lands — the
        quality panels on every Overview visit, payer freshness inside every
        benchmark, a market's leads and leaderboard (round 6: 0.45-14 s each
        on a 99M-rate store, recomputed per request). `key` must hold every
        input of the answer; values are handed out as deep copies, so a
        caller that decorates its result cannot change the cached one."""
        import copy
        gen = (self.data_generation, self.user_edit_generation)
        k = (name, key)
        with self._gen_cache_lock:
            if self._gen_cache.get("_gen") != gen:
                self._gen_cache = {"_gen": gen}
            held = self._gen_cache.get(k, _MISSING)
        if held is not _MISSING:
            # copied OUTSIDE the process-wide lock: a big answer's deep copy
            # held every other cached reader waiting (round 7 P2)
            return copy.deepcopy(held)
        value = compute()
        if _run_cancelled():
            # computed under a Stop: a fault-isolated section may have caught
            # the stop and degraded ("stopped" per client, a missing
            # position) — never kept for the next caller (review 14 item 2.9)
            return value
        with self._gen_cache_lock:
            if self._gen_cache.get("_gen") == gen:
                if len(self._gen_cache) > 512:
                    self._gen_cache = {"_gen": gen}
                self._gen_cache[k] = value
        return copy.deepcopy(value)

    def spine_cached(self, name: str, key, compute):
        """`compute()`, remembered until the SPINE changes — not every data
        generation: the enrichment name refresh bumps data_generation every
        few minutes without touching a rate, and an aggregate over the spine
        alone must not be recomputed for that (review 13 item 1.4: 5-6 s per
        99M rows on every Files poll after each refresh)."""
        import copy
        gen = self.spine_generation
        k = (name, key)
        with self._gen_cache_lock:
            held = self.__dict__.setdefault("_spine_cache", {})
            if held.get("_gen") != gen:
                held.clear()
                held["_gen"] = gen
            got = held.get(k, _MISSING)
        if got is not _MISSING:
            return copy.deepcopy(got)
        value = compute()
        if _run_cancelled():
            return value                     # never cache under a Stop (2.9)
        with self._gen_cache_lock:
            held = self._spine_cache
            if held.get("_gen") == gen:
                if len(held) > 256:
                    held.clear()
                    held["_gen"] = gen
                held[k] = value
        return copy.deepcopy(value)

    def _bump_generation(self, spine: bool = True) -> None:
        with self._generation_lock:
            self.data_generation += 1
            if spine:
                self.spine_generation += 1

    def spine_materialised(self, name: str, select_sql: str, params=()) -> str:
        """`read_parquet(...)` of `select_sql`'s rows, built lazily ONCE per
        spine generation (round 7 P1.2: every benchmark-family call recomputed
        the same per-practice medians from the whole spine — 2-8 s each on a
        99M-rate store).

        `select_sql` must read only the TIN spine (rates_by_tin): the file is
        valid exactly as long as the spine is, and every spine write bumps
        spine_generation after its commit (the same contract as
        Store.cached). `name` carries its own schema version — a change to
        the SQL renames the file, it never needs ROLLUP_SCHEMA_VERSION.
        Written outside the database (no writer lock) to a temp name and
        os.replace'd; the previous generation's file is removed (on Windows a
        file still open is left for the next build)."""
        gen = self.spine_generation
        with self._spine_mat_guard:
            lock = self._spine_mat_locks.setdefault(name, threading.Lock())
        with lock:
            held = self._spine_mat.get(name)
            if held and held[0] == gen and held[1].exists():
                return f"read_parquet('{sql_path(held[1])}')"
            # keyed by the spine's CONTENT (spine_key), not by this process:
            # a random per-process tag rebuilt every file on every start (part
            # of an 8-13 s first call) and left other instances' files for 6
            # hours — five restarts left ~490 MB beside the store (review 9
            # item 4.6). The same spine is the same file, across restarts.
            path = self.dir / f"{name}.{self.spine_key()}.parquet"
            if not path.exists():
                tmp = path.with_name(f"{path.name}.{os.getpid()}-{os.urandom(3).hex()}.tmp")
                # the cold-only step a progress bar must name (review 13 Group 2)
                _progress.current().stage(
                    "Preparing the practice medians (first run since new data)")
                try:
                    with self.connect() as con:
                        con.execute(f"COPY ({select_sql}) TO '{sql_path(tmp)}' "
                                    "(FORMAT PARQUET)", list(params))
                    os.replace(tmp, path)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
            self._spine_mat[name] = (gen, path)
            # every other file of this name is superseded (a newer spine) or
            # an old naming; a reader that still holds one rebuilds it (the
            # exists() check above)
            for old in self.dir.glob(f"{name}.*.parquet*"):
                if old == path or old.name.endswith(".tmp"):
                    continue
                try:
                    old.unlink()
                except OSError:          # still open on Windows: next time
                    pass
            return f"read_parquet('{sql_path(path)}')"

    def spine_key(self) -> str:
        """The identity of the spine's content: the token every spine write
        stores in its own transaction, plus the rate parts' signatures and the
        rollup bookkeeping (so a writer that forgot the token still changes
        the key). Two processes on the same spine compute the same key."""
        import hashlib
        with self.connect() as con:
            meta = dict(con.execute(
                "SELECT key, value FROM meta WHERE key IN ('spine_token', "
                "'rollup_covered_through', 'rollup_covered_names', "
                "'rollup_pending_payers', 'rollup_needs_full', "
                "'rollup_schema_version')").fetchall())
        if not meta.get("spine_token"):
            with self.write_lock, self.connect() as con:
                con.execute("INSERT INTO meta SELECT 'spine_token', ? WHERE NOT EXISTS "
                            "(SELECT 1 FROM meta WHERE key = 'spine_token')",
                            [os.urandom(8).hex()])
                meta["spine_token"] = con.execute(
                    "SELECT value FROM meta WHERE key = 'spine_token'").fetchone()[0]
        return hashlib.sha1(json.dumps(
            [sorted(meta.items()), sorted(self._part_sigs().items())]
        ).encode()).hexdigest()[:16]

    def note_user_edit(self) -> None:
        """A user edit that a cached answer reads (Store.cached) just landed:
        drop every cached answer. Round 7 P1.3: Leads, the leaderboard and
        the payer roster are cached per generation and print
        store.org_websites(); a website saved for A REHAB still showed
        `website: null` until the next rollup. Call from EVERY writer whose
        table a cached reader reads — today set_org_website and
        set_entity_map (tests/mrfx/test_round7.py pins both)."""
        with self._generation_lock:
            self.user_edit_generation += 1

    def _invalidate_scan_caches(self) -> None:
        """Drop the cached whole-store scans (header counts, states,
        enrichment progress) after any write that changes the rates view or
        directories — a 60s TTL alone let the header disagree with the Files
        tab right after an ingest/forget (verification audit)."""
        self._store_stats_cache = None
        self._states_cache = None
        self._enrich_progress_cache = None

    # legacy name used by tests/older callers
    def rebuild_dedup(self) -> None:
        self.rebuild_rollups()

    # -- files table ---------------------------------------------------------

    def upsert_file(self, filename: str, **fields) -> None:
        cols = {
            "payer", "file_type", "status", "schema_version", "last_updated_on",
            "size_bytes", "rows_emitted", "ref_groups_skipped", "error",
            "preflight", "qa", "progress", "chunks_done", "chunks_total",
            "started_at", "finished_at", "row_rules", "file_month", "published_month",
            "code_set", "read_identity",
        }
        fields = {k: v for k, v in fields.items() if k in cols}
        if fields.get("status") == "done":
            # every "done" follows a fresh parse by THIS version's parser, so
            # its part already follows the current row rules
            fields.setdefault("row_rules", ROW_RULES_VERSION)
        for jcol in ("preflight", "qa"):
            if jcol in fields and not isinstance(fields[jcol], (str, type(None))):
                fields[jcol] = json.dumps(fields[jcol])
        if not fields:
            return  # nothing to write (all keys filtered out) — never emit empty SET
        with self.write_lock, self.connect() as con:
            if fields.get("finished_at") is FINISHED_NOW:
                # stamped INSIDE the lock, at commit order: a timestamp taken
                # before a (possibly ~90s) write_lock wait can land BEHIND an
                # already-advanced rollup coverage marker — that file would be
                # invisible to every future delta AND to rollups_stale(),
                # its rows silently missing from analytics forever.
                # naive UTC, like ingest's _now() (see _utcnow: an aware stamp
                # is stored as local time and breaks the marker's ordering)
                fields["finished_at"] = _utcnow()
            exists = con.execute("SELECT 1 FROM files WHERE filename = ?", [filename]).fetchone()
            if exists:
                sets = ", ".join(f"{k} = ?" for k in fields)
                con.execute(f"UPDATE files SET {sets} WHERE filename = ?", [*fields.values(), filename])
            else:
                keys = ["filename", *fields.keys()]
                con.execute(
                    f"INSERT INTO files ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                    [filename, *fields.values()],
                )

    # the active code list, as the files table records it (set by whoever
    # holds the config: serve, the CLI, ingest_file) — None: not known
    current_code_set: str | None = None

    def stamp_code_sets(self, key: str) -> int:
        """Files loaded before 0.7.7 carry no code list: they are taken to have
        been read with the list configured the first time 0.7.7 runs (the only
        honest default — it is the list the user had). Once per store."""
        self.current_code_set = key
        try:
            with self.write_lock, self.connect() as con:
                if self._meta_get(con, "code_set_baseline"):
                    return 0
                n = con.execute("UPDATE files SET code_set = ? WHERE code_set IS NULL "
                                "AND status = 'done' RETURNING 1", [key]).fetchall()
                con.execute("INSERT OR REPLACE INTO meta VALUES ('code_set_baseline', ?)", [key])
                return len(n)
        except Exception:  # noqa: BLE001 — bookkeeping never stops the open
            return 0

    def files_missing_codes(self, key: str | None = None) -> list[dict]:
        """Done rate files read WITHOUT some code the active list now has:
        [{filename, added}] (review 13 item 5.2.4). "ALL" covers everything."""
        key = key or self.current_code_set
        if not key:
            return []
        want = None if key == "ALL" else set(key.split(","))
        out = []
        with self.connect() as con:
            for name, had in con.execute(
                    "SELECT filename, code_set FROM files WHERE status = 'done' "
                    "AND file_type = 'in_network' AND code_set IS NOT NULL "
                    "AND code_set <> ? ORDER BY filename", [key]).fetchall():
                if had == "ALL":
                    continue
                added = ["(every code)"] if want is None else sorted(want - set(had.split(",")))
                if added:
                    out.append({"filename": name, "added": added})
        return out

    def file_status(self, filename: str) -> dict | None:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM files WHERE filename = ?", [filename]).fetchall()
            if not rows:
                return None
            cols = [d[0] for d in con.description]
            return dict(zip(cols, rows[0]))

    # -- provider refs ---------------------------------------------------------

    def save_provider_refs(
        self, payer: str, source_file: str, last_updated_on: str | None,
        refs: dict[int, list[tuple[str | None, str | None, tuple[str, ...]]]],
        append: bool = False,
    ) -> None:
        """Replace this file's references (append=True adds a batch — the
        streaming writer clears the file's old rows once, up front)."""
        with self.write_lock, self.connect() as con:
            if not append:
                con.execute(
                    "DELETE FROM provider_refs WHERE payer = ? AND source_file = ?",
                    [payer, source_file],
                )
            if not refs:
                return
            # one Arrow table, one INSERT: executemany ran ~420 rows/s
            # (measured) — hours for a 4M-group reference file, all of it
            # under the store write lock that every other worker waits on
            rids, tins, types, npi_lists = [], [], [], []
            for rid, groups in refs.items():
                from .refid import REMOTE_REF
                if groups == REMOTE_REF:
                    groups = [(None, REMOTE_REF, ())]
                for tin_value, tin_type, npis in (groups or [(None, None, ())]):
                    rids.append(rid)
                    tins.append(tin_value)
                    types.append(tin_type)
                    npi_lists.append(list(npis))
            n = len(rids)
            refs_tbl = pa.table({  # noqa: F841 — scanned by name below
                "payer": pa.array([payer] * n, pa.string()),
                "source_file": pa.array([source_file] * n, pa.string()),
                "last_updated_on": pa.array([last_updated_on] * n, pa.string()),
                "ref_id": pa.array(rids, pa.int64()),
                "tin_value": pa.array(tins, pa.string()),
                "tin_type": pa.array(types, pa.string()),
                "npis": pa.array(npi_lists, pa.list_(pa.string())),
            })
            con.register("__refs_in", refs_tbl)
            try:
                con.execute("INSERT INTO provider_refs SELECT payer, source_file, last_updated_on, "
                            "ref_id, tin_value, tin_type, npis FROM __refs_in")
            finally:
                con.unregister("__refs_in")

    def finish_provider_refs(self, source_file: str, payer: str,
                             last_updated_on: str | None) -> None:
        """After a streamed reference file: its batches were written under
        the header known at the time; stamp the final payer/date on all."""
        with self.write_lock, self.connect() as con:
            con.execute("UPDATE provider_refs SET payer = ?, last_updated_on = ? "
                        "WHERE source_file = ?", [payer, last_updated_on, source_file])
            # the list is complete from here on: bump BEFORE the caller asks
            # which rate files wait for it (requeue_skipped), so a rate file
            # finishing on either side of that query is read again
            self._refs_epoch[payer] = self._refs_epoch.get(payer, 0) + 1

    def refs_epochs(self) -> dict[str, int]:
        """A copy of the per-payer provider-list counters (see __init__)."""
        with self.write_lock:
            return dict(self._refs_epoch)

    def clear_provider_refs(self, source_file: str) -> None:
        with self.write_lock, self.connect() as con:
            con.execute("DELETE FROM provider_refs WHERE source_file = ?", [source_file])

    def companion_files_for(self, payer: str, last_updated_on: str | None,
                            month: str | None) -> list[str]:
        """The payer's standalone provider-reference files of ONE vintage:
        those with the rate file's exact `last_updated_on` if any carry it,
        else those of its publication `month` (each companion's month by the
        same header -> filename -> read-month rule as a rate file's,
        parser.file_month_for). Reference ids are local to one publication:
        April's list must never name May's practices (review 9 item 1.3 —
        "the newest companion per id" put a May rate file on April's
        practices, and re-ingesting April's moved it to May's). With neither
        given (an old caller), the newest vintage."""
        from .parser import file_month_for
        with self.connect() as con:
            files = con.execute(
                "SELECT DISTINCT r.source_file, r.last_updated_on, "
                "       strftime(coalesce(f.started_at, f.finished_at), '%Y-%m') "
                "FROM provider_refs r LEFT JOIN files f ON f.filename = r.source_file "
                "WHERE r.payer = ? ORDER BY 1", [payer]).fetchall()
        if not files:
            return []
        if last_updated_on is None and month is None:
            newest = max(str(lu or "") for _f, lu, _m in files)
            return [f for f, lu, _m in files if str(lu or "") == newest]
        lu = str(last_updated_on or "").strip()[:10]
        exact = [f for f, flu, _m in files if lu and str(flu or "").strip()[:10] == lu]
        if exact:
            return exact
        if not month:
            return []
        return [f for f, flu, read_m in files
                if file_month_for(flu, f, read_m or str(month))[0] == str(month)[:7]]

    def load_provider_refs(self, payer: str, last_updated_on: str | None = None,
                           month: str | None = None,
                           ) -> dict[int, list[tuple[str | None, str | None, tuple[str, ...]]] | None]:
        """The payer's standalone provider lists of the RATE FILE'S vintage
        (companion_files_for): never another month's (review 9 item 1.3).
        None of that vintage -> {} -> the ids are missing and counted, and
        requeue_skipped loads the file again when the right list lands.
        Within the vintage, an id two lists define differently is ambiguous
        (None); byte-for-byte twins agree. Duplicate identical groups are
        collapsed."""
        chosen = self.companion_files_for(payer, last_updated_on, month)
        if not chosen:
            return {}
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT ref_id, source_file, tin_value, tin_type, npis
                FROM provider_refs
                WHERE payer = ? AND source_file IN (SELECT unnest(?::VARCHAR[]))
                ORDER BY ref_id, source_file DESC
                """,
                [payer, chosen],
            ).fetchall()
        # Ids are file-local: two companion files of the SAME vintage that
        # define one id differently (a PPO and an HMO table, say) cannot both
        # be right for an in-network file that names neither — the old
        # "highest source_file wins" tiebreak attached one network's rates to
        # the other's practices. Such an id maps to None (unresolved, counted
        # as ambiguous_ref_ids); byte-for-byte twins (a re-download under a
        # new name) still agree and resolve (audit 8 P0-1).
        per_file: dict[int, dict[str, set]] = {}
        for rid, src, tin_value, tin_type, npis in rows:
            per_file.setdefault(rid, {}).setdefault(src, set()).add(
                (tin_value, tin_type, tuple(npis or ())))
        out: dict[int, list | None] = {}
        pool: dict = {}   # one object per distinct NPI/TIN (see parser.share_strings);
        get = pool.setdefault   # also shrinks the pickle shipped to each worker
        from .refid import REMOTE_REF
        empty = frozenset({(None, None, ())})
        remote = frozenset({(None, REMOTE_REF, ())})
        for rid, by_src in per_file.items():
            versions = {frozenset(v) for v in by_src.values()}
            if len(versions) > 1:
                out[rid] = None
                continue
            only = next(iter(versions))
            if only == empty:
                # an entry with no providers is MISSING, not "resolved to
                # nothing": a group citing it was dropped with no counter
                # (review 9 item 3.4) — leave it out so it is counted
                continue
            if only == remote:
                out[rid] = REMOTE_REF
                continue
            out[rid] = [
                (get(tin_value, tin_value) if tin_value is not None else None, tin_type,
                 tuple(get(n, n) for n in npis))
                for tin_value, tin_type, npis in sorted(
                    next(iter(versions)), key=lambda g: (g[0] or "", g[1] or "", g[2]))]
        return out

    def has_provider_refs(self, payer: str, last_updated_on: str | None = None) -> bool:
        q = "SELECT 1 FROM provider_refs WHERE payer = ?"
        params: list = [payer]
        if last_updated_on:
            q += " AND last_updated_on = ?"
            params.append(last_updated_on)
        with self.connect() as con:
            return con.execute(q + " LIMIT 1", params).fetchone() is not None

    # -- npi directory -----------------------------------------------------------

    def unenriched_npis(self, limit: int = 500, after: str = "") -> list[str]:
        """`after` is a keyset cursor (npi > after): the query is
        deterministic, so callers that page through the full population
        without saving rows in between MUST advance the cursor or every
        page is identical."""
        # Referral-pair NPIs (sources AND targets) are enrichment citizens too:
        # they are the practices the Medicare tab names and the referral
        # leaderboard ranks, and without enrichment a competitor clinic that
        # is in the referral data but not the rate store stayed unidentified
        # forever — blank name, unknown taxonomy, invisible to the therapy
        # filter. The view is optional (no referral data imported yet), so the
        # wide query falls back to the rates-only one.
        base = """
            SELECT DISTINCT npi FROM (
                SELECT npi FROM rates
                UNION
                -- tin.type='npi' rows put an NPI in the TIN slot; it names
                -- the entity, so it needs enrichment too
                SELECT tin_value AS npi FROM rates
                WHERE tin_is_really_npi AND tin_value IS NOT NULL
                {referrals}
            )
            WHERE npi > ? AND npi NOT IN (SELECT npi FROM npi_directory)
              -- only well-formed 10-digit NPIs: junk ids from messy files
              -- always fail at NPPES, sort to the FRONT of every batch,
              -- and can wedge enrichment permanently at the same spot
              AND regexp_full_match(npi, '[0-9]{{10}}')
            ORDER BY npi LIMIT ?
        """
        with self.connect() as con:
            try:
                rows = con.execute(base.format(referrals=(
                    "UNION SELECT source_npi AS npi FROM referral_pairs "
                    "UNION SELECT target_npi AS npi FROM referral_pairs")),
                    [after, limit]).fetchall()
            except duckdb.Error:   # no referral view in this store
                rows = con.execute(base.format(referrals=""),
                                   [after, limit]).fetchall()
        return [r[0] for r in rows]

    def store_stats(self, max_age_seconds: float = 60.0) -> dict:
        """Whole-store {rates, tins, payers} counts for the dashboard header.
        Cached + single-flight for exactly the reason enrichment_progress is:
        count(DISTINCT tin_value/payer) forces a full scan + hash of every raw
        parquet row (~86M), and the header polls this every 15s. Uncached, a
        scan slower than the poll made polls STACK — several whole-store HDD
        scans at once, starving the parser workers of the disk. One thread
        computes; the rest serve the last value (a header count up to a minute
        stale is invisible; the I/O storm is not)."""
        cached = self._store_stats_cache
        if cached is not None and (time.monotonic() - cached[0]) < max_age_seconds:
            return cached[1]
        if not self._store_stats_lock.acquire(blocking=False):
            return cached[1] if cached is not None else {"rates": 0, "tins": 0, "payers": 0}
        try:
            cached = self._store_stats_cache  # re-check: another thread may have filled it
            if cached is not None and (time.monotonic() - cached[0]) < max_age_seconds:
                return cached[1]
            # count(*) over the parquet glob is answered from the files'
            # metadata (no row scan); the DISTINCTs come from the TIN spine —
            # identical when it is current (real 32M store: 20,101 TINs /
            # 9 payers both ways, 0.02 s vs a full raw hash) — and fall back
            # to the raw scan only while new files await their rollup
            spine = (not self.rollups_stale())
            with self.connect() as con:
                if spine and con.execute(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_name = 'rates_by_tin_tbl'").fetchone()[0]:
                    rates_n = con.execute("SELECT count(*) FROM rates").fetchone()[0]
                    # the spine changes only with a data generation (every
                    # rollup write bumps it after its commit): its distinct
                    # counts are recounted once per generation, not every
                    # minute the header polls (2 s a poll on a 79M-row spine)
                    gen = self.data_generation
                    held = self._spine_counts
                    if held is not None and held[0] == gen:
                        tins, payers = held[1]
                    else:
                        # kept on disk per spine content (review 13 item 3.5:
                        # 6.8 s on every fresh start of a 99M-rate store)
                        kept = None
                        try:
                            kpath = self.dir / f"spine_counts.v1.{self.spine_key()}.json"
                            if kpath.exists():
                                kept = json.loads(kpath.read_text())
                        except (OSError, ValueError, duckdb.Error):
                            kpath, kept = None, None
                        if isinstance(kept, list) and len(kept) == 2:
                            tins, payers = int(kept[0]), int(kept[1])
                        else:
                            tins, payers = con.execute(
                                "SELECT count(DISTINCT tin_value), count(DISTINCT payer) "
                                "FROM rates_by_tin_tbl").fetchone()
                            if kpath is not None:
                                try:
                                    for old in self.dir.glob("spine_counts.v1.*.json"):
                                        old.unlink(missing_ok=True)
                                    tmp = kpath.with_suffix(".tmp")
                                    tmp.write_text(json.dumps([tins, payers]))
                                    os.replace(tmp, kpath)
                                except OSError:
                                    pass
                        self._spine_counts = (gen, (tins, payers))
                else:
                    # analytics behind: what is LOADED, from the per-part sets
                    # (one scan per new part) — a raw count(DISTINCT) over the
                    # whole store every minute while a billion-row rollup
                    # catches up competed with the rollup itself (review 13)
                    inv = self.loaded_inventory()
                    if inv.get("complete"):
                        rates_n = con.execute("SELECT count(*) FROM rates").fetchone()[0]
                        tins, payers = inv["tins"], len(inv["payers"])
                    else:
                        rates_n, tins, payers = con.execute(
                            "SELECT count(*), count(DISTINCT tin_value), count(DISTINCT payer) "
                            "FROM rates").fetchone()
            result = {"rates": rates_n, "tins": tins, "payers": payers}
            self._store_stats_cache = (time.monotonic(), result)
            return result
        finally:
            self._store_stats_lock.release()

    def enrichment_progress(self, max_age_seconds: float = 60.0) -> dict:
        """How far NPI->name enrichment has gotten, for the dashboard banner.
        Cached for `max_age_seconds`: the two DISTINCT-over-UNION queries scan
        the full rates parquet view, and the dashboard polls /api/stats every
        15s — on a national-file store that would make every poll a
        multi-second I/O storm competing with ingest. Progress moving up to a
        minute late is invisible to a human; the scans are not.
        `total` counts well-formed NPIs actually referenced by rates (the same
        population enrichment works through); `named` is those NPPES gave a
        real organization/person name. `remaining` drives the 'still working'
        hint so an incomplete state filter reads as 'names still filling in',
        not 'broken'."""
        cached = self._enrich_progress_cache
        if cached is not None and (time.monotonic() - cached[0]) < max_age_seconds:
            return cached[1]
        # single-flight: on a big store this scan can outlive the poll
        # interval, and every 15s poll then stacked ANOTHER identical scan
        # (seen live: four threads in this query at once). One computes;
        # the rest serve the last known value, stale is fine for a banner.
        if not self._enrich_progress_lock.acquire(blocking=False):
            return cached[1] if cached is not None else {
                "total": 0, "enriched": 0, "named": 0, "remaining": 0}
        try:
            # The NPIs the rates reference change ONLY when a rate part does,
            # so they are kept PER PART (rate_npi_set / rate_npi_parts): a
            # file landing costs one scan of that file, outside the writer
            # lock, and the counts are a join of the name directory with the
            # kept set. Before round 4 this was two DISTINCT scans of every
            # raw row each minute the dashboard was open; round 4's first cut
            # rebuilt a whole-store table under the writer lock whenever a
            # part changed — 80 s at 99M rates, during which every file
            # finishing had to wait for its bookkeeping (found in the round-5
            # review; see docs/NEXT_STEPS_PROMPT_5.md).
            with self.connect() as con:
                rel = self._rate_npis_relation(con)
                total = con.execute(f"SELECT count(DISTINCT npi) FROM {rel}").fetchone()[0]
                enriched, named = con.execute(
                    "SELECT count(*), count(*) FILTER (WHERE org_name IS NOT NULL) "
                    f"FROM npi_directory WHERE npi IN (SELECT npi FROM {rel})").fetchone()
            remaining = max(0, total - enriched)
            result = {"total": total, "enriched": enriched, "named": named,
                      "remaining": remaining}
            self._enrich_progress_cache = (time.monotonic(), result)
            return result
        finally:
            self._enrich_progress_lock.release()

    PLACEMENT_SCHEMA = 1

    def placement_relation(self) -> str:
        """`read_parquet(...)` of every practice's NPI-ZIP counts, current.

        (tin_value, zip, c, nn, nt): the rate rows per (TIN, 5-digit ZIP) of
        its NPIs, and its distinct NPIs / therapy NPIs there — exactly what
        sqlfn.zip_counts_by_tin computed from the raw rows on EVERY leverage,
        radius-map, dossier and new-to-network call (round 6: a radius map
        11-62 s on a 99M-rate store; the whole-store placement is 8 s once).
        The file is stamped with the rate parts' signatures and the NPI
        directory's size, ZIP count and newest enrichment time, so any change
        to either input yields a new file on the next request — never a stale
        placement. Written outside the database (no writer lock), atomically."""
        import hashlib
        import json as _json

        from .catalog import therapy_taxonomy_sql
        from .sqlfn import _HAS_ZIP, ZIP5
        with self.connect() as con:
            n, z, last = con.execute(
                "SELECT count(*), count(zip), max(enriched_at) FROM npi_directory").fetchone()
        stamp = hashlib.sha1(_json.dumps(
            [sorted(self._part_sigs().items()), n, z, str(last), self.PLACEMENT_SCHEMA]
        ).encode()).hexdigest()[:16]
        path = self.dir / f"tin_zip_place.{stamp}.parquet"
        if not path.exists():
            with self._placement_lock:
                if not path.exists():
                    therapy = therapy_taxonomy_sql("n.taxonomy_code", all_col="n.taxonomy_codes")
                    tmp = self.dir / f"tin_zip_place.{stamp}.parquet.tmp"
                    # rows per (TIN, NPI) first, then ONE directory lookup per
                    # NPI: joining first evaluated the therapy-taxonomy LIKEs on
                    # every rate row (39 s on a 99M-rate store; 6 s this way,
                    # the same 194,000 rows to the byte)
                    with self.connect() as con:
                        con.execute(f"""
                            COPY (
                                WITH tn AS (
                                    SELECT tin_value, npi, count(*) AS c FROM rates
                                    WHERE tin_value IS NOT NULL AND npi IS NOT NULL
                                    GROUP BY 1, 2
                                ),
                                nz AS (
                                    SELECT n.npi, {ZIP5} AS zip, {therapy} AS is_t
                                    FROM npi_directory n WHERE {_HAS_ZIP}
                                )
                                SELECT tn.tin_value, nz.zip, sum(tn.c)::BIGINT AS c,
                                       count(DISTINCT tn.npi) AS nn,
                                       count(DISTINCT tn.npi) FILTER (nz.is_t) AS nt
                                FROM tn JOIN nz USING (npi)
                                GROUP BY 1, 2
                            ) TO '{sql_path(tmp)}' (FORMAT PARQUET)""")
                    os.replace(tmp, path)
                    # keep the newest PREVIOUS stamp too: a request that took
                    # its read_parquet('…') string a moment ago (leverage,
                    # radius and the dossier reuse it across statements)
                    # must not get "No files found" because a part landed
                    # mid-request (round 7 P2)
                    olds = sorted((p for p in self.dir.glob("tin_zip_place.*.parquet")
                                   if p != path), key=lambda p: p.stat().st_mtime,
                                  reverse=True)
                    for old in olds[1:]:
                        try:
                            old.unlink()
                        except OSError:      # still open on Windows: next time
                            pass
        return f"read_parquet('{sql_path(path)}')"

    PAIRS_SCHEMA = 1

    def pairs_relation(self) -> str:
        """`read_parquet(...)` of every (tin_value, npi, tin_is_really_npi)
        pair the rate parts hold, with its row count `c` (review 13 item 3.2).

        `SELECT DISTINCT tin_value, npi FROM rates` ran on EVERY call of the
        city rate map, market sizing, the client digest, outreach, new-to-
        network, steal-share and the entity drawer — 2-12 s of raw scan each on
        a 99M-rate store (1,939,999 pairs). Built once from the raw rows and
        stamped with the parts' signatures like placement_relation, so a part
        landing, changing or leaving yields a new file on the next request and
        never a stale pair. NULL npi (TIN-only rows) and NULL tin are kept, so
        a reader's DISTINCT over this equals the same DISTINCT over `rates`."""
        import hashlib
        import json as _json
        stamp = hashlib.sha1(_json.dumps(
            [sorted(self._part_sigs().items()), self.PAIRS_SCHEMA]).encode()).hexdigest()[:16]
        path = self.dir / f"tin_npi_pairs.{stamp}.parquet"
        if not path.exists():
            lock = self.__dict__.setdefault("_pairs_lock", threading.Lock())
            with lock:
                if not path.exists():
                    tmp = self.dir / f"tin_npi_pairs.{stamp}.parquet.{os.getpid()}.tmp"
                    try:
                        with self.connect() as con:
                            con.execute(
                                "COPY (SELECT tin_value, npi, tin_is_really_npi, count(*) AS c "
                                "FROM rates GROUP BY tin_value, npi, tin_is_really_npi "
                                "ORDER BY tin_value, npi) "
                                f"TO '{sql_path(tmp)}' (FORMAT PARQUET)")
                        os.replace(tmp, path)
                    except BaseException:
                        tmp.unlink(missing_ok=True)
                        raise
                    olds = sorted((p for p in self.dir.glob("tin_npi_pairs.*.parquet")
                                   if p != path), key=lambda p: p.stat().st_mtime, reverse=True)
                    for old in olds[1:]:
                        try:
                            old.unlink()
                        except OSError:
                            pass
        return f"read_parquet('{sql_path(path)}')"

    def _part_sigs(self) -> dict[str, str]:
        """{part file name: size|mtime} for the rate parts on disk: every
        change to the `rates` view's rows changes a part's entry — a part
        landing, being replaced, rewritten by a row-rule migration, removed."""
        out = {}
        for p in self.rates_dir.glob("*.parquet"):
            try:
                st = p.stat()
            except OSError:
                continue
            out[p.name] = f"{st.st_size}|{st.st_mtime_ns}"
        return out

    def _rate_npis_relation(self, con: duckdb.DuckDBPyConnection) -> str:
        """Bring rate_npi_set up to date with the parts on disk and return a
        relation (SQL, one column `npi`) of the current set, on `con`.

        Parts that are new or rewritten are scanned one file at a time with
        NO lock held, into a temp table on `con`; the result is applied in one
        short transaction under the writer lock — the rows of parts that are
        gone or rescanned dropped, the new rows added, each scanned part's
        signature recorded. A part that cannot be read right now (being
        replaced, quarantined) keeps its old rows and signature and is tried
        again next call. If a writer holds the lock for more than a few
        seconds the apply waits for the next call — a banner never waits on a
        rollup — and the answer comes from the kept rows plus the temp table,
        which is the same set, exactly."""
        current = self._part_sigs()
        known = dict(con.execute("SELECT part, sig FROM rate_npi_parts").fetchall())
        gone = [p for p in known if p not in current]
        changed = sorted(p for p, sig in current.items() if known.get(p) != sig)
        if not gone and not changed:
            return "(SELECT npi FROM rate_npi_set)"
        con.execute("CREATE OR REPLACE TEMP TABLE __npi_new (part VARCHAR, npi VARCHAR)")
        scanned = []
        for name in changed:
            src = f"read_parquet('{sql_path(self.rates_dir / name)}')"
            try:
                con.execute("INSERT INTO __npi_new SELECT ?, npi FROM ("
                            + _RATE_NPIS_SQL_T.format(src=src) + ")", [name])
                scanned.append(name)
            except duckdb.Error:
                continue        # unreadable right now: retried next call
        drop = gone + scanned
        if not drop:
            return "(SELECT npi FROM rate_npi_set)"
        if self.write_lock.acquire(timeout=3):
            try:
                con.execute("BEGIN TRANSACTION")
                try:
                    con.execute("DELETE FROM rate_npi_set WHERE part IN (SELECT unnest(?::VARCHAR[]))",
                                [drop])
                    con.execute("DELETE FROM rate_npi_parts WHERE part IN (SELECT unnest(?::VARCHAR[]))",
                                [drop])
                    con.execute("INSERT INTO rate_npi_set SELECT part, npi FROM __npi_new")
                    for name in scanned:
                        con.execute("INSERT INTO rate_npi_parts VALUES (?, ?)", [name, current[name]])
                    # the round-4 whole-store table, superseded by the per-part set
                    con.execute("DROP TABLE IF EXISTS rate_npis")
                    con.execute("DELETE FROM meta WHERE key = 'rate_npis_parts'")
                    con.execute("COMMIT")
                    return "(SELECT npi FROM rate_npi_set)"
                except Exception:
                    try:
                        con.execute("ROLLBACK")
                    except duckdb.Error:
                        pass
                    raise
            finally:
                self.write_lock.release()
        # a writer is busy: the exact current set, from what was just read
        con.execute("CREATE OR REPLACE TEMP TABLE __npi_drop AS "
                    "SELECT unnest(?::VARCHAR[]) AS part", [drop])
        return ("(SELECT npi FROM rate_npi_set WHERE part NOT IN (SELECT part FROM __npi_drop) "
                "UNION SELECT npi FROM __npi_new)")

    def available_states(self, max_age_seconds: float = 60.0) -> list[str]:
        """Distinct US states present in the enriched directory — used to fill
        the dashboard's state filter so users only pick states that have data
        (an empty result then clearly means 'no rates there yet', not a bug).

        Cached + single-flight like store_stats/enrichment_progress: the
        DISTINCT-unnest over both directories is another full scan on the 15s
        poll (loadStateOptions), and the state list changes slowly (only as
        enrichment resolves new geography)."""
        cached = self._states_cache
        if cached is not None and (time.monotonic() - cached[0]) < max_age_seconds:
            return cached[1]
        if not self._states_lock.acquire(blocking=False):
            return cached[1] if cached is not None else []
        try:
            cached = self._states_cache
            if cached is not None and (time.monotonic() - cached[0]) < max_age_seconds:
                return cached[1]
            result = self._compute_states()
            self._states_cache = (time.monotonic(), result)
            return result
        finally:
            self._states_lock.release()

    def _compute_states(self) -> list[str]:
        with self.connect() as con:
            try:
                # union both directories: the state filter applies on every
                # grain, and an NPI-grain-only state lives in npi_directory but
                # not necessarily in a tin_directory row's aggregated `states`.
                rows = con.execute(
                    """
                    SELECT DISTINCT s FROM (
                        SELECT unnest(states) AS s FROM tin_directory
                        WHERE states IS NOT NULL AND len(states) > 0
                        UNION
                        SELECT state AS s FROM npi_directory WHERE state IS NOT NULL
                    )
                    WHERE s IS NOT NULL AND s <> ''
                    ORDER BY s
                    """
                ).fetchall()
                # only real US state codes: NPPES addresses abroad and full
                # names ("BRITISH COLUMBIA", "RIYADH", "MASSACHUSETTS", AA/AE/AP
                # military codes) made "69 states with data" and filled every
                # State suggestion list (review 10 item 3.4)
                from .states import STATE_CODES
                rows = [r for r in rows if r[0] in STATE_CODES]
            except Exception as e:  # noqa: BLE001 — no rows/view yet on a fresh store
                # A fresh store legitimately has no tin_directory view yet, so
                # an empty result is normal — but log unexpected errors so a
                # real problem (a broken view, a lock) isn't indistinguishable
                # from "no data yet" forever.
                import logging as _logging
                _logging.getLogger(__name__).debug(
                    "available_states query returned nothing (%s)", e)
                return []
        return [r[0] for r in rows if r[0]]

    # -- what is LOADED vs what the analytics cover (review 13 item 1.2) -------

    def _part_sets_current(self, con: duckdb.DuckDBPyConnection,
                           budget_s: float | None = None) -> bool:
        """Bring part_payer_set / part_tin_set up to date with the parts on
        disk — the rate_npi_set pattern: new or rewritten parts are scanned
        one at a time with NO lock held, the result applied in one short
        transaction under the writer lock. Returns True when every part is
        counted (False: the time budget ran out or a writer held the lock;
        the next call continues)."""
        current = self._part_sigs()
        known = dict(con.execute("SELECT part, sig FROM part_set_parts").fetchall())
        gone = [p for p in known if p not in current]
        changed = sorted(p for p, sig in current.items() if known.get(p) != sig)
        if not gone and not changed:
            return True
        t0 = time.monotonic()
        con.execute("CREATE OR REPLACE TEMP TABLE __pp_new (part VARCHAR, payer VARCHAR, "
                    "file_month VARCHAR, n BIGINT)")
        con.execute("CREATE OR REPLACE TEMP TABLE __pt_new (part VARCHAR, tin_value VARCHAR)")
        scanned, complete = [], True
        for name in changed:
            if budget_s is not None and time.monotonic() - t0 > budget_s:
                complete = False
                break
            src = f"read_parquet('{sql_path(self.rates_dir / name)}')"
            try:
                con.execute(f"INSERT INTO __pp_new SELECT ?, payer, file_month, count(*) "
                            f"FROM {src} GROUP BY payer, file_month", [name])
                con.execute(f"INSERT INTO __pt_new SELECT DISTINCT ?, tin_value FROM {src} "
                            "WHERE tin_value IS NOT NULL", [name])
                scanned.append(name)
            except duckdb.Error:
                con.execute("DELETE FROM __pp_new WHERE part = ?", [name])
                con.execute("DELETE FROM __pt_new WHERE part = ?", [name])
                complete = False      # unreadable right now: next call
        drop = gone + scanned
        if not drop:
            return complete and not changed
        if not self.write_lock.acquire(timeout=3):
            return False
        try:
            con.execute("BEGIN TRANSACTION")
            try:
                for tbl in ("part_payer_set", "part_tin_set", "part_set_parts"):
                    con.execute(f"DELETE FROM {tbl} WHERE part IN "
                                "(SELECT unnest(?::VARCHAR[]))", [drop])
                con.execute("INSERT INTO part_payer_set SELECT * FROM __pp_new")
                con.execute("INSERT INTO part_tin_set SELECT * FROM __pt_new")
                for name in scanned:
                    con.execute("INSERT INTO part_set_parts VALUES (?, ?)", [name, current[name]])
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except duckdb.Error:
                    pass
                raise
        finally:
            self.write_lock.release()
        return complete

    def parts_holding(self, ident: str) -> list[str] | None:
        """The rate parts that hold `ident` as a TIN or an NPI (review 13 item
        3.5: /api/validate read every part — an OR of tin and npi defeats
        Parquet pruning; 13.9 s cold on a 99M-rate store). From the kept
        per-part TIN and NPI sets; None when they cannot answer exactly right
        now (the caller then scans every part, as before)."""
        if not re.fullmatch(r"[0-9]{9,10}", str(ident or "")):
            return None     # rate_npi_set keeps well-formed NPIs only
        try:
            with self.connect() as con:
                if not self._part_sets_current(con, None):
                    return None
                rel = self._rate_npis_relation(con)
                if "__npi_new" in rel:       # a writer held the lock: not applied
                    return None
                got = con.execute(
                    "SELECT part FROM part_tin_set WHERE tin_value = ? "
                    "UNION SELECT part FROM rate_npi_set WHERE npi = ?",
                    [ident, ident]).fetchall()
            return sorted(str(self.rates_dir / r[0]) for r in got
                          if (self.rates_dir / r[0]).exists())
        except Exception:  # noqa: BLE001 — the full scan is always right
            return None

    def loaded_inventory(self, budget_s: float | None = 2.0) -> dict:
        """{payers, months, tins, rows_by_payer, complete} of every LOADED rate
        part (not what the analytics cover). Cached until a part changes; a
        first call on a big store counts what it can in `budget_s` and says
        complete=False (the next poll continues). Never raises."""
        try:
            sig = hashlib.sha1(json.dumps(sorted(self._part_sigs().items())).encode()).hexdigest()
        except Exception:  # noqa: BLE001
            sig = None
        held = getattr(self, "_inventory_cache", None)
        if held is not None and held[0] == sig and held[1].get("complete"):
            return held[1]
        lock = self.__dict__.setdefault("_inventory_lock", threading.Lock())
        if not lock.acquire(blocking=False):
            return held[1] if held is not None else {
                "payers": [], "months": [], "tins": 0, "rows_by_payer": {}, "complete": False}
        try:
            with self.connect() as con:
                complete = self._part_sets_current(con, budget_s)
                payers = con.execute(
                    "SELECT payer, sum(n) FROM part_payer_set WHERE part IN "
                    "(SELECT part FROM part_set_parts) GROUP BY payer ORDER BY payer").fetchall()
                months = [r[0] for r in con.execute(
                    "SELECT DISTINCT file_month FROM part_payer_set WHERE file_month IS NOT NULL "
                    "AND file_month <> '' ORDER BY file_month DESC").fetchall()]
                tins = con.execute(
                    "SELECT count(DISTINCT tin_value) FROM part_tin_set").fetchone()[0] or 0
            out = {"payers": [p for p, _n in payers if p is not None],
                   "rows_by_payer": {p: int(n or 0) for p, n in payers if p is not None},
                   "months": months, "tins": int(tins), "complete": bool(complete)}
            self._inventory_cache = (sig, out)
            return out
        except Exception as e:  # noqa: BLE001 — a count never breaks a screen
            logging.getLogger(__name__).debug("loaded inventory unavailable (%s)", e)
            return held[1] if held is not None else {
                "payers": [], "months": [], "tins": 0, "rows_by_payer": {}, "complete": False}
        finally:
            lock.release()

    def spine_payers(self) -> list[str]:
        """Payers present in the analytics spine — what every screen can show.
        Counted once per spine generation."""
        gen = self.spine_generation
        held = getattr(self, "_spine_payers_cache", None)
        if held is not None and held[0] == gen:
            return list(held[1])
        try:
            with self.connect() as con:
                if not con.execute("SELECT count(*) FROM information_schema.tables "
                                   "WHERE table_name = 'rates_by_tin_tbl'").fetchone()[0]:
                    return []
                got = [r[0] for r in con.execute(
                    "SELECT DISTINCT payer FROM rates_by_tin_tbl WHERE payer IS NOT NULL "
                    "ORDER BY payer").fetchall()]
        except Exception:  # noqa: BLE001
            return list(held[1]) if held is not None else []
        self._spine_payers_cache = (gen, got)
        return list(got)

    def analytics_coverage(self, max_age_seconds: float = 3.0) -> dict:
        """How much of what is LOADED the analytics (and so every screen,
        report and export) cover, and what the rollup is doing about the rest
        (review 13 item 1.2) — the header line, /api/payers' `covered`, the
        methodology sentence. Cheap (the files table, the kept part sets, one
        spine DISTINCT per spine generation); cached a few seconds. Never
        raises: a failure reads as state 'current' with no numbers rather than
        a broken header."""
        from . import rollplan
        held = getattr(self, "_coverage_cache", None)
        if held is not None and time.monotonic() - held[0] < max_age_seconds \
                and held[2] == (self.data_generation, self._rollup_active):
            return held[1]
        try:
            out = self._analytics_coverage()
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).debug("coverage unavailable (%s)", e)
            out = {"state": "current", "covered_files": 0, "done_files": 0,
                   "covered_payers": 0, "loaded_payers": 0, "covered_rows": 0,
                   "loaded_rows": 0, "reason": "", "pct": None, "eta_text": "",
                   "covered": [], "loaded": []}
        out["sentence"] = rollplan.coverage_sentence(out)
        self._coverage_cache = (time.monotonic(), out, (self.data_generation, self._rollup_active))
        return out

    def _coverage_facts(self, con: duckdb.DuckDBPyConnection) -> dict:
        """The raw facts every coverage answer is computed from — ONE rule for
        the header, the payer chips, /api/payers, the activity panel, the
        fold-in events and every document sentence (review 14 items 1.1c and
        2.7: the header said 0 of 4 payers while the chips said 3 and every
        payer was answering)."""
        files = con.execute(
            "SELECT filename, finished_at::VARCHAR, coalesce(rows_emitted, 0), payer "
            "FROM files WHERE status = 'done' AND rows_emitted > 0 "
            "AND coalesce(file_type, '') <> 'provider_reference'").fetchall()
        built = bool(self._meta_get(con, "rollup_schema_version"))
        marker = self._meta_get(con, "rollup_covered_through") or ""
        names = set(json.loads(self._meta_get(con, "rollup_covered_names") or "[]"))
        needs_full = _needs_full_set(self._meta_get(con, "rollup_needs_full"))
        pending = self._read_pending_payers(con)[1]
        plan = self._plan_get(con)
        part_payers: dict[str, set] = {}
        part_rows: dict[tuple, int] = {}
        for part, payer, n in con.execute(
                "SELECT part, payer, sum(n) FROM part_payer_set GROUP BY part, payer").fetchall():
            part_payers.setdefault(part, set()).add(payer)
            part_rows[(part, payer)] = int(n or 0)
        # files the analytics hold: the coverage marker's (a completed build
        # or update), plus — while a sliced build is part-way — every file
        # whose payers' slices it has committed (and which it snapshotted)
        plan_done: set = set()
        part_way: set = set()
        plan_parts: dict = {}
        p_ts, p_names = "", set()
        if plan is not None:
            last: dict = {}
            first: dict = {}
            nxt = int(plan.get("next", 0))
            for i, sl in enumerate(plan["slices"]):
                if sl.get("t") == "spine":
                    p = sl.get("payer")
                    last[p] = i
                    first.setdefault(p, i)
                    tot, dn = plan_parts.get(p, (0, 0))
                    plan_parts[p] = (tot + 1, dn + (1 if i < nxt else 0))
            plan_done = {p for p, i in last.items() if i < nxt}
            # some of the payer's slices committed, not all: its rows in the
            # spine are a mix of rebuilt and not yet rebuilt (or missing)
            part_way = {p for p, i in last.items() if i >= nxt and first[p] < nxt} \
                if plan.get("kind") == "full" else set()
            p_ts, p_names = plan.get("cover_ts") or "", set(plan.get("cover_names") or [])

        def covered(fn: str, ts: str | None, payer: str | None) -> bool:
            if ts is None:      # (no finish time: rollups_stale never counts it either)
                return built
            if built and (ts < marker or (ts == marker and fn in names)):
                return True
            if plan is not None and plan.get("kind") == "full" \
                    and (ts < p_ts or (ts == p_ts and fn in p_names)):
                # the part's own payers; while its per-part set is not counted
                # yet, the file's payer (a sample mid first build read "2 of 2
                # payers (0 of 2 files)" — review 14 item 1.1f)
                pp = part_payers.get(f"{file_key(fn)}.parquet") or ({payer} if payer else set())
                return bool(pp) and pp <= plan_done
            return False

        return {"files": files, "covered": [covered(f[0], f[1], f[3]) for f in files],
                "built": built, "needs_full": needs_full, "pending": pending,
                "plan": plan, "plan_done": plan_done, "part_way": part_way,
                "plan_parts": plan_parts, "part_payers": part_payers,
                "part_rows": part_rows}

    def _analytics_coverage(self) -> dict:
        from . import rollplan
        inv = self.loaded_inventory()
        with self.connect() as con:
            f = self._coverage_facts(con)
        files, plan = f["files"], f["plan"]
        cov = [fl for fl, c in zip(files, f["covered"]) if c]
        spine_payers = self.spine_payers()
        loaded = list(inv["payers"]) if inv.get("complete") else sorted(
            set(inv["payers"]) | set(spine_payers) | {fl[3] for fl in files if fl[3]})
        # THE payer rule: in the spine and not half-rebuilt. During a rebuild
        # of an EXISTING spine a payer the plan has not reached still has all
        # its previous rows, and they answer every screen (review 14 1.1c/2.7)
        covered_list = [p for p in spine_payers if p in set(loaded) and p not in f["part_way"]]
        needs_full, pending = f["needs_full"], f["pending"]
        stale = (needs_full or bool(pending) or plan is not None
                 or len(cov) < len(files))
        failure = self.rollup_failure()
        job = self.rollup_job()
        paused = self.analytics_paused()
        if self._rollup_active:
            state = "building"
        elif failure and stale:
            state = "failed"
        elif stale:
            state = "behind"
        else:
            state = "current"
        pct = job["pct"] if job else None
        eta = rollplan.eta_text(job["eta_s"]) if job and job.get("eta_s") else ""
        n_new = len(files) - len(cov)
        rebuilding = plan is not None and plan.get("kind") == "full" and f["built"]
        parts_saved = (int(plan.get("next", 0)), len(plan["slices"])) if plan is not None else None
        when = self._foldin_when()
        kind = (job or {}).get("kind")
        if state == "building":
            if kind == "full":
                what = "Rebuilding the analytics" if rebuilding else "Building the analytics"
                reason = (f"{what} — {pct} %" + (f", {eta}" if eta else "")
                          + (f" ({job['label']})" if job.get("label") else ""))
            elif kind == "names":
                reason = (f"Refreshing practice names and places — {pct} %"
                          + (f", {eta}" if eta else ""))
            elif kind == "names_refresh":
                reason = "Refreshing the names of newly identified practices"
            elif kind == "remove":
                reason = "Taking a removed file's numbers out of the analytics"
            else:
                nf = len((job or {}).get("files") or [])
                reason = (f"Adding {nf:,} newly loaded file{'s' if nf != 1 else ''} to the "
                          "analytics" if nf else "Adding the newly loaded files to the analytics")
                if pct:
                    reason += f" — {pct} %"
        elif state == "failed":
            reason = failure.get("sentence") or "stopped"
            reason = (reason if reason.startswith("stopped") else "stopped: " + reason) \
                + " — Files tab"
        elif plan is not None:
            what = ("The analytics are being rebuilt" if rebuilding else
                    "The analytics build is part-way") if plan.get("kind") == "full" \
                else "The practice-name build is part-way"
            reason = (f"{what} ({parts_saved[0]} of {parts_saved[1]} parts saved)"
                      + (" — the rows in place still answer every screen" if rebuilding else "")
                      + (f"; it continues {when}" if when else ""))
        elif needs_full:
            reason = "The analytics need one full rebuild" + (f" — it runs {when}" if when else "")
        elif n_new or pending:
            reason = (f"{n_new:,} newly loaded file{'s' if n_new != 1 else ''} not in the "
                      "analytics yet" + (f" — added {when}" if when else "")) if n_new else \
                     ("A removed file's numbers are still in the analytics — they come out "
                      + (when or "at the next update"))
        else:
            reason = ""
        if paused and stale and state != "building":
            reason = "Analytics paused — " + (reason[0].lower() + reason[1:] if reason else
                                              "nothing is added until you resume")
        cov_rows = sum(fl[2] for fl in cov)
        return {
            "state": state, "reason": reason, "pct": pct, "eta_text": eta,
            "covered_files": len(cov), "done_files": len(files),
            "covered_payers": len(covered_list), "loaded_payers": len(loaded),
            "covered_rows": int(cov_rows),
            "loaded_rows": int(sum(fl[2] for fl in files)),
            "covered": covered_list, "loaded": loaded,
            "failure": failure, "inventory_complete": bool(inv.get("complete")),
            "paused": bool(paused), "rebuilding": bool(rebuilding),
            "part_way": sorted(p for p in f["part_way"] if p is not None),
            "parts_saved": list(parts_saved) if parts_saved else None,
            "pending_removal": bool(pending), "needs_full": bool(needs_full),
            "new_files": int(n_new), "plan_kind": plan.get("kind") if plan else None,
        }

    def _foldin_when(self) -> str:
        """When the waiting work goes in, from the fold-in scheduler's real
        state ("in about 4 min (the queue is busy)", "in a few seconds",
        "when you resume") — '' with no scheduler (a CLI command updates its
        analytics itself before it returns). Never raises."""
        try:
            f = self.foldin
            return f.when_text() if f is not None and f.is_running() else ""
        except Exception:  # noqa: BLE001
            return ""

    # -- pause + fold-in events (review 14 item 1.2) ---------------------------

    def analytics_paused(self) -> bool:
        """The user's "Pause analytics" switch (persisted). Never raises."""
        if self._analytics_paused is _MISSING:
            try:
                with self.connect() as con:
                    self._analytics_paused = self._meta_get(con, "analytics_paused") == "1"
            except Exception:  # noqa: BLE001
                return False
        return bool(self._analytics_paused)

    def set_analytics_paused(self, paused: bool) -> None:
        with self.write_lock, self.connect() as con:
            if paused:
                con.execute("INSERT OR REPLACE INTO meta VALUES ('analytics_paused', '1')")
            else:
                con.execute("DELETE FROM meta WHERE key = 'analytics_paused'")
        self._analytics_paused = bool(paused)
        self._coverage_cache = None

    FOLD_EVENTS_KEEP = 200

    def _note_fold_ins(self, kind: str) -> list[dict]:
        """Record one event per file that the analytics hold now and did not
        at the last call — the moment the coverage marker or a plan's
        committed slices first include it — in a ring of the last 200 kept in
        `meta` (it survives a restart). This is what the panel's "Added
        recently" shows and what the tests read. Cosmetic: never raises, never
        fails the update that called it."""
        try:
            now = _utcnow()
            with self.connect() as con:
                f = self._coverage_facts(con)
                raw_known = self._meta_get(con, "analytics_known_covered")
                raw_events = self._meta_get(con, "analytics_events")
            have = {f"{fl[0]}|{fl[1]}": fl for fl, c in zip(f["files"], f["covered"]) if c}
            events = json.loads(raw_events) if raw_events else []
            if raw_known is None:
                known: set = set(have)          # first run of 0.8.0: no backlog of events
                fresh = []
            else:
                known = set(json.loads(raw_known))
                fresh = sorted((have[k] for k in have.keys() - known), key=lambda r: (r[1] or "", r[0]))
            for fn, ts, rows, payer in fresh:
                waited = None
                try:
                    t0 = dt.datetime.fromisoformat(str(ts)).replace(tzinfo=None)
                    waited = round(max(0.0, (now - t0).total_seconds()), 1)
                except (TypeError, ValueError):
                    pass
                events.append({"file": fn, "payer": payer, "rows": int(rows or 0),
                               "loaded_at": ts, "added_at": now.isoformat(timespec="seconds"),
                               "waited_s": waited, "kind": kind})
            events = events[-self.FOLD_EVENTS_KEEP:]
            if fresh or raw_known is None or set(have) != known:
                with self.write_lock, self.connect() as con2:
                    con2.execute("INSERT OR REPLACE INTO meta VALUES ('analytics_known_covered', ?)",
                                 [json.dumps(sorted(have))])
                    con2.execute("INSERT OR REPLACE INTO meta VALUES ('analytics_events', ?)",
                                 [json.dumps(events)])
            self._coverage_cache = None
            return [e for e in events[-len(fresh):]] if fresh else []
        except Exception as e:  # noqa: BLE001 — cosmetic tier
            logging.getLogger(__name__).debug("fold-in events not recorded (%s)", e)
            return []

    def analytics_events(self, limit: int = 50) -> list[dict]:
        """The last `limit` fold-in events, newest first. Never raises."""
        try:
            with self.connect() as con:
                raw = self._meta_get(con, "analytics_events")
            ev = json.loads(raw) if raw else []
            return list(reversed(ev))[:max(0, int(limit))]
        except Exception:  # noqa: BLE001
            return []

    def analytics_note(self) -> list[str]:
        """The coverage sentence every methodology and CSV sidecar prints while
        the analytics are behind ([] when current; never raises — a footer
        line never fails a document)."""
        try:
            from . import rollplan
            line = rollplan.coverage_sentence(self.analytics_coverage(), document=True)
            return [line] if line else []
        except Exception:  # noqa: BLE001
            return []

    def save_npi(self, npi: str, org_name: str | None, taxonomy_code: str | None,
                 taxonomy_desc: str | None, city: str | None, state: str | None,
                 entity_type: str | None = None, address: str | None = None,
                 zip_code: str | None = None, phone: str | None = None,
                 taxonomy_codes: str | None = None) -> None:
        state = _state_cell(state)
        with self.write_lock, self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO npi_directory "
                "(npi, entity_type, org_name, taxonomy_code, taxonomy_desc, "
                " taxonomy_codes, city, state, address, zip, phone, enriched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [npi, entity_type, org_name, taxonomy_code, taxonomy_desc,
                 taxonomy_codes, city, state,
                 address, (zip_code or "")[:5] or None, phone,
                 _utcnow()],
            )
        self._enrich_progress_cache = None  # progress moved; don't serve stale counts
        self._store_stats_cache = None
        self._states_cache = None  # a new state may now be present

    def save_npis_bulk(self, rows: list[dict]) -> None:
        """Insert many NPPES records at once. Keys per row: npi, entity_type,
        org_name, taxonomy_code, city, state, address, zip, phone.

        SET-BASED upsert: build one Arrow batch and run a single
        `INSERT OR REPLACE ... SELECT`. A row-by-row executemany upsert costs
        ~1ms/row of prepared-statement overhead (≈5s per 5000-row batch — the
        dominant cost of resolving a national book); the set-based form
        vectorizes it (~20x faster, measured 4.9s -> 0.21s per 5000)."""
        if not rows:
            return
        import pyarrow as pa

        def sarr(key):
            return pa.array([r.get(key) for r in rows], type=pa.string())

        batch = pa.table({
            "npi": pa.array([r["npi"] for r in rows], type=pa.string()),
            "entity_type": sarr("entity_type"),
            "org_name": sarr("org_name"),
            "taxonomy_code": sarr("taxonomy_code"),
            "taxonomy_desc": pa.array([None] * len(rows), type=pa.string()),
            "taxonomy_codes": sarr("taxonomy_codes"),
            "city": sarr("city"),
            "state": pa.array([_state_cell(r.get("state")) for r in rows], type=pa.string()),
            "address": sarr("address"),
            "zip": pa.array([(r.get("zip") or "")[:5] or None for r in rows], type=pa.string()),
            "phone": sarr("phone"),
        })
        batch_cols = ("npi, entity_type, org_name, taxonomy_code, taxonomy_desc, "
                      "taxonomy_codes, city, state, address, zip, phone")
        cols = batch_cols + ", enriched_at"
        with self.write_lock, self.connect() as con:
            # enriched_at is stamped INSIDE the lock so it reflects COMMIT
            # order: a timestamp captured before the (serialized) write could
            # land out of order vs a concurrent writer, and the scoped
            # directory refresh keys on max(enriched_at) — an out-of-order
            # stamp below that max would be permanently skipped (latent; the
            # single enrichment loop serializes today, but a second bulk writer
            # would trip it).
            now = _utcnow()
            con.register("_npi_batch", batch)
            try:
                con.execute(f"INSERT OR REPLACE INTO npi_directory ({cols}) "
                            f"SELECT {batch_cols}, ? FROM _npi_batch", [now])
            finally:
                con.unregister("_npi_batch")
        self._enrich_progress_cache = None
        self._store_stats_cache = None
        self._states_cache = None

    # -- entity map -----------------------------------------------------------

    def set_entity_map(self, mapping: dict[str, str]) -> None:
        """Replace the TIN -> entity-name table (from config/entity_map.yaml).

        Upsert-shaped on purpose: INSERT OR REPLACE for present keys, DELETE
        only for absent ones, in ONE transaction. The old DELETE-all +
        re-INSERT hit a DuckDB PK/MVCC edge: while any long transaction (a
        rollup rebuild) holds an old snapshot, a deleted key can't be vacuumed
        from the PK index and re-inserting it fails with a spurious
        'Duplicate key' (measured: 86/480 concurrent entity edits 500'd during
        an ingest). OR REPLACE is verified safe against both live and
        tombstoned keys, and the single transaction removes the instant where
        a concurrent reader saw an EMPTY map between the two statements."""
        with self.write_lock, self.connect() as con:
            con.execute("BEGIN TRANSACTION")
            try:
                if mapping:
                    con.execute(
                        "DELETE FROM entity_map WHERE tin_value NOT IN "
                        "(SELECT unnest(?::VARCHAR[]))", [list(mapping)])
                    con.executemany(
                        "INSERT OR REPLACE INTO entity_map VALUES (?, ?)",
                        [[tin, name] for tin, name in mapping.items()])
                else:
                    con.execute("DELETE FROM entity_map")
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except duckdb.Error:
                    pass
                raise
        self.note_user_edit()

    def entity_map(self) -> dict[str, str]:
        with self.connect() as con:
            return dict(con.execute("SELECT tin_value, entity_name FROM entity_map").fetchall())

    # -- verified websites -------------------------------------------------------

    def org_websites(self) -> dict[str, str]:
        """TIN -> hand-verified practice URL (NPPES has no website field, so
        these are the only confirmed URLs)."""
        with self.connect() as con:
            return dict(con.execute(
                "SELECT tin_value, url FROM org_websites WHERE url IS NOT NULL AND url <> ''"
            ).fetchall())

    def set_org_website(self, tins: list[str], url: str | None) -> None:
        """Set (or clear, when url is falsy) the verified website for these tax
        ids. An org's URL is stored on ALL its constituent TINs so it shows on
        the entity no matter which TIN a later view resolves through."""
        tins = [t for t in (tins or []) if t]
        if not tins:
            return
        with self.write_lock, self.connect() as con:
            if url:
                con.executemany("INSERT OR REPLACE INTO org_websites VALUES (?, ?)",
                                [[t, url] for t in tins])
            else:
                con.executemany("DELETE FROM org_websites WHERE tin_value = ?",
                                [[t] for t in tins])
        self.note_user_edit()

    # -- peer sets ---------------------------------------------------------------

    def save_peer_set(self, name: str, definition: dict) -> None:
        with self.write_lock, self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO peer_sets VALUES (?, ?, ?)",
                [name, json.dumps(definition), _utcnow()],
            )

    def peer_sets(self) -> dict[str, dict]:
        with self.connect() as con:
            return {
                name: json.loads(defn)
                for name, defn in con.execute("SELECT name, definition FROM peer_sets").fetchall()
            }

    def delete_peer_set(self, name: str) -> None:
        with self.write_lock, self.connect() as con:
            con.execute("DELETE FROM peer_sets WHERE name = ?", [name])

    # -- MPFS ----------------------------------------------------------------------

    def load_mpfs(self, rows: list[dict], source: str, state: str | None = None) -> int:
        """Load a Medicare fee schedule. With `state` (an official locality
        import) it REPLACES that state's anchor only — so MO's and KS's
        localities can both be loaded — plus any older anchor whose locality
        was never stated (it could have been either). Without a state (the
        simple CSV) it replaces everything, as before; rows may still carry a
        per-row "state"."""
        st = (state or "").strip().upper() or None
        # one transaction: readers (benchmark % of Medicare) must never see the
        # empty instant between the DELETE and the INSERT (no PK on mpfs, so
        # the DuckDB tombstone edge that bit entity_map doesn't apply here)
        with self.write_lock, self.connect() as con:
            con.execute("BEGIN TRANSACTION")
            try:
                if st:
                    con.execute("DELETE FROM mpfs WHERE state = ? OR state IS NULL", [st])
                else:
                    con.execute("DELETE FROM mpfs")
                if rows:
                    con.executemany(
                        "INSERT INTO mpfs (code, locality, non_facility_rate, source, state) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [[r["code"], r.get("locality", ""), float(r["non_facility_rate"]), source,
                          (str(r.get("state") or "").strip().upper() or st)] for r in rows],
                    )
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except duckdb.Error:
                    pass
                raise
        return len(rows)

    def mpfs_loaded(self) -> str | None:
        """Every loaded anchor's source line (locality, state, CF), or None."""
        with self.connect() as con:
            srcs = [r[0] for r in con.execute(
                "SELECT DISTINCT source FROM mpfs WHERE source IS NOT NULL ORDER BY 1").fetchall()]
        return "; ".join(srcs) if srcs else None

    def medicare_anchor(self, state: str | None) -> dict:
        """The Medicare rates a "% of Medicare" may be computed against, for a
        view or practice in `state`: {"rates": {code: rate}, "label": what was
        used (locality + source), "refusal": why nothing is shown}.

        One locality's fee schedule applied to another state's practices
        misstates the percentage (GPCIs differ by state), so:
        - the state's own anchor when loaded (several localities of that state
          -> their median, and the label says so);
        - no state given and exactly one state loaded -> that one, labelled;
        - an anchor whose file never said its locality -> used, labelled so;
        - otherwise REFUSED with the reason — never estimated."""
        st = (state or "").strip().upper() or None
        with self.connect() as con:
            # a non-finite or non-positive stored rate (an anchor uploaded by
            # 0.7.5, which accepted 'nan') is no anchor for that code — never a
            # NaN percentage that fails the whole screen (review 12 item 2.1)
            rows = con.execute("SELECT state, locality, source, code, non_facility_rate "
                               "FROM mpfs WHERE isfinite(non_facility_rate) "
                               "AND non_facility_rate > 0").fetchall()
        if not rows:
            return {"rates": {}, "label": None, "refusal": None}
        states = sorted({r[0] for r in rows if r[0]})
        unlabelled = [r for r in rows if not r[0]]
        if st and st in states:
            use = [r for r in rows if r[0] == st]
        elif unlabelled:
            use = unlabelled
        elif st:
            return {"rates": {}, "label": None, "refusal": (
                f"% of Medicare not shown: the Medicare fee schedule loaded is for "
                f"{', '.join(states)}, not {st} — load {st}'s locality (Benchmark tab -> "
                "Load MPFS) to compare.")}
        elif len(states) == 1:
            use = rows
        else:
            return {"rates": {}, "label": None, "refusal": (
                "% of Medicare not shown: Medicare fee schedules are loaded for "
                f"{', '.join(states)} — pick a state (or a practice) so the right "
                "locality is used.")}
        by_code: dict[str, list[float]] = {}
        for r in use:
            by_code.setdefault(r[3], []).append(r[4])
        import statistics as _st
        rates = {c: _st.median(v) for c, v in by_code.items()}
        locs = sorted({r[1] for r in use if r[1]})
        label = "; ".join(sorted({r[2] for r in use if r[2]})) or "Medicare fee schedule"
        if len(locs) > 1:
            label += f" (median of {len(locs)} localities)"
        if use is unlabelled:
            label += " — the loaded file does not say which locality"
        return {"rates": rates, "label": label, "refusal": None}

    # -- reset ------------------------------------------------------------------------

    def reset(self) -> None:
        held: list[str] = []
        with self.write_lock:
            for p in self.rates_dir.glob("*.parquet"):
                if not unlink_with_retry(p) and p.exists():
                    held.append(p.name)       # retried at the next open (doomed)
            # a parked replacement would be ADOPTED at the next open — with
            # no files row (round 7 P1.9)
            for p in self.rates_dir.glob(".*.parked"):
                unlink_with_retry(p)
            for p in self.dir.glob("tin_zip_place.*"):
                unlink_with_retry(p)
            for pat in ("practice_medians*.parquet*", "spine_units*.parquet*", "tin_npi_pairs.*",
                        "spine_counts.*",
                        "summary_strips.*.json"):
                for p in self.dir.glob(pat):
                    unlink_with_retry(p)
            self._spine_mat.clear()
            with self.connect() as con:
                con.execute(
                    "DELETE FROM files; DELETE FROM npi_directory; DELETE FROM provider_refs; "
                    "DELETE FROM peer_sets; DELETE FROM mpfs; "
                    # the queue too: surviving 'done' rows (and their content
                    # hashes) would refuse to re-queue / dedup-away the very
                    # URLs the user re-pastes to rebuild the store they just
                    # wiped. entity_map and org_websites stay — user config.
                    "DELETE FROM url_queue; DELETE FROM rollup_pending_tins; "
                    "DELETE FROM content_fingerprints; DELETE FROM inflight_fingerprints; "
                    "DELETE FROM rate_npi_parts; DELETE FROM rate_npi_set; "
                    "DELETE FROM part_payer_set; DELETE FROM part_tin_set; "
                    "DELETE FROM part_set_parts; "
                    # part-operation and rollup bookkeeping of the wiped store
                    "DELETE FROM meta WHERE key IN ('parked_parts', "
                    "'rollup_pending_payers', 'rollup_needs_full', 'rollup_plan', "
                    "'rollup_failure');"
                )
                for tbl in ("rates_by_tin_tbl__next", "tin_directory_tbl__next"):
                    con.execute(f"DROP TABLE IF EXISTS {tbl}")
                # a part Windows would not let go of is deleted at the next
                # open, never served as the "empty" store's rates
                con.execute("INSERT OR REPLACE INTO meta VALUES ('doomed_parts', ?)",
                            [json.dumps(held)])
                for tbl in ("rates_dedup_tbl", "rates_by_tin_tbl", "tin_directory_tbl"):
                    con.execute(f"DROP TABLE IF EXISTS {tbl}")
                con.execute("INSERT OR REPLACE INTO meta VALUES ('spine_token', ?)", [os.urandom(8).hex()])
                self._register_views(con)
            # url_queue ids are gone; drop any lingering cancel flags so a reused
            # id can never inherit a stale "canceled" verdict
            with self._cancel_lock:
                self._canceled_downloads.clear()
        # AFTER the wipe: a count computed mid-reset (from the old tables) must
        # not be cached under the post-reset generation and served for 30s
        # against the now-empty store.
        self._rollup_failure = None
        self._inventory_cache = None
        self._bump_generation()
        self._invalidate_scan_caches()


# Every Store method that writes url_queue signals the waiting worker threads
# (Store.queue_changed). "always": the write always changes something. "if
# done": it returns what it changed (a claimed row, a count, True) and signals
# only then — a claim that found nothing must NOT signal, or every idle worker
# would wake every other one in a loop. url_progress (byte counts during a
# download) is deliberately absent: no worker waits on progress.
# tests/mrfx/test_queue_signal.py fails if a url_queue writer is missing here.
_QUEUE_WRITERS_ALWAYS = ("update_url", "pause_url", "park_url", "park_waiting", "record_identity",
                         "forget_file", "reset", "note_unlisted_children")
_QUEUE_WRITERS_IF_DONE = ("_claim_next", "enqueue_url", "enqueue_urls", "requeue_failed",
                          "requeue_oversize_within", "requeue_urls", "wake_paused_urls",
                          "wake_parked_urls",
                          "recover_stuck_urls", "set_url_status_by_id", "force_size_requeue",
                          "clear_queued", "cancel_all_downloading", "resume_canceled",
                          "revive_skipped_duplicates", "claim_content_ingest",
                          "release_claim", "release_mismatched_waiters",
                          "requeue_url_for_file", "requeue_republished")


def _signals_queue(fn, always: bool):
    import functools

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        result = fn(self, *args, **kwargs)
        if always or result:
            self.queue_changed()
        return result
    return wrapper


for _name in _QUEUE_WRITERS_ALWAYS:
    setattr(Store, _name, _signals_queue(getattr(Store, _name), True))
for _name in _QUEUE_WRITERS_IF_DONE:
    setattr(Store, _name, _signals_queue(getattr(Store, _name), False))


_MISSING = object()


def _state_cell(state):
    """A directory row's state as a USPS code when it names one ("Massachusetts"
    -> MA): a State = MA filter is an exact match and missed the spelled-out
    rows (review 10 item 3.4). A foreign province / military code stays as
    NPPES wrote it — the state lists show only real states."""
    if state is None:
        return None
    from .states import state_code_or_none
    return state_code_or_none(state) or state


def generation_cached(name: str, *, daily: bool = False):
    """Decorator: remember `fn(store, *args, **kwargs)` per data generation
    (Store.cached). `daily` adds today's date to the key, for answers that
    report an age in days."""
    import functools
    import json as _json

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(store, *args, **kwargs):
            key = _json.dumps([args, kwargs], sort_keys=True, default=str)
            if daily:
                key += "|" + dt.date.today().isoformat()
            return store.cached(name, key, lambda: fn(store, *args, **kwargs))
        wrapper.uncached = fn
        return wrapper
    return deco


class BackgroundParquetWriter:
    """A ParquetWriter fed through a small queue and drained by a thread.

    Encoding + zstd compression of a batch is real work (measured: 48% of a
    row-dense file's ingest — 12.7 s of 26.5 s for 13.5M rows) and pyarrow
    releases the GIL while it does it, so handing batches to a thread lets the
    parser keep scanning instead of waiting. At most `depth` tables are in
    flight (bounded memory). A write error surfaces on the next write() or
    on close(), which always joins the thread and closes the file."""

    def __init__(self, path, schema, compression, depth: int = 2):
        import queue
        import threading

        self.path, self.schema, self.compression = path, schema, compression
        self._q: "queue.Queue" = queue.Queue(maxsize=depth)
        self._err: BaseException | None = None
        self._t = threading.Thread(target=self._run, name="mrfx-parquet-writer", daemon=True)
        self._t.start()
        self._closed = False

    def _run(self) -> None:
        writer = None
        try:
            while True:
                table = self._q.get()
                if table is None:
                    break
                if self._err is not None:
                    continue              # keep draining so producers never block
                try:
                    if writer is None:
                        writer = pq.ParquetWriter(self.path, self.schema, compression=self.compression)
                    writer.write_table(table)
                except BaseException as e:  # noqa: BLE001 — re-raised in the producer
                    self._err = e
        finally:
            if writer is not None:
                try:
                    writer.close()
                except BaseException as e:  # noqa: BLE001
                    if self._err is None:
                        self._err = e

    def write_table(self, table) -> None:
        if self._err is not None:
            raise self._err
        self._q.put(table)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._q.put(None)
        self._t.join()
        if self._err is not None:
            raise self._err


class RatesPartWriter:
    """Incremental parquet writer for one source file's rows. Use as a context
    manager; call write_batch(rows) any number of times. Rows are appended to a
    temp parquet, atomically renamed into the part path on a clean exit, and
    discarded on error — so a failed parse never leaves a partial part behind."""

    def __init__(self, store: "Store", source_file: str):
        self.store = store
        self.path = store.rates_dir / f"{file_key(source_file)}.parquet"
        # NOT *.parquet: the rates view binds a glob over rates_dir, and a
        # half-written temp matching it would break concurrent readers.
        # The pid keeps the temp private to this process — two processes on
        # one store (a second serve, CLI ingest next to the app) must never
        # unlink each other's in-progress writes (Store.__init__ sweeps
        # orphans left by dead processes).
        self.tmp = store.rates_dir / f".{file_key(source_file)}.{os.getpid()}.parquet.tmp"
        self.source_file = source_file
        self._writer: BackgroundParquetWriter | None = None
        self.rows_written = 0

    def __enter__(self) -> "RatesPartWriter":
        self.tmp.unlink(missing_ok=True)
        return self

    def write_batch(self, rows) -> None:
        """rows: a parser RowBatch (columnar, fast) or a list of row dicts."""
        if not rows:
            return
        from .parser import batch_to_table
        table = batch_to_table(rows, RATES_SCHEMA)
        if self._writer is None:
            self._writer = BackgroundParquetWriter(self.tmp, RATES_SCHEMA, PARQUET_COMPRESSION)
        self._writer.write_table(table)
        self.rows_written += len(rows)

    def __exit__(self, exc_type, exc, tb):
        if self._writer is not None:
            try:
                self._writer.close()
            except BaseException:
                if exc_type is None:
                    # a failed WRITE fails the file exactly like a failed parse
                    self.tmp.unlink(missing_ok=True)
                    raise
        if exc_type is not None:
            self.tmp.unlink(missing_ok=True)
            return False
        # old-part payer scan outside the lock — same contract as
        # finalize_rates_part (replacement AND 0-row removal both queue)
        doomed = self.store._part_payers_for_removal(self.path)
        _fsync_path(self.tmp)       # before the rename that makes it live (4.4b)
        with self.store.write_lock:
            with self.store.connect() as con:
                self.store._queue_removed_payers(con, doomed)
            self.store._swap_in_part(self.source_file, self.tmp, self.path, self.rows_written)
            with self.store.connect() as con:
                self.store._register_views(con)
        self.store._invalidate_scan_caches()
        return False


def _duck_types() -> list[str]:
    """DuckDB column types matching RATES_SCHEMA order (for the empty view)."""
    mapping = {
        "string": "VARCHAR", "double": "DOUBLE", "bool": "BOOLEAN",
        "list<item: string>": "VARCHAR[]",
    }
    return [mapping[str(f.type)] for f in RATES_SCHEMA]
