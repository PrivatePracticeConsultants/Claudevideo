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


def enrich_via_api(cfg: MrfxConfig, store: Store, stop: threading.Event | None = None) -> int:
    """Sequential, polite NPPES lookups for every un-enriched NPI in the store."""
    done = 0
    with httpx.Client(headers={"User-Agent": cfg.user_agent}, timeout=30) as client:
        while True:
            batch = store.unenriched_npis(limit=200)
            if not batch:
                break
            for npi in batch:
                if stop is not None and stop.is_set():
                    return done
                try:
                    resp = client.get(NPPES_API, params={"version": "2.1", "number": npi})
                    if resp.status_code == 429:
                        time.sleep(10)
                        resp = client.get(NPPES_API, params={"version": "2.1", "number": npi})
                    data = resp.json()
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("NPPES lookup failed for %s: %s — will retry next run", npi, e)
                    time.sleep(2)
                    continue
                results = data.get("results") or []
                if not results:
                    store.save_npi(npi, None, None, None, None, None)  # dead NPI: don't retry forever
                    continue
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
                done += 1
                time.sleep(0.15)  # politeness
    if done:
        log.info("enriched %d NPIs via NPPES API", done)
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
    while True:
        batch = store.unenriched_npis(limit=100000)
        if not batch:
            break
        wanted.update(batch)
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
    if mode == "bulk":
        return enrich_via_bulk(cfg, store)
    return enrich_via_api(cfg, store, stop)


def start_background_enrichment(cfg: MrfxConfig, store: Store) -> threading.Event:
    """Fire-and-forget enrichment thread; returns its stop event."""
    stop = threading.Event()
    if cfg.enrichment.mode == "off":
        return stop

    def _loop():
        try:
            run_enrichment(cfg, store, stop)
        except Exception:  # noqa: BLE001
            log.exception("enrichment thread died; names stay un-enriched until next run")

    threading.Thread(target=_loop, name="mrfx-enrich", daemon=True).start()
    return stop
