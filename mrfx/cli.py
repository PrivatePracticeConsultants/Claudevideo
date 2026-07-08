"""mrfx CLI: serve | preflight | ingest | status | export | reset."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path

from .config import MrfxConfig, load_mrfx_config
from .enrich import run_enrichment, start_background_enrichment
from .ingest import ingest_file, scan_inbox
from .sniff import format_preflight, preflight
from .store import Store

log = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)


def _watcher_loop(cfg: MrfxConfig, store: Store, stop: threading.Event) -> None:
    """Inbox watcher: any file event triggers a scan pass. A bad file never
    kills this loop (scan_inbox isolates per-file failures)."""
    import watchfiles

    log.info("watching %s", cfg.inbox_dir)
    scan_inbox(cfg, store)  # pick up anything already sitting there
    start_background_enrichment(cfg, store)
    try:
        for _changes in watchfiles.watch(cfg.inbox_dir, stop_event=stop, step=1000):
            try:
                results = scan_inbox(cfg, store)
                if any(r.get("status") == "done" for r in results):
                    start_background_enrichment(cfg, store)
            except Exception:  # noqa: BLE001 — watcher survives everything
                log.exception("inbox scan failed; watcher continues")
    except Exception:  # noqa: BLE001
        log.exception("watcher stopped unexpectedly")


def cmd_serve(cfg: MrfxConfig, args) -> int:
    import uvicorn

    from .api import create_app

    store = Store(cfg.store_dir)
    stop = threading.Event()
    threading.Thread(
        target=_watcher_loop, args=(cfg, store, stop), name="mrfx-watcher", daemon=True
    ).start()
    app = create_app(cfg, store)
    print(f"\n  MRF Explorer  →  http://localhost:{cfg.port}\n"
          f"  inbox: {cfg.inbox_dir}  (drop .json / .json.gz / .zip here)\n")
    try:
        uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="warning")
    finally:
        stop.set()
    return 0


def cmd_preflight(cfg: MrfxConfig, args) -> int:
    store = Store(cfg.store_dir)
    path = Path(args.path)
    paths = sorted(p for p in path.glob("*") if p.is_file()) if path.is_dir() else [path]
    worst = 0
    for p in paths:
        pf = preflight(p, cfg, store)
        print(format_preflight(pf))
        print("-" * 60)
        worst = max(worst, {"READY": 0, "NOT A RATE FILE": 1, "NEEDS COMPANION": 2, "UNREADABLE": 3}[pf.verdict])
    return worst


def cmd_ingest(cfg: MrfxConfig, args) -> int:
    store = Store(cfg.store_dir)
    path = Path(args.path) if args.path else cfg.inbox_dir
    if path.is_dir():
        if path != cfg.inbox_dir:
            results = [
                {"file": p.name, **ingest_file(cfg, store, p)}
                for p in sorted(path.glob("*")) if p.is_file()
            ]
        else:
            results = scan_inbox(cfg, store, force=args.force)
    else:
        results = [{"file": path.name, **ingest_file(cfg, store, path)}]
    for r in results:
        line = f"{r['file']}: {r['status']}"
        if r.get("rows") is not None:
            line += f" ({r['rows']} rows"
            if r.get("ref_groups_skipped"):
                line += f", {r['ref_groups_skipped']} rate groups skipped — missing provider reference file"
            line += ")"
        if r.get("error"):
            line += f" — {r['error']}"
        print(line)
    if cfg.enrichment.mode != "off" and any(r.get("status") == "done" for r in results):
        print("enriching NPI names...")
        run_enrichment(cfg, store)
    return 0 if all(r["status"] in ("done", "pending_confirmation") for r in results) else 1


def cmd_status(cfg: MrfxConfig, args) -> int:
    store = Store(cfg.store_dir)
    with store.connect() as con:
        rows = con.execute(
            """
            SELECT filename, payer, file_type, status, rows_emitted,
                   ref_groups_skipped, coalesce(error, '') AS error
            FROM files ORDER BY coalesce(finished_at, started_at) DESC NULLS LAST
            """
        ).fetchall()
        totals = con.execute(
            "SELECT count(*), count(DISTINCT npi), count(DISTINCT payer) FROM rates"
        ).fetchone()
    if not rows:
        print("no files ingested yet — drop MRFs into", cfg.inbox_dir)
        return 0
    widths = (40, 22, 18, 22, 10, 8)
    hdr = ("filename", "payer", "type", "status", "rows", "skipped")
    print("  ".join(h.ljust(w) for h, w in zip(hdr, widths)))
    for r in rows:
        cells = [str(c or "") for c in r[:6]]
        print("  ".join(c[:w].ljust(w) for c, w in zip(cells, widths)) + ("  " + r[6] if r[6] else ""))
    print(f"\nstore: {totals[0]:,} rate rows, {totals[1]:,} NPIs, {totals[2]} payers")
    return 0


def cmd_export(cfg: MrfxConfig, args) -> int:
    from .api import order_export_sql  # lazy import to keep CLI light

    store = Store(cfg.store_dir)
    out = Path(args.out)
    sql = order_export_sql(args)
    with store.connect() as con:
        con.execute(f"COPY ({sql[0]}) TO '{out}' (FORMAT CSV, HEADER)", sql[1])
    data = out.read_bytes()
    out.write_bytes(b"\xef\xbb\xbf" + data)
    n = data.count(b"\n") - 1
    print(f"exported ~{max(n, 0):,} rows -> {out}")
    return 0


def cmd_reset(cfg: MrfxConfig, args) -> int:
    if not args.confirm:
        print("refusing: pass --confirm to clear the store (processed files are kept)")
        return 1
    Store(cfg.store_dir).reset()
    print("store cleared. processed files remain in", cfg.processed_dir)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mrfx", description="MRF Explorer — drop-in payer rate dashboard")
    ap.add_argument("--config", default="config/mrfx.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="start API + dashboard + inbox watcher")
    p = sub.add_parser("preflight", help="inspect a file before committing to a long parse")
    p.add_argument("path")
    p = sub.add_parser("ingest", help="one-shot ingest of a file or directory (default: inbox)")
    p.add_argument("path", nargs="?")
    p.add_argument("--force", action="store_true", help="ignore confirm_over_gb and done-checkpoints")
    sub.add_parser("status", help="files table summary")
    p = sub.add_parser("export", help="export rates to CSV")
    p.add_argument("out")
    p.add_argument("--payer")
    p.add_argument("--cpt")
    p.add_argument("--modifier")
    p.add_argument("--billing-class", dest="billing_class")
    p.add_argument("--q")
    p.add_argument("--all-types", action="store_true", help="include non-dollar negotiated_type rows")
    p.add_argument("--rate-min", type=float, dest="rate_min")
    p.add_argument("--rate-max", type=float, dest="rate_max")
    p = sub.add_parser("reset", help="clear the store (keeps processed files)")
    p.add_argument("--confirm", action="store_true")

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = load_mrfx_config(args.config)
    cfg.ensure_dirs()
    return {
        "serve": cmd_serve,
        "preflight": cmd_preflight,
        "ingest": cmd_ingest,
        "status": cmd_status,
        "export": cmd_export,
        "reset": cmd_reset,
    }[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
