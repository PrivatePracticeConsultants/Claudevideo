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


def _maybe_refresh_directory(store: Store, force: bool = False) -> bool:
    """Rebuild the name/geo directory, throttled GLOBALLY (across enrichment
    cycles) so the persistent serve loop — which re-polls every ~20s — can't
    trigger a minutes-long rebuild on every trickle of new NPIs. `force` runs it
    now regardless (the one-shot CLI path, so names show before it exits).
    Best-effort: a rebuild here is cosmetic and must never fail enrichment."""
    global _last_dir_refresh
    with _refresh_lock:
        now = time.monotonic()
        if not force and now - _last_dir_refresh < _DIRECTORY_REFRESH_SECONDS:
            return False
        _last_dir_refresh = now
    try:
        store.rebuild_rollups()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("name-directory refresh skipped (%s)", e)
        return False


def _recently_refreshed(within: float = 30.0) -> bool:
    with _refresh_lock:
        return time.monotonic() - _last_dir_refresh < within


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
            if done > done_at_refresh and _maybe_refresh_directory(store):
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


def enrich_via_bulk(cfg: MrfxConfig, store: Store) -> int:
    """Stream the NPPES bulk file once, filling every un-enriched NPI it covers.
    Accepts the raw monthly ZIP or an unzipped CSV. Saves in batches so hundreds
    of thousands of NPIs resolve in minutes, not hours of per-row commits."""
    path = cfg.enrichment.bulk_csv_path
    if not path or not Path(path).exists():
        log.error("enrichment.mode=bulk but bulk_csv_path %r not found", str(path))
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
    if not wanted:
        return 0
    done = 0
    pending: list[dict] = []

    def flush():
        nonlocal done
        if pending:
            store.save_npis_bulk(pending)
            done += len(pending)
            pending.clear()

    try:
        with _open_bulk_text(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                npi = row.get(_BULK_COLS["npi"], "")
                if npi not in wanted:
                    continue
                name = row.get(_BULK_COLS["org"]) or " ".join(
                    p for p in (row.get(_BULK_COLS["first"]), row.get(_BULK_COLS["last"])) if p
                ) or None
                entity = {"1": "NPI-1", "2": "NPI-2"}.get(str(row.get(_BULK_COLS["entity"], "")).strip())
                pending.append({
                    "npi": npi, "org_name": name, "entity_type": entity,
                    "taxonomy_code": row.get(_BULK_COLS["tax1"]) or None,
                    "city": row.get(_BULK_COLS["city"]) or None,
                    "state": row.get(_BULK_COLS["state"]) or None,
                    "address": row.get(_BULK_COLS["address"]) or None,
                    "zip": row.get(_BULK_COLS["zip"]) or None,
                    "phone": row.get(_BULK_COLS["phone"]) or None,
                })
                wanted.discard(npi)
                if len(pending) >= 5000:
                    flush()
                if not wanted:
                    break
            flush()
    except (OSError, zipfile.BadZipFile, ValueError) as e:
        log.error("bulk enrichment could not read %s: %s", path, e)
        flush()  # keep whatever we already matched
    log.info("enriched %d NPIs from the bulk file", done)
    return done


def run_enrichment(cfg: MrfxConfig, store: Store, stop: threading.Event | None = None,
                   final_refresh: bool = True) -> int:
    mode = cfg.enrichment.mode
    if mode == "off":
        return 0
    done = enrich_via_bulk(cfg, store) if mode == "bulk" else enrich_via_api(cfg, store, stop)
    if done:
        # tin_directory materializes NPPES names/states/cities — without a
        # rebuild, geographic benchmarks (state/city joins) silently run against
        # NULLs until the next ingest triggers one. `force` for the one-shot CLI
        # path so names show before it returns; the persistent serve loop passes
        # final_refresh=False and relies on the global throttle so a 20s poll of
        # trickling NPIs can't rebuild every cycle. Skip the forced rebuild if a
        # progressive one JUST ran (the api path refreshes mid-drain) — otherwise
        # the CLI pays two back-to-back minutes-long rebuilds.
        _maybe_refresh_directory(store, force=final_refresh and not _recently_refreshed())
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
        while not stop.is_set():
            try:
                # final_refresh=False: the global throttle governs rebuilds, so a
                # 20s poll of trickling NPIs doesn't rebuild the store every cycle
                run_enrichment(cfg, store, stop, final_refresh=False)
            except Exception:  # noqa: BLE001 — a bad cycle must not kill the loop
                log.exception("enrichment cycle failed; retrying after a pause")
            stop.wait(_ENRICH_POLL_SECONDS)  # re-check for NPIs ingested since

    threading.Thread(target=_loop, name="mrfx-enrich", daemon=True).start()


# how often the persistent loop re-checks for newly-ingested NPIs once drained
_ENRICH_POLL_SECONDS = 20.0
