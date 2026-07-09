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


def _url_worker_loop(cfg: MrfxConfig, store: Store, stop: threading.Event) -> None:
    """Drains the URL queue while the server runs: download -> classify ->
    (expand TOC | ingest). One file at a time; failures never kill the loop."""
    from .fetch import run_queue

    try:
        run_queue(cfg, store, stop=stop)
    except Exception:  # noqa: BLE001
        log.exception("url worker stopped unexpectedly")


def cmd_serve(cfg: MrfxConfig, args) -> int:
    import uvicorn

    from .api import create_app

    store = Store(cfg.store_dir)
    stop = threading.Event()
    threading.Thread(
        target=_watcher_loop, args=(cfg, store, stop), name="mrfx-watcher", daemon=True
    ).start()
    threading.Thread(
        target=_url_worker_loop, args=(cfg, store, stop), name="mrfx-urls", daemon=True
    ).start()
    app = create_app(cfg, store)
    print(f"\n  MRF Explorer  →  http://localhost:{cfg.port}\n"
          f"  inbox: {cfg.inbox_dir}  (drop .json / .json.gz / .zip here)\n"
          f"  or paste MRF/TOC URLs on the Files tab — downloads run automatically\n")
    try:
        uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="warning")
    finally:
        stop.set()
    return 0


def cmd_add(cfg: MrfxConfig, args) -> int:
    """Add MRF/TOC URLs. If the dashboard is running, hand them to it (its
    background worker downloads and ingests); otherwise process them right
    here until the queue is drained."""
    urls = list(args.urls or [])
    if args.file:
        urls += [ln.strip() for ln in Path(args.file).read_text().splitlines() if ln.strip()]
    urls = [u for u in urls if not u.startswith("#")]
    if args.known:
        from .known_sources import load_known_sources

        known = load_known_sources(cfg.known_sources_path)
        queueable = [s for s in known if s["queueable"]]
        for s in queueable:
            print(f"  + {s['name']}")
        urls += [s["url"] for s in queueable]
        skipped_portals = len(known) - len(queueable)
        if skipped_portals:
            print(f"({skipped_portals} portal-only sources need a browser — "
                  "see the Sources list or config/known_sources.yaml)")
    if not urls and not args.retry_failed:
        print("nothing to add — pass URLs as arguments, --file urls.txt, or "
              "--known for the tested payer indexes")
        return 1

    # If a server is already running it owns the store (and has a worker);
    # send the URLs there instead of fighting over the database file.
    import httpx as _httpx

    try:
        r = _httpx.post(f"http://localhost:{cfg.port}/api/urls",
                        json={"urls": urls}, timeout=30)
        if r.status_code == 200:
            d = r.json()
            print(f"handed to the running dashboard: {d['added']} queued, "
                  f"{d['skipped']} already known, {d['invalid']} not URLs")
            if args.retry_failed:
                rf = _httpx.post(f"http://localhost:{cfg.port}/api/urls/retry-failed", timeout=30)
                print(f"re-queued {rf.json().get('requeued', 0)} previously failed URL(s)"
                      if rf.status_code == 200 else
                      "could not re-queue failed URLs on the server — retry them in the Files tab")
            print(f"watch progress at http://localhost:{cfg.port} → Files tab")
            return 0
        # something answered on our port but not correctly — do NOT open the
        # same database from a second process (two workers would fight)
        print(f"a server on port {cfg.port} answered HTTP {r.status_code} — "
              "not adding locally while it may own the database. Check the "
              "dashboard, or stop it and re-run this command.")
        return 1
    except _httpx.ConnectError:
        pass  # nothing listening — safe to process locally below
    except _httpx.HTTPError as e:
        print(f"a server on port {cfg.port} is running but didn't accept the links ({e}). "
              "Not adding locally while it may own the database — check the dashboard.")
        return 1

    from .fetch import add_urls, run_queue

    store = Store(cfg.store_dir)
    counts = add_urls(store, urls)
    print(f"queued {counts['added']} URL(s) "
          f"({counts['skipped']} already known, {counts['invalid']} not URLs)")
    if args.retry_failed:
        n = store.requeue_failed()
        print(f"re-queued {n} previously failed URL(s)")
    if store.url_queue_counts().get("queued", 0) == 0:
        return 0
    print("downloading and ingesting (one file at a time — Ctrl-C to stop; "
          "re-running `mrfx add` resumes where it left off)...")
    processed = run_queue(cfg, store, drain=True, progress_bar=_make_cli_progress())
    counts = store.url_queue_counts()
    print(f"\nfinished: {processed} processed this run — "
          f"{counts.get('done', 0)} done, {counts.get('failed', 0)} failed, "
          f"{counts.get('queued', 0)} still queued")
    for rec in store.list_urls(limit=50):
        if rec["status"] == "failed":
            print(f"  FAILED  {rec['url'][:90]}\n          → {rec['error']}")
    if cfg.enrichment.mode != "off" and processed:
        print("looking up practice names (NPPES)...")
        run_enrichment(cfg, store)
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


def _make_cli_progress():
    """A progress-bar callback for large-file ingest. Uses tqdm when present,
    else a plain carriage-return line — either way the user sees chunks worked
    through in real time."""
    state = {"bar": None}
    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    def cb(done, total, pct):
        if tqdm is not None:
            if state["bar"] is None:
                state["bar"] = tqdm(total=total, unit="chunk", desc="  ingesting", leave=True)
            state["bar"].n = done
            state["bar"].refresh()
            if done >= total:
                state["bar"].close()
                state["bar"] = None
        else:
            end = "\n" if done >= total else "\r"
            print(f"  ingesting: chunk {done}/{total} ({pct:.0f}%)", end=end, flush=True)

    return cb


def cmd_ingest(cfg: MrfxConfig, args) -> int:
    store = Store(cfg.store_dir)
    path = Path(args.path) if args.path else cfg.inbox_dir
    bar = _make_cli_progress()
    if path.is_dir():
        if path != cfg.inbox_dir:
            results = [
                {"file": p.name, **ingest_file(cfg, store, p, progress_bar=bar)}
                for p in sorted(path.glob("*")) if p.is_file()
            ]
        else:
            results = scan_inbox(cfg, store, force=args.force, progress_bar=bar)
    else:
        results = [{"file": path.name, **ingest_file(cfg, store, path, progress_bar=bar)}]
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
    qc = store.url_queue_counts()
    if qc:
        line = " · ".join(f"{n} {st}" for st, n in sorted(qc.items()))
        print(f"link queue: {line}")
    if not rows:
        print("no files ingested yet — drop MRFs into", cfg.inbox_dir,
              "or run: mrfx add --file config/starter_links.txt")
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
    from .api import FilterSet, methodology_text, order_export_sql

    store = Store(cfg.store_dir)
    out = Path(args.out)
    sql, params = order_export_sql(args)
    with store.connect() as con:
        con.execute(f"COPY ({sql}) TO '{out}' (FORMAT CSV, HEADER)", params)
    data = out.read_bytes()
    out.write_bytes(b"\xef\xbb\xbf" + data)
    qp = {k: v for k, v in {
        "payer": args.payer, "cpt": args.cpt, "modifier": args.modifier,
        "billing_class": args.billing_class, "q": args.q,
        "dollar_only": "0" if args.all_types else "1",
        "rate_min": args.rate_min, "rate_max": args.rate_max,
    }.items() if v not in (None, "")}
    sidecar = out.with_name(out.stem + "_methodology.txt")
    sidecar.write_text(
        methodology_text(cfg, store, args.grain, FilterSet(qp), "negotiated_rate", "desc", "cli")
    )
    n = data.count(b"\n") - 1
    print(f"exported ~{max(n, 0):,} rows -> {out}\nmethodology -> {sidecar}")
    return 0


def cmd_outreach(cfg: MrfxConfig, args) -> int:
    """One row per entity: org name + geography + per-code rate/percentile merge
    fields, for contact-list cross-referencing and mail merge (e.g. Brevo)."""
    from .api import FilterSet, grain_of, methodology_text, rel_sql
    from .outreach import build_outreach_rows, outreach_csv

    store = Store(cfg.store_dir)
    qp = {k: v for k, v in {
        "payer": args.payer, "cpt": args.cpt, "state": args.state, "city": args.city,
        "month": args.month, "discipline": args.discipline,
        "modifier": "base" if args.base_only else None,
    }.items() if v not in (None, "")}
    grain = grain_of({"grain": args.grain} if args.grain else {}, cfg, store)
    if grain == "npi":
        grain = "tin"
    fs = FilterSet(qp)
    headers, rows = build_outreach_rows(store, rel_sql(grain, fs), fs.params, fs.described.get("codes"))
    out = Path(args.out)
    out.write_text(outreach_csv(headers, rows), encoding="utf-8")
    sidecar = out.with_name(out.stem + "_methodology.txt")
    sidecar.write_text(methodology_text(cfg, store, grain, fs, "display_name", "asc", "outreach"))
    print(f"exported {len(rows)} entities -> {out}\nmethodology -> {sidecar}")
    print("columns:", ", ".join(headers[:14]), f"... + {len(headers) - 14} per-code merge fields")
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
    p = sub.add_parser("add", help="paste MRF or TOC/index URLs — downloads and ingests automatically")
    p.add_argument("urls", nargs="*", help="one or more http(s) URLs")
    p.add_argument("--file", help="text file with one URL per line (# comments ok)")
    p.add_argument("--retry-failed", action="store_true", help="also re-queue previously failed URLs")
    p.add_argument("--known", action="store_true",
                   help="queue every tested payer index from config/known_sources.yaml")
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
    p.add_argument("--grain", choices=["entity", "tin", "npi"], default="tin")
    p.add_argument("--all-types", action="store_true", help="include non-dollar negotiated_type rows")
    p.add_argument("--rate-min", type=float, dest="rate_min")
    p.add_argument("--rate-max", type=float, dest="rate_max")
    p = sub.add_parser("outreach", help="entity contact/geography CSV for mail-merge cross-referencing")
    p.add_argument("out")
    p.add_argument("--payer")
    p.add_argument("--cpt", help="comma list of codes for the merge-field columns")
    p.add_argument("--state")
    p.add_argument("--city")
    p.add_argument("--month")
    p.add_argument("--discipline")
    p.add_argument("--grain", choices=["entity", "tin"])
    p.add_argument("--base-only", action="store_true", default=True,
                   help="base-modifier rows only (default on)")
    p = sub.add_parser("reset", help="clear the store (keeps processed files)")
    p.add_argument("--confirm", action="store_true")

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = load_mrfx_config(args.config)
    cfg.ensure_dirs()
    return {
        "serve": cmd_serve,
        "add": cmd_add,
        "preflight": cmd_preflight,
        "ingest": cmd_ingest,
        "status": cmd_status,
        "export": cmd_export,
        "outreach": cmd_outreach,
        "reset": cmd_reset,
    }[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
