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
    ParseResult,
    parse_provider_reference_file,
    skim_needed_ref_ids,
)
from .sniff import Preflight, open_stream, preflight
from .store import FINISHED_NOW, Store, file_key, sql_path

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


def qa_report(store: Store, qa_counters: dict, source_file: str) -> dict:
    """Per-file data-quality summary (§7A.3). The incremental counters come from
    the parser (as a plain dict, so parallel workers can ship them across
    processes); the aggregate metrics (outliers, duplicate ratio, TIN=NPI
    count) are computed in DuckDB over the written part so this scales to
    files with millions of rows without holding them in Python memory."""
    qa = dict(qa_counters)
    total = qa["rows"]
    part = store.rates_dir / f"{file_key(source_file)}.parquet"
    try:
        _qa_aggregates(store, part, total, qa)
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


def _qa_aggregates(store: Store, part: Path, total: int, qa: dict) -> None:
    outliers = tin_npi = distinct_facts = 0
    if total and part.exists():
        with store.connect() as con:
            p = str(part)
            outliers = con.execute(
                f"""
                WITH r AS (SELECT * FROM read_parquet('{sql_path(p)}') WHERE is_dollar_rate),
                med AS (SELECT billing_code, median(negotiated_rate) m FROM r GROUP BY billing_code)
                SELECT count(*) FROM r JOIN med USING (billing_code)
                WHERE m > 0 AND (negotiated_rate > 5 * m OR negotiated_rate < 0.2 * m)
                """
            ).fetchone()[0]
            tin_npi = con.execute(
                f"SELECT count(*) FROM read_parquet('{sql_path(p)}') WHERE tin_is_really_npi"
            ).fetchone()[0]
            distinct_facts = con.execute(
                f"""
                SELECT count(*) FROM (
                    SELECT DISTINCT payer, tin_value, npi, billing_code,
                           array_to_string(billing_code_modifier, '|'),
                           negotiated_rate, billing_class
                    FROM read_parquet('{sql_path(p)}')
                )
                """
            ).fetchone()[0]
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


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


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
        import concurrent.futures

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
            self._pool.shutdown(wait=False, cancel_futures=True)
            for proc in list(getattr(self._pool, "_processes", {}).values()):
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


def _parse_worker(cfg: MrfxConfig, path_str: str, name: str, header_defaults: dict,
                  external_refs: dict, est: int, compressed_bytes: int,
                  tmp_out: str, progress_path: str) -> dict:
    """Runs in a child process. Returns a picklable summary; raises on failure
    (the parent marks the file failed)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from .store import PARQUET_COMPRESSION, RATES_SCHEMA

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
        chunk = state["pass"] * file_chunks + in_pass
        if chunk == state["last"]:
            return
        state["last"] = chunk
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
            raise SystemExit("parent process died — abandoning orphaned parse")
        try:
            tmp = progress_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"chunks_done": chunk, "chunks_total": chunks_total,
                           "pct": min(100.0, 100.0 * chunk / chunks_total)}, f)
            os.replace(tmp, progress_path)
        except OSError:
            pass  # progress is cosmetic

    keep_ref_ids = None
    if big:
        with open_stream(path, progress_cb=report) as stream:
            keep_ref_ids, target_items = skim_needed_ref_ids(cfg, stream)
        if target_items == 0:
            return {"short_circuit": True, "chunks_total": chunks_total}
        state["pass"] = 1

    rows_written = 0
    writer = None

    def sink(rows: list[dict]) -> None:
        nonlocal rows_written, writer
        if not rows:
            return
        table = pa.Table.from_pylist(rows, schema=RATES_SCHEMA)
        if writer is None:
            writer = pq.ParquetWriter(tmp_out, RATES_SCHEMA, compression=PARQUET_COMPRESSION)
        writer.write_table(table)
        rows_written += len(rows)

    parser = InNetworkParser(
        cfg, source_file=name, external_refs=external_refs,
        sink=sink, header_defaults=header_defaults, keep_ref_ids=keep_ref_ids,
    )
    try:
        with open_stream(path, progress_cb=report) as stream:
            result = parser.parse(stream)
    finally:
        if writer is not None:
            writer.close()
    return {
        "short_circuit": False,
        "chunks_total": chunks_total,
        "payer": result.payer,
        "schema_version": result.schema_version,
        "last_updated_on": result.last_updated_on,
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
    external_refs = store.load_provider_refs(pf.payer) if pf.payer else {}
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
        while True:
            try:
                return fut.result(timeout=2.0)
            except concurrent.futures.TimeoutError:
                try:
                    prog = json.loads(Path(progress_path).read_text())
                except (OSError, ValueError):
                    prog = None
                now = time.monotonic()
                if prog and prog.get("chunks_done", -1) != last:
                    last = prog["chunks_done"]
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
                        last if last >= 0 else "0", out_mb)
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
                            "it so this file re-queues; other in-flight files "
                            "re-queue too and resume automatically.",
                            name, (now - kill_ref_at) / 60)
                        if hasattr(pool, "force_heal"):
                            pool.force_heal()
                        raise RuntimeError(
                            f"parser worker wedged on {name} (no progress for "
                            f"{stall_kill/60:.0f} min) — killed and re-queued")

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
    except Exception:
        tmp_out.unlink(missing_ok=True)
        raise
    finally:
        Path(progress_path).unlink(missing_ok=True)
        Path(progress_path + ".tmp").unlink(missing_ok=True)

    store.update_progress(name, 100.0, chunks_done=payload["chunks_total"],
                          chunks_total=payload["chunks_total"])
    if payload["short_circuit"]:
        msg = ("scanned: none of the target billing codes appear in this "
               "file — extraction pass skipped")
        # a RE-ingest under the same filename must not keep serving the OLD
        # version's rates: drop any existing part so the 0-row record and the
        # store agree (the normal paths do this via finalize_rates_part)
        store.drop_rates_part(name)
        store.upsert_file(name, payer=pf.payer, status="done", rows_emitted=0,
                          qa={"rows": 0, "messages": [msg]}, finished_at=FINISHED_NOW)
        _finish_file(cfg, path, ok=True)
        log.info("%s: %s", name, msg)
        return {"status": "done", "rows": 0, "payer": pf.payer, "note": msg}

    store.finalize_rates_part(name, tmp_out, payload["rows_written"])
    store.upsert_file(
        name,
        payer=payload["payer"],
        schema_version=payload["schema_version"],
        last_updated_on=payload["last_updated_on"],
        status="done",
        rows_emitted=payload["rows"],
        ref_groups_skipped=payload["ref_groups_skipped"],
        qa=qa_report(store, payload["qa"], name),
        finished_at=FINISHED_NOW,
    )
    # rollup AFTER the done-upsert: the incremental update finds its work by
    # "done files newer than the coverage marker" — called before the upsert
    # it would recompute the PREVIOUS file's slice and miss this one entirely
    # (a crash between the two leaves the marker behind; serve-start catches up)
    if rebuild_rollups:
        _rebuild_rollups_best_effort(store, name)
    _finish_file(cfg, path, ok=True)
    if payload["ref_groups_skipped"]:
        log.warning("%s: %d rate groups skipped — missing provider reference file",
                    name, payload["ref_groups_skipped"])
    return {"status": "done", "rows": payload["rows"],
            "ref_groups_skipped": payload["ref_groups_skipped"]}


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
    for d in (cfg.inbox_dir, cfg.processed_dir, cfg.failed_dir, cfg.downloads_dir):
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
    that explicitly want the full rebuild."""
    try:
        if incremental:
            try:
                store.update_rollups_incremental()
                return
            except Exception as e:  # noqa: BLE001 — strict-precondition
                # optimization; the full rebuild is the always-correct fallback
                log.info("%s: incremental analytics update unavailable (%s) — "
                         "running a full rebuild", name, e)
        store.rebuild_rollups()
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
            with open_stream(path) as stream:
                payer, last_updated, refs = parse_provider_reference_file(cfg, stream)
            store.save_provider_refs(payer, name, last_updated, refs)
            store.upsert_file(
                name, payer=payer, status="done", last_updated_on=last_updated,
                rows_emitted=len(refs), finished_at=FINISHED_NOW,
            )
            _finish_file(cfg, path, ok=True)
            requeued = requeue_skipped(cfg, store, payer)
            return {"status": "done", "refs": len(refs), "requeued": requeued}

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
        external_refs = store.load_provider_refs(pf.payer) if pf.payer else {}
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
        if big:
            # progress spans two passes: pass 1 fills 0-50%, pass 2 fills 50-100%
            progress = _ProgressTracker(store, name, pf.compressed_bytes,
                                        on_bar=progress_bar, passes=2)
            log.info("%s: large file (~%.1f GB uncompressed) — two-pass chunked ingest (%d chunks)",
                     name, est / 1e9, progress.chunks_total)
            store.upsert_file(name, chunks_total=progress.chunks_total, progress=0.0)
            progress.set_pass(0)
            with open_stream(path, progress_cb=progress.update) as stream:
                keep_ref_ids, target_items = skim_needed_ref_ids(cfg, stream)
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
                _finish_file(cfg, path, ok=True)
                log.info("%s: %s", name, msg)
                return {"status": "done", "rows": 0, "payer": pf.payer, "note": msg}
            progress.set_pass(1)
        else:
            progress = None

        with store.rates_part_writer(name) as writer:
            parser = InNetworkParser(
                cfg, source_file=name, external_refs=external_refs,
                sink=writer.write_batch, header_defaults=header_defaults,
                keep_ref_ids=keep_ref_ids,
            )
            with open_stream(path, progress_cb=progress.update if progress else None) as stream:
                result = parser.parse(stream)
        if progress is not None:
            progress.finish()
        store.upsert_file(
            name,
            payer=result.payer,
            schema_version=result.schema_version,
            last_updated_on=result.last_updated_on,
            status="done",
            rows_emitted=result.qa.rows,
            ref_groups_skipped=result.ref_groups_skipped,
            qa=qa_report(store, result.qa.to_dict(), name),
            finished_at=FINISHED_NOW,
        )
        # rollup AFTER the done-upsert — same reason as the pooled path: the
        # incremental update keys on done files newer than the coverage marker
        if rebuild_rollups:
            _rebuild_rollups_best_effort(store, name)
        _finish_file(cfg, path, ok=True)
        if result.ref_groups_skipped:
            log.warning(
                "%s: %d rate groups skipped — missing provider reference file", name, result.ref_groups_skipped
            )
        return {
            "status": "done",
            "rows": result.qa.rows,
            "ref_groups_skipped": result.ref_groups_skipped,
        }

    except Exception as e:  # noqa: BLE001 — fault isolation is the contract here
        log.exception("ingest failed for %s", name)
        store.upsert_file(name, status="failed", error=f"{type(e).__name__}: {e}", finished_at=_now())
        _finish_file(cfg, path, ok=False)  # never raises; the row is already 'failed'
        return {"status": "failed", "error": str(e)}


def requeue_skipped(cfg: MrfxConfig, store: Store, payer: str) -> list[str]:
    """After a reference file lands, re-ingest this payer's in-network files
    that had skipped groups (if their source file is still on disk)."""
    with store.connect() as con:
        rows = con.execute(
            """
            SELECT filename FROM files
            WHERE payer = ? AND file_type = 'in_network'
              AND status = 'done' AND ref_groups_skipped > 0
            """,
            [payer],
        ).fetchall()
    requeued = []
    for (fname,) in rows:
        for base in (cfg.processed_dir, cfg.inbox_dir):
            src = base / fname
            if src.exists():
                log.info("re-ingesting %s now that %s reference data is present", fname, payer)
                ingest_file(cfg, store, src)
                requeued.append(fname)
                break
    return requeued


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
    for p in paths:
        prior = store.file_status(p.name)
        if prior and prior.get("status") in ("done", "processing") and not force:
            continue
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
