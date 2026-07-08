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
from .store import Store

log = logging.getLogger(__name__)


def qa_report(result: ParseResult) -> dict:
    """Per-file data-quality summary (§7A.3). Every number shown to a client
    should come from a file whose QA report has been eyeballed."""
    rows = result.rows
    qa = result.qa.to_dict()
    dollar = [r["negotiated_rate"] for r in rows if r["is_dollar_rate"]]
    outliers = 0
    if dollar:
        by_code: dict[str, list[float]] = {}
        for r in rows:
            if r["is_dollar_rate"]:
                by_code.setdefault(r["billing_code"], []).append(r["negotiated_rate"])
        medians = {c: sorted(v)[len(v) // 2] for c, v in by_code.items()}
        outliers = sum(
            1 for r in rows
            if r["is_dollar_rate"] and medians[r["billing_code"]] > 0
            and (r["negotiated_rate"] > 5 * medians[r["billing_code"]]
                 or r["negotiated_rate"] < 0.2 * medians[r["billing_code"]])
        )
    distinct_facts = len({
        (r["payer"], r["tin_value"], r["npi"], r["billing_code"],
         tuple(r["billing_code_modifier"]), r["negotiated_rate"], r["billing_class"])
        for r in rows
    })
    qa.update({
        "outlier_rates": outliers,
        "outlier_rule": ">5x or <0.2x of the code's within-file median",
        "non_dollar_share": round(qa["non_dollar_rows"] / len(rows), 4) if rows else 0.0,
        "duplicate_explosion_ratio": round(len(rows) / distinct_facts, 2) if distinct_facts else 1.0,
        "tin_is_really_npi_rows": sum(1 for r in rows if r["tin_is_really_npi"]),
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

        # in-network rate file
        external_refs = store.load_provider_refs(pf.payer) if pf.payer else {}
        parser = InNetworkParser(cfg, source_file=name, external_refs=external_refs)
        with open_stream(path) as stream:
            result = parser.parse(stream)
        if result.payer != pf.payer and result.payer != "Unknown payer":
            # header window missed the entity name; reload refs under the real payer
            if not external_refs:
                parser2 = InNetworkParser(
                    cfg, source_file=name, external_refs=store.load_provider_refs(result.payer)
                )
                with open_stream(path) as stream:
                    result = parser2.parse(stream)
        store.drop_rates_part(name)
        store.write_rates_part(name, result.rows)
        store.rebuild_rollups()
        store.upsert_file(
            name,
            payer=result.payer,
            schema_version=result.schema_version,
            last_updated_on=result.last_updated_on,
            status="done",
            rows_emitted=len(result.rows),
            ref_groups_skipped=result.ref_groups_skipped,
            qa=qa_report(result),
            finished_at=_now(),
        )
        _finish_file(cfg, path, ok=True)
        if result.ref_groups_skipped:
            log.warning(
                "%s: %d rate groups skipped — missing provider reference file", name, result.ref_groups_skipped
            )
        return {
            "status": "done",
            "rows": len(result.rows),
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
