"""Brand-new therapy clinics: the earliest lead this data can produce.

An NPI carries the date it was ISSUED. A therapy clinic enumerated last month
has, by definition, no payer contracts yet — it is about to sign whatever the
first payer offers, which is exactly the moment a rate consultant is worth the
most. Every other lead signal in this app finds practices AFTER they signed:
below-market rates, new-to-network, expiring contracts. This one finds them
before.

The data is already local. Bulk enrichment (`enrichment.mode: bulk`) converts
the NPPES monthly full file into a compact parquet next to the store; this
module reads the enumeration date out of that same cache, so the feed costs one
extra column, not a second multi-gigabyte download.

HONESTY:
- An NPI is not a business. A new NPI can be a relocation, a re-organisation,
  or a solo therapist who will never have a contract to negotiate. The feed
  says "newly enumerated", never "newly opened".
- Enumeration date is the NPI's issue date, NOT an open-for-business date.
- A practice already in this store's rate files is flagged `in_store`: it has
  published contracts already, so it is a different (and later) kind of lead.
- ZIPs with no Census centroid cannot be distance-filtered; they are reported
  as `unplaced` rather than silently dropped, matching the referral leaderboard.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

from .catalog import therapy_taxonomy_sql
from .medicare import haversine_miles_sql, load_centroids
from .states import state_code_or_none
from .store import Store, sql_path

log = logging.getLogger(__name__)


class NppesFeedError(Exception):
    """The feed cannot answer — say why, never return a misleading empty list."""


FEED_NOTE = (
    "'Newly enumerated' means the NPI was ISSUED in this window — the date NPPES "
    "records, not an opening date. A new NPI can also be a relocation, a "
    "re-organisation, or a solo clinician. Practices already carrying published "
    "rates in your store are marked, because they have contracts and are a "
    "later-stage lead."
)


def cache_path(store: Store) -> Path:
    return Path(store.dir) / "nppes_cache.parquet"


def feed_status(store: Store) -> dict:
    """Whether the new-clinic feed can run, and on what. Never raises: the
    dashboard asks this on load."""
    p = cache_path(store)
    out = {"ready": False, "rows": 0, "with_dates": 0, "newest": None,
           "path": str(p), "note": FEED_NOTE, "reason": None}
    if not p.exists():
        out["reason"] = (
            "the NPPES bulk file has not been converted yet — set "
            "enrichment.mode: bulk (and bulk_csv_path) in config/mrfx.yaml, or "
            "run `mrfx enrich --bulk <NPPES zip>` once.")
        return out
    import duckdb
    try:
        con = duckdb.connect()
        try:
            n, dated, newest = con.execute(
                f"SELECT count(*), count(enumeration_date), max(enumeration_date) "
                f"FROM read_parquet('{sql_path(p)}')").fetchone()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 — an old cache has no such column
        out["reason"] = (
            "the local NPPES cache predates enumeration dates — it rebuilds "
            f"itself on the next enrichment run ({e.__class__.__name__}).")
        return out
    out.update({"rows": n, "with_dates": dated, "newest": newest,
                "ready": bool(dated)})
    if not dated:
        out["reason"] = ("this NPPES file carries no enumeration dates — the "
                         "full monthly file does; a trimmed export may not.")
    return out


CLOSURE_NOTE = (
    "A DEACTIVATED NPI is not proof a practice closed. NPPES deactivates for "
    "paperwork lapses, mergers, re-enumerations and death as well as closures, "
    "and it deactivates on the date the registry was updated, not the date the "
    "doors shut. NPIs later REACTIVATED are excluded — those never closed. "
    "Treat a row as a lead to verify with a phone call, never as a fact to put "
    "in a client deliverable."
)


def _parsed_date(col: str) -> str:
    """NPPES writes MM/DD/YYYY; a trimmed export may already be ISO. Try both.
    An unreadable date must EXCLUDE the row from a recency filter rather than
    silently pass as recent."""
    return (f"coalesce(try_strptime({col}, '%m/%d/%Y'), "
            f"try_strptime({col}, '%Y-%m-%d'))")


def closure_status(store: Store) -> dict:
    """Whether the closure feed can run. Never raises — the dashboard asks on
    load, and a cache built before the deactivation columns existed is a
    'rebuild pending' answer, not an error."""
    p = cache_path(store)
    out = {"ready": False, "rows": 0, "with_dates": 0, "newest": None,
           "path": str(p), "note": CLOSURE_NOTE, "reason": None}
    if not p.exists():
        out["reason"] = (
            "the NPPES bulk file has not been converted yet — set "
            "enrichment.mode: bulk (and bulk_csv_path) in config/mrfx.yaml, or "
            "run `mrfx enrich --bulk <NPPES zip>` once.")
        return out
    import duckdb
    try:
        con = duckdb.connect()
        try:
            n, dated, newest = con.execute(
                "SELECT count(*), count(deactivation_date), "
                f"max({_parsed_date('deactivation_date')}) "
                f"FROM read_parquet('{sql_path(p)}')").fetchone()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 — a cache built before these columns
        out["reason"] = (
            "the local NPPES cache predates deactivation dates — it rebuilds "
            f"itself on the next enrichment run ({e.__class__.__name__}).")
        return out
    out.update({"rows": n, "with_dates": dated,
                "newest": str(newest)[:10] if newest else None,
                "ready": bool(dated)})
    if not dated:
        out["reason"] = (
            "this NPPES file carries no deactivation dates — the full monthly "
            "file does; a trimmed export may not.")
    return out


def closures(store: Store, zip_code: str | None = None,
             radius_miles: float | None = None, days: int = 365,
             therapy_only: bool = True, limit: int = 200,
             centroids_path=None, state: str | None = None) -> dict:
    """Therapy NPIs DEACTIVATED in the last `days` and not since reactivated.

    Two consulting uses from one list: a closed REFERRAL SOURCE is a hole in a
    client's inbound volume they should hear about from their consultant first,
    and a closed COMPETITOR is a market that just got less crowded. Practices
    that carried published rates in this store are flagged `in_store` — those
    are the ones whose closure actually moves a local market.
    """
    p = cache_path(store)
    st = closure_status(store)
    if not st["ready"]:
        return {"rows": [], "total": 0, "unplaced": 0, "zip": None,
                "radius_miles": None, "days": days, "reason": st["reason"],
                "note": CLOSURE_NOTE}
    try:
        days = max(1, min(int(days), 3650))
    except (TypeError, ValueError):
        raise NppesFeedError("'days' must be a number")
    limit = max(1, min(int(limit), 1000))
    zip_code = (zip_code or "").strip()[:5] or None
    if zip_code and not re.fullmatch(r"\d{5}", zip_code):
        raise NppesFeedError(f"'{zip_code}' is not a 5-digit ZIP code")
    state = state_code_or_none(state)
    use_radius = bool(zip_code and radius_miles)
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()

    reader = f"read_parquet('{sql_path(p)}')"
    therapy = "TRUE"
    if therapy_only:
        with store.connect() as _c:
            has_all = "taxonomy_codes" in {
                r[0] for r in _c.execute(f"DESCRIBE SELECT * FROM {reader}").fetchall()}
        therapy = therapy_taxonomy_sql(
            "n.taxonomy_code", all_col="n.taxonomy_codes" if has_all else None)
    deact = _parsed_date("n.deactivation_date")
    react = _parsed_date("n.reactivation_date")
    where = [f"{deact} >= CAST(? AS TIMESTAMP)", therapy,
             # reactivated AFTER the deactivation = back in business, not closed
             f"({react} IS NULL OR {react} < {deact})"]
    params: list = [since]
    if state:
        where.append("upper(trim(n.state)) = ?")
        params.append(state)

    with store.connect() as con:
        origin = None
        if use_radius:
            load_centroids(con, centroids_path)
            origin = con.execute("SELECT lat, lon FROM _zcta WHERE zip = ?",
                                 [zip_code]).fetchone()
            if origin is None:
                raise NppesFeedError(
                    f"ZIP {zip_code} is not in the Census ZCTA centroid list")
        window = limit * 5 if use_radius else limit
        rows = con.execute(f"""
            SELECT n.npi, n.org_name, n.entity_type, n.taxonomy_code,
                   n.city, n.state, lpad(substr(trim(n.zip), 1, 5), 5, '0') AS zip,
                   n.phone, n.address,
                   CAST({deact} AS DATE) AS deactivated
            FROM {reader} n
            WHERE {' AND '.join(where)}
            ORDER BY deactivated DESC
            LIMIT {window}
        """, params).fetchall()
        capped = len(rows) >= window
        cols = ["npi", "org_name", "entity_type", "taxonomy_code", "city",
                "state", "zip", "phone", "address", "deactivated"]
        out = [dict(zip(cols, r)) for r in rows]

        unplaced = 0
        if use_radius and out:
            dist = dict(con.execute(f"""
                SELECT z.zip, {haversine_miles_sql('z.lat', 'z.lon',
                                                   origin[0], origin[1])}
                FROM _zcta z WHERE z.zip IN (SELECT unnest(?::VARCHAR[]))
            """, [sorted({r["zip"] for r in out if r["zip"]})]).fetchall())
            kept = []
            for r in out:
                d = dist.get(r["zip"]) if r["zip"] else None
                if d is None:
                    unplaced += 1
                    continue
                if d > float(radius_miles):
                    continue
                r["miles"] = round(d, 1)
                kept.append(r)
            out = kept
        if out:
            known = {n for (n,) in con.execute(
                "SELECT DISTINCT npi FROM rates WHERE npi IN "
                "(SELECT unnest(?::VARCHAR[]))",
                [[r["npi"] for r in out]]).fetchall()}
            for r in out:
                r["in_store"] = r["npi"] in known
    for r in out:
        r["deactivated"] = str(r["deactivated"])[:10]
        r["taxonomy"] = _label(r.get("taxonomy_code"))
    out.sort(key=lambda r: (r["deactivated"], r.get("org_name") or ""), reverse=True)
    note = CLOSURE_NOTE
    if capped:
        note += (f" Only the {window:,} most recently deactivated NPIs were "
                 "scanned, so older matches in this window may not be listed — "
                 "narrow the look-back or the area to see all of them.")
    return {"rows": out[:limit], "total": len(out), "unplaced": unplaced,
            "zip": zip_code, "radius_miles": radius_miles if use_radius else None,
            "days": days, "state": state, "since": since, "reason": None,
            "capped": capped, "note": note}


def new_enumerations(store: Store, zip_code: str | None = None,
                     radius_miles: float | None = None, days: int = 180,
                     therapy_only: bool = True, limit: int = 200,
                     centroids_path=None, state: str | None = None) -> dict:
    """Therapy NPIs issued in the last `days`, optionally clipped to a radius.

    Reads the local NPPES parquet directly — no API, no network. Sorted newest
    first, so the top of the list is the freshest lead."""
    p = cache_path(store)
    st = feed_status(store)
    if not st["ready"]:
        return {"rows": [], "total": 0, "unplaced": 0, "zip": None,
                "radius_miles": None, "days": days, "reason": st["reason"],
                "note": FEED_NOTE}
    try:
        days = max(1, min(int(days), 3650))
    except (TypeError, ValueError):
        raise NppesFeedError("'days' must be a number")
    limit = max(1, min(int(limit), 1000))
    zip_code = (zip_code or "").strip()[:5] or None
    if zip_code and not re.fullmatch(r"\d{5}", zip_code):
        raise NppesFeedError(f"'{zip_code}' is not a 5-digit ZIP code")
    state = state_code_or_none(state)
    use_radius = bool(zip_code and radius_miles)
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()

    reader = f"read_parquet('{sql_path(p)}')"
    # A cache written before the all-taxonomies column existed has only the
    # primary; classify on whatever it actually carries rather than binder-
    # erroring (it rebuilds itself on the next enrichment pass, because the
    # cache schema stamp changed).
    therapy = "TRUE"
    if therapy_only:
        with store.connect() as _c:
            has_all = "taxonomy_codes" in {
                r[0] for r in _c.execute(f"DESCRIBE SELECT * FROM {reader}").fetchall()}
        therapy = therapy_taxonomy_sql(
            "n.taxonomy_code", all_col="n.taxonomy_codes" if has_all else None)
    # NPPES writes MM/DD/YYYY; a trimmed export may already be ISO. Try both and
    # keep whichever parses — a date we cannot read must exclude the row from a
    # *recency* filter rather than silently pass it as "new".
    parsed = ("coalesce(try_strptime(n.enumeration_date, '%m/%d/%Y'), "
              "try_strptime(n.enumeration_date, '%Y-%m-%d'))")
    where = [f"{parsed} >= CAST(? AS TIMESTAMP)", therapy]
    params: list = [since]
    if state:
        where.append("upper(trim(n.state)) = ?")
        params.append(state)

    with store.connect() as con:
        origin = None
        if use_radius:
            load_centroids(con, centroids_path)
            origin = con.execute("SELECT lat, lon FROM _zcta WHERE zip = ?",
                                 [zip_code]).fetchone()
            if origin is None:
                raise NppesFeedError(
                    f"ZIP {zip_code} is not in the Census ZCTA centroid list")
        # Pull a wider page than `limit` because the radius filter runs after
        # this, then REPORT whether that window was hit — a geography filter
        # that quietly discards matches beyond an invisible cap would read as
        # "that's all there is" (no silent caps).
        window = limit * 5 if use_radius else limit
        rows = con.execute(f"""
            SELECT n.npi, n.org_name, n.entity_type, n.taxonomy_code,
                   n.city, n.state, lpad(substr(trim(n.zip), 1, 5), 5, '0') AS zip,
                   n.phone, n.address,
                   CAST({parsed} AS DATE) AS enumerated
            FROM {reader} n
            WHERE {' AND '.join(where)}
            ORDER BY enumerated DESC
            LIMIT {window}
        """, params).fetchall()
        capped = len(rows) >= window
        cols = ["npi", "org_name", "entity_type", "taxonomy_code", "city",
                "state", "zip", "phone", "address", "enumerated"]
        out = [dict(zip(cols, r)) for r in rows]

        unplaced = 0
        if use_radius and out:
            dist = dict(con.execute(f"""
                SELECT z.zip, {haversine_miles_sql('z.lat', 'z.lon',
                                                   origin[0], origin[1])}
                FROM _zcta z WHERE z.zip IN (SELECT unnest(?::VARCHAR[]))
            """, [sorted({r["zip"] for r in out if r["zip"]})]).fetchall())
            kept = []
            for r in out:
                d = dist.get(r["zip"]) if r["zip"] else None
                if d is None:
                    unplaced += 1
                    continue
                if d > float(radius_miles):
                    continue
                r["miles"] = round(d, 1)
                kept.append(r)
            out = kept
        # already contracted? one lookup for the whole page, not per row
        if out:
            known = {n for (n,) in con.execute(
                "SELECT DISTINCT npi FROM rates WHERE npi IN "
                "(SELECT unnest(?::VARCHAR[]))",
                [[r["npi"] for r in out]]).fetchall()}
            for r in out:
                r["in_store"] = r["npi"] in known
    for r in out:
        r["enumerated"] = str(r["enumerated"])[:10]
        r["taxonomy"] = _label(r.get("taxonomy_code"))
    out.sort(key=lambda r: (r["enumerated"], r.get("org_name") or ""), reverse=True)
    note = FEED_NOTE
    if capped:
        note += (f" Only the {window:,} most recently issued NPIs were scanned, "
                 "so older matches in this window may not be listed — narrow the "
                 "look-back or the area to see all of them.")
    return {"rows": out[:limit], "total": len(out), "unplaced": unplaced,
            "zip": zip_code, "radius_miles": radius_miles if use_radius else None,
            "days": days, "state": state, "since": since, "reason": None,
            "capped": capped, "note": note}


def _label(code: str | None) -> str:
    from .medicare import taxonomy_label
    return taxonomy_label(code)
