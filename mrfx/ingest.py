"""Ingest orchestration: preflight -> parse -> store -> move/quarantine.

A bad file quarantines with a visible error; it never raises out of
`ingest_file`, so the watcher and server stay alive (spec §8.6).
"""

from __future__ import annotations

import datetime as dt
import logging
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


def qa_report(store: Store, result: ParseResult, source_file: str) -> dict:
    """Per-file data-quality summary (§7A.3). The incremental counters come from
    the parser; the aggregate metrics (outliers, duplicate ratio, TIN=NPI count)
    are computed in DuckDB over the written part so this scales to files with
    millions of rows without holding them in Python memory."""
    qa = result.qa.to_dict()
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

        # in-network rate file — stream rows straight into the parquet part in
        # batches (constant memory, §8.1). The parser is seeded with the header
        # preflight already sniffed, so batched rows carry the right payer/month
        # even though the parser flushes before EOF.
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
                keep_ref_ids = skim_needed_ref_ids(cfg, stream)
            log.info("%s: pass 1 done — %d target-cited references to keep in memory",
                     name, len(keep_ref_ids))
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
            store.rebuild_rollups()
        store.upsert_file(
            name,
            payer=result.payer,
            schema_version=result.schema_version,
            last_updated_on=result.last_updated_on,
            status="done",
            rows_emitted=result.qa.rows,
            ref_groups_skipped=result.ref_groups_skipped,
            qa=qa_report(store, result, name),
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
