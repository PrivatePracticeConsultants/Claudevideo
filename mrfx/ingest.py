"""Ingest orchestration: preflight -> parse -> store -> move/quarantine.

A bad file quarantines with a visible error; it never raises out of
`ingest_file`, so the watcher and server stay alive (spec §8.6).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

from .config import MrfxConfig
from .parser import (
    InNetworkParser,
    fast_big_file_available,
    parse_provider_reference_file,
    published_month_of,
    run_big_file_fast,
    run_in_network_parse,
    run_skim,
    skipped_groups_reason,
)
from .sniff import Preflight, open_stream, preflight
from .store import FINISHED_NOW, Store, file_key, sql_path, unlink_with_retry

log = logging.getLogger(__name__)

# Files whose estimated uncompressed size is at or above this use the
# disk-backed reference index + chunked progress. Below it, the fast in-memory
# path. ~1.5 GB uncompressed ≈ 150 MB compressed.
LARGE_FILE_UNCOMPRESSED_BYTES = 1_500_000_000

# One progress "chunk" = this many COMPRESSED bytes consumed. The bar reports
# chunks_done / chunks_total so the user sees the file worked through in
# digestible pieces.
CHUNK_COMPRESSED_BYTES = 64 * 1024 * 1024  # 64 MB


def thread_stacks_text() -> str:
    """Every main-process thread's live Python stack, formatted for a bug
    report. Shared by /api/debug/stacks and the parse-stall watchdog's
    auto-dump. Pure in-memory frame walk — no locks, no store access — so it
    is safe to call from any thread even when the rest of the program is
    wedged (which is exactly when it gets called)."""
    import sys as _sys
    import traceback as _tb

    from . import __version__

    names = {t.ident: t.name for t in threading.enumerate()}
    out = [f"mrfx {__version__} — {len(names)} threads "
           "(parser worker processes are separate and not shown)\n"]
    for tid, frame in sorted(_sys._current_frames().items()):
        out.append(f"--- thread {names.get(tid, tid)} ---")
        out.append("".join(_tb.format_stack(frame)))
    return "\n".join(out)


class _ProgressTracker:
    """Drives the chunk progress bar from compressed bytes read. Updates the
    files table (dashboard bar) and an optional CLI bar, throttled to chunk
    boundaries so it never thrashes the store."""

    def __init__(self, store: Store, filename: str, compressed_bytes: int, on_bar=None, passes: int = 1):
        self.store = store
        self.filename = filename
        self.total_bytes = max(compressed_bytes, 1)
        file_chunks = max(1, -(-self.total_bytes // CHUNK_COMPRESSED_BYTES))  # ceil
        self.passes = passes
        self.chunks_total = file_chunks * passes  # each pass reads the whole file
        self._file_chunks = file_chunks
        self.on_bar = on_bar
        self._pass = 0
        self._last_chunk = -1

    def set_pass(self, pass_idx: int) -> None:
        self._pass = pass_idx

    def update(self, compressed_read: int) -> None:
        in_pass = min(self._file_chunks, compressed_read // CHUNK_COMPRESSED_BYTES)
        chunk = self._pass * self._file_chunks + in_pass
        if chunk == self._last_chunk:
            return
        self._last_chunk = chunk
        pct = min(100.0, 100.0 * chunk / self.chunks_total)
        try:
            self.store.update_progress(self.filename, pct, chunks_done=chunk, chunks_total=self.chunks_total)
        except Exception:  # noqa: BLE001 — progress writes must never break ingest
            pass
        if self.on_bar:
            try:
                self.on_bar(chunk, self.chunks_total, pct)
            except Exception:  # noqa: BLE001 — a display bar is cosmetic; drop it
                self.on_bar = None

    def set_percent(self, pct: float) -> None:
        """Drive the bar by percent (the single-read path's phases). Chunks
        are 64 MB compressed — too coarse to show a long replay moving — so
        a half-percent of movement updates the bar too."""
        chunk = int(self.chunks_total * min(100.0, pct) / 100.0)
        if chunk == self._last_chunk and pct - getattr(self, "_last_pct", -1.0) < 0.5:
            return
        self._last_chunk = chunk
        self._last_pct = pct
        try:
            self.store.update_progress(self.filename, pct, chunks_done=chunk, chunks_total=self.chunks_total)
        except Exception:  # noqa: BLE001 — progress writes must never break ingest
            pass
        if self.on_bar:
            try:
                self.on_bar(chunk, self.chunks_total, pct)
            except Exception:  # noqa: BLE001 — a display bar is cosmetic; drop it
                self.on_bar = None

    def finish(self) -> None:
        try:
            self.store.update_progress(self.filename, 100.0, self.chunks_total, self.chunks_total)
        except Exception:  # noqa: BLE001
            pass
        if self.on_bar:
            try:
                self.on_bar(self.chunks_total, self.chunks_total, 100.0)
            except Exception:  # noqa: BLE001
                self.on_bar = None


def _chunk_label(last) -> str:
    """The last chunk count the parent saw from a worker, for log lines.
    `last` is -1 before any report, else (chunks_done, pct)."""
    if isinstance(last, tuple) and isinstance(last[0], int) and last[0] >= 0:
        return str(last[0])
    return "0"


class SingleReadProgress:
    """Progress for run_big_file_fast, whose work is NOT two equal passes:
    a full scan, an optional SHORT re-read of the reference table, then a
    replay of the spooled target items (on a row-dense file — 590M rows from
    one Aetna file — the replay is most of the time). A two-pass bar sat at
    ~50% for the whole single read and jumped to 100% at the end.

    Shares: scan 0-80% (compressed bytes read), reference re-read 80-85%
    (scaled by where the reference table ended, learned during the scan),
    replay 85-99% (spool bytes replayed), 100% at finish. Monotonic, and it
    keeps moving during the replay — the parent's frozen-worker watchdog
    reads a moving chunk count as "alive"."""

    SCAN_END, REFS_END, REPLAY_END = 80.0, 85.0, 99.0
    STEP = 0.25   # emit at most every quarter percent

    def __init__(self, compressed_total: int, emit):
        self.total = max(int(compressed_total or 0), 1)
        self.emit = emit
        self.phase = "scan"
        self.pos = 0
        self.refs_end: int | None = None
        self.pct = 0.0
        self._last = -1.0

    def feed(self, compressed_read: int) -> None:      # open_stream progress_cb
        self.pos = compressed_read
        if self.phase == "scan":
            self._set(self.SCAN_END * min(1.0, compressed_read / self.total))
        elif self.phase == "refs":
            span = self.refs_end or self.total
            self._set(self.SCAN_END + (self.REFS_END - self.SCAN_END) * min(1.0, compressed_read / span))

    def refs_closed(self) -> None:
        if self.phase == "scan" and self.refs_end is None:
            self.refs_end = max(self.pos, 1)

    def second_pass(self) -> None:
        self.phase = "refs"

    def replay(self, done: int, total: int) -> None:
        self.phase = "replay"
        self._set(self.REFS_END + (self.REPLAY_END - self.REFS_END) * min(1.0, done / max(total, 1)))

    def _set(self, pct: float) -> None:
        pct = max(pct, self.pct)
        self.pct = pct
        if pct - self._last >= self.STEP:
            self._last = pct
            self.emit(pct)


def qa_report(store: Store, qa_counters: dict, source_file: str,
              agg: tuple[int, int, int] | None = None) -> dict:
    """Per-file data-quality summary (§7A.3). The incremental counters come from
    the parser (as a plain dict, so parallel workers can ship them across
    processes); the aggregate metrics (outliers, duplicate ratio, TIN=NPI
    count) are computed in DuckDB over the written part so this scales to
    files with millions of rows without holding them in Python memory.

    `agg` = (outliers, tin_is_really_npi rows, distinct facts) already
    computed by the parse WORKER on its own part (qa_aggregate_counts on a
    private connection) — then the main process's shared DuckDB, which also
    serves the dashboard and the rollups, isn't scanned at all."""
    qa = dict(qa_counters)
    total = qa["rows"]
    part = store.rates_dir / f"{file_key(source_file)}.parquet"
    try:
        if agg is None:
            with store.connect() as con:
                agg = (qa_aggregate_counts(con, part) if total and part.exists()
                       else (0, 0, 0))
        _apply_qa_aggregates(qa, total, agg)
    except Exception as e:  # noqa: BLE001 — this runs AFTER the parse succeeded
        # and its parquet is durable: derived stats failing (store briefly
        # locked by an external reader) must degrade, not flip a finished
        # multi-hour file to 'failed' and re-parse it
        log.warning("%s: QA aggregates unavailable (%s) — keeping parser counters only",
                    source_file, e)
        qa.setdefault("messages", []).append(
            "aggregate QA metrics (outliers, duplicate ratio) could not be "
            "computed this run — the rates themselves are unaffected")
    return qa


def qa_aggregate_counts(con, part: Path) -> tuple[int, int, int]:
    """(outlier dollar rates, tin_is_really_npi rows, distinct negotiated
    facts) of one rate part — THE definition, run by the parse worker on a
    private connection or by the main process as a fallback. Two scans:
    outliers and the TIN=NPI count share one pass (verified identical to the
    former three-query form on 11 real parts, 32M rows)."""
    src = f"read_parquet('{sql_path(str(part))}')"
    outliers, tin_npi = con.execute(
        f"""
        WITH med AS (SELECT billing_code, median(negotiated_rate) AS m FROM {src}
                     WHERE is_dollar_rate GROUP BY billing_code)
        SELECT count(*) FILTER (WHERE r.is_dollar_rate AND med.m > 0
                                AND (r.negotiated_rate > 5 * med.m
                                     OR r.negotiated_rate < 0.2 * med.m)),
               count(*) FILTER (WHERE r.tin_is_really_npi)
        FROM {src} r LEFT JOIN med USING (billing_code)
        """
    ).fetchone()
    distinct_facts = con.execute(
        f"""
        SELECT count(*) FROM (
            SELECT DISTINCT payer, tin_value, npi, billing_code,
                   array_to_string(billing_code_modifier, '|'),
                   negotiated_rate, billing_class
            FROM {src}
        )
        """
    ).fetchone()[0]
    return int(outliers or 0), int(tin_npi or 0), int(distinct_facts or 0)


# the worker's private QA connection: a slice of the machine, never the
# whole of it (the other workers are parsing meanwhile)
WORKER_QA_THREADS = 2
WORKER_QA_MEMORY = "1GB"


def _worker_qa_counts(part: Path) -> tuple[int, int, int] | None:
    """qa_aggregate_counts on a private in-memory DuckDB, inside the parse
    worker. Spills (a 590M-row DISTINCT does) to a pid-named folder beside the
    part — never the current directory — removed afterwards and swept at the
    next start if the worker dies. None on any failure: the parent then runs
    the same query on the main connection, so QA never fails a parse."""
    import duckdb

    spill = part.with_name(part.name.removesuffix(".parquet.tmp") + ".qa-spill")
    con = None
    try:
        con = duckdb.connect()
        con.execute(f"SET threads = {WORKER_QA_THREADS}")
        con.execute(f"SET memory_limit = '{WORKER_QA_MEMORY}'")
        con.execute("SET preserve_insertion_order = false")
        con.execute("SET enable_progress_bar = false")   # else it paints the console
        con.execute(f"SET temp_directory = '{sql_path(str(spill))}'")
        return qa_aggregate_counts(con, part)
    except Exception as e:  # noqa: BLE001 — cosmetic tier: the parent recomputes
        log.info("%s: worker QA skipped (%s) — the main process will compute it",
                 part.name, e)
        return None
    finally:
        if con is not None:
            con.close()
        shutil.rmtree(spill, ignore_errors=True)


def _apply_qa_aggregates(qa: dict, total: int, agg: tuple[int, int, int]) -> None:
    outliers, tin_npi, distinct_facts = agg
    prices = qa.get("prices") or 0
    qa.update({
        "outlier_rates": outliers,
        "outlier_rule": ">5x or <0.2x of the code's within-file median",
        # share of PRICES (both counters are per-price): dividing a per-price
        # numerator by the NPI-fanned row count understated it dramatically
        "non_dollar_share": round(qa["non_dollar_rows"] / prices, 4) if prices else 0.0,
        "duplicate_explosion_ratio": round(total / distinct_facts, 2) if distinct_facts else 1.0,
        "tin_is_really_npi_rows": tin_npi,
    })


class PoolRestarted(RuntimeError):
    """A QUEUED parse cancelled by another file's force_heal (a wedged worker
    was killed). Nothing of THIS file was read: the URL path re-queues it
    (the download and its sidecar are on disk) instead of leaving a 'failed'
    row the user must notice and retry by hand."""


class ParentGone(RuntimeError):
    """A parse worker found the app that started it gone and stopped. If the
    app IS alive (Windows refused the check), the file fails with this plain
    message and "retry" processes it again — nothing is silently lost."""


def _now() -> dt.datetime:
    # naive UTC — the store's one clock (store._utcnow explains why not aware)
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


# (size, mtime_ns) of each file as its ingest started, by path: _finish_ok
# compares, so a file replaced while it was being read is neither recorded
# as "loaded" nor moved (review 9 item 1.2 / F9)
_INGEST_STAT: dict[str, tuple[int, int]] = {}


def _note_ingest_start(path: Path) -> None:
    try:
        st = path.stat()
        _INGEST_STAT[str(path)] = (st.st_size, st.st_mtime_ns)
    except OSError:
        pass


def _download_identity(path: Path) -> dict | None:
    """sha + trailer_proves the downloader computed while it wrote `path`."""
    from .fetch import DOWNLOAD_IDENTITY
    ident = DOWNLOAD_IDENTITY.pop(str(path), None)
    if ident:
        return ident
    meta = Path(str(path) + ".fetchmeta")
    try:
        m = json.loads(meta.read_text())
        if m.get("sha"):
            return {"sha": m["sha"], "trailer_proves": m.get("trailer_proves")}
    except (OSError, ValueError):
        pass
    return None


def _finish_ok(cfg: MrfxConfig, store: Store, path: Path) -> None:
    """A file ingested successfully: remember its fingerprint (so a copy of
    it under another web address is recognised before it finishes
    downloading — fetch.early_duplicate_check) and its IDENTITY (size + the
    mtime it had when read, full sha256 when known, whether its tail vouches
    for its whole content), then the usual housekeeping. A file that changed
    while it was being read is left where it is and recorded as nothing:
    the rows came from the bytes that were read, and the next scan offers
    the new bytes as what they are. Cosmetic tier: an identity that can't be
    recorded costs only an early skip / a same-name check, never the
    ingest."""
    from .identity import LOCAL_HASH_MAX_BYTES, hash_file
    started = _INGEST_STAT.pop(str(path), None)
    try:
        st = path.stat()
        if started is not None and (st.st_size, st.st_mtime_ns) != started:
            log.warning("%s changed while it was being read — its rows are from the "
                        "version that was read; the new version stays where it is and "
                        "is offered at the next scan", path.name)
            return
        from .fetch import file_fingerprint, trailer_proves_for
        fp = file_fingerprint(path, cfg.early_dedup_mb)
        if fp is not None:
            store.record_fingerprint(path.name, fp)
        ident = _download_identity(path)
        if ident is None and st.st_size <= LOCAL_HASH_MAX_BYTES:
            sha, gz = hash_file(path)
            ident = {"sha": sha, "trailer_proves": trailer_proves_for(path, gz)}
        store.record_identity(path.name, st.st_size, st.st_mtime_ns,
                              (ident or {}).get("sha"), (ident or {}).get("trailer_proves"))
    except Exception:  # noqa: BLE001
        log.debug("fingerprint not recorded for %s", path.name, exc_info=True)
    _finish_file(cfg, path, ok=True)


def _finish_file(cfg: MrfxConfig, path: Path, ok: bool) -> None:
    """Move a fully-processed file out of the inbox (config-controlled).

    This is HOUSEKEEPING: the files-table status and the parquet rows are the
    real record, so a move failure must never propagate. On Windows the source
    file is routinely locked for a moment by antivirus or the search indexer
    just after we write it — letting that OSError escape here would flip an
    already-successful ingest to 'failed' in the caller's except block, scaring
    a non-technical user with a failure on data that actually ingested fine.
    Log it and leave the file in place (a re-scan sees status='done' and skips
    it)."""
    if not path.exists():
        return
    try:
        if not ok:
            cfg.failed_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), cfg.failed_dir / path.name)
        elif cfg.move_processed and cfg.inbox_dir in path.parents:
            cfg.processed_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), cfg.processed_dir / path.name)
    except OSError as e:
        log.warning("could not move %s out of the inbox (%s) — the file's data is "
                    "safe and its status is unchanged; leaving it in place", path.name, e)


# ---------------------------------------------------------------------------
# Parallel parse workers (parallel_ingests > 1). A worker is a separate OS
# process that does PURE computation: read the file, write rows to its own
# temp parquet, report progress through a sidecar JSON file. It never touches
# DuckDB — the main process owns every database write, which is what makes
# running several workers safe.
# ---------------------------------------------------------------------------

_PARSE_POOL = None


class ParsePoolManager:
    """Self-healing wrapper around a ProcessPoolExecutor. If a worker process
    dies (OOM killer, crash, someone kills it), the executor is permanently
    'broken' — every later submit raises. Rebuild it and let the caller retry
    the one file that was in flight; the queue never gets poisoned."""

    def __init__(self, max_workers: int):
        import threading

        self._max_workers = max_workers
        self._lock = threading.Lock()
        self._pool = self._make()

    def _make(self):
        import concurrent.futures
        import multiprocessing

        return concurrent.futures.ProcessPoolExecutor(
            max_workers=self._max_workers,
            mp_context=multiprocessing.get_context("spawn"))

    def submit(self, fn, *args):
        with self._lock:
            return self._pool.submit(fn, *args)

    def heal(self) -> None:
        """Replace a broken executor (no-op if another thread already did)."""

        with self._lock:
            broken = getattr(self._pool, "_broken", False)
            if broken:
                log.warning("a parser worker process died — restarting the worker pool")
                try:
                    self._pool.shutdown(wait=False, cancel_futures=True)
                except Exception:  # noqa: BLE001
                    pass
                self._pool = self._make()

    def force_heal(self) -> None:
        """Terminate ALL worker processes and rebuild the pool — the escape
        hatch for a worker that is alive but WEDGED (a pathological file spun
        it into a byte-frozen CPU loop; heal() only replaces an already-dead
        pool). Collateral: other in-flight parses on this pool die too, but
        they re-queue and resume — far better than a processor thread blocked
        forever on one hung worker. Rare (guarded by a long both-frozen
        deadline)."""
        with self._lock:
            for proc in list(getattr(self._pool, "_processes", {}).values()):
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001 — already gone
                    pass
            try:
                self._pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass
            self._pool = self._make()

    def shutdown(self) -> None:
        with self._lock:
            self._pool.shutdown(wait=True, cancel_futures=True)

    def shutdown_now(self) -> None:
        """Hard stop for process exit: cancel_futures + terminate workers.
        Plain shutdown (and concurrent.futures' atexit hook) WAITS for
        running futures — a SIGTERM'd server would block for the rest of a
        multi-hour parse before exiting."""
        with self._lock:
            pool = self._pool
            if pool is None:
                return
            # Grab the worker handles BEFORE shutdown(). Two bugs lived here:
            # ProcessPoolExecutor sets _processes to None as it tears down, and
            # `getattr(pool, "_processes", {})` returns that None (the attribute
            # EXISTS, so the {} default never applies) — `.values()` then raised
            # AttributeError and printed a traceback over the user's Ctrl-C. And
            # reading it after shutdown() meant the list was usually already
            # empty, so the terminate loop — the whole point of a hard stop —
            # silently did nothing.
            procs = list((getattr(pool, "_processes", None) or {}).values())
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001 — exiting must not need a clean stop
                pass
            for proc in procs:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001 — already gone
                    pass


def set_parse_pool(pool) -> None:
    """Install/remove the shared parse pool (run_queue owns its lifetime)."""
    global _PARSE_POOL
    _PARSE_POOL = pool


def get_parse_pool():
    return _PARSE_POOL


class ParseStopped(Exception):
    """The user pressed "stop" on a file mid-parse. Not a failure: the row
    says so in plain words and the download is kept for a retry."""

    def __init__(self, msg: str = "stopped by you"):
        super().__init__(msg)


# files-table / url-queue wording for a stopped parse
STOPPED_MSG = ("stopped by you before it finished — nothing from this run was "
               "saved (an earlier version of the file, if any, is unchanged)")


def stop_sentinel_for(progress_path: str) -> Path:
    """The file the parent creates to tell a parse WORKER to stop (workers
    have no access to the store's in-memory flags)."""
    return Path(progress_path + ".stop")


def _stop_aware(store: Store, name: str, inner=None):
    """A read-progress callback that also honors a stop request for `name`
    (in-process parse): open_stream calls it on every ~1 MB read, so a stop
    lands within one read wherever the parse is."""
    def cb(n):
        if store.is_parse_stopped(name):
            raise ParseStopped()
        if inner is not None:
            inner(n)
    return cb


def _cleanup_unlink(p) -> None:
    """Best-effort temp-file delete for cleanup paths: retries a transient
    Windows lock, and never raises (a leftover is swept at the next start)."""
    try:
        unlink_with_retry(Path(p))
    except OSError:
        pass


def spool_path_for(part_tmp) -> Path:
    """The big-file spool sits next to its part's .parquet.tmp with the SAME
    key and pid, so the store's orphan sweep removes it after a crash."""
    p = Path(part_tmp)
    return p.with_name(p.name.removesuffix(".parquet.tmp") + ".spool.tmp")


def _parse_worker(cfg: MrfxConfig, path_str: str, name: str, header_defaults: dict,
                  external_refs: dict, est: int, compressed_bytes: int,
                  tmp_out: str, progress_path: str) -> dict:
    """Runs in a child process. Returns a picklable summary; raises on failure
    (the parent marks the file failed)."""

    from .parser import batch_to_table
    from .store import PARQUET_COMPRESSION, RATES_SCHEMA, BackgroundParquetWriter

    path = Path(path_str)
    file_chunks = max(1, -(-max(compressed_bytes, 1) // CHUNK_COMPRESSED_BYTES))
    big = (est or 0) >= LARGE_FILE_UNCOMPRESSED_BYTES
    passes = 2 if big else 1
    chunks_total = file_chunks * passes
    state = {"pass": 0, "last": -1}
    parent_pid = os.getppid()
    from .store import _pid_alive  # platform-aware; see below

    def report(compressed_read: int) -> None:
        in_pass = min(file_chunks, compressed_read // CHUNK_COMPRESSED_BYTES)
        report_chunk(state["pass"] * file_chunks + in_pass)

    def report_percent(pct: float) -> None:
        report_chunk(int(chunks_total * min(100.0, pct) / 100.0), pct)

    stop_flag = stop_sentinel_for(progress_path)

    def report_chunk(chunk: int, pct: float | None = None) -> None:
        # the user pressed "stop" (the parent created the sentinel): checked
        # on EVERY report, ahead of the throttle — one stat per ~1 MB read
        if stop_flag.exists():
            raise ParseStopped()
        if pct is None:
            pct = min(100.0, 100.0 * chunk / chunks_total)
        # a chunk is 64 MB compressed — a 700 MB file has ~22 of them, so a
        # long replay phase would show no movement; percent moves count too
        if chunk == state["last"] and pct - state.get("last_pct", -1.0) < 0.5:
            return
        state["last"] = chunk
        state["last_pct"] = pct
        # the server that dispatched this parse is GONE (killed without pool
        # shutdown). Nobody will collect the result — seen live as orphans
        # burning 85% CPU on 12 GB files. Stop within one chunk; the
        # pid-suffixed temp is swept as a dead-pid orphan on the next store
        # start. POSIX signals death by re-parenting (getppid changes); on
        # Windows getppid keeps returning the DEAD parent's pid forever, so
        # probe the parent's liveness directly.
        died = (os.getppid() != parent_pid if os.name != "nt"
                else not _pid_alive(parent_pid))
        if died:
            # RuntimeError, not SystemExit: a SystemExit re-raised by
            # fut.result() in a LIVE parent (a false "parent gone") passed
            # every `except Exception` and silently killed a processor thread
            raise ParentGone("this file's worker lost contact with MRF Explorer and "
                             "stopped (press retry to process it again)")
        try:
            tmp = progress_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"chunks_done": chunk, "chunks_total": chunks_total,
                           "pct": round(min(100.0, pct), 2)}, f)
            os.replace(tmp, progress_path)
        except OSError:
            pass  # progress is cosmetic

    rows_written = 0
    writer = None

    def sink(rows: list[dict]) -> None:
        nonlocal rows_written, writer
        if not rows:
            return
        table = batch_to_table(rows, RATES_SCHEMA)
        if writer is None:
            writer = BackgroundParquetWriter(tmp_out, RATES_SCHEMA, PARQUET_COMPRESSION)
        writer.write_table(table)
        rows_written += len(rows)

    made: list = []

    def make_parser(keep_ref_ids=None) -> InNetworkParser:
        made.append(InNetworkParser(
            cfg, source_file=name, external_refs=external_refs,
            sink=sink, header_defaults=header_defaults, keep_ref_ids=keep_ref_ids,
        ))
        return made[-1]

    def refs_only() -> bool:
        return bool(made) and made[-1].reference_only

    try:
        result = None
        if big and fast_big_file_available(cfg):
            # ONE full read + a short read of the reference table (see
            # run_big_file_fast) instead of skim + extract's two full reads
            from .fastscan import FastScanUnsupported
            srp = SingleReadProgress(compressed_bytes, report_percent)
            try:
                result, target_items = run_big_file_fast(
                    cfg, make_parser(), lambda: open_stream(path, progress_cb=srp.feed),
                    spool_path_for(tmp_out), progress=srp)
            except FastScanUnsupported as e:
                log.info("%s: single read declined (%s) — two-pass", name, e)
                state["pass"] = 0
            else:
                if target_items == 0:
                    return {"short_circuit": True, "chunks_total": chunks_total,
                            "refs_only": refs_only()}
        if result is None:
            keep_ref_ids = None
            if big:
                shape: dict = {}
                keep_ref_ids, target_items = run_skim(
                    cfg, lambda: open_stream(path, progress_cb=report), shape)
                if target_items == 0:
                    # a reference table and no in_network: a provider list
                    # the sniff misread (review 9 item 3.1)
                    return {"short_circuit": True, "chunks_total": chunks_total,
                            "refs_only": bool(shape.get("refs") and not shape.get("in_network"))}
                state["pass"] = 1
            _, result = run_in_network_parse(
                cfg, lambda: make_parser(keep_ref_ids), lambda: open_stream(path, progress_cb=report))
    except ParentGone:
        # the parent is gone (report_chunk noticed): nobody will adopt these
        # files, and they carry the dead parent's pid — they'd sit (possibly
        # GBs) until the next start's sweep. Remove our own temps now.
        if writer is not None:
            try:
                writer.close()
            except BaseException:  # noqa: BLE001 — leaving anyway
                pass
            writer = None
        for leftover in (tmp_out, spool_path_for(tmp_out), progress_path,
                         progress_path + ".tmp", stop_sentinel_for(progress_path)):
            _cleanup_unlink(leftover)   # held open (AV/indexer) -> next start's sweep
        raise
    finally:
        if writer is not None:
            writer.close()
    # QA aggregates HERE, on this worker's own part, so the main process's
    # shared DuckDB (dashboard, rollups, every other file's adoption) isn't
    # scanned once per file. Stop requests are honored up to this point only.
    qa_agg = (_worker_qa_counts(Path(tmp_out))
              if rows_written and Path(tmp_out).exists() else (0, 0, 0))
    return {
        "short_circuit": False,
        "refs_only": refs_only() and not rows_written,
        "chunks_total": chunks_total,
        "qa_agg": qa_agg,
        "payer": result.payer,
        "schema_version": result.schema_version,
        "last_updated_on": result.last_updated_on,
        "file_month": result.file_month,
        "published_month": published_month_of(name, result.ingest_month or result.file_month),
        "ref_groups_skipped": result.ref_groups_skipped,
        "qa": result.qa.to_dict(),
        "rows": result.qa.rows,
        "rows_written": rows_written,
    }


def _ingest_in_network_pooled(cfg: MrfxConfig, store: Store, path: Path, pf: Preflight,
                              pool, progress_bar, rebuild_rollups: bool) -> dict:
    """Dispatch the CPU-heavy parse to a worker process; this (main-process)
    thread does all store writes: progress mirroring, part adoption, upserts."""
    import concurrent.futures

    name = path.name
    external_refs = _companion_refs(store, pf, path.name)
    header_defaults = {
        "payer": pf.payer, "schema_version": pf.schema_version,
        "last_updated_on": pf.last_updated_on,
    }
    # must not match *.parquet; pid keeps it private to this process (a second
    # process ingesting the same file must never unlink our in-progress write)
    tmp_out = store.rates_dir / f".{file_key(name)}.{os.getpid()}.parquet.tmp"
    # progress sidecar lives under the STORE, not beside the source: writing
    # it next to an inbox file fired a watch event every 64 MB chunk, and a
    # crash left an orphan the next scan quarantined as a ghost "file"
    progress_path = str(store.rates_dir / f".{file_key(name)}.{os.getpid()}.progress")

    def dispatch_and_wait() -> dict:
        tmp_out.unlink(missing_ok=True)
        fut = pool.submit(
            _parse_worker, cfg, str(path), name, header_defaults, external_refs,
            pf.est_uncompressed_bytes or 0, pf.compressed_bytes, str(tmp_out), progress_path,
        )
        last = -1
        bar = progress_bar  # LOCAL copy — never rebind the closed-over param
        # (assigning `progress_bar = None` here would make it local to this
        # function and UnboundLocalError the `if` read one line up)
        # Stall watchdog: this loop reads the worker's progress FILE, so it
        # sees advancement even while the dashboard mirror (update_progress)
        # is skip-on-busy behind a long rollup's write lock. Hours of frozen
        # bars used to be indistinguishable from a hung worker — say which.
        stall_warn = 900.0  # 15 min between complaints
        # Hard deadline: a worker whose chunk counter AND output file have BOTH
        # been frozen this long is genuinely wedged (no healthy parse freezes
        # its output for an hour — it emits row batches continuously). Kill it
        # so the file fails+re-queues instead of blocking this processor thread
        # forever. Generous, so a legitimately slow-but-advancing parse is never
        # touched (any chunk tick or output growth resets the clock).
        stall_kill = 3600.0
        last_advance = time.monotonic()
        warned_at = 0.0
        last_out_mb = -1.0   # output size at the previous complaint
        kill_ref_mb = -1.0   # output size when the freeze began (for the kill test)
        kill_ref_at = time.monotonic()
        stacks_dumped = False  # one auto-dump per stall episode, not per warn

        def _relay_stop() -> None:
            # the worker can't see the store's flags: hand a "stop" over as a
            # sentinel file it checks on its next read
            if store.is_parse_stopped(name):
                try:
                    stop_sentinel_for(progress_path).touch()
                except OSError:
                    pass

        _relay_stop()   # a stop pressed before dispatch reaches the first read
        while True:
            try:
                return fut.result(timeout=2.0)
            except concurrent.futures.TimeoutError:
                _relay_stop()
                try:
                    prog = json.loads(Path(progress_path).read_text())
                except (OSError, ValueError):
                    prog = None
                now = time.monotonic()
                if prog and (prog.get("chunks_done", -1), prog.get("pct")) != last:
                    last = (prog.get("chunks_done", -1), prog.get("pct"))
                    last_advance = now
                    last_out_mb = -1.0
                    kill_ref_mb = -1.0  # progress resets the hard-kill clock
                    kill_ref_at = now
                    stacks_dumped = False
                    store.update_progress(name, prog["pct"], chunks_done=prog["chunks_done"],
                                          chunks_total=prog["chunks_total"])
                    if bar:
                        try:
                            bar(prog["chunks_done"], prog["chunks_total"], prog["pct"])
                        except Exception:  # noqa: BLE001 — a display bar must
                            # never fail the multi-hour parse it decorates
                            bar = None
                elif (now - last_advance > stall_warn
                        and now - warned_at > stall_warn):
                    warned_at = now
                    # ground truth beats the chunk counter: if the worker's
                    # output parquet is GROWING, rows are being extracted no
                    # matter what any frozen counter says
                    try:
                        out_mb = tmp_out.stat().st_size / 1e6
                    except OSError:
                        out_mb = 0.0
                    log.warning(
                        "%s: no chunk advance for %.0f minutes (chunk %s); "
                        "extracted output so far: %.0f MB. If that MB number "
                        "GROWS between these messages, extraction is working "
                        "and only the counter is quiet (huge single section). "
                        "If it stays frozen too for hours, Ctrl-C and restart "
                        "— the file re-queues and everything else resumes.",
                        name, (now - last_advance) / 60,
                        _chunk_label(last), out_mb)
                    # counter AND output both frozen across two consecutive
                    # complaints = a real stall. Put the diagnosis in the
                    # console right here — when a stall wedges the whole
                    # server, the /api/debug/stacks page may not answer, and
                    # this log is the one channel that always works.
                    if last_out_mb >= 0 and out_mb <= last_out_mb and not stacks_dumped:
                        stacks_dumped = True
                        log.warning(
                            "%s: output hasn't grown either — dumping every "
                            "thread's stack below (once per stall). Paste it "
                            "into a bug report; it names the exact line each "
                            "part of the program is waiting on.\n%s",
                            name, thread_stacks_text())
                    last_out_mb = out_mb
                    # hard-kill test: track when the OUTPUT last grew; if both
                    # the chunk counter and the output have been frozen for
                    # stall_kill, the worker is wedged (not merely slow) — kill
                    # the pool so this file fails and re-queues instead of
                    # blocking a processor thread forever.
                    if kill_ref_mb < 0 or out_mb > kill_ref_mb + 0.5:
                        kill_ref_mb = out_mb   # output advanced -> reset the clock
                        kill_ref_at = now
                    elif now - kill_ref_at > stall_kill:
                        log.error(
                            "%s: no progress AND no output growth for %.0f "
                            "minutes — the parser worker is wedged. Terminating "
                            "it; THIS file is marked failed (press retry in the "
                            "Files tab), other in-flight files re-queue and "
                            "resume automatically.",
                            name, (now - kill_ref_at) / 60)
                        if hasattr(pool, "force_heal"):
                            pool.force_heal()
                        raise RuntimeError(
                            f"parser worker wedged (no progress for "
                            f"{stall_kill/60:.0f} min) — worker killed; press "
                            "retry to try this file again")

    try:
        try:
            payload = dispatch_and_wait()
        except concurrent.futures.process.BrokenProcessPool:
            # a worker died mid-parse (crash / OOM kill). Heal the pool and
            # retry this file once — the download is still on disk.
            log.warning("%s: parser worker died mid-parse; restarting pool and retrying once", name)
            if hasattr(pool, "heal"):
                pool.heal()
            payload = dispatch_and_wait()
        except concurrent.futures.CancelledError as e:
            # a QUEUED future cancelled by another thread's force_heal.
            # CancelledError is a BaseException since 3.8 — left unconverted it
            # sails past every `except Exception` fault-isolation layer and
            # silently kills the processor/watcher thread that hit it
            # (verification audit). Convert so the normal failed+retry flow runs.
            raise PoolRestarted(
                "parse was cancelled by a worker-pool restart before it began — "
                "it is re-queued automatically") from e
    except Exception:
        _cleanup_unlink(tmp_out)
        raise
    finally:
        # Every delete here is retry-tolerant and never raises: on Windows an
        # antivirus/indexer briefly holding one of these files open made the
        # unlink raise FROM THIS finally, replacing a successful parse's
        # result (or the real error) with a PermissionError — a finished file
        # marked failed over a temp file. A file still held is swept at the
        # next start.
        _cleanup_unlink(progress_path)
        _cleanup_unlink(progress_path + ".tmp")
        _cleanup_unlink(stop_sentinel_for(progress_path))
        # the worker deletes its own spool, but a worker that DIES (OOM kill,
        # crash, force_heal) can't — and the spool carries THIS (live)
        # process's pid, so the orphan sweep would keep it until restart.
        # It can be GBs; the parent always removes it.
        _cleanup_unlink(spool_path_for(tmp_out))
        # same for the worker's QA spill folder (a worker killed mid-QA leaves
        # GBs that the sweep keeps while THIS process lives)
        _qa_spill = Path(str(tmp_out).removesuffix(".parquet.tmp") + ".qa-spill")
        if _qa_spill.exists():
            shutil.rmtree(_qa_spill, ignore_errors=True)

    store.update_progress(name, 100.0, chunks_done=payload["chunks_total"],
                          chunks_total=payload["chunks_total"])
    if payload.get("refs_only"):
        _cleanup_unlink(tmp_out)
        return _ingest_reference_file(cfg, store, path, pf)
    if payload["short_circuit"]:
        msg = ("scanned: none of the target billing codes appear in this "
               "file — extraction pass skipped")
        # a RE-ingest under the same filename must not keep serving the OLD
        # version's rates: drop any existing part so the 0-row record and the
        # store agree (the normal paths do this via finalize_rates_part)
        store.drop_rates_part(name)
        store.upsert_file(name, payer=pf.payer, status="done", rows_emitted=0,
                          qa={"rows": 0, "messages": [msg]}, finished_at=FINISHED_NOW)
        _finish_ok(cfg, store, path)
        log.info("%s: %s", name, msg)
        return {"status": "done", "rows": 0, "payer": pf.payer, "note": msg}

    store.finalize_rates_part(name, tmp_out, payload["rows_written"])
    _commit_done(
        store, name,
        payer=payload["payer"],
        schema_version=payload["schema_version"],
        last_updated_on=payload["last_updated_on"],
        status="done",
        rows_emitted=payload["rows"],
        file_month=payload.get("file_month"),
        published_month=payload.get("published_month"),
        ref_groups_skipped=payload["ref_groups_skipped"],
        qa=qa_report(store, payload["qa"], name, agg=payload.get("qa_agg")),
        finished_at=FINISHED_NOW,
    )
    # rollup AFTER the done-upsert: the incremental update finds its work by
    # "done files newer than the coverage marker" — called before the upsert
    # it would recompute the PREVIOUS file's slice and miss this one entirely
    # (a crash between the two leaves the marker behind; serve-start catches up)
    if rebuild_rollups:
        _rebuild_rollups_best_effort(store, name)
    _finish_ok(cfg, store, path)
    qa = payload.get("qa") or {}
    if payload["ref_groups_skipped"]:
        log.warning("%s: %d rate groups skipped — %s", name, payload["ref_groups_skipped"],
                    skipped_groups_reason(payload["ref_groups_skipped"],
                                          qa.get("remote_ref_groups", 0),
                                          qa.get("ambiguous_ref_ids", 0)))
    return {"status": "done", "rows": payload["rows"],
            "ref_groups_skipped": payload["ref_groups_skipped"],
            "remote_ref_groups": qa.get("remote_ref_groups", 0),
            "ambiguous_ref_ids": qa.get("ambiguous_ref_ids", 0)}


def _commit_done(store: Store, name: str, **fields) -> None:
    """The 'done' upsert that follows a part going live. If it fails, the
    handler marked the row failed while its rates stayed live in every
    number — and the source went to failed/, so nothing ever healed it
    (round 7 P1.8). Retry once (a transient lock), then take the part back
    out before the error propagates: a row that is not 'done' never owns a
    live part it just wrote."""
    try:
        store.upsert_file(name, **fields)
        return
    except Exception:  # noqa: BLE001 — one retry, then undo
        log.warning("%s: recording the finished file failed once — retrying", name,
                    exc_info=True)
    try:
        store.upsert_file(name, **fields)
    except Exception:
        try:
            store.drop_rates_part(name)
        except Exception:  # noqa: BLE001 — the original error is the one to report
            log.exception("%s: could not withdraw the part after a failed commit", name)
        raise


# One ingest per filename at a time, process-wide. Guards the shared
# pid-suffixed temp path AND gives forget a way to exclude live parses.
_CLAIMS_LOCK = threading.Lock()
_ACTIVE_INGESTS: set[str] = set()


def _claim_ingest(name: str) -> bool:
    with _CLAIMS_LOCK:
        if name in _ACTIVE_INGESTS:
            return False
        _ACTIVE_INGESTS.add(name)
        return True


def _release_ingest(name: str) -> None:
    with _CLAIMS_LOCK:
        _ACTIVE_INGESTS.discard(name)


def ingest_in_progress(name: str) -> bool:
    with _CLAIMS_LOCK:
        return name in _ACTIVE_INGESTS


def forget_file(cfg: MrfxConfig, store: Store, filename: str) -> dict:
    """Erase one ingested file on the user's request: its rates, provider
    references, files-table row, AND every raw copy on disk (inbox — else the
    watcher would immediately re-ingest it — processed, failed, downloads).
    Rollups rebuild afterward so dashboards stop showing the removed rows.
    Returns {rows, bytes} freed (bytes includes raw copies)."""
    if Path(filename).name != filename:
        # filenames come from the API path / CLI args — never let "../x"
        # reach the unlink calls below
        raise ValueError("not a plain filename")
    if not _claim_ingest(filename):
        # a live parse holds the claim: erasing under it would let the parse
        # quietly resurrect every record after we report "forgotten"
        raise RuntimeError("this file is being processed right now — "
                           "wait for it to finish, then remove it")
    try:
        return _forget_file_locked(cfg, store, filename)
    finally:
        _release_ingest(filename)


def _forget_file_locked(cfg: MrfxConfig, store: Store, filename: str) -> dict:
    # raw copies go FIRST — deleting the inbox copy after the DB rows would
    # leave a window where the watcher re-ingests the very file being erased
    freed_raw = 0
    for d in (cfg.inbox_dir, cfg.processed_dir, cfg.failed_dir, cfg.downloads_dir,
              cfg.inbox_dir / HELD_DIR, cfg.inbox_dir / REPLACING_DIR):
        p = d / filename
        try:
            if p.exists():
                freed_raw += p.stat().st_size
                p.unlink()
            Path(str(p) + ".fetchmeta").unlink(missing_ok=True)
        except OSError as e:
            log.warning("forget %s: could not delete %s: %s", filename, p, e)
    info = store.forget_file(filename)
    # forget_file queued the removed payers' slices (rollup_pending_payers),
    # so the incremental path recomputes exactly those — minutes, not the
    # hour-long full rebuild; if THIS fails, the queued note keeps
    # rollups_stale() True and serve-start catches up
    _rebuild_rollups_best_effort(store, filename)
    info["bytes"] += freed_raw
    log.info("forgot %s: %d rows and %.1f MB removed", filename, info["rows"], info["bytes"] / 1e6)
    return info


def _rebuild_rollups_best_effort(store: Store, name: str,
                                 incremental: bool = True) -> None:
    """The parquet part is already durable when this runs: an analytics
    rebuild failing (disk pressure, memory) must NOT fail the file — hours of
    parsing would be retried for data that already landed. Rollups refresh on
    the next successful rebuild, same contract as the queue's batched path.

    Removals are found via the rollup_pending_payers note the removal itself
    queues (forget_file / drop_rates_part / a replaced part), so the
    incremental path handles them too. incremental=False remains for callers
    that explicitly want the full rebuild.

    COALESCED (skip_if_busy): this is best-effort, so if a rebuild is already
    running we skip rather than BLOCK behind it — a blocking wait here was the
    last remaining pile-up source (the inbox watcher / a direct ingest queuing
    behind the queue worker's multi-hour rebuild, seen as "waiting 57 min").
    The rollup refreshes on the next cycle. ROLLUP_SKIPPED is a normal return,
    so the incremental's `return` already avoids the full-rebuild fallback."""
    from .store import ROLLUP_SKIPPED

    def _skip_kw(fn):
        try:
            import inspect
            return {"skip_if_busy": True} if "skip_if_busy" in inspect.signature(fn).parameters else {}
        except (TypeError, ValueError):  # C-callable / old test stub
            return {}
    try:
        if incremental:
            try:
                if store.update_rollups_incremental(
                        **_skip_kw(store.update_rollups_incremental)) == ROLLUP_SKIPPED:
                    log.info("%s: a rebuild is already running — skipping this "
                             "best-effort refresh (the running one covers it)", name)
                return
            except Exception as e:  # noqa: BLE001 — strict-precondition
                # optimization; the full rebuild is the always-correct fallback
                log.info("%s: incremental analytics update unavailable (%s) — "
                         "running a full rebuild", name, e)
        store.rebuild_rollups(**_skip_kw(store.rebuild_rollups))
    except Exception:  # noqa: BLE001
        log.exception(
            "%s: rollup rebuild failed — the file's rows are safely stored; "
            "analytics will refresh on the next successful rebuild", name)


def ingest_file(cfg: MrfxConfig, store: Store, path: Path, pf: Preflight | None = None,
                progress_bar=None, rebuild_rollups: bool = True) -> dict:
    """Ingest one file. Returns the final files-table record fields.

    progress_bar(chunks_done, chunks_total, pct) is called for large files so a
    CLI can render a bar; the dashboard reads progress from the files table.

    rebuild_rollups=False defers the (full, O(all-rows)) analytics rebuild —
    the URL-queue worker batches it across many files instead of paying
    minutes per file when grinding a large payer book. The raw `rates` view
    is always current; only the dedup/by-TIN rollups lag until the batch
    rebuild."""
    name = path.name
    if not _claim_ingest(name):
        # two threads on one filename share the same pid-suffixed temp: the
        # slower writer's half-written parquet would be renamed into the live
        # part. Watcher + upload background scan + confirm double-click +
        # requeue_skipped can all collide here — one wins, the rest bow out.
        log.info("%s: already being ingested by another worker — skipping", name)
        return {"status": "skipped", "error": "already being ingested by another worker"}
    try:
        return _ingest_file_locked(cfg, store, path, pf, progress_bar, rebuild_rollups)
    finally:
        _release_ingest(name)


def _ingest_file_locked(cfg: MrfxConfig, store: Store, path: Path, pf: Preflight | None,
                        progress_bar, rebuild_rollups: bool) -> dict:
    name = path.name
    _note_ingest_start(path)
    try:
        # preflight + the initial 'processing' upsert live INSIDE the try so a
        # corrupt file (e.g. a truncated .gz that makes preflight raise) on the
        # one-shot CLI / requeue path becomes a 'failed' row with a plain
        # message, not a raw traceback — honoring the "never raises" contract.
        if pf is None:
            pf = preflight(path, cfg, store)

        store.upsert_file(
            name,
            payer=pf.payer,
            file_type=pf.file_type,
            status="processing",
            schema_version=pf.schema_version,
            last_updated_on=pf.last_updated_on,
            size_bytes=pf.compressed_bytes,
            preflight=pf.to_dict(),
            started_at=_now(),
            error=None,
        )

        if pf.file_type == "toc":
            msg = "index/TOC file — not a rate file; drop the in-network files it references"
            store.upsert_file(name, status="quarantined", error=msg, finished_at=_now())
            _finish_file(cfg, path, ok=False)
            return {"status": "quarantined", "error": msg}

        if pf.file_type == "allowed_amounts":
            msg = ("out-of-network allowed-amounts file (billed/allowed averages) — "
                   "contains no negotiated rates, skipped")
            store.upsert_file(name, status="quarantined", error=msg, finished_at=_now())
            _finish_file(cfg, path, ok=False)
            return {"status": "quarantined", "error": msg}

        if pf.file_type == "unknown":
            msg = "; ".join(pf.messages) or "unrecognized file"
            store.upsert_file(name, status="quarantined", error=msg, finished_at=_now())
            _finish_file(cfg, path, ok=False)
            return {"status": "quarantined", "error": msg}

        if pf.file_type == "provider_reference":
            return _ingest_reference_file(cfg, store, path, pf)

        # in-network rate file. When a parse pool is installed (queue worker
        # with parallel_ingests > 1) the CPU-heavy parse runs in a child
        # process; this thread keeps every store write. Same code path
        # otherwise (inbox watcher, CLI, tests): stream rows straight into
        # the parquet part in batches (constant memory, §8.1). The parser is
        # seeded with the header preflight already sniffed, so batched rows
        # carry the right payer/month even though the parser flushes early.
        pool = get_parse_pool()
        if pool is not None:
            return _ingest_in_network_pooled(cfg, store, path, pf, pool,
                                             progress_bar, rebuild_rollups)
        external_refs = _companion_refs(store, pf, path.name)
        header_defaults = {
            "payer": pf.payer, "schema_version": pf.schema_version,
            "last_updated_on": pf.last_updated_on,
        }
        # Huge files (multi-GB with a millions-strong embedded provider_
        # references table) get a fast TWO-PASS ingest so the ref table never
        # lands in RAM: pass 1 skims which references the target codes cite;
        # pass 2 keeps only that subset in memory while extracting. Both passes
        # feed the chunk progress bar. Small files keep the single-pass path.
        est = pf.est_uncompressed_bytes or 0
        big = est >= LARGE_FILE_UNCOMPRESSED_BYTES
        keep_ref_ids = None
        result = None
        parser = None
        if big:
            # progress spans two passes: pass 1 fills 0-50%, pass 2 fills 50-100%
            progress = _ProgressTracker(store, name, pf.compressed_bytes,
                                        on_bar=progress_bar, passes=2)
            log.info("%s: large file (~%.1f GB uncompressed) — two-pass chunked ingest (%d chunks)",
                     name, est / 1e9, progress.chunks_total)
            store.upsert_file(name, chunks_total=progress.chunks_total, progress=0.0)
            progress.set_pass(0)
            if fast_big_file_available(cfg):
                # ONE full read + a short read of the reference table
                # (run_big_file_fast) instead of skim + extract's two full reads
                from .fastscan import FastScanUnsupported
                try:
                    srp = SingleReadProgress(pf.compressed_bytes, progress.set_percent)
                    with store.rates_part_writer(name) as writer:
                        big_parser = InNetworkParser(
                            cfg, source_file=name, external_refs=external_refs,
                            sink=writer.write_batch, header_defaults=header_defaults)
                        result, target_items = run_big_file_fast(
                            cfg, big_parser,
                            lambda: open_stream(path, progress_cb=_stop_aware(store, name, srp.feed)),
                            spool_path_for(writer.tmp), progress=srp)
                    if big_parser.reference_only and not result.qa.rows:
                        progress.finish()
                        return _ingest_reference_file(cfg, store, path, pf)
                except FastScanUnsupported as e:
                    log.info("%s: single read declined (%s) — two-pass", name, e)
                    result = None
                    progress.set_pass(0)
            if result is None:
                shape: dict = {}
                keep_ref_ids, target_items = run_skim(
                    cfg, lambda: open_stream(
                        path, progress_cb=_stop_aware(store, name, progress.update)), shape)
                if target_items == 0 and shape.get("refs") and not shape.get("in_network"):
                    progress.finish()
                    return _ingest_reference_file(cfg, store, path, pf)
                log.info("%s: pass 1 done — %d target-code items, %d target-cited references",
                         name, target_items, len(keep_ref_ids))
            if target_items == 0:
                # the scan IS the answer: none of the user's billing codes
                # appear anywhere in this file, so the extraction pass would
                # read gigabytes to emit zero rows — skip it and say so
                progress.finish()
                msg = ("scanned: none of the target billing codes appear in this "
                       "file — extraction pass skipped")
                # same-filename re-ingest: drop the old part so 0 rows recorded
                # means 0 rows served (mirrors the pooled short-circuit path)
                store.drop_rates_part(name)
                store.upsert_file(
                    name, payer=pf.payer, status="done", rows_emitted=0,
                    qa={"rows": 0, "messages": [msg]}, finished_at=FINISHED_NOW,
                )
                _finish_ok(cfg, store, path)
                log.info("%s: %s", name, msg)
                return {"status": "done", "rows": 0, "payer": pf.payer, "note": msg}
            if result is None:
                progress.set_pass(1)
        else:
            progress = None

        if result is None:
            with store.rates_part_writer(name) as writer:
                parser, result = run_in_network_parse(
                    cfg,
                    lambda: InNetworkParser(
                        cfg, source_file=name, external_refs=external_refs,
                        sink=writer.write_batch, header_defaults=header_defaults,
                        keep_ref_ids=keep_ref_ids,
                    ),
                    lambda: open_stream(path, progress_cb=_stop_aware(
                        store, name, progress.update if progress else None)),
                )
        if progress is not None:
            progress.finish()
        if parser is not None and parser.reference_only and not result.qa.rows:
            return _ingest_reference_file(cfg, store, path, pf)
        _commit_done(
            store, name,
            payer=result.payer,
            schema_version=result.schema_version,
            last_updated_on=result.last_updated_on,
            status="done",
            rows_emitted=result.qa.rows,
            file_month=result.file_month if result.qa.rows else None,
            published_month=published_month_of(name, result.ingest_month or result.file_month),
            ref_groups_skipped=result.ref_groups_skipped,
            qa=qa_report(store, result.qa.to_dict(), name),
            finished_at=FINISHED_NOW,
        )
        # rollup AFTER the done-upsert — same reason as the pooled path: the
        # incremental update keys on done files newer than the coverage marker
        if rebuild_rollups:
            _rebuild_rollups_best_effort(store, name)
        _finish_ok(cfg, store, path)
        if result.ref_groups_skipped:
            log.warning(
                "%s: %d rate groups skipped — %s", name, result.ref_groups_skipped,
                skipped_groups_reason(result.ref_groups_skipped, result.qa.remote_ref_groups,
                                      result.qa.ambiguous_ref_ids))
        return {
            "status": "done",
            "rows": result.qa.rows,
            "ref_groups_skipped": result.ref_groups_skipped,
            "remote_ref_groups": result.qa.remote_ref_groups,
            "ambiguous_ref_ids": result.qa.ambiguous_ref_ids,
        }

    except ParseStopped:
        store.requeue_part_payers(name)   # a stopped RE-ingest leaves the old part live
        in_inbox = cfg.inbox_dir in path.parents
        msg = STOPPED_MSG + (" — the file was moved to data/failed; drop it into the "
                             "inbox again to process it" if in_inbox else
                             " — the download is kept; press retry to process it")
        log.info("%s: %s", name, msg)
        store.upsert_file(name, status="stopped", error=msg, finished_at=_now())
        if in_inbox:
            # left in the inbox, the watcher would simply start it again
            _finish_file(cfg, path, ok=False)
        return {"status": "stopped", "error": msg}
    except PoolRestarted as e:
        # nothing of this file was read; its source stays where it is (inbox
        # file / downloaded copy) and it is picked up again automatically
        log.info("%s: %s", name, e)
        store.upsert_file(name, status="queued", error=str(e))
        return {"status": "requeued", "error": str(e)}
    except Exception as e:  # noqa: BLE001 — fault isolation is the contract here
        log.exception("ingest failed for %s", name)
        store.upsert_file(name, status="failed", error=plain_parse_error(e), finished_at=_now())
        store.requeue_part_payers(name)   # a failed RE-ingest leaves the old part live
        _finish_file(cfg, path, ok=False)  # never raises; the row is already 'failed'
        return {"status": "failed", "error": str(e)}
    finally:
        # ANY outcome ends a stop request: a stop pressed after the last read
        # (QA, finalize, rollup) was never honored by ParseStopped, stayed set
        # for the server's life, and the NEXT ingest of this filename died at
        # its first read as "stopped by you"
        store.clear_parse_stop(name)


def plain_parse_error(e: BaseException) -> str:
    """A failed file's message in words, with the technical cause after it.
    The one rule for malformed VALUES (audit 8 P3-1): the fast reader (the
    default) skips in_network members for codes it does not need without
    reading them, so a NaN or a 20-digit number THERE never fails a file; a
    malformed value in data that must be read (a target code's rates, the
    reference table, or anything when the classic reader is in use) fails
    the file with this sentence — never a silently wrong number."""
    text = f"{type(e).__name__}: {e}"
    low = str(e).lower()
    if "overflow" in low:
        why = ("the payer's file holds a number too large to be a real value "
               "(e.g. a 20-digit NPI or rate)")
    elif "lexical error" in low or "invalid char" in low or "nan" in low.split():
        why = ("the payer's file holds a value that is not valid JSON "
               "(e.g. NaN or Infinity)")
    else:
        return text
    return (f"{why} in data MRF Explorer had to read — the file is malformed; "
            f"ask the payer for a corrected copy ({text[:200]})")


def _companion_refs(store: Store, pf: Preflight, name: str) -> dict:
    """The payer's standalone provider lists of THIS rate file's vintage —
    its header date, else its publication month by the file_month rule
    (review 9 item 1.3)."""
    if not pf.payer:
        return {}
    from .parser import file_month_for
    now_month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    month = file_month_for(pf.last_updated_on, name, now_month)[0]
    return store.load_provider_refs(pf.payer, pf.last_updated_on, month)


def _ingest_reference_file(cfg: MrfxConfig, store: Store, path: Path, pf: Preflight) -> dict:
    """A standalone provider-reference file: stream its table into
    provider_refs in batches (flat memory for a multi-million-id table —
    the old path built the whole dict first), then re-ingest this payer's
    rate files that were waiting on it. Also reached AFTER an in-network
    parse that found a reference table and no in_network array (the 1 MB
    sniff could not see past a big table — audit 8 P1-2)."""
    name = path.name
    store.drop_rates_part(name)          # a 0-row in-network attempt left nothing to serve
    store.clear_provider_refs(name)
    state = {"n": 0, "payer": pf.payer or "Unknown payer"}

    def write(batch: dict) -> None:
        store.save_provider_refs(state["payer"], name, pf.last_updated_on, batch, append=True)
        state["n"] += len(batch)

    with open_stream(path) as stream:
        payer, last_updated, _ = parse_provider_reference_file(cfg, stream, on_batch=write)
    store.finish_provider_refs(name, payer, last_updated)
    store.upsert_file(
        name, payer=payer, status="done", last_updated_on=last_updated,
        file_type="provider_reference", rows_emitted=state["n"], finished_at=FINISHED_NOW,
    )
    _finish_ok(cfg, store, path)
    from .parser import file_month_for
    ref_month = file_month_for(last_updated, name,
                               dt.datetime.now(dt.timezone.utc).strftime("%Y-%m"))[0]
    requeued = requeue_skipped(cfg, store, payer, last_updated, ref_month)
    log.info("%s: provider reference file — %d reference ids stored", name, state["n"])
    return {"status": "done", "refs": state["n"], "requeued": requeued}


def requeue_skipped(cfg: MrfxConfig, store: Store, payer: str,
                    last_updated_on: str | None = None, month: str | None = None) -> list[str]:
    """After a reference file lands, load again this payer's in-network files
    OF ITS VINTAGE (item 1.3) that had skipped / missing / ambiguous ids:
    from the raw copy if it is still on disk (processed/, inbox/ or
    downloads/), else by downloading its link again — a linked rate file
    loaded before its provider list stayed at 0 rows for good, because
    downloads were looked for only in processed/ and inbox/ (review 9 item
    3.2). Files of other months are left alone: a new list never makes
    another month's files download again."""
    with store.connect() as con:
        rows = con.execute(
            """
            SELECT filename, last_updated_on, file_month FROM files
            WHERE payer = ? AND file_type = 'in_network'
              AND status = 'done'
              AND (ref_groups_skipped > 0
                   OR coalesce(TRY_CAST(json_extract_string(qa, '$.missing_ref_ids') AS BIGINT), 0) > 0
                   OR coalesce(TRY_CAST(json_extract_string(qa, '$.ambiguous_ref_ids') AS BIGINT), 0) > 0)
            ORDER BY filename
            """,
            [payer],
        ).fetchall()
    lu = str(last_updated_on or "").strip()[:10]
    requeued = []
    for fname, flu, fmonth in rows:
        if lu or month:
            same = (lu and str(flu or "").strip()[:10] == lu) or \
                   (month and str(fmonth or "")[:7] == str(month)[:7])
            if not same:
                continue
        for base in (cfg.processed_dir, cfg.inbox_dir, cfg.downloads_dir):
            src = base / fname
            if src.exists():
                log.info("re-ingesting %s now that %s reference data is present", fname, payer)
                ingest_file(cfg, store, src)
                requeued.append(fname)
                break
        else:
            if store.requeue_url_for_file(
                    fname, "downloading again: this payer's provider reference file "
                           "arrived after this file was loaded"):
                log.info("%s: its provider reference file arrived after it was loaded and "
                         "its download was removed — downloading it again", fname)
                requeued.append(fname)
    return requeued


_SHA_SEEN: dict = {}   # (path, size, mtime_ns) -> sha256, so a kept file is hashed once

HELD_DIR = ".held"          # inbox subfolders the scan never classifies:
REPLACING_DIR = ".replacing"  # a candidate waiting for the user, a chosen replacement
NEEDS_CHOICE = "needs_choice"


def _sha_of(path: Path) -> str | None:
    from .identity import LOCAL_HASH_MAX_BYTES, hash_file
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size > LOCAL_HASH_MAX_BYTES:
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _SHA_SEEN:
        if st.st_size > (256 << 20):
            log.info("%s: reading it to see whether it is the file already loaded "
                     "(%.1f GB)", path.name, st.st_size / 1e9)
        _SHA_SEEN[key] = hash_file(path)[0]
    return _SHA_SEEN[key]


def _month_of(payer_month: tuple) -> tuple:
    payer, month = payer_month
    return (str(payer or "").strip().casefold(), str(month or "")[:7])


def same_name_verdict(cfg: MrfxConfig, store: Store, path: Path, name: str,
                      prior: dict, pf: Preflight | None = None) -> tuple[str, str | None]:
    """A file arrives under `name`, which the store already holds (`prior`).
    Returns (verdict, sha256 or None):

    'same'    — these exact bytes are already loaded (under this name or any
                other): the size + mtime recorded when the file was read are
                unchanged (the very file left in place), or the full sha256
                equals a loaded file's. Never by size or samples alone: a
                corrected file of the same size is a different file, and the
                same file must always be recognised (review 9 item 1.2: a
                floored MiB count reloaded a 2.3 MiB file on every scan).
    'replace' — different bytes for the same payer and month (or for a file
                still being loaded): a corrected re-publication. The user
                chooses Replace or Keep both — two versions of one file are
                never blended into every number silently (review 9 4.5).
    'new'     — different bytes for another month / payer: loaded beside it.
    """
    try:
        st = path.stat()
    except OSError:
        return "same", None
    ident = store.loaded_identity(name)
    if ident and ident.get("mtime_ns") is not None and \
            (ident.get("size"), ident["mtime_ns"]) == (st.st_size, st.st_mtime_ns):
        return "same", ident.get("full_sha")
    sha = _sha_of(path)
    if sha and store.done_file_with_sha(sha):
        return "same", sha
    if not (ident and ident.get("full_sha")):
        # loaded by a build that recorded no sha: the loaded copy may still be
        # on disk (processed/, or in place) — compare with it
        for twin in (cfg.processed_dir / name, cfg.inbox_dir / name,
                     cfg.inbox_dir / REPLACING_DIR / name):
            try:
                if twin.exists() and twin.resolve() != path.resolve():
                    tsha = _sha_of(twin)
                    if sha and tsha:
                        if tsha == sha:
                            return "same", sha
                        break
            except OSError:
                continue
        else:
            # nothing to compare with: unchanged since that load finished
            # (same size, not modified after it) is that file
            fin = prior.get("finished_at")
            try:
                fin_ts = fin.replace(tzinfo=dt.timezone.utc).timestamp() if fin else None
            except AttributeError:
                fin_ts = None
            if fin_ts and prior.get("size_bytes") == st.st_size and \
                    st.st_mtime <= fin_ts and prior.get("status") == "done":
                return "same", sha
    if prior.get("status") in ("processing", "queued"):
        return "replace", sha
    if pf is None:
        try:
            pf = preflight(path, cfg, store)
        except Exception:  # noqa: BLE001 — an unreadable newcomer is loaded and fails there
            return "new", sha
    from .parser import file_month_for
    now_month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    mine = _month_of((pf.payer, file_month_for(pf.last_updated_on, name, now_month)[0]))
    theirs = _month_of((prior.get("payer"), prior.get("file_month") or prior.get("last_updated_on")))
    if not theirs[0] or not theirs[1] or mine == theirs:
        return "replace", sha
    return "new", sha


def unique_inbox_name(directory: Path, name: str, store: Store, tag: str) -> str:
    """`name` with `-<tag>` before its TRAILING known extensions (.json.gz
    stays .json.gz; a ".json" in the middle of a name is part of the name —
    "rates.json.2026-05.json" lost its date before), made unique against the
    directory and the store's files table."""
    known = (".json", ".gz", ".zip", ".csv", ".jsonl", ".bz2", ".xz", ".zst")
    sfx = ""
    rest = name
    while True:
        dot = rest.rfind(".")
        if dot <= 0 or rest[dot:].lower() not in known:
            break
        sfx = rest[dot:] + sfx
        rest = rest[:dot]
    i = 0
    while True:
        cand = f"{rest}-{tag}{'' if not i else f'-{i}'}{sfx}"
        if not (directory / cand).exists() and store.file_status(cand) is None:
            return cand
        i += 1


def hold_for_choice(cfg: MrfxConfig, store: Store, src: Path, orig: str,
                    sha: str | None, move: bool = True) -> str:
    """Park a different file that arrived under `orig` until the user picks
    Replace or Keep both (Files tab). It waits in inbox/.held/, which the
    scan never classifies, under a name tagged with its content."""
    import hashlib
    held = cfg.inbox_dir / HELD_DIR
    held.mkdir(parents=True, exist_ok=True)
    st = src.stat()
    tag = (sha or hashlib.sha256(f"{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest())[:8]
    new = unique_inbox_name(held, orig, store, tag)
    dest = held / new
    if move:
        src.replace(dest)
    prior = store.file_status(orig) or {}
    store.upsert_file(
        new, status=NEEDS_CHOICE, size_bytes=st.st_size, payer=prior.get("payer"),
        qa={"replaces": orig},
        error=(f"A different file with the same name as {orig} (the same payer and month). "
               f"Replace {orig} with it, or keep both — then both count toward every number."))
    log.info("%s: a different file with the same name as a loaded file — held as %s until "
             "you choose Replace or Keep both on the Files tab", orig, new)
    return new


def resolve_choice(cfg: MrfxConfig, store: Store, name: str, action: str) -> dict:
    """The user's answer for a held file: 'replace' re-loads `replaces`
    from it (the part is swapped atomically); 'keep' loads it under its own
    name beside the original."""
    st = store.file_status(name)
    if not st or st.get("status") != NEEDS_CHOICE:
        raise ValueError("this file is not waiting for a choice")
    held = cfg.inbox_dir / HELD_DIR / name
    if not held.exists():
        raise ValueError("the held file is no longer there — drop it in again")
    qa = st.get("qa")
    if isinstance(qa, str):
        try:
            qa = json.loads(qa)
        except ValueError:
            qa = {}
    orig = (qa or {}).get("replaces")
    if action == "replace":
        if not orig:
            raise ValueError("this file does not say which file it replaces")
        rdir = cfg.inbox_dir / REPLACING_DIR
        rdir.mkdir(parents=True, exist_ok=True)
        target = rdir / orig
        held.replace(target)
        store.drop_file_row(name)
        store.upsert_file(orig, status="queued", error=None)
        return {"action": "replace", "path": target, "filename": orig}
    if action == "keep":
        store.upsert_file(name, status="queued", error=None)
        return {"action": "keep", "path": held, "filename": name}
    raise ValueError("choose replace or keep")


def scan_inbox(cfg: MrfxConfig, store: Store, force: bool = False, progress_bar=None) -> list[dict]:
    """One-shot pass over the inbox (used by `mrfx ingest` and the watcher).

    Files above confirm_over_gb are parked as pending_confirmation unless
    force=True. Reference files ingest before in-network files so companions
    resolve in one pass.
    """
    results = []
    paths = []
    candidates = []
    for p in sorted(cfg.inbox_dir.glob("*")):
        if not p.is_file():
            continue
        # working files must never be treated as MRFs: progress sidecars,
        # half-renamed temps, and uploads still streaming in
        if p.name.endswith((".progress", ".tmp", ".part", ".uploading", ".fetchmeta")):
            continue
        try:
            candidates.append((p, p.stat().st_size))
        except OSError:
            continue
    if candidates:
        # a file still being copied/scp'd in GROWS between these two stats —
        # preflighting it mid-write used to quarantine the half-file and MOVE
        # it out from under the writer. Growing files get the next watch
        # event/scan once the writer finishes.
        time.sleep(0.3)
        for p, size0 in candidates:
            try:
                if p.stat().st_size == size0:
                    paths.append(p)
            except OSError:
                continue
    flights = []
    # a replacement the user chose that a restart interrupted: load it now
    for p in sorted((cfg.inbox_dir / REPLACING_DIR).glob("*")) \
            if (cfg.inbox_dir / REPLACING_DIR).is_dir() else []:
        if p.is_file() and (store.file_status(p.name) or {}).get("status") != "processing":
            try:
                flights.append((p, preflight(p, cfg, store)))
            except Exception as e:  # noqa: BLE001
                log.warning("%s: could not read the chosen replacement (%s)", p.name, e)
    for p in paths:
        prior = store.file_status(p.name)
        if prior and prior.get("status") == NEEDS_CHOICE:
            continue
        if prior and prior.get("status") == "processing" and not force:
            continue          # being loaded right now: this IS that file
        if prior and prior.get("status") == "done" and not force:
            # a file under a name the store already holds (review 9 1.2 / 4.5)
            verdict, sha = same_name_verdict(cfg, store, p, p.name, prior)
            if verdict == "same":
                if cfg.move_processed:
                    _finish_file(cfg, p, ok=True)
                continue
            try:
                if verdict == "replace":
                    hold_for_choice(cfg, store, p, p.name, sha)
                    results.append({"file": p.name, "status": NEEDS_CHOICE})
                    continue
                import hashlib
                st = p.stat()
                tag = (sha or hashlib.sha256(f"{st.st_size}:{st.st_mtime_ns}".encode()
                                             ).hexdigest())[:8]
                new = unique_inbox_name(p.parent, p.name, store, tag)
                p = p.rename(p.with_name(new))
            except OSError as e:
                log.warning("%s: a different file with this name was dropped in, but it "
                            "could not be set aside (%s) — it was NOT loaded", p.name, e)
                continue
            log.info("%s: a different file (another month or payer) under an already-loaded "
                     "name — loading it as %s", prior["filename"], p.name)
            prior = None
        try:
            flights.append((p, preflight(p, cfg, store)))
        except Exception as e:  # noqa: BLE001 — one unreadable neighbor (e.g.
            # a corrupt .gz raising zlib.error) must not abort the whole scan
            # forever; quarantine it and keep going
            log.exception("%s: preflight crashed — quarantining", p.name)
            store.upsert_file(p.name, status="quarantined",
                              error=f"could not read file: {e}", finished_at=_now())
            _finish_file(cfg, p, ok=False)
            results.append({"file": p.name, "status": "quarantined", "error": str(e)})

    order = {"provider_reference": 0, "in_network": 1}
    flights.sort(key=lambda t: order.get(t[1].file_type, 2))

    ingested = False
    for p, pf in flights:
        gb = pf.compressed_bytes / 1e9
        if not force and gb > cfg.confirm_over_gb:
            store.upsert_file(
                p.name, payer=pf.payer, file_type=pf.file_type, status="pending_confirmation",
                size_bytes=pf.compressed_bytes, preflight=pf.to_dict(),
                error=f"{gb:.1f} GB exceeds confirm_over_gb={cfg.confirm_over_gb}; confirm in Files view or run mrfx ingest --force",
            )
            results.append({"file": p.name, "status": "pending_confirmation"})
            continue
        r = ingest_file(cfg, store, p, pf, progress_bar=progress_bar,
                        rebuild_rollups=False)
        results.append({"file": p.name, **r})
        ingested = ingested or r.get("status") == "done"
    if ingested:
        # ONE rebuild for the whole pass: the per-file default turned an
        # N-file inbox drop into N full scans of the entire store (an hour+
        # each on a big book). If this rebuild fails or is skipped, the
        # covered-through marker stays behind and serve-start catches up.
        _rebuild_rollups_best_effort(store, "inbox scan")
    return results
