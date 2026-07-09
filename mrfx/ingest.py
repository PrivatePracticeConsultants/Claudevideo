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
from pathlib import Path

from .config import MrfxConfig
from .parser import (
    InNetworkParser,
    ParseResult,
    parse_provider_reference_file,
    skim_needed_ref_ids,
)
from .sniff import Preflight, open_stream, preflight
from .store import Store, file_key

log = logging.getLogger(__name__)

# Files whose estimated uncompressed size is at or above this use the
# disk-backed reference index + chunked progress. Below it, the fast in-memory
# path. ~1.5 GB uncompressed ≈ 150 MB compressed.
LARGE_FILE_UNCOMPRESSED_BYTES = 1_500_000_000

# One progress "chunk" = this many COMPRESSED bytes consumed. The bar reports
# chunks_done / chunks_total so the user sees the file worked through in
# digestible pieces.
CHUNK_COMPRESSED_BYTES = 64 * 1024 * 1024  # 64 MB


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
            self.on_bar(chunk, self.chunks_total, pct)

    def finish(self) -> None:
        try:
            self.store.update_progress(self.filename, 100.0, self.chunks_total, self.chunks_total)
        except Exception:  # noqa: BLE001
            pass
        if self.on_bar:
            self.on_bar(self.chunks_total, self.chunks_total, 100.0)


def qa_report(store: Store, qa_counters: dict, source_file: str) -> dict:
    """Per-file data-quality summary (§7A.3). The incremental counters come from
    the parser (as a plain dict, so parallel workers can ship them across
    processes); the aggregate metrics (outliers, duplicate ratio, TIN=NPI
    count) are computed in DuckDB over the written part so this scales to
    files with millions of rows without holding them in Python memory."""
    qa = dict(qa_counters)
    total = qa["rows"]
    part = store.rates_dir / f"{file_key(source_file)}.parquet"
    outliers = tin_npi = distinct_facts = 0
    if total and part.exists():
        with store.connect() as con:
            p = str(part)
            outliers = con.execute(
                f"""
                WITH r AS (SELECT * FROM read_parquet('{p}') WHERE is_dollar_rate),
                med AS (SELECT billing_code, median(negotiated_rate) m FROM r GROUP BY billing_code)
                SELECT count(*) FROM r JOIN med USING (billing_code)
                WHERE m > 0 AND (negotiated_rate > 5 * m OR negotiated_rate < 0.2 * m)
                """
            ).fetchone()[0]
            tin_npi = con.execute(
                f"SELECT count(*) FROM read_parquet('{p}') WHERE tin_is_really_npi"
            ).fetchone()[0]
            distinct_facts = con.execute(
                f"""
                SELECT count(*) FROM (
                    SELECT DISTINCT payer, tin_value, npi, billing_code,
                           array_to_string(billing_code_modifier, '|'),
                           negotiated_rate, billing_class
                    FROM read_parquet('{p}')
                )
                """
            ).fetchone()[0]
    qa.update({
        "outlier_rates": outliers,
        "outlier_rule": ">5x or <0.2x of the code's within-file median",
        "non_dollar_share": round(qa["non_dollar_rows"] / total, 4) if total else 0.0,
        "duplicate_explosion_ratio": round(total / distinct_facts, 2) if distinct_facts else 1.0,
        "tin_is_really_npi_rows": tin_npi,
    })
    return qa


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _finish_file(cfg: MrfxConfig, path: Path, ok: bool) -> None:
    """Move a fully-processed file out of the inbox (config-controlled)."""
    if not path.exists():
        return
    if not ok:
        dest = cfg.failed_dir / path.name
        cfg.failed_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), dest)
        return
    if cfg.move_processed and cfg.inbox_dir in path.parents:
        dest = cfg.processed_dir / path.name
        cfg.processed_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), dest)


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

    def shutdown(self) -> None:
        with self._lock:
            self._pool.shutdown(wait=True, cancel_futures=True)


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

    from .store import RATES_SCHEMA

    path = Path(path_str)
    file_chunks = max(1, -(-max(compressed_bytes, 1) // CHUNK_COMPRESSED_BYTES))
    big = (est or 0) >= LARGE_FILE_UNCOMPRESSED_BYTES
    passes = 2 if big else 1
    chunks_total = file_chunks * passes
    state = {"pass": 0, "last": -1}

    def report(compressed_read: int) -> None:
        in_pass = min(file_chunks, compressed_read // CHUNK_COMPRESSED_BYTES)
        chunk = state["pass"] * file_chunks + in_pass
        if chunk == state["last"]:
            return
        state["last"] = chunk
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
            writer = pq.ParquetWriter(tmp_out, RATES_SCHEMA)
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
    progress_path = str(path) + ".progress"

    def dispatch_and_wait() -> dict:
        tmp_out.unlink(missing_ok=True)
        fut = pool.submit(
            _parse_worker, cfg, str(path), name, header_defaults, external_refs,
            pf.est_uncompressed_bytes or 0, pf.compressed_bytes, str(tmp_out), progress_path,
        )
        last = -1
        while True:
            try:
                return fut.result(timeout=2.0)
            except concurrent.futures.TimeoutError:
                try:
                    prog = json.loads(Path(progress_path).read_text())
                except (OSError, ValueError):
                    continue
                if prog.get("chunks_done", -1) != last:
                    last = prog["chunks_done"]
                    store.update_progress(name, prog["pct"], chunks_done=prog["chunks_done"],
                                          chunks_total=prog["chunks_total"])
                    if progress_bar:
                        progress_bar(prog["chunks_done"], prog["chunks_total"], prog["pct"])

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
        store.upsert_file(name, payer=pf.payer, status="done", rows_emitted=0,
                          qa={"rows": 0, "messages": [msg]}, finished_at=_now())
        _finish_file(cfg, path, ok=True)
        log.info("%s: %s", name, msg)
        return {"status": "done", "rows": 0, "payer": pf.payer, "note": msg}

    store.finalize_rates_part(name, tmp_out, payload["rows_written"])
    if rebuild_rollups:
        _rebuild_rollups_best_effort(store, name)
    store.upsert_file(
        name,
        payer=payload["payer"],
        schema_version=payload["schema_version"],
        last_updated_on=payload["last_updated_on"],
        status="done",
        rows_emitted=payload["rows"],
        ref_groups_skipped=payload["ref_groups_skipped"],
        qa=qa_report(store, payload["qa"], name),
        finished_at=_now(),
    )
    _finish_file(cfg, path, ok=True)
    if payload["ref_groups_skipped"]:
        log.warning("%s: %d rate groups skipped — missing provider reference file",
                    name, payload["ref_groups_skipped"])
    return {"status": "done", "rows": payload["rows"],
            "ref_groups_skipped": payload["ref_groups_skipped"]}


def _rebuild_rollups_best_effort(store: Store, name: str) -> None:
    """The parquet part is already durable when this runs: an analytics
    rebuild failing (disk pressure, memory) must NOT fail the file — hours of
    parsing would be retried for data that already landed. Rollups refresh on
    the next successful rebuild, same contract as the queue's batched path."""
    try:
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

    try:
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
                rows_emitted=len(refs), finished_at=_now(),
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
                store.upsert_file(
                    name, payer=pf.payer, status="done", rows_emitted=0,
                    qa={"rows": 0, "messages": [msg]}, finished_at=_now(),
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
        if rebuild_rollups:
            _rebuild_rollups_best_effort(store, name)
        store.upsert_file(
            name,
            payer=result.payer,
            schema_version=result.schema_version,
            last_updated_on=result.last_updated_on,
            status="done",
            rows_emitted=result.qa.rows,
            ref_groups_skipped=result.ref_groups_skipped,
            qa=qa_report(store, result.qa.to_dict(), name),
            finished_at=_now(),
        )
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
        try:
            _finish_file(cfg, path, ok=False)
        except OSError:
            pass
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
    paths = [p for p in sorted(cfg.inbox_dir.glob("*")) if p.is_file()]
    flights = []
    for p in paths:
        prior = store.file_status(p.name)
        if prior and prior.get("status") in ("done", "processing") and not force:
            continue
        flights.append((p, preflight(p, cfg, store)))

    order = {"provider_reference": 0, "in_network": 1}
    flights.sort(key=lambda t: order.get(t[1].file_type, 2))

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
        results.append({"file": p.name, **ingest_file(cfg, store, p, pf, progress_bar=progress_bar)})
    return results
