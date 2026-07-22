"""mrfx CLI: serve | add | preflight | ingest | status | export | outreach | forget | reset."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

import duckdb

from .config import MrfxConfig, load_mrfx_config
from .enrich import run_enrichment, start_persistent_enrichment
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


def _start_stack_recorder(store_dir: Path, interval: float = 30.0,
                          stop: threading.Event | None = None) -> threading.Thread:
    """Black-box flight recorder for `serve`: every `interval` seconds, write
    every thread's live stack to <store>/diagnostics/stacks_latest.txt
    (atomic replace, so the file is never half-written). When the server
    wedges so hard that even HTTP stops answering, this file is the channel
    that still works — open it in Notepad and paste it into a bug report.
    The timestamp in the first line doubles as a heartbeat: if it stops
    refreshing, the whole process is dead or frozen, not just one part.
    Cosmetic helper — it must never fail real work."""
    import time

    from .ingest import thread_stacks_text

    diag = Path(store_dir) / "diagnostics"
    path = diag / "stacks_latest.txt"

    def loop() -> None:
        while stop is None or not stop.is_set():
            try:
                diag.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".txt.tmp")
                tmp.write_text(
                    f"snapshot written {time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"(refreshed every {interval:.0f}s while the server runs; "
                    "if this time is old, the process is dead or frozen)\n\n"
                    + thread_stacks_text(),
                    encoding="utf-8")
                os.replace(tmp, path)
            except Exception:  # noqa: BLE001 — the recorder must never hurt the flight
                pass
            if stop is None:
                time.sleep(interval)
            elif stop.wait(interval):
                return

    t = threading.Thread(target=loop, daemon=True, name="mrfx-stack-recorder")
    t.start()
    return t


def _watcher_loop(cfg: MrfxConfig, store: Store, stop: threading.Event) -> None:
    """Inbox watcher: any file event triggers a scan pass. A bad file never
    kills this loop (scan_inbox isolates per-file failures)."""
    import watchfiles

    log.info("watching %s", cfg.inbox_dir)
    try:
        scan_inbox(cfg, store)  # pick up anything already sitting there
    except Exception:  # noqa: BLE001 — a store hiccup on the FIRST scan must not
        # silently kill the watcher thread before it ever watches
        log.exception("initial inbox scan failed; watching for changes anyway")
    # enrichment is driven by the always-on persistent loop started in
    # cmd_serve (it covers inbox AND URL-worker ingests) — not re-triggered here
    while not stop.is_set():
        try:
            for _changes in watchfiles.watch(cfg.inbox_dir, stop_event=stop, step=1000):
                try:
                    scan_inbox(cfg, store)
                except Exception:  # noqa: BLE001 — watcher survives everything
                    log.exception("inbox scan failed; watcher continues")
            return  # stop_event set — clean shutdown
        except Exception:  # noqa: BLE001 — the watch itself died (inbox dir
            # deleted, inotify hiccup). Re-arm instead of silently never
            # watching again for the rest of the server's life.
            log.exception("inbox watch errored; re-arming in 15s")
            try:
                cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            stop.wait(15)


def _url_worker_loop(cfg: MrfxConfig, store: Store, stop: threading.Event) -> None:
    """Drains the URL queue while the server runs: download -> classify ->
    (expand TOC | ingest). One file at a time; failures never kill the loop.
    run_queue survives per-row failures internally, but its startup section
    (crash recovery, pool creation) can still throw — without the re-arm loop
    one such error would silently kill the worker for the server's whole life,
    leaving every pasted link sitting at 'queued' with no error anywhere the
    user looks."""
    from .fetch import run_queue

    while not stop.is_set():
        try:
            run_queue(cfg, store, stop=stop)
            return  # clean exit (stop requested)
        except Exception:  # noqa: BLE001
            log.exception("url worker stopped unexpectedly — restarting in 30s")
            stop.wait(30)


def cmd_serve(cfg: MrfxConfig, args) -> int:
    import faulthandler
    import socket

    import uvicorn

    from .api import create_app

    # A native-code crash (e.g. the 0xC0000374 heap-corruption class) normally
    # kills the process with NO output — the console just closes. This prints
    # every thread's stack on the way down so there's something to report.
    try:
        faulthandler.enable()
    except (RuntimeError, OSError, ValueError):
        pass  # no usable stderr (e.g. pythonw) — diagnostics are cosmetic

    # Claim the port BEFORE touching the store: a second `mrfx serve` used to
    # run crash-recovery (flipping the live server's in-flight rows) and start
    # worker threads, only to die minutes of damage later at uvicorn's bind.
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt":
            # Windows: SO_REUSEADDR lets a bind SUCCEED on a port with a live
            # listener, so the guard never fired — a second `mrfx serve` ran
            # crash-recovery against the live server before dying on the DB
            # lock. SO_EXCLUSIVEADDRUSE makes the probe honest, and Windows
            # has no POSIX TIME_WAIT bind problem so nothing is lost.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            # uvicorn binds with SO_REUSEADDR; without it here, TIME_WAIT
            # sockets from a server stopped seconds ago fail this probe and a
            # perfectly valid restart gets told "another dashboard is running"
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", cfg.port))
        probe.close()
    except OSError:
        print(f"port {cfg.port} is already in use — another mrfx dashboard (or other "
              "app) is running. Not touching its database. Stop it first, or change "
              "`port:` in config/mrfx.yaml.", file=sys.stderr)
        return 1

    # keep_warm: serve is long-lived and fires several queries per dashboard
    # click, so pin the DuckDB settings once instead of re-SETting them on every
    # request connection (~14ms each). One-shot CLI commands stay unpinned.
    store = Store(cfg.store_dir, cfg.duckdb_memory_gb, keep_warm=True, temp_dir=cfg.duckdb_temp_dir, auto_spill=True)
    # BEFORE any worker starts: a crash mid-parse leaves files at
    # 'processing', which scan_inbox skips forever. Flipping them to failed
    # here (single-threaded, nothing else owns the store yet) lets the very
    # first inbox scan re-ingest them without racing a live ingest.
    n_stuck = store.recover_stuck_files()
    if n_stuck:
        log.info("recovered %d file(s) left mid-parse by a previous run", n_stuck)
    if store.rollups_stale():
        # a kill landed between files going 'done' and their BATCHED rollup:
        # the Files tab showed them done while their rates were missing from
        # every number, and nothing on restart noticed. Catch up now, in the
        # background so the dashboard is usable meanwhile. Log the REASON so a
        # slow full rebuild (rollup_needs_full) isn't a silent multi-hour hang.
        log.info("analytics are behind the ingested files — catching up in the "
                 "background. Reason: %s", store.rollup_stale_reason())

        def _catch_up() -> None:
            # COALESCE (skip_if_busy): the URL worker may already be running its
            # own catch-up rebuild by the time this thread is scheduled. Queuing
            # behind it (blocking) is what logged "waiting for the previous
            # rebuild to finish (1000+ min)" and then ran a REDUNDANT second pass
            # over the same backlog. Skip when one is already in flight — it
            # covers the data, and rollups_stale() re-triggers if anything is
            # left. Only run our own pass when nothing else is.
            from .store import ROLLUP_SKIPPED
            try:
                if store.update_rollups_incremental(skip_if_busy=True) == ROLLUP_SKIPPED:
                    log.info("analytics catch-up: a rebuild is already running — "
                             "it covers the backlog, so skipping the duplicate "
                             "startup pass")
            except Exception as e:  # noqa: BLE001 — incremental is an
                # optimization; the full rebuild is the always-correct fallback
                log.info("incremental analytics catch-up unavailable (%s) — "
                         "running a full rebuild", e)
                store.rebuild_rollups(skip_if_busy=True)

        threading.Thread(target=_catch_up,
                         name="mrfx-rollup-catchup", daemon=True).start()
    stop = threading.Event()
    _start_stack_recorder(store.dir, stop=stop)
    threading.Thread(
        target=_watcher_loop, args=(cfg, store, stop), name="mrfx-watcher", daemon=True
    ).start()
    threading.Thread(
        target=_url_worker_loop, args=(cfg, store, stop), name="mrfx-urls", daemon=True
    ).start()
    # always-on enrichment: identifies names as extraction proceeds (covers both
    # the inbox watcher AND the URL worker), refreshing the directory
    # progressively rather than only when everything finishes.
    start_persistent_enrichment(cfg, store, stop)
    app = create_app(cfg, store)
    spill_line = ""
    if store.spill_is_relocated:
        # confirm where rollup scratch landed — an explicit duckdb_temp_dir, or
        # an auto-picked fast SSD when the store sits on a slow HDD — so the
        # user isn't guessing whether the speedup took
        how = "auto-selected (store is on a slower disk)" if store.spill_is_auto \
            else "from your config"
        spill_line = f"  rollup spill → {store._tmp_dir}  [{how}]\n"
    elif store.spill_fallback_from:
        # the configured dir was unusable — say so, or the user believes it took
        spill_line = (f"  rollup spill → {store._tmp_dir}  [your duckdb_temp_dir "
                      f"({store.spill_fallback_from}) wasn't usable — check the drive]\n")
    # say WHICH store this server opened and how much is in it — an empty count
    # against a book the user knows is big means the wrong folder/store, and
    # that must be readable at a glance, not a mystery of an empty dashboard
    try:
        with store.connect() as con:
            n_files = con.execute("SELECT count(*) FROM files").fetchone()[0] or 0
    except duckdb.Error:
        n_files = 0
    print(f"\n  MRF Explorer  →  http://localhost:{cfg.port}\n"
          f"  store: {Path(cfg.store_dir).resolve()}  ({n_files:,} file(s) ingested)\n"
          f"  inbox: {cfg.inbox_dir}  (drop .json / .json.gz / .zip here)\n"
          f"{spill_line}"
          f"  or paste MRF/TOC URLs on the Files tab — downloads run automatically\n"
          f"  if the app ever freezes: open {store.dir / 'diagnostics' / 'stacks_latest.txt'}\n"
          f"  in Notepad and send its contents (auto-refreshed every 30s)\n")
    try:
        uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="warning")
    finally:
        stop.set()
        # don't let concurrent.futures' atexit hook block process exit for
        # the remainder of a multi-hour parse: terminate workers now (their
        # pid-suffixed temps are swept as orphans on the next start)
        from .ingest import get_parse_pool

        pool = get_parse_pool()
        if pool is not None and hasattr(pool, "shutdown_now"):
            pool.shutdown_now()
    return 0


def cmd_add(cfg: MrfxConfig, args) -> int:
    """Add MRF/TOC URLs. If the dashboard is running, hand them to it (its
    background worker downloads and ingests); otherwise process them right
    here until the queue is drained."""
    urls = list(args.urls or [])
    if args.file:
        try:
            text = Path(args.file).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"could not read --file {args.file}: {e}")
            return 1
        urls += [ln.strip() for ln in text.splitlines() if ln.strip()]
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
            try:
                d = r.json()
                d["added"], d["skipped"], d["invalid"]
            except (ValueError, KeyError, TypeError):
                # something ELSE answered 200 on our port (another local app)
                print(f"a server on port {cfg.port} answered, but it doesn't "
                      "look like the mrfx dashboard — not adding locally while "
                      "it may own the database. Stop it or change `port:` in "
                      "config/mrfx.yaml, then re-run this command.")
                return 1
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
    except (_httpx.InvalidURL, ValueError) as e:
        # a malformed HTTP_PROXY/HTTPS_PROXY env var surfaces here — say so
        # instead of a traceback (downloads would hit the same wall anyway)
        print(f"could not make an HTTP request at all ({e}). If HTTP_PROXY or "
              "HTTPS_PROXY is set, check its value.")
        return 1

    from .fetch import add_urls, run_queue

    store = Store(cfg.store_dir, cfg.duckdb_memory_gb, temp_dir=cfg.duckdb_temp_dir,
                  auto_spill=True)  # drains/rebuilds rollups — rollup-heavy
    store.recover_stuck_files()  # no server owns the store — safe to recover
    counts = add_urls(store, urls)
    print(f"queued {counts['added']} URL(s) "
          f"({counts['skipped']} already known, {counts['invalid']} not URLs)")
    if args.retry_failed:
        n = store.requeue_failed()
        print(f"re-queued {n} previously failed URL(s)")
    counts_now = store.url_queue_counts()
    live = sum(counts_now.get(s, 0) for s in
               ("queued", "downloading", "fetched", "expanding", "ingesting"))
    if live == 0:
        return 0  # nothing queued and nothing left mid-flight by a crash
    from .fetch import resolve_worker_count

    n_workers = resolve_worker_count(cfg)
    print(f"downloading and ingesting ({n_workers} file(s) at a time — Ctrl-C to stop; "
          "re-running `mrfx add` resumes where it left off)...")
    processed = run_queue(cfg, store, drain=True, progress_bar=_make_cli_progress())
    counts = store.url_queue_counts()
    over = counts.get("oversize", 0)
    print(f"\nfinished: {processed} processed this run — "
          f"{counts.get('done', 0)} done, {counts.get('failed', 0)} failed, "
          + (f"{over} too big, " if over else "")
          + f"{counts.get('queued', 0)} still queued")
    for rec in store.list_urls(limit=50):
        if rec["status"] == "failed":
            print(f"  FAILED  {rec['url'][:90]}\n          → {rec['error']}")
        elif rec["status"] == "oversize":
            print(f"  TOO BIG {rec['url'][:90]}\n          → over confirm_over_gb; raise it in "
                  "config/mrfx.yaml (or use the dashboard's \"download anyway\") and re-run")
    if cfg.enrichment.mode != "off" and processed:
        print("looking up practice names (NPPES)...")
        _run_enrichment_best_effort(cfg, store)
    return 0


def _run_enrichment_best_effort(cfg: MrfxConfig, store: Store) -> None:
    """Name lookups are a bonus pass AFTER the ingest already succeeded — a
    bad bulk CSV or NPPES hiccup must not end the command with a traceback."""
    try:
        run_enrichment(cfg, store)
    except Exception as e:  # noqa: BLE001
        print(f"name enrichment hit a problem ({e}) — your rates are saved; "
              "names fill in on the next run")


def cmd_preflight(cfg: MrfxConfig, args) -> int:
    # the running dashboard holds the database open for its whole lifetime, so
    # opening the store here would just spin ~6s and fail confusingly
    if _something_owns_the_port(cfg, "opening the store for preflight"):
        return 1
    store = Store(cfg.store_dir, cfg.duckdb_memory_gb, temp_dir=cfg.duckdb_temp_dir)
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


def _something_owns_the_port(cfg: MrfxConfig, action: str) -> bool:
    """True (with a printed explanation) when the dashboard — or anything —
    answers on our port, so store-owning CLI commands must not proceed.
    trust_env=False keeps a corporate proxy from answering FOR localhost, and
    a TIMEOUT counts as 'owned': a busy mid-startup server that can't answer
    in 5s is exactly the case where opening its store would do damage."""
    import httpx as _httpx

    try:
        r = _httpx.get(f"http://localhost:{cfg.port}/api/files", timeout=5,
                       trust_env=False)
        looks_mrfx = False
        try:
            r.json()["files"]
            looks_mrfx = True
        except Exception:  # noqa: BLE001
            pass
        who = "the mrfx dashboard" if looks_mrfx else "something (not obviously mrfx)"
        print(f"{who} answered on port {cfg.port} — not {action} while it may own "
              "the database. Use the dashboard, or stop it and re-run.")
        return True
    except _httpx.TimeoutException:
        print(f"something on port {cfg.port} didn't answer within 5s — refusing to "
              f"{action} in case it's the dashboard mid-start. Re-run in a moment, "
              "or stop the server first.")
        return True
    except Exception:  # noqa: BLE001 — connection refused etc.: port is free
        return False


def cmd_ingest(cfg: MrfxConfig, args) -> int:
    # like cmd_add: never open the store while a running server owns it — and
    # especially never run crash-recovery against its live 'processing' rows
    if _something_owns_the_port(cfg, "ingesting locally"):
        return 1
    # auto_spill: a CLI ingest of a directory can trigger big rollups, so let it
    # route spill to a fast disk too (same as serve)
    store = Store(cfg.store_dir, cfg.duckdb_memory_gb, temp_dir=cfg.duckdb_temp_dir,
                  auto_spill=True)
    store.recover_stuck_files()  # crashed 'processing' rows re-ingest this pass
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
        _run_enrichment_best_effort(cfg, store)
    return 0 if all(r["status"] in ("done", "pending_confirmation") for r in results) else 1


def cmd_status(cfg: MrfxConfig, args) -> int:
    if _something_owns_the_port(cfg, "reading the store locally"):
        print("(the dashboard's Files tab shows the same information live)")
        return 1
    store = Store(cfg.store_dir, cfg.duckdb_memory_gb, temp_dir=cfg.duckdb_temp_dir)
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
    from .store import sql_path

    if _something_owns_the_port(cfg, "exporting locally"):
        return 1
    store = Store(cfg.store_dir, cfg.duckdb_memory_gb, temp_dir=cfg.duckdb_temp_dir)
    out = Path(args.out)
    sql, params = order_export_sql(args)
    # COPY to a temp, then stream BOM + bytes to the real path. Reading the
    # whole CSV into Python just to prepend the Excel BOM materialized a
    # multi-GB export (plus a concatenated copy) in RAM — the exact blowup the
    # API's streaming export path was built to avoid.
    tmp = out.with_name(out.name + ".tmp")
    n = 0
    try:
        with store.connect() as con:
            # sql_path: an output path containing an apostrophe (C:\Users\O'Brien\…)
            # must not break the COPY statement
            con.execute(f"COPY ({sql}) TO '{sql_path(tmp)}' (FORMAT CSV, HEADER)", params)
        import shutil
        with open(out, "wb") as dst, open(tmp, "rb") as src:
            dst.write(b"\xef\xbb\xbf")
            shutil.copyfileobj(src, dst, 1 << 20)
        with open(tmp, "rb") as src:  # row count without holding the file in RAM
            for chunk in iter(lambda: src.read(1 << 20), b""):
                n += chunk.count(b"\n")
        n -= 1  # header
    finally:
        tmp.unlink(missing_ok=True)
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
    print(f"exported ~{max(n, 0):,} rows -> {out}\nmethodology -> {sidecar}")
    return 0


def cmd_outreach(cfg: MrfxConfig, args) -> int:
    """One row per entity: org name + geography + per-code rate/percentile merge
    fields, for contact-list cross-referencing and mail merge (e.g. Brevo)."""
    from .api import FilterSet, grain_of, methodology_text, rel_sql
    from .outreach import build_outreach_rows, outreach_csv

    if _something_owns_the_port(cfg, "exporting outreach locally"):
        return 1
    store = Store(cfg.store_dir, cfg.duckdb_memory_gb, temp_dir=cfg.duckdb_temp_dir)
    qp = {k: v for k, v in {
        "payer": args.payer, "cpt": args.cpt, "state": args.state, "city": args.city,
        "month": args.month, "discipline": args.discipline,
        "modifier": "base" if args.base_only else None,
    }.items() if v not in (None, "")}
    grain = grain_of({"grain": args.grain} if args.grain else {}, cfg, store)
    if grain == "npi":
        grain = "tin"
    try:
        fs = FilterSet(qp)
    except ValueError as e:  # e.g. --month latest (a report-tab sentinel, not a month)
        print(f"can't build that export: {e}", file=sys.stderr)
        return 1
    headers, rows = build_outreach_rows(store, rel_sql(grain, fs), fs.params, fs.described.get("codes"))
    out = Path(args.out)
    out.write_text(outreach_csv(headers, rows), encoding="utf-8")
    sidecar = out.with_name(out.stem + "_methodology.txt")
    sidecar.write_text(methodology_text(cfg, store, grain, fs, "display_name", "asc", "outreach"))
    print(f"exported {len(rows)} entities -> {out}\nmethodology -> {sidecar}")
    print("columns:", ", ".join(headers[:14]), f"... + {len(headers) - 14} per-code merge fields")
    return 0


def cmd_forget(cfg: MrfxConfig, args) -> int:
    """Erase chosen files' data (rates + raw copies) without touching the
    rest of the store — the user-controlled counterpart to `reset`."""
    from .ingest import forget_file

    if _something_owns_the_port(cfg, "erasing files locally"):
        print("tip: while the dashboard runs, use its Files tab's remove button instead.")
        return 1
    store = Store(cfg.store_dir, cfg.duckdb_memory_gb, temp_dir=cfg.duckdb_temp_dir,
                  auto_spill=True)  # drains/rebuilds rollups — rollup-heavy
    rc = 0
    for name in args.filenames:
        st = store.file_status(name)
        if not st:
            print(f"{name}: not in the store (check `mrfx status` for exact filenames)")
            rc = 1
            continue
        if st.get("status") in ("processing", "queued") or store.url_inflight_for_filename(name):
            print(f"{name}: being processed right now — wait for it to finish, "
                  "then forget it (removing mid-parse would quietly resurrect)")
            rc = 1
            continue
        try:
            info = forget_file(cfg, store, name)
        except (RuntimeError, ValueError) as e:
            # ValueError: a path-like name ("../x") — same message the API
            # returns as a 400, printed plainly instead of a traceback
            print(f"{name}: {e}")
            rc = 1
            continue
        print(f"{name}: removed {info['rows']:,} rows, freed {info['bytes'] / 1e6:.1f} MB")
    return rc


def cmd_enrich(cfg: MrfxConfig, args) -> int:
    """Resolve NPI -> org names/geography on demand. `--bulk <NPPES zip/csv>`
    does it in one fast local pass; otherwise the mode in config/mrfx.yaml
    applies (api or bulk). Requires the dashboard to be STOPPED — the running
    server owns the database (and its own always-on enrichment loop picks up a
    configured bulk file by itself, so with serve running there's nothing this
    command adds)."""
    if _something_owns_the_port(cfg, "enriching locally"):
        print("Tip: the running dashboard identifies names on its own — with "
              "bulk_csv_path set in config/mrfx.yaml it uses your NPPES file "
              "automatically. To run this command instead, stop the server first.")
        return 1
    store = Store(cfg.store_dir, cfg.duckdb_memory_gb, temp_dir=cfg.duckdb_temp_dir,
                  auto_spill=True)  # drains/rebuilds rollups — rollup-heavy
    if getattr(args, "bulk_file", None):
        cfg.enrichment.mode = "bulk"
        cfg.enrichment.bulk_csv_path = Path(args.bulk_file)
    if cfg.enrichment.mode == "off":
        print("enrichment.mode is 'off'. Set it to 'api' or 'bulk' in config/mrfx.yaml, "
              "or pass --bulk <NPPES zip/csv>.", file=sys.stderr)
        return 1
    from .enrich import use_bulk_enrichment
    if cfg.enrichment.mode == "bulk":
        src = cfg.enrichment.bulk_csv_path
        if not src or not Path(src).exists():
            print(f"bulk file not found: {src}\nDownload the NPPES full monthly file from "
                  "https://download.cms.gov/nppes/NPI_Files.html and point --bulk (or "
                  "enrichment.bulk_csv_path) at the .zip.", file=sys.stderr)
            return 1
    if use_bulk_enrichment(cfg):
        print(f"resolving names from {cfg.enrichment.bulk_csv_path} in one local "
              "pass — this reads the file once…")
    else:
        print("resolving names via the NPPES API (can take a while for a large "
              "book; Ctrl-C is safe — it resumes).\n"
              "  Tip: to identify the whole book in ONE fast local pass, download "
              "the NPPES full monthly file from\n"
              "  https://download.cms.gov/nppes/NPI_Files.html and re-run with "
              "--bulk <file.zip> (or set enrichment.bulk_csv_path).")
    n = run_enrichment(cfg, store)
    print(f"identified {n:,} name(s).")
    return 0


def cmd_reset(cfg: MrfxConfig, args) -> int:
    if not args.confirm:
        print("refusing: pass --confirm to clear the store (processed files are kept)")
        return 1
    if _something_owns_the_port(cfg, "resetting the store"):
        return 1
    Store(cfg.store_dir, cfg.duckdb_memory_gb, temp_dir=cfg.duckdb_temp_dir).reset()
    print("store cleared. processed files remain in", cfg.processed_dir)
    return 0


def _supervise_serve(config_path: str) -> int:
    """Run `mrfx serve` in a child process, restarting on any abnormal exit.
    Stops cleanly on a normal exit (code 0, e.g. the port is taken) or on
    Ctrl-C. Backs off between restarts so a crash-on-boot can't hot-loop."""
    import subprocess
    import time as _time

    child = [sys.executable, "-m", "mrfx", "--config", config_path, "serve"]
    print("MRF Explorer supervisor: auto-restart is ON. If the server ever "
          "stops unexpectedly it will relaunch in a few seconds and resume "
          "where it left off. Press Ctrl-C (twice) to stop for good.\n",
          file=sys.stderr)
    backoff = 5
    while True:
        started = _time.monotonic()
        try:
            code = subprocess.call(child)
        except KeyboardInterrupt:
            return 0  # user stopping the whole thing
        # a server that ran healthily for a while then died is a fresh incident,
        # not a boot loop — reset the backoff so it comes right back
        if _time.monotonic() - started > 120:
            backoff = 5
        if code == 0:
            # a CLEAN exit is deliberate (Ctrl-C shutdown, or the port was
            # already in use) — do NOT relaunch, or a second dashboard would
            # fight the first forever
            print("MRF Explorer exited normally — supervisor stopping.",
                  file=sys.stderr)
            return 0
        print(f"\nMRF Explorer stopped unexpectedly (exit code {code}). "
              f"Restarting in {backoff}s — your data is safe and it resumes "
              f"where it left off. Press Ctrl-C to stop.\n", file=sys.stderr)
        try:
            _time.sleep(backoff)
        except KeyboardInterrupt:
            return 0
        # escalate the wait if it keeps dying quickly, so a persistent
        # crash-on-boot (e.g. bad config) can't spin at 100% relaunching
        backoff = min(backoff * 2, 60)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mrfx", description="MRF Explorer — drop-in payer rate dashboard")
    ap.add_argument("--config", default="config/mrfx.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="start API + dashboard + inbox watcher")
    p.add_argument("--supervise", action="store_true",
                   help="auto-restart if the server ever exits unexpectedly "
                        "(native crash, power blip) — resumes where it left off")
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
    # BooleanOptionalAction: the old store_true+default=True made the flag a
    # no-op (nothing could ever turn it off) — --no-base-only now works
    p.add_argument("--base-only", action=argparse.BooleanOptionalAction, default=True,
                   help="base-modifier rows only (default on; --no-base-only includes modified rows)")
    p = sub.add_parser("forget", help="erase chosen files' rates + raw copies (see `mrfx status` for names)")
    p.add_argument("filenames", nargs="+")
    p = sub.add_parser("enrich", help="resolve NPI names/geography now (NPPES bulk file or API)")
    p.add_argument("--bulk", dest="bulk_file", metavar="PATH",
                   help="NPPES full-file .zip (or unzipped .csv) — resolves all names in one local pass")
    p = sub.add_parser("reset", help="clear the store (keeps processed files)")
    p.add_argument("--confirm", action="store_true")

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    if args.cmd in ("serve", "ingest", "add"):
        from .parser import warn_if_slow_json_backend

        warn_if_slow_json_backend()  # loud warning if the pure-Python parser is active
    from .config import ConfigFileError

    # Wrong-folder rescue: with the DEFAULT --config, if config/mrfx.yaml isn't
    # under the current directory, fall back to the one next to the installed
    # package (editable install => the project folder). Without this, running
    # `mrfx serve` from any other directory silently created a brand-new empty
    # store there and the dashboard showed nothing — the real store was fine.
    if args.config == "config/mrfx.yaml" and not Path(args.config).exists():
        pkg_cfg = Path(__file__).resolve().parent.parent / "config" / "mrfx.yaml"
        if pkg_cfg.exists():
            log.info("no config/mrfx.yaml here — using the project's: %s", pkg_cfg)
            args.config = str(pkg_cfg)
    # Supervisor: relaunch a clean child `mrfx serve` whenever it exits
    # abnormally (a native DuckDB crash under memory pressure, a power blip).
    # The store resumes exactly where it left off (crash-recovery + rollup
    # catch-up run on every boot), so for the user a crash becomes a ~5s blip
    # instead of a dead window they must notice and restart by hand. Runs the
    # child OUT OF PROCESS so even an interpreter-level crash can't take the
    # supervisor down with it.
    if args.cmd == "serve" and getattr(args, "supervise", False):
        return _supervise_serve(args.config)

    try:
        cfg = load_mrfx_config(args.config)
        cfg.ensure_dirs()
    except ConfigFileError as e:
        print(f"config problem: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"could not create the data directories from {args.config}: {e}\n"
              "Check the *_dir settings point at usable folder paths.", file=sys.stderr)
        return 1
    handler = {
        "serve": cmd_serve,
        "add": cmd_add,
        "preflight": cmd_preflight,
        "ingest": cmd_ingest,
        "status": cmd_status,
        "export": cmd_export,
        "outreach": cmd_outreach,
        "forget": cmd_forget,
        "enrich": cmd_enrich,
        "reset": cmd_reset,
    }[args.cmd]
    try:
        return handler(cfg, args)
    except KeyboardInterrupt:
        # the FAQ's promise holds — downloads resume and in-flight files
        # recover on the next start — so say that instead of a traceback
        print("\nstopped. Your progress is saved: restart the same command "
              "(or `mrfx serve`) and it picks up where it left off.")
        return 130
    except duckdb.IOException as e:
        msg = str(e)
        if "lock" in msg.lower():
            print("another program has the data store open — usually the "
                  "dashboard (`mrfx serve`), which keeps it open for its whole "
                  "run (it will NOT free up on its own). Stop the dashboard "
                  "(Ctrl-C in its window) and re-run this command, or use the "
                  "dashboard's own buttons instead. If it's a BI tool or "
                  "another window, close that.", file=sys.stderr)
        elif "open file" in msg.lower() or "No such file" in msg:
            print(f"could not open a file: {e}\nCheck the output path exists "
                  "and is writable.", file=sys.stderr)
        else:
            print(f"database problem: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
