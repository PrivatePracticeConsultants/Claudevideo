"""NPI -> org name enrichment: NPPES API (default), local bulk CSV, or off.

Runs as a background thread after ingest; the dashboard shows raw NPIs
immediately and names fill in as rows land in `npi_directory`.
"""

from __future__ import annotations

import contextlib
import csv
import io
import logging
import threading
import time
import zipfile
from pathlib import Path

import httpx

from .config import MrfxConfig
from .store import Store

log = logging.getLogger(__name__)

NPPES_API = "https://npiregistry.cms.hhs.gov/api/"


class _EnrichFailure(Exception):
    """A transient NPPES failure for one NPI — leave it un-enriched to retry."""


def _apply_nppes_result(store: Store, npi: str, data: dict) -> None:
    """Persist one NPPES response. Raises _EnrichFailure for a transient/error
    body so the NPI is left un-enriched (never poisoned as permanently dead)."""
    if "results" not in data:
        # NPPES 200-wraps errors ({"Errors": [...]}, no "results" key). That is
        # NOT a dead NPI — writing one here would poison it as permanently
        # un-enrichable, so treat it as a retryable failure.
        raise _EnrichFailure(str((data.get("Errors") or ["unknown error"])[0]))
    results = data.get("results") or []
    if not results:
        store.save_npi(npi, None, None, None, None, None)  # genuine dead NPI: don't retry forever
        return
    r = results[0]
    basic = r.get("basic", {})
    name = basic.get("organization_name") or " ".join(
        p for p in (basic.get("first_name"), basic.get("last_name")) if p
    ) or None
    tax = next((t for t in r.get("taxonomies", []) if t.get("primary")), None) or (
        r.get("taxonomies") or [{}]
    )[0]
    addr = next(
        (a for a in r.get("addresses", []) if a.get("address_purpose") == "LOCATION"),
        (r.get("addresses") or [{}])[0],
    )
    store.save_npi(
        npi, name, tax.get("code"), tax.get("desc"),
        addr.get("city"), addr.get("state"),
        entity_type=r.get("enumeration_type"),
        address=addr.get("address_1"),
        zip_code=addr.get("postal_code"),
        phone=addr.get("telephone_number"),
    )


# surface names PROGRESSIVELY: rebuild the directory at most this often once new
# NPIs have resolved, so a long backlog (or a still-ingesting monster file) shows
# names as they come in instead of only when the whole run finishes. Throttled
# so the (minutes-long, write-locked) rebuild can't dominate a big store.
_DIRECTORY_REFRESH_SECONDS = 240.0
_refresh_lock = threading.Lock()
_last_dir_refresh = 0.0
# True when names have been saved to npi_directory but not yet materialized into
# tin_directory by a rebuild. A rebuild clears it; every enrichment cycle retries
# a throttled rebuild while it's set, so a batch whose rebuild was throttled (or
# skipped) can never stay unshown — it materializes on a later cycle.
_names_dirty = False


def _mark_dirty() -> None:
    global _names_dirty
    with _refresh_lock:
        _names_dirty = True


def _maybe_refresh_directory(store: Store, force: bool = False) -> bool:
    """Rebuild the name/geo directory, throttled GLOBALLY (across enrichment
    cycles) so the persistent serve loop — which re-polls frequently — can't
    trigger a minutes-long rebuild on every trickle of new NPIs. `force` runs it
    now regardless (the one-shot CLI path, so names show before it exits).
    Clears the dirty flag on success. Best-effort: a rebuild here is cosmetic
    and must never fail enrichment."""
    global _last_dir_refresh, _names_dirty
    with _refresh_lock:
        now = time.monotonic()
        if not force and now - _last_dir_refresh < _DIRECTORY_REFRESH_SECONDS:
            return False
        _last_dir_refresh = now
    try:
        store.rebuild_rollups()
        with _refresh_lock:
            _names_dirty = False
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("name-directory refresh skipped (%s)", e)
        return False


def enrich_via_api(cfg: MrfxConfig, store: Store, stop: threading.Event | None = None) -> int:
    """Polite CONCURRENT NPPES lookups for every un-enriched NPI in the store,
    so names/states fill in quickly on a large book instead of one-every-0.15s.
    Poisoning guards are preserved: a transient/error response leaves the NPI
    un-enriched to retry; only a genuine empty result marks it dead. A persistent
    failure run trips a circuit breaker and pauses until the next run."""
    import concurrent.futures

    from .fetch import ssl_verify

    workers = max(1, int(getattr(cfg.enrichment, "api_concurrency", 8)))
    done = 0
    lock = threading.Lock()
    recent_failures = 0  # trips the circuit breaker when NPPES is persistently down
    aborted = False

    with httpx.Client(headers={"User-Agent": cfg.user_agent}, timeout=30,
                      verify=ssl_verify()) as client:

        def lookup(npi: str) -> None:
            resp = client.get(NPPES_API, params={"version": "2.1", "number": npi})
            if resp.status_code == 429:
                time.sleep(10)
                resp = client.get(NPPES_API, params={"version": "2.1", "number": npi})
            if resp.status_code != 200:
                raise _EnrichFailure(f"HTTP {resp.status_code}")
            _apply_nppes_result(store, npi, resp.json())

        done_at_refresh = 0
        while not aborted:
            if stop is not None and stop.is_set():
                break
            batch = store.unenriched_npis(limit=200)
            if not batch:
                break
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(lookup, npi): npi for npi in batch}
                for fut in concurrent.futures.as_completed(futs):
                    npi = futs[fut]
                    try:
                        fut.result()
                    except concurrent.futures.CancelledError:
                        continue  # cancelled below after abort/stop
                    except (_EnrichFailure, httpx.HTTPError, ValueError) as e:
                        log.warning("NPPES lookup failed for %s: %s — will retry next run", npi, e)
                        with lock:
                            recent_failures += 1
                            # threshold scales with concurrency: a whole batch of
                            # in-flight requests can be failing before we react
                            if recent_failures >= 20 + workers:
                                aborted = True
                    else:
                        with lock:
                            recent_failures = 0
                            done += 1
                    if aborted or (stop is not None and stop.is_set()):
                        # don't grind through the rest of a 200-NPI batch of
                        # 30s timeouts against a dead NPPES (or a stop request)
                        # — drop everything not yet running; the ≤`workers`
                        # in-flight requests finish on their own timeouts
                        ex.shutdown(wait=False, cancel_futures=True)
                        break
            if stop is not None and stop.is_set():
                break
            # progressive refresh: materialize the names resolved so far into the
            # directory that powers the dashboard (globally throttled).
            if done > done_at_refresh:
                _mark_dirty()
                if _maybe_refresh_directory(store):
                    log.info("names refreshed (%d resolved so far)", done)
                    done_at_refresh = done
            if aborted:
                log.warning("NPPES failing persistently — pausing enrichment until the "
                            "next run (%d NPIs done)", done)
    if done:
        log.info("enriched %d NPIs via NPPES API (%d-way concurrent)", done, workers)
    return done


# NPPES bulk Data Dissemination CSV column names
_BULK_COLS = {
    "npi": "NPI",
    "org": "Provider Organization Name (Legal Business Name)",
    "first": "Provider First Name",
    "last": "Provider Last Name (Legal Name)",
    "city": "Provider Business Practice Location Address City Name",
    "state": "Provider Business Practice Location Address State Name",
    "tax1": "Healthcare Provider Taxonomy Code_1",
    "entity": "Entity Type Code",
    "address": "Provider First Line Business Practice Location Address",
    "zip": "Provider Business Practice Location Address Postal Code",
    "phone": "Provider Business Practice Location Address Telephone Number",
}


@contextlib.contextmanager
def _open_bulk_text(path: Path):
    """Yield a text stream over the NPPES bulk CSV. Accepts the raw monthly ZIP
    (streams the big `npidata_pfile_*.csv` member directly — no 10 GB manual
    unzip) or a plain .csv. The member is picked as the largest npidata_pfile
    CSV that is NOT the small *_fileheader.csv."""
    p = Path(path)
    if p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as zf:
            csvs = [m for m in zf.infolist() if m.filename.lower().endswith(".csv")]
            main = [m for m in csvs if "npidata_pfile" in m.filename.lower()
                    and "fileheader" not in m.filename.lower()]
            pick = max(main or csvs, key=lambda m: m.file_size, default=None)
            if pick is None:
                raise ValueError(f"no CSV found inside {p.name}")
            log.info("bulk: reading %s (%.1f GB) from %s",
                     pick.filename, pick.file_size / 1e9, p.name)
            with zf.open(pick) as raw:
                yield io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
    else:
        with open(p, newline="", encoding="utf-8", errors="replace") as f:
            yield f


# change-awareness for the persistent serve loop: after a full scan we remember
# the file's signature and the NPIs it does NOT contain (deactivated/new/junk
# ids that would otherwise force a fresh multi-GB scan on every poll forever).
_bulk_sig: tuple | None = None
_bulk_absent: set[str] = set()
# consecutive COMPLETE-read failures for the current signature. A genuinely
# corrupt/truncated file would otherwise be re-read in full every poll forever
# (a scan that raises never accumulates _bulk_absent); after this many failures
# we give up on that exact file until it changes (a re-download changes the sig
# and resets everything).
_bulk_read_failures = 0
_BULK_MAX_READ_FAILURES = 3


def enrich_via_bulk(cfg: MrfxConfig, store: Store,
                    stop: threading.Event | None = None) -> int:
    """Stream the NPPES bulk file once, filling every un-enriched NPI it covers.
    Accepts the raw monthly ZIP or an unzipped CSV. Saves in batches so hundreds
    of thousands of NPIs resolve in minutes, not hours of per-row commits.

    Change-aware: skips the (multi-GB) scan entirely when the file is unchanged
    and the only remaining un-enriched NPIs are ones this file already proved it
    doesn't contain — otherwise a handful of deactivated/junk NPIs would make the
    persistent loop re-read the whole file every cycle for nothing."""
    global _bulk_sig, _bulk_absent, _bulk_read_failures
    path = cfg.enrichment.bulk_csv_path
    if not path or not Path(path).exists():
        log.error("enrichment.mode=bulk but bulk_csv_path %r not found", str(path))
        return 0
    try:
        st = Path(path).stat()
        sig = (str(path), int(st.st_mtime), st.st_size)
    except OSError:
        sig = (str(path), 0, 0)
    if sig != _bulk_sig:  # new file (or first run): everything is fresh again
        _bulk_sig = sig
        _bulk_absent = set()
        _bulk_read_failures = 0
    if _bulk_read_failures >= _BULK_MAX_READ_FAILURES:
        # gave up on this exact (unreadable) file; wait for it to change
        return 0

    wanted = set()
    cursor = ""
    while True:
        # keyset pagination: nothing is saved between pages, so without the
        # cursor every page would be identical (an infinite loop at >=100k)
        batch = store.unenriched_npis(limit=100000, after=cursor)
        if not batch:
            break
        wanted.update(batch)
        cursor = batch[-1]
        if len(batch) < 100000:
            break
    wanted -= _bulk_absent  # don't re-scan the file for ids it already lacked
    if not wanted:
        return 0  # nothing NEW to find in this unchanged file
    done = 0
    pending: list[dict] = []

    def flush():
        nonlocal done
        if pending:
            store.save_npis_bulk(pending)
            done += len(pending)
            pending.clear()
            _mark_dirty()

    completed = False
    try:
        with _open_bulk_text(path) as f:
            # Positional csv.reader, NOT DictReader: the NPPES pfile has ~330
            # columns and ~8M rows but we keep only ~11 fields for a few thousand
            # NPIs. DictReader builds a full ~330-key dict for EVERY row before we
            # discard 99.9% of them — the dominant cost on a 10 GB file. Reading
            # the header once and indexing by position skips that entirely.
            reader = csv.reader(f)
            header = next(reader, [])
            col = {k: header.index(v) for k, v in _BULK_COLS.items() if v in header}
            i_npi = col.get("npi")
            if i_npi is None:  # not the npidata pfile / unexpected layout
                raise ValueError("NPPES file has no 'NPI' column — wrong file inside the zip?")
            i_org, i_first, i_last = col.get("org"), col.get("first"), col.get("last")
            i_ent, i_tax = col.get("entity"), col.get("tax1")
            i_city, i_state = col.get("city"), col.get("state")
            i_addr, i_zip, i_phone = col.get("address"), col.get("zip"), col.get("phone")

            def at(row, ix):  # tolerate short/ragged rows; only called on matches
                return (row[ix] if ix is not None and ix < len(row) else None) or None

            for i, row in enumerate(reader):
                if stop is not None and (i & 0x3FFF) == 0 and stop.is_set():
                    break  # interruptible: don't finish a 10 GB scan on shutdown
                npi = row[i_npi] if i_npi < len(row) else ""
                if npi not in wanted:
                    continue
                name = at(row, i_org) or " ".join(
                    p for p in (at(row, i_first), at(row, i_last)) if p
                ) or None
                entity = {"1": "NPI-1", "2": "NPI-2"}.get(str(at(row, i_ent) or "").strip())
                pending.append({
                    "npi": npi, "org_name": name, "entity_type": entity,
                    "taxonomy_code": at(row, i_tax),
                    "city": at(row, i_city), "state": at(row, i_state),
                    "address": at(row, i_addr), "zip": at(row, i_zip),
                    "phone": at(row, i_phone),
                })
                wanted.discard(npi)
                if len(pending) >= 5000:
                    flush()
                if not wanted:
                    break
            flush()
            completed = stop is None or not stop.is_set()
    except Exception as e:  # noqa: BLE001 — never crash enrichment; keep matches
        _bulk_read_failures += 1
        log.error("bulk enrichment could not fully read %s (attempt %d/%d): %s",
                  path, _bulk_read_failures, _BULK_MAX_READ_FAILURES, e)
        flush()  # retain whatever we already matched
    if completed:
        _bulk_read_failures = 0  # a clean read clears the give-up counter
        # A COMPLETE scan proves the ids still in `wanted` are absent from this
        # file. Two things follow:
        #  1. remember them so the next cycle doesn't re-read the whole file, and
        #  2. write them as no-name dead rows — exactly what the API path does for
        #     an empty NPPES result — so they count as PROCESSED and the dashboard
        #     "identifying N more…" banner can actually reach zero. Without this a
        #     single junk/deactivated NPI from a messy MRF wedges the banner on
        #     forever even though bulk enrichment is genuinely finished.
        _bulk_absent |= wanted
        absent = list(wanted)
        for j in range(0, len(absent), 5000):
            store.save_npis_bulk([{"npi": n} for n in absent[j:j + 5000]])
    log.info("enriched %d NPIs from the bulk file", done)
    return done


def run_enrichment(cfg: MrfxConfig, store: Store, stop: threading.Event | None = None,
                   final_refresh: bool = True) -> int:
    mode = cfg.enrichment.mode
    if mode == "off":
        return 0
    done = enrich_via_bulk(cfg, store, stop) if mode == "bulk" else enrich_via_api(cfg, store, stop)
    # tin_directory materializes NPPES names/states/cities — without a rebuild,
    # geographic benchmarks (state/city joins) silently run against NULLs. The
    # dirty flag (set by the save paths) means this rebuilds iff there are names
    # not yet materialized — so a tail batch whose own refresh was throttled is
    # picked up here or on a later cycle, and a run with nothing new does no
    # rebuild. `force` (CLI one-shot) bypasses the throttle so names show before
    # the command returns; serve passes final_refresh=False and the throttle
    # governs the cadence.
    if _names_dirty:
        _maybe_refresh_directory(store, force=final_refresh)
    return done


def start_persistent_enrichment(cfg: MrfxConfig, store: Store,
                                stop: threading.Event) -> None:
    """Long-lived enrichment for `serve`: keeps draining newly-ingested NPIs and
    refreshing names for the whole server lifetime, so identification tracks
    extraction instead of waiting for it to finish.

    The old fire-on-trigger model only ran at startup and after INBOX scans, so
    files pulled by the URL worker (pasted payer links) were never enriched —
    names sat at 0 until something touched the inbox. This single always-on
    loop covers every ingestion source: it drains what's there, then re-polls."""
    if cfg.enrichment.mode == "off":
        return

    def _loop():
        first = True
        while not stop.is_set():
            try:
                # Force the rebuild on the FIRST cycle so names materialize
                # promptly on boot regardless of the monotonic-clock value the
                # throttle happens to start from (bulk mode resolves the whole
                # book in one pass, then would wait a full poll to show it).
                # Later cycles pass False so the global throttle governs cadence
                # and a poll of trickling NPIs can't rebuild every time.
                run_enrichment(cfg, store, stop, final_refresh=first)
                first = False
            except Exception:  # noqa: BLE001 — a bad cycle must not kill the loop
                log.exception("enrichment cycle failed; retrying after a pause")
            # bulk mode re-reads a multi-GB file each pass, so poll far less
            # often (the NPPES file is static for a month; change-awareness skips
            # scans with nothing new); the per-NPI API path can poll frequently.
            wait = (_BULK_POLL_SECONDS if cfg.enrichment.mode == "bulk"
                    else _ENRICH_POLL_SECONDS)
            stop.wait(wait)

    threading.Thread(target=_loop, name="mrfx-enrich", daemon=True).start()


# how often the persistent loop re-checks for newly-ingested NPIs once drained
_ENRICH_POLL_SECONDS = 20.0
# bulk mode scans a multi-GB file, so poll on the order of tens of minutes
_BULK_POLL_SECONDS = 1800.0
