"""NPI -> org name enrichment: NPPES API (default), local bulk CSV, or off.

Runs as a background thread after ingest; the dashboard shows raw NPIs
immediately and names fill in as rows land in `npi_directory`.
"""

from __future__ import annotations

import csv
import logging
import threading
import time
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


def enrich_via_bulk(cfg: MrfxConfig, store: Store) -> int:
    """Stream the NPPES bulk CSV once, filling every un-enriched NPI it covers."""
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
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            npi = row.get(_BULK_COLS["npi"], "")
            if npi not in wanted:
                continue
            name = row.get(_BULK_COLS["org"]) or " ".join(
                p for p in (row.get(_BULK_COLS["first"]), row.get(_BULK_COLS["last"])) if p
            ) or None
            entity = {"1": "NPI-1", "2": "NPI-2"}.get(str(row.get(_BULK_COLS["entity"], "")).strip())
            store.save_npi(
                npi, name, row.get(_BULK_COLS["tax1"]) or None, None,
                row.get(_BULK_COLS["city"]) or None, row.get(_BULK_COLS["state"]) or None,
                entity_type=entity,
                address=row.get(_BULK_COLS["address"]) or None,
                zip_code=row.get(_BULK_COLS["zip"]) or None,
                phone=row.get(_BULK_COLS["phone"]) or None,
            )
            done += 1
            wanted.discard(npi)
            if not wanted:
                break
    log.info("enriched %d NPIs from bulk CSV", done)
    return done


def run_enrichment(cfg: MrfxConfig, store: Store, stop: threading.Event | None = None) -> int:
    mode = cfg.enrichment.mode
    if mode == "off":
        return 0
    done = enrich_via_bulk(cfg, store) if mode == "bulk" else enrich_via_api(cfg, store, stop)
    if done:
        # tin_directory materializes NPPES names/states/cities — without a
        # rebuild here, geographic benchmarks (state/city joins) silently run
        # against NULLs until the next ingest happens to trigger one
        try:
            store.rebuild_rollups()
        except Exception:  # noqa: BLE001 — names are saved; directory refreshes
            # on the next successful rebuild
            log.exception("rollup refresh after enrichment failed — new names "
                          "appear after the next rebuild")
    return done


_ENRICH_LOCK = threading.Lock()
_ENRICH_ACTIVE = False


def start_background_enrichment(cfg: MrfxConfig, store: Store) -> threading.Event:
    """Fire-and-forget enrichment thread; returns its stop event. Single
    flight: the watcher calls this after EVERY scan with new files, and with
    a long NPPES backlog N stacked threads would all fetch the same batch —
    duplicate lookups, politeness sleep divided by N, 429s."""
    global _ENRICH_ACTIVE
    stop = threading.Event()
    if cfg.enrichment.mode == "off":
        return stop
    with _ENRICH_LOCK:
        if _ENRICH_ACTIVE:
            return stop  # a run is already grinding the same backlog
        _ENRICH_ACTIVE = True

    def _loop():
        global _ENRICH_ACTIVE
        try:
            run_enrichment(cfg, store, stop)
        except Exception:  # noqa: BLE001
            log.exception("enrichment thread died; names stay un-enriched until next run")
        finally:
            with _ENRICH_LOCK:
                _ENRICH_ACTIVE = False

    threading.Thread(target=_loop, name="mrfx-enrich", daemon=True).start()
    return stop
