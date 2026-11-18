"""CMS reference data for telling outpatient rehab clinics apart (0.8.6).

Three public CMS sources, fetched automatically by `serve` (and by
`mrfx cmsref`), cached in cms_dac_dir with their release dates:

* Hospital General Information (provider-data xubh-q36u) and Nursing Home
  Provider Information (4pq5-n9py): every Medicare-certified hospital and
  nursing home with its street address -> table `cms_facilities`, keyed by a
  normalized address (`addr_key_sql`). A tax ID whose practice addresses are
  mostly at those addresses is hospital-based or SNF-based, not an outpatient
  clinic.
* Medicare Physician & Other Practitioners — by Provider and Service (the
  newest year), ONLY the app's therapy codes, through the CMS data API ->
  utilization/<year>.parquet (the same file `mrfx utilization` writes, plus
  provider_type, entity code, state and ZIP). Clinicians billing therapy codes
  as "Physical/Occupational Therapist in Private Practice" are outpatient
  private practice; hospital and SNF therapy runs on institutional claims and
  is absent. The same rows feed "Fill from Medicare" volumes.

Nothing here is ever estimated: a source not loaded contributes no evidence.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

# data.cms.gov answers 403 to a User-Agent containing "Explorer" (and to a
# bare "Mozilla/5.0") — measured 2026-11-14; "mrfx/<version>" is accepted
from . import __version__ as _v
UA = f"mrfx/{_v}"
PROVIDER_DATA = "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/"
FACILITY_SETS = {"hospital": "xubh-q36u", "nursing_home": "4pq5-n9py"}
CATALOG = "https://data.cms.gov/data.json"
MEDICARE_TITLE = "Medicare Physician & Other Practitioners - by Provider and Service"
API_PAGE = 5000

_STREET_WORDS = (("STREET", "ST"), ("AVENUE", "AVE"), ("ROAD", "RD"), ("DRIVE", "DR"),
                 ("BOULEVARD", "BLVD"), ("LANE", "LN"), ("COURT", "CT"), ("PLACE", "PL"),
                 ("PARKWAY", "PKWY"), ("HIGHWAY", "HWY"), ("CIRCLE", "CIR"),
                 ("TERRACE", "TER"), ("SQUARE", "SQ"), ("PLAZA", "PLZ"), ("TRAIL", "TRL"),
                 ("EXPRESSWAY", "EXPY"), ("FREEWAY", "FWY"), ("CENTER", "CTR"),
                 ("NORTH", "N"), ("SOUTH", "S"), ("EAST", "E"), ("WEST", "W"),
                 ("NORTHEAST", "NE"), ("NORTHWEST", "NW"), ("SOUTHEAST", "SE"),
                 ("SOUTHWEST", "SW"))


def addr_key_sql(addr: str, zip_: str) -> str:
    """SQL: a comparison key for a street address — ZIP5 | the street line
    with punctuation, the unit part (STE / SUITE / UNIT / BLDG / FL / ROOM /
    #…) and street-word spelling normalized. NULL without a ZIP or a line."""
    x = f"upper(coalesce({addr}, ''))"
    x = f"regexp_replace({x}, '[^A-Z0-9# ]', ' ', 'g')"
    # whole words only: "1500 FLORIDA AVE" keeps FLORIDA (it matched FL and
    # was cut to "1500", colliding with "1500 FLOYD ST" and any CMS hospital
    # at house number 1500 in the ZIP)
    x = (f"regexp_replace({x}, '( |^)(STE|SUITE|UNIT|BLDG|BUILDING|FL|FLOOR|ROOM|RM|APT|"
         f"DEPT)( .*)?$', '')")
    x = f"regexp_replace({x}, ' *#.*$', '')"
    for long, short in _STREET_WORDS:
        x = f"regexp_replace({x}, '\\b{long}\\b', '{short}', 'g')"
    x = f"trim(regexp_replace({x}, ' +', ' ', 'g'))"
    z = f"left(regexp_replace(coalesce({zip_}, ''), '[^0-9]', '', 'g'), 5)"
    return f"CASE WHEN length({z}) = 5 AND {x} <> '' THEN {z} || '|' || {x} END"


def _client():
    import httpx
    return httpx.Client(follow_redirects=True, headers={"User-Agent": UA},
                        timeout=httpx.Timeout(60.0, read=300.0))


def _get_json(client, url: str, params=None, tries: int = 4):
    for i in range(tries):
        try:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except Exception:  # noqa: BLE001 — transient: back off, then the last error
            if i == tries - 1:
                raise
            time.sleep(2 ** i)


# ---------------------------------------------------------------- facilities

def fetch_facilities(cache_dir: Path) -> dict:
    """The two CMS lists in `cache_dir` (downloaded when CMS has a newer
    release than the kept copy). {kind: {path, release}}."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    with _client() as c:
        for kind, ds in FACILITY_SETS.items():
            side = cache_dir / f"cms_{kind}.json"
            f = cache_dir / f"cms_{kind}.csv"
            have = json.loads(side.read_text()) if side.exists() and f.exists() else None
            try:
                meta = _get_json(c, PROVIDER_DATA + ds)
                url = next(d["downloadURL"] for d in meta.get("distribution", [])
                           if str(d.get("downloadURL", "")).lower().endswith(".csv"))
                release = str(meta.get("modified") or "")
            except Exception as e:  # noqa: BLE001 — offline: the kept copy
                if have:
                    out[kind] = {"path": str(f), "release": have.get("release", "")}
                    continue
                raise RuntimeError(f"could not reach data.cms.gov for the {kind} list ({e})")
            if not have or have.get("release") != release:
                part = f.with_name(f.name + f".{os.getpid()}.part")
                try:
                    with c.stream("GET", url) as r:
                        r.raise_for_status()
                        with open(part, "wb") as fh:
                            for chunk in r.iter_bytes(1 << 20):
                                fh.write(chunk)
                    os.replace(part, f)
                finally:
                    part.unlink(missing_ok=True)
                side.write_text(json.dumps({"release": release, "url": url}, indent=1))
            out[kind] = {"path": str(f), "release": release}
    return out


def load_facilities(store, files: dict) -> int:
    """Load the CMS hospital / nursing-home lists into cms_facilities (by
    header name; the 2026 layouts: 'Address' / 'Provider Address', 'City/Town',
    'State', 'ZIP Code'). Returns the rows loaded."""
    import csv as _csv

    from .store import sql_path
    parts = []
    for kind, info in files.items():
        path = Path(info["path"])
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            low = {h.strip().lower(): h for h in next(_csv.reader(fh))}

        def col(*names, _low=low, _path=path):
            for n in names:
                if n in _low:
                    return '"' + _low[n].replace('"', '""') + '"'
            raise ValueError(f"{_path.name}: no {names[0]!r} column — not the CMS {kind} list")
        a = col("address", "provider address")
        name = col("facility name", "provider name")
        ident = col("facility id", "cms certification number (ccn)", "federal provider number")
        parts.append(
            f"SELECT '{kind}' AS kind, trim({ident}) AS ccn, upper(trim({name})) AS name, "
            f"upper(trim({a})) AS address, upper(trim({col('city/town', 'city')})) AS city, "
            f"upper(trim({col('state')})) AS state, trim({col('zip code', 'zip')}) AS zip, "
            f"{addr_key_sql(a, col('zip code', 'zip'))} AS addr_key, '{info.get('release', '')}' AS release "
            f"FROM read_csv('{sql_path(path)}', header = true, all_varchar = true, "
            "ignore_errors = true)")
    with store.write_lock, store.connect() as con:
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("CREATE OR REPLACE TABLE cms_facilities AS " + " UNION ALL ".join(parts))
            n = con.execute("SELECT count(*) FROM cms_facilities").fetchone()[0]
            con.execute("INSERT OR REPLACE INTO meta VALUES ('cms_facilities_release', ?)",
                        [json.dumps({k: v.get("release", "") for k, v in files.items()})])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return int(n)


# ------------------------------------------------------- Medicare utilization

def medicare_dataset() -> dict:
    """The newest year of the by-provider-and-service file that has an API:
    {year, api, modified}."""
    with _client() as c:
        cat = _get_json(c, CATALOG)
    best = None
    for ds in cat.get("dataset", []):
        if MEDICARE_TITLE not in str(ds.get("title", "")):
            continue
        for d in ds.get("distribution", []):
            api = str(d.get("accessURL") or "")
            title = str(d.get("title") or "")
            if "/data-api/" not in api:
                continue
            year = title.rsplit(":", 1)[-1].strip()[:4]
            if year.isdigit() and (best is None or year > best["year"]):
                best = {"year": year, "api": api, "modified": str(ds.get("modified") or "")}
    if best is None:
        raise RuntimeError("data.cms.gov lists no API for the Medicare by-provider-and-service file")
    return best


def fetch_medicare_therapy(store, codes: list[str] | None = None, *, force: bool = False) -> dict:
    """The newest year's therapy-code rows from the CMS API into
    utilization/<year>.parquet (atomic). Skipped when that year is already
    fetched and CMS reports no newer release (unless `force`)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from .catalog import CODE_CATALOG
    from .store import replace_with_retry
    info = medicare_dataset()
    stamp = f"{info['year']}|{info['modified']}"
    with store.connect() as con:
        have = store._meta_get(con, "medicare_util_release")
    out_dir = Path(store.dir) / "utilization"
    out = out_dir / f"{info['year']}.parquet"
    if have == stamp and out.exists() and not force:
        return {"year": info["year"], "rows": None, "fetched": False}
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    cols = ("npi", "billing_code", "services", "beneficiaries", "avg_allowed",
            "place_of_service", "data_year", "provider_type", "entity_code", "state", "zip5")
    schema = pa.schema([(c, pa.float64() if c in ("services", "avg_allowed") else
                         pa.int64() if c == "beneficiaries" else pa.string()) for c in cols])

    def num(v, kind=float):
        try:
            return kind(float(v)) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    n = 0
    log.info("Medicare %s therapy billing: fetching %d codes from the CMS API…",
             info["year"], len(codes or CODE_CATALOG))
    try:
        with _client() as c, pq.ParquetWriter(tmp, schema, compression="zstd") as w:
            for code in sorted(codes or CODE_CATALOG):
                offset = 0
                while True:
                    rows = _get_json(c, info["api"], params={
                        "filter[HCPCS_Cd]": code, "size": API_PAGE, "offset": offset})
                    if not rows:
                        break
                    batch = {k: [] for k in cols}
                    for r in rows:
                        batch["npi"].append(str(r.get("Rndrng_NPI") or "").strip())
                        batch["billing_code"].append(code)
                        batch["services"].append(num(r.get("Tot_Srvcs")))
                        batch["beneficiaries"].append(num(r.get("Tot_Benes"), int))
                        batch["avg_allowed"].append(num(r.get("Avg_Mdcr_Alowd_Amt")))
                        batch["place_of_service"].append((r.get("Place_Of_Srvc") or None))
                        batch["data_year"].append(info["year"])
                        batch["provider_type"].append(r.get("Rndrng_Prvdr_Type") or None)
                        batch["entity_code"].append(r.get("Rndrng_Prvdr_Ent_Cd") or None)
                        batch["state"].append(r.get("Rndrng_Prvdr_State_Abrvtn") or None)
                        batch["zip5"].append((str(r.get("Rndrng_Prvdr_Zip5") or "")[:5]) or None)
                    w.write_table(pa.table(batch, schema=schema))
                    n += len(rows)
                    if len(rows) < API_PAGE:
                        break
                    offset += API_PAGE
        replace_with_retry(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    from .utilization import _register_view
    with store.write_lock, store.connect() as con:
        _register_view(con, store)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('medicare_util_release', ?)", [stamp])
    log.info("Medicare %s therapy billing: %s provider-code rows", info["year"], f"{n:,}")
    return {"year": info["year"], "rows": n, "fetched": True}


def ensure_cmsref(store, cache_dir: Path) -> dict:
    """Both sources current in `store` (each fetched only when CMS has a newer
    release). Each failure is reported, never fatal to the other; the check
    is recorded (meta cmsref_checked_at — serve's monthly clock) only when
    both succeeded."""
    out = {}
    try:
        files = fetch_facilities(cache_dir)
        with store.connect() as con:
            have = store._meta_get(con, "cms_facilities_release")
        want = json.dumps({k: v.get("release", "") for k, v in files.items()})
        out["facilities"] = (load_facilities(store, files) if have != want else "current")
    except Exception as e:  # noqa: BLE001
        out["facilities"] = f"not loaded: {e}"
    try:
        out["medicare"] = fetch_medicare_therapy(store)
    except Exception as e:  # noqa: BLE001
        out["medicare"] = f"not loaded: {e}"
    # the monthly clock starts only when both are in: a failure (CMS down, a
    # refused request) is tried again the next day, not in 30 days
    if not any(isinstance(v, str) and v.startswith("not loaded") for v in out.values()):
        with store.write_lock, store.connect() as con:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('cmsref_checked_at', ?)",
                        [dt.datetime.now().isoformat(timespec="seconds")])
    return out
