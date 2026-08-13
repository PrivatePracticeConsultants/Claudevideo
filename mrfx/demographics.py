"""ZIP-level demographics: how big the market actually is.

The ZIP+radius searches answer "who is here". They cannot answer "is this a
market worth working" — twelve therapy practices in a ZIP is crowded if 4,000
seniors live there and thin if 40,000 do. The Census Bureau publishes exactly
the missing denominator per ZCTA, free and public domain: total population,
population 65+, and median household income.

WHAT IT IS USED FOR
- **Market sizing**: seniors per therapy practice inside a radius. High means
  underserved (room for a new client, or a client who can raise rates); low
  means saturated.
- **Context on every ZIP list**: a lead in a 60k-population ZIP is not the same
  lead as one in a 900-population ZIP.

HONESTY
- ZCTAs are not ZIP codes. The Census builds ZCTAs from census blocks; PO-box
  and single-building ZIPs have no ZCTA at all. Those ZIPs are reported as
  UNMATCHED, never counted as zero population.
- ACS figures are 5-year rolling ESTIMATES with margins of error, not a census
  count. The vintage is stored and printed.
- "Therapy practices per capita" counts practices THIS STORE knows about (from
  ingested payer files and the NPI directory). A practice no ingested payer
  priced is invisible to it, so the ratio is an upper bound on how underserved
  a market looks. Stated on every result.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from pathlib import Path

from .store import Store

log = logging.getLogger(__name__)


class DemographicsImportError(Exception):
    """A file that isn't ZCTA demographics — refuse it, don't guess."""


SIZING_NOTE = (
    "Population figures are US Census ACS 5-year ESTIMATES by ZCTA (not ZIP "
    "codes: PO-box and single-building ZIPs have no ZCTA and are reported "
    "unmatched, never as zero). Practice counts are the practices THIS store "
    "knows from ingested payer files, so 'seniors per practice' is an upper "
    "bound — a competitor no ingested payer priced is invisible to it."
)

# The ACS table/variable names a user is most likely to download, mapped to our
# fields. Matching is by normalized TOKENS, so both the human export
# ("Estimate!!Total:!!65 to 74 years") and the API's variable ids (B01001_001E)
# resolve without the user reshaping anything.
_SENIOR_AGE = re.compile(r"\b(6[5-9]|7\d|8\d|9\d|100)\b")
# B01001 (sex by age) variable ids for the 65+ buckets: males 65-66 … 85+ are
# _020E.._025E, females _044E.._049E. A data.census.gov / API download carries
# THESE, not the human labels, so both spellings have to resolve — otherwise a
# perfectly good export silently reports zero seniors.
_B01001_SENIOR = {"020", "021", "022", "023", "024", "025",
                  "044", "045", "046", "047", "048", "049"}
_B01001_VAR = re.compile(r"\bb01001[ _]?(\d{3})\s*e?\b")


def _is_senior_col(header_name: str) -> bool:
    n = _norm(header_name)
    if "margin" in n:                     # margins of error are not estimates
        return False
    m = _B01001_VAR.search(n)
    if m:
        return m.group(1) in _B01001_SENIOR
    return bool(_SENIOR_AGE.search(n) and ("year" in n or "over" in n))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _find(cols: list[str], *want_all: str, exclude: tuple = ()) -> str | None:
    for c in cols:
        n = _norm(c)
        if all(w in n for w in want_all) and not any(x in n for x in exclude):
            return c
    return None


def _zip5(raw) -> str | None:
    """A ZCTA id out of whatever the export carries: '63103', 'ZCTA5 63103',
    '86000US63103' (the Census GEO_ID), or an integer that lost its zeros."""
    s = str(raw or "").strip()
    if not s:
        return None
    # The Census GEO_ID is '86000US63103' — the ZCTA is the part AFTER the 'US'
    # summary-level prefix. Taking the first 5-digit run instead would import
    # every row of a national table under the ZIP "86000".
    m = re.search(r"US(\d{5})(?!\d)", s)
    if m:
        return m.group(1)
    hits = re.findall(r"\d{5}(?!\d)", s)
    if hits:
        return hits[-1]          # 'ZCTA5 63103' -> 63103
    return s.zfill(5)[:5] if s.isdigit() else None


def _num(raw) -> float | None:
    s = str(raw or "").strip().replace(",", "")
    if not s or s in {"-", "N", "(X)", "null", "*****"}:
        return None            # Census suppression markers are NOT zero
    try:
        v = float(s)
    except ValueError:
        return None
    return None if v < 0 else v      # ACS uses negatives as annotation flags


def parse_acs_csv(text: str) -> tuple[list[dict], list[str]]:
    """(rows, problems) from an ACS ZCTA export.

    Accepts the two shapes a user actually ends up with: a data.census.gov
    download (one column per estimate, sometimes a second header row of human
    labels) or an API-shaped CSV of variable ids. Any 65+ age BUCKETS present
    are summed, so the common "B01001 by sex by age" export works without the
    user pre-aggregating 22 columns by hand."""
    text = (text or "").lstrip("﻿")
    if not text.strip():
        raise DemographicsImportError("that file is empty")
    reader = csv.reader(io.StringIO(text))
    records = [r for r in reader if any((c or "").strip() for c in r)]
    if len(records) < 2:
        raise DemographicsImportError("that file has no data rows")
    header = [(c or "").strip() for c in records[0]]
    body = records[1:]
    # data.census.gov ships a SECOND header row of human-readable labels; it is
    # the more informative one for token matching, and dropping it silently
    # would otherwise import one junk row per file.
    if body and not any(_num(c) is not None for c in body[0][1:]):
        header = [f"{a} {b}".strip() for a, b in
                  zip(header, list(body[0]) + [""] * len(header))]
        body = body[1:]

    col_zip = (_find(header, "zcta") or _find(header, "geo", "id")
               or _find(header, "geography") or _find(header, "zip")
               or (header[0] if header else None))
    col_total = (_find(header, "b01001 001") or
                 _find(header, "estimate", "total", exclude=("male", "female",
                                                             "years", "margin"))
                 or _find(header, "total", "population", exclude=("margin",)))
    col_income = (_find(header, "b19013 001")
                  or _find(header, "median", "household", "income",
                           exclude=("margin",)))
    # every 65+ age bucket, male and female
    senior_cols = [c for c in header if _is_senior_col(c)]
    col_65 = _find(header, "65", "over", exclude=("margin", "male", "female")) \
        if not senior_cols else None
    if col_zip is None or (col_total is None and not senior_cols and col_65 is None):
        raise DemographicsImportError(
            "this does not look like an ACS ZCTA table — expected a ZCTA/GEO_ID "
            "column plus total population (B01001_001E) and/or 65+ age columns. "
            f"Header starts: {header[:8]}")

    idx = {c: i for i, c in enumerate(header)}
    rows, problems = [], []
    for lineno, rec in enumerate(body, start=2):
        def cell(c):
            i = idx.get(c) if c else None
            return rec[i] if i is not None and i < len(rec) else ""
        z = _zip5(cell(col_zip))
        if not z:
            problems.append(f"line {lineno}: no ZCTA in {','.join(rec)[:60]!r}")
            continue
        total = _num(cell(col_total)) if col_total else None
        if senior_cols:
            parts = [_num(cell(c)) for c in senior_cols]
            seniors = sum(p for p in parts if p is not None) if any(
                p is not None for p in parts) else None
        else:
            seniors = _num(cell(col_65)) if col_65 else None
        income = _num(cell(col_income)) if col_income else None
        if total is None and seniors is None and income is None:
            continue          # a footnote/annotation row, not a ZCTA
        rows.append({"zip": z, "population": total, "pop_65_plus": seniors,
                     "median_income": income})
    if not rows:
        raise DemographicsImportError(
            "no ZCTA rows could be read from that file"
            + (f" ({problems[0]})" if problems else ""))
    return rows, problems


def import_demographics(store: Store, path: str | Path,
                        vintage: str | None = None) -> dict:
    """Replace the ZIP demographics table from an ACS export."""
    path = Path(path)
    if not path.exists():
        raise DemographicsImportError(f"no such file: {path}")
    rows, problems = parse_acs_csv(path.read_text(encoding="utf-8-sig",
                                                  errors="replace"))
    m = re.search(r"(19|20)\d{2}", path.stem)
    vintage = str(vintage or "").strip() or (m.group(0) if m else "unknown")
    source = f"{path.name} · ACS 5-year · {vintage}"
    # SET-BASED insert via one Arrow batch, not executemany: a national ZCTA
    # table is ~34k rows and prepared-statement overhead runs ~1ms/row, so the
    # row-by-row form took 28s — holding the single write lock the whole time —
    # where this takes well under a second (same fix, same reason, as
    # store.save_npis_bulk).
    import pyarrow as pa

    def dedup(rs: list[dict]) -> list[dict]:
        """zip is the PRIMARY KEY; a file listing a ZCTA twice would abort the
        whole insert. Last row wins, as INSERT OR REPLACE would have done."""
        seen: dict[str, dict] = {}
        for r in rs:
            seen[r["zip"]] = r
        return list(seen.values())

    rows = dedup(rows)
    batch = pa.table({
        "zip": pa.array([r["zip"] for r in rows], type=pa.string()),
        "population": pa.array([r["population"] for r in rows], type=pa.float64()),
        "pop_65_plus": pa.array([r["pop_65_plus"] for r in rows], type=pa.float64()),
        "pct_65_plus": pa.array(
            [(round(100.0 * r["pop_65_plus"] / r["population"], 1)
              if r["population"] and r["pop_65_plus"] is not None else None)
             for r in rows], type=pa.float64()),
        "median_income": pa.array([r["median_income"] for r in rows], type=pa.float64()),
        "source": pa.array([source] * len(rows), type=pa.string()),
    })
    with store.write_lock, store.connect() as con:
        _ensure_table(con)
        con.register("_demo_batch", batch)
        # one transaction: a reader must never see the empty instant between
        # the DELETE and the INSERT (same rule as the MPFS load)
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("DELETE FROM zip_demographics")
            con.execute("INSERT INTO zip_demographics SELECT * FROM _demo_batch")
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            con.unregister("_demo_batch")
    log.info("demographics: %s ZCTAs from %s", f"{len(rows):,}", path.name)
    return {"zctas": len(rows), "source": source, "vintage": vintage,
            "problems": problems[:20], "note": SIZING_NOTE}


def _table_exists(con) -> bool:
    """Probe, never CREATE, on a read path. `CREATE TABLE IF NOT EXISTS` is
    still a CATALOG WRITE: two dashboard requests running it concurrently can
    collide in DuckDB's catalog and 500 for no reason (the same trap the
    referral view documents). Only the import path creates."""
    try:
        return bool(con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = 'zip_demographics'").fetchone()[0])
    except Exception:  # noqa: BLE001
        return False


def _ensure_table(con) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS zip_demographics (
            zip VARCHAR PRIMARY KEY,
            population DOUBLE,
            pop_65_plus DOUBLE,
            pct_65_plus DOUBLE,
            median_income DOUBLE,
            source VARCHAR
        )
    """)


def demographics_status(store: Store) -> dict:
    with store.connect() as con:
        if not _table_exists(con):
            return {"loaded": False, "zctas": 0, "source": None,
                    "population": 0, "note": SIZING_NOTE}
        try:
            n, src, pop = con.execute(
                "SELECT count(*), any_value(source), sum(population) "
                "FROM zip_demographics").fetchone()
        except Exception:  # noqa: BLE001 — table absent = not loaded
            return {"loaded": False, "zctas": 0, "source": None,
                    "population": 0, "note": SIZING_NOTE}
    return {"loaded": bool(n), "zctas": n, "source": src,
            "population": int(pop or 0), "note": SIZING_NOTE}


def market_sizing(store: Store, zip_code: str, radius_miles: float = 25,
                  therapy_only: bool = True, centroids_path=None) -> dict:
    """Population, seniors and therapy practices inside a radius.

    The headline is seniors per therapy practice: the crowding measure that
    decides whether a market has room. Practices are counted at the TIN grain
    (one practice, however many clinicians) from the same directory the ZIP
    search uses, so the two can never disagree."""
    from .catalog import therapy_taxonomy_sql
    from .medicare import haversine_miles_sql, load_centroids

    zip_code = str(zip_code or "").strip()[:5]
    if not re.fullmatch(r"\d{5}", zip_code):
        raise DemographicsImportError(f"'{zip_code}' is not a 5-digit ZIP code")
    try:
        radius = float(radius_miles)
    except (TypeError, ValueError):
        raise DemographicsImportError("radius must be a number of miles")
    radius = max(1.0, min(radius, 250.0))
    therapy = therapy_taxonomy_sql("n.taxonomy_code") if therapy_only else "TRUE"

    with store.connect() as con:
        has_demo = _table_exists(con)
        load_centroids(con, centroids_path)
        origin = con.execute("SELECT lat, lon FROM _zcta WHERE zip = ?",
                             [zip_code]).fetchone()
        if origin is None:
            raise DemographicsImportError(
                f"ZIP {zip_code} is not in the Census ZCTA centroid list")
        miles = haversine_miles_sql("z.lat", "z.lon", origin[0], origin[1])
        in_radius = [r[0] for r in con.execute(
            f"SELECT z.zip FROM _zcta z WHERE {miles} <= ?", [radius]).fetchall()]
        if not in_radius:
            raise DemographicsImportError(
                f"no ZIP centroids within {radius:g} miles of {zip_code}")
        demo = (0, None, None, None) if not has_demo else con.execute("""
            SELECT count(*), sum(population), sum(pop_65_plus),
                   median(median_income)
            FROM zip_demographics
            WHERE zip IN (SELECT unnest(?::VARCHAR[]))
        """, [in_radius]).fetchone()
        matched = demo[0] or 0
        # practices: TIN grain, located by their clinicians' modal ZIP — the
        # same rule the leads/leaderboard ZIP search uses
        practices, npis = con.execute(f"""
            WITH p AS (
                SELECT r.tin_value AS tin,
                       mode(lpad(substr(trim(n.zip), 1, 5), 5, '0')) AS zip,
                       count(DISTINCT r.npi) AS npis
                FROM rates r JOIN npi_directory n ON n.npi = r.npi
                WHERE r.tin_value IS NOT NULL AND NOT r.tin_is_really_npi
                  AND {therapy}
                GROUP BY 1
            )
            SELECT count(*), sum(npis) FROM p
            WHERE zip IN (SELECT unnest(?::VARCHAR[]))
        """, [in_radius]).fetchone()
        top = [] if not has_demo else [
            {"zip": z, "population": int(p or 0), "pop_65_plus": int(s or 0),
             "pct_65_plus": pct, "median_income": inc, "miles": round(d, 1)}
            for z, p, s, pct, inc, d in con.execute(f"""
            SELECT d.zip, d.population, d.pop_65_plus, d.pct_65_plus,
                   d.median_income, {miles} AS miles
            FROM zip_demographics d JOIN _zcta z ON z.zip = d.zip
            WHERE {miles} <= ? AND d.pop_65_plus IS NOT NULL
            ORDER BY d.pop_65_plus DESC LIMIT 15
        """, [radius]).fetchall()]

    pop, seniors = demo[1], demo[2]
    practices = practices or 0
    return {
        "zip": zip_code, "radius_miles": radius,
        "zctas_in_radius": len(in_radius), "zctas_matched": matched,
        "zctas_unmatched": len(in_radius) - matched,
        "population": int(pop or 0), "pop_65_plus": int(seniors or 0),
        "pct_65_plus": (round(100.0 * seniors / pop, 1)
                        if pop and seniors is not None else None),
        "median_income": round(demo[3], 0) if demo[3] is not None else None,
        "practices": practices, "clinicians": int(npis or 0),
        "seniors_per_practice": (round(seniors / practices)
                                 if practices and seniors else None),
        "people_per_practice": (round(pop / practices)
                                if practices and pop else None),
        "top_zips": top,
        "loaded": bool(matched),
        "reason": (None if matched else
                   "no ZIP demographics are loaded for this area — import an "
                   "ACS ZCTA table on the Data tab to size markets"),
        "note": SIZING_NOTE,
    }
