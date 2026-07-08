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
from .parser import InNetworkParser, ParseResult, parse_provider_reference_file
from .sniff import Preflight, open_stream, preflight
from .store import Store, file_key

log = logging.getLogger(__name__)


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


def ingest_file(cfg: MrfxConfig, store: Store, path: Path, pf: Preflight | None = None) -> dict:
    """Ingest one file. Returns the final files-table record fields."""
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
        with store.rates_part_writer(name) as writer:
            parser = InNetworkParser(
                cfg, source_file=name, external_refs=external_refs,
                sink=writer.write_batch, header_defaults=header_defaults,
            )
            with open_stream(path) as stream:
                result = parser.parse(stream)
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


def scan_inbox(cfg: MrfxConfig, store: Store, force: bool = False) -> list[dict]:
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
        results.append({"file": p.name, **ingest_file(cfg, store, p, pf)})
    return results
