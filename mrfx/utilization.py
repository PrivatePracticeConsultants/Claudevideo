"""Medicare utilization: how much therapy a practice actually bills.

WHY. Every dollar claim this app makes — the opportunity column, the proposal's
"this ask is worth $X/yr", the engagement win report — needs ANNUAL UNITS, and
until now the only source was the owner typing them in. For a client that is a
20-minute phone call; for a PROSPECT it is impossible, so prospects could never
be sized or dollar-valued at all.

CMS publishes the missing half: the **Medicare Physician & Other Practitioners
— by Provider and Service** public use file. One row per rendering NPI x HCPCS
x place of service, with the number of services Medicare paid and what it
allowed. Public domain, no licence, one file per year.

WHAT THE NUMBER IS — AND IS NOT (stated on every result, because a volume that
is quietly wrong turns every downstream dollar into a lie):

- **Medicare fee-for-service ONLY.** No commercial, no Medicare Advantage, no
  Medicaid, no cash. A practice's true volume is LARGER — usually much larger
  for a peds or ortho clinic, closer for a geriatric one. So these units are a
  FLOOR, and the app labels them as such rather than pretending they are the
  practice's book.
- **CMS suppresses any provider-code row under 11 beneficiaries.** Small-volume
  codes are therefore ABSENT, not zero. Another reason the total is a floor.
- **"Services" is Medicare's unit count**: for a timed code that is 15-minute
  units, which is exactly the unit the volumes boxes and the fee schedule use.
- **It lags ~2 years.** The year is carried on every row and printed.

So: excellent for sizing a practice and for pre-filling a volumes box the user
then edits; never presented as the practice's actual annual volume.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .catalog import CODE_CATALOG, code_info
from .store import Store, sql_path

log = logging.getLogger(__name__)


class UtilizationImportError(Exception):
    """A file that isn't the PUF it claims to be — refuse it, don't guess."""


VOLUME_NOTE = (
    "Units come from Medicare fee-for-service claims only (CMS Physician & "
    "Other Practitioners PUF) — no commercial, Medicare Advantage, Medicaid or "
    "cash volume is in them, and CMS suppresses any code with fewer than 11 "
    "beneficiaries. Treat them as a FLOOR for sizing and edit them before "
    "quoting a dollar figure."
)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _find(cols: list[str], *want_all: str, exclude: tuple = ()) -> str | None:
    """First column whose normalized name contains every token and none of the
    excluded ones. CMS renamed every column between the 2013-era layout
    (`line_srvc_cnt`) and the current one (`Tot_Srvcs`), so match tokens —
    never positions, and never one hard-coded vintage's spelling."""
    for c in cols:
        n = _norm(c)
        if all(w in n for w in want_all) and not any(x in n for x in exclude):
            return c
    return None


def _ident(name: str) -> str:
    """A column name safe to inline as a SQL identifier. DuckDB cannot
    parameterize an identifier, and a CSV header may contain a double quote —
    which, undoubled, ENDS the identifier and lets the rest of the header
    become SQL (verified: a header `Tot_Srvcs" AS x, 999 AS "y` restructured
    the SELECT). Doubling is the standard escape."""
    return '"' + str(name).replace('"', '""') + '"'


def _columns(con, reader: str) -> dict[str, str]:
    """Map our field names onto this file's actual headers, or refuse."""
    try:
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {reader}").fetchall()]
    except Exception as e:  # noqa: BLE001 — a binary/garbled file must refuse
        # in plain language, not surface a raw DuckDB traceback as a 500
        raise UtilizationImportError(
            f"that file could not be read as a CSV ({e}). Point at the "
            "unzipped Physician & Other Practitioners CSV itself.") from e
    got = {
        "npi": _find(cols, "npi", exclude=("hcpcs", "entity")),
        "code": (_find(cols, "hcpcs", "cd", exclude=("desc",))
                 or _find(cols, "hcpcs", "code", exclude=("desc",))),
        "services": (_find(cols, "tot", "srvcs") or _find(cols, "line", "srvc", "cnt")
                     or _find(cols, "srvc", "cnt") or _find(cols, "total", "services")),
        "benes": (_find(cols, "tot", "benes") or _find(cols, "bene", "unique", "cnt")
                  or _find(cols, "bene", "cnt")),
        "allowed": (_find(cols, "avg", "mdcr", "alowd")
                    or _find(cols, "average", "medicare", "allowed")),
        "pos": _find(cols, "place", "srvc") or _find(cols, "place", "service"),
    }
    missing = [k for k in ("npi", "code", "services") if not got[k]]
    if missing:
        raise UtilizationImportError(
            "this does not look like the CMS Physician & Other Practitioners "
            f"file — could not find column(s) {', '.join(missing)}. Header "
            f"starts: {cols[:10]}")
    return got


def _year_from(path: Path, year: str | None) -> str:
    """The data year: explicit, else the first 4-digit run in the filename.
    Embedded in a COPY statement and used as a FILENAME, so it is validated
    here rather than trusted."""
    y = str(year or "").strip()
    if not y:
        m = re.search(r"(19|20)\d{2}", path.stem)
        if m:
            y = m.group(0)
        else:
            # the CURRENT CMS delivery has no 4-digit year in its name —
            # MUP_PHY_R24_P05_V10_D23_Prov_Svc.csv encodes the data year as
            # "DYY". Refusing THE file the download page hands out would be
            # pointless friction, and the pattern is specific enough not to
            # misfire (underscore-delimited D + exactly two digits).
            m = re.search(r"(?:^|_)D(\d{2})(?:_|$)", path.stem, re.IGNORECASE)
            if m:
                y = f"20{m.group(1)}"
    if not y:
        raise UtilizationImportError(
            "could not tell which year this file covers — pass --year 2023.")
    if not re.fullmatch(r"(19|20)\d{2}", str(y)):
        raise UtilizationImportError(f"'{y}' is not a data year (e.g. 2023).")
    return str(y)


def import_utilization(store: Store, path: str | Path, year: str | None = None,
                       codes: tuple[str, ...] | None = None) -> dict:
    """Load one year of the PUF, keeping only therapy HCPCS rows.

    Streamed by DuckDB — the national file is ~10M rows and is never
    materialized in Python. Filtered to the app's therapy code catalog, which
    both bounds the store (a few hundred thousand rows) and keeps the file
    on-topic: rows for cardiology codes could never answer a question this app
    asks. Written to a .tmp and renamed on success, so a failed import can
    never leave a corrupt parquet inside the view's glob."""
    path = Path(path)
    if not path.exists():
        raise UtilizationImportError(f"no such file: {path}")
    yr = _year_from(path, year)
    want = tuple(codes or CODE_CATALOG.keys())
    code_list = ", ".join("'" + c.replace("'", "''") + "'" for c in want)

    out_dir = Path(store.dir) / "utilization"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{yr}.parquet"
    tmp = out.with_suffix(".parquet.tmp")
    reader = f"read_csv('{sql_path(path)}', header = true, all_varchar = true)"

    with store.write_lock, store.connect() as con:
        for stale in out_dir.glob("*.parquet.tmp"):
            if stale != tmp:
                try:
                    stale.unlink()
                    log.info("utilization: removed stale partial %s", stale.name)
                except OSError:
                    pass
        c = _columns(con, reader)
        col = {k: (f"u.{_ident(v)}" if v else None) for k, v in c.items()}
        pos = f"nullif(trim({col['pos']}), '')" if col["pos"] else "CAST(NULL AS VARCHAR)"
        benes = (f"TRY_CAST({col['benes']} AS BIGINT)" if col["benes"]
                 else "CAST(NULL AS BIGINT)")
        allowed = (f"TRY_CAST({col['allowed']} AS DOUBLE)" if col["allowed"]
                   else "CAST(NULL AS DOUBLE)")
        try:
            con.execute(f"""
                COPY (
                    SELECT trim({col['npi']}) AS npi,
                           upper(trim({col['code']})) AS billing_code,
                           TRY_CAST({col['services']} AS DOUBLE) AS services,
                           {benes} AS beneficiaries,
                           {allowed} AS avg_allowed,
                           {pos} AS place_of_service,
                           '{yr}' AS data_year
                    FROM {reader} u
                    WHERE upper(trim({col['code']})) IN ({code_list})
                      AND trim({col['npi']}) <> ''
                ) TO '{sql_path(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise UtilizationImportError(
                f"{path.name} could not be read through ({e}). Previously "
                "imported utilization data is untouched.") from e
        from .store import replace_with_retry
        replace_with_retry(tmp, out)
        _register_view(con, store)
        n, npis = con.execute(
            "SELECT count(*), count(DISTINCT npi) FROM utilization "
            "WHERE data_year = ?", [yr]).fetchone()
    warning = None
    if n == 0:
        warning = (f"{path.name} imported cleanly but held no therapy codes. "
                   "That usually means the wrong PUF (this app keeps only the "
                   "PT/OT/SLP code set), not an absence of therapy billing.")
        log.warning("utilization: %s", warning)
    else:
        log.info("utilization %s: %s rows over %s providers", yr, f"{n:,}", f"{npis:,}")
    return {"year": yr, "rows": n, "providers": npis, "path": str(out),
            "warning": warning, "note": VOLUME_NOTE}


def _register_view(con, store: Store) -> None:
    """(Re)write the `utilization` view — a CATALOG WRITE, so import-path only
    (under write_lock). Read paths use _ensure_view."""
    glob = sql_path(Path(store.dir) / "utilization" / "*.parquet")
    try:
        con.execute(
            "CREATE OR REPLACE VIEW utilization AS "
            f"SELECT * FROM read_parquet('{glob}', union_by_name = true)")
    except Exception:  # noqa: BLE001 — nothing imported yet: an empty view
        con.execute(
            "CREATE OR REPLACE VIEW utilization AS SELECT "
            "NULL::VARCHAR AS npi, NULL::VARCHAR AS billing_code, "
            "NULL::DOUBLE AS services, NULL::BIGINT AS beneficiaries, "
            "NULL::DOUBLE AS avg_allowed, NULL::VARCHAR AS place_of_service, "
            "NULL::VARCHAR AS data_year WHERE FALSE")


def _ensure_view(con, store: Store) -> None:
    """Read-path: probe by NAME, register only when missing. Unconditional
    CREATE OR REPLACE on a read is a catalog write two dashboard requests can
    collide on; and probing a real column heals a view left over from an
    earlier layout (the same trap the referral view documents)."""
    try:
        con.execute("SELECT data_year FROM utilization LIMIT 0")
    except Exception:  # noqa: BLE001
        _register_view(con, store)


def utilization_status(store: Store) -> dict:
    """What utilization data is loaded — for `mrfx status` and the dashboard."""
    with store.connect() as con:
        _ensure_view(con, store)
        try:
            rows = con.execute(
                "SELECT data_year, count(*), count(DISTINCT npi), "
                "       sum(services) FROM utilization GROUP BY 1 ORDER BY 1 DESC"
            ).fetchall()
        except Exception:  # noqa: BLE001
            rows = []
    return {"years": [{"year": y, "rows": n, "providers": p,
                       "services": int(s or 0)} for y, n, p, s in rows],
            "loaded": bool(rows), "latest": rows[0][0] if rows else None,
            "note": VOLUME_NOTE}


def _latest_year(con, year: str | None) -> str | None:
    if year:
        return str(year)
    r = con.execute("SELECT max(data_year) FROM utilization").fetchone()
    return r[0] if r else None


def practice_utilization(store: Store, tins: list[str], year: str | None = None,
                         office_only: bool = True) -> dict:
    """Medicare volume for one practice, by code.

    Matched through the practice's OWN NPIs as this store's rate files list
    them, so the answer is about this practice and not a name-alike. Reports
    how many of those NPIs the PUF actually covers — a practice whose
    clinicians bill under a group NPI the PUF doesn't carry gets a low
    coverage figure rather than a quietly small number."""
    tins = [t for t in (tins or []) if t]
    if not tins:
        return {"codes": [], "summary": {"npis": 0, "matched_npis": 0},
                "year": None, "note": VOLUME_NOTE}
    with store.connect() as con:
        _ensure_view(con, store)
        yr = _latest_year(con, year)
        if not yr:
            return {"codes": [], "summary": {"npis": 0, "matched_npis": 0},
                    "year": None, "note": VOLUME_NOTE}
        npis = [n for (n,) in con.execute(
            "SELECT DISTINCT npi FROM rates WHERE tin_value IN "
            "(SELECT unnest(?::VARCHAR[])) AND npi IS NOT NULL", [tins]).fetchall()]
        if not npis:
            return {"codes": [], "summary": {"npis": 0, "matched_npis": 0},
                    "year": yr, "note": VOLUME_NOTE}
        # Place of service: 'O' (office) is the setting a private practice's
        # negotiated non-facility rate applies to; facility rows would add
        # units the practice never bills under this contract.
        #
        # But an office filter that matches NOTHING must never be reported as
        # "this practice bills no therapy". CMS codes this column 'O'/'F'; a
        # differently-coded export ("Office"/"Facility", or a blank column)
        # would silently zero the answer. So: if the filter empties a result
        # that otherwise has rows, fall back to ALL settings and say so.
        pos = " AND (place_of_service IS NULL OR upper(place_of_service) = 'O')" \
              if office_only else ""
        if pos and not con.execute(
                f"SELECT 1 FROM utilization WHERE data_year = ? AND npi IN "
                f"(SELECT unnest(?::VARCHAR[])){pos} LIMIT 1",
                [yr, npis]).fetchone():
            if con.execute(
                    "SELECT 1 FROM utilization WHERE data_year = ? AND npi IN "
                    "(SELECT unnest(?::VARCHAR[])) LIMIT 1",
                    [yr, npis]).fetchone():
                log.info("utilization: no office-coded rows for this practice — "
                         "reporting all places of service instead")
                pos, office_only = "", False
        rows = con.execute(f"""
            SELECT billing_code, sum(services) AS services,
                   sum(beneficiaries) AS benes,
                   round(avg(avg_allowed), 2) AS avg_allowed,
                   count(DISTINCT npi) AS npis
            FROM utilization
            WHERE data_year = ? AND npi IN (SELECT unnest(?::VARCHAR[])){pos}
            GROUP BY 1 ORDER BY 2 DESC
        """, [yr, npis]).fetchall()
        matched = con.execute(
            f"SELECT count(DISTINCT npi) FROM utilization WHERE data_year = ? "
            f"AND npi IN (SELECT unnest(?::VARCHAR[])){pos}", [yr, npis]).fetchone()[0]
    codes = [{"billing_code": c, "units": int(s or 0), "beneficiaries": int(b or 0),
              "avg_allowed": a, "npis": n,
              "description": CODE_CATALOG.get(c, ("", (), False))[0]}
             for c, s, b, a, n in rows]
    return {
        "codes": codes, "year": yr,
        "summary": {
            "npis": len(npis), "matched_npis": matched,
            "total_units": sum(c["units"] for c in codes),
            "total_beneficiaries": sum(c["beneficiaries"] for c in codes),
            "medicare_allowed": round(sum(
                (c["avg_allowed"] or 0) * c["units"] for c in codes), 2),
            "n_codes": len(codes),
            "office_only": office_only,
        },
        "note": VOLUME_NOTE + ("" if office_only else
                               " This practice's rows are not coded as office "
                               "place-of-service, so ALL settings are included."),
    }


def suggested_volumes(store: Store, subject: str, year: str | None = None,
                      multiplier: float = 1.0) -> dict:
    """{code: annual units} to pre-fill a volumes box for `subject`.

    `multiplier` lets the user scale Medicare units to their whole book when
    they know the share (e.g. Medicare is a third of their visits -> 3.0). It
    defaults to 1.0 — Medicare only — and whatever is used is echoed back and
    printed, so a scaled number can never masquerade as a measured one."""
    from .benchmark import BenchmarkError, resolve_subject_tins

    try:
        mult = float(multiplier)
    except (TypeError, ValueError):
        raise BenchmarkError("the volume multiplier must be a number (1 = Medicare only)")
    if not 0.1 <= mult <= 20:
        raise BenchmarkError("the volume multiplier must be between 0.1 and 20")
    tins = resolve_subject_tins(store, subject)
    u = practice_utilization(store, tins, year)
    if not u["codes"]:
        raise BenchmarkError(
            "no Medicare utilization on file for this practice"
            + (f" in {u['year']}" if u.get("year") else
               " — import a CMS Physician & Other Practitioners file first "
               "(Data tab -> Medicare utilization)"))
    volumes = {c["billing_code"]: (round(c["units"] * mult) if mult != 1.0
                                  else c["units"]) for c in u["codes"]}
    return {"subject": subject, "year": u["year"], "volumes": volumes,
            "multiplier": mult, "summary": u["summary"], "codes": u["codes"],
            "note": VOLUME_NOTE + (
                f" These were scaled by {mult:g}x from the Medicare units at "
                "your instruction; the scaling is an assumption, not data."
                if mult != 1.0 else "")}


GROWTH_NOTE = (
    "Growth compares this practice's Medicare service counts between two PUF "
    "years. Medicare fee-for-service only, so a practice growing its commercial "
    "or cash book while its Medicare panel ages out can read flat or down here. "
    "CMS suppresses codes under 11 beneficiaries, so a code can APPEAR or "
    "VANISH between years purely by crossing that threshold — the comparison "
    "therefore reports codes present in BOTH years separately from those in "
    "only one, and never counts a suppression as a real change."
)

SERVICE_LINE_NOTE = (
    "Service-line gaps compare what this practice bills Medicare against what "
    "nearby therapy practices bill Medicare — a BILLING-BEHAVIOUR comparison, "
    "not a contract one (for codes peers have published rates on that this "
    "practice has not, see contract gaps). A code the practice does not bill "
    "may be a genuine service it could add, a service it chooses not to offer, "
    "or one it bills too rarely for CMS to publish. Every row is a question to "
    "ask the owner, never a diagnosis."
)


def available_years(store: Store) -> list[str]:
    """PUF years present, newest first."""
    with store.connect() as con:
        _ensure_view(con, store)
        try:
            return [r[0] for r in con.execute(
                "SELECT DISTINCT data_year FROM utilization "
                "WHERE data_year IS NOT NULL ORDER BY data_year DESC").fetchall()]
        except Exception:  # noqa: BLE001
            return []


def practice_growth(store: Store, subject: str, *, from_year: str | None = None,
                    to_year: str | None = None) -> dict:
    """A practice's Medicare volume between two PUF years.

    A growing practice is a better prospect; a shrinking referral source is a
    call worth making. Codes present in only ONE year are reported separately,
    because CMS suppression can create or destroy a code line without anything
    changing in the practice.
    """
    from .benchmark import BenchmarkError, resolve_subject_tins

    years = available_years(store)
    if len(years) < 2:
        return {"loaded": False, "subject": subject, "rows": [],
                "years_available": years,
                "reason": ("growth needs two Medicare utilization years in the "
                           "store — import a second CMS Physician & Other "
                           "Practitioners file (a different year)"),
                "note": GROWTH_NOTE}
    newer = str(to_year or years[0])
    older = str(from_year or years[1])
    if newer == older:
        raise BenchmarkError("pick two different years to compare")
    if older > newer:
        older, newer = newer, older
    for y in (older, newer):
        if y not in years:
            raise BenchmarkError(
                f"no Medicare utilization loaded for {y} — years present: "
                f"{', '.join(years)}")

    tins = resolve_subject_tins(store, subject)
    if not tins:
        raise BenchmarkError(f"no practice matches {subject!r}")
    old_u = practice_utilization(store, tins, older)
    new_u = practice_utilization(store, tins, newer)
    old_by = {c["billing_code"]: float(c["units"] or 0) for c in old_u["codes"]}
    new_by = {c["billing_code"]: float(c["units"] or 0) for c in new_u["codes"]}
    if not old_by and not new_by:
        return {"loaded": False, "subject": subject, "rows": [],
                "from_year": older, "to_year": newer, "years_available": years,
                "reason": ("this practice has no Medicare utilization in either "
                           "year — the PUF may not cover its NPIs"),
                "note": GROWTH_NOTE}

    both = sorted(set(old_by) & set(new_by))
    rows = []
    for code in both:
        o, n = old_by[code], new_by[code]
        rows.append({
            "billing_code": code, "description": code_info(code)[0],
            "units_from": int(o), "units_to": int(n),
            "delta": int(n - o),
            "pct_change": round(100.0 * (n - o) / o, 1) if o else None,
        })
    rows.sort(key=lambda r: r["delta"])

    # totals over the MATCHED codes only: adding a code that merely crossed the
    # suppression threshold would book a fake gain
    tot_old = sum(old_by[c] for c in both)
    tot_new = sum(new_by[c] for c in both)
    pct = round(100.0 * (tot_new - tot_old) / tot_old, 1) if tot_old else None
    direction = ("grew" if pct and pct > 0 else "shrank" if pct and pct < 0
                 else "held flat")
    only_new = sorted(set(new_by) - set(old_by))
    only_old = sorted(set(old_by) - set(new_by))
    return {
        "loaded": True, "subject": subject,
        "from_year": older, "to_year": newer, "years_available": years,
        "rows": rows, "count": len(rows),
        "total_units_from": int(tot_old), "total_units_to": int(tot_new),
        "pct_change": pct, "direction": direction,
        "codes_only_in_newer": [
            {"billing_code": c, "description": code_info(c)[0],
             "units": int(new_by[c])} for c in only_new][:50],
        "codes_only_in_older": [
            {"billing_code": c, "description": code_info(c)[0],
             "units": int(old_by[c])} for c in only_old][:50],
        "headline": (
            f"On codes billed in both years, this practice's Medicare volume "
            f"{direction}"
            + (f" {abs(pct):g}%" if pct else "")
            + f" from {older} to {newer} ({int(tot_old):,} to {int(tot_new):,} "
              "services)."
            + (f" {len(only_new)} code(s) appear only in {newer} and "
               f"{len(only_old)} only in {older} — CMS suppression can do that "
               "on its own, so they are listed apart."
               if (only_new or only_old) else "")),
        "note": GROWTH_NOTE,
    }


def service_line_gaps(store: Store, subject: str, *, zip_code: str | None = None,
                      radius_miles: float = 25.0, year: str | None = None,
                      min_peers: int = 3, centroids_path=None) -> dict:
    """Codes nearby therapy practices bill Medicare that this one does not.

    Distinct from contract gaps: that asks "what do peers have RATES for?",
    this asks "what do peers actually DO?" A code the practice never bills may
    be a service line worth adding — or one it deliberately doesn't offer, so
    every row is a question for the owner.
    """
    from .benchmark import BenchmarkError, resolve_subject_tins
    from .catalog import therapy_taxonomy_sql
    from .medicare import haversine_miles_sql, load_centroids

    tins = resolve_subject_tins(store, subject)
    if not tins:
        raise BenchmarkError(f"no practice matches {subject!r}")
    try:
        radius = max(1.0, min(float(radius_miles), 250.0))
    except (TypeError, ValueError):
        raise BenchmarkError("radius must be a number of miles")
    min_peers = max(1, int(min_peers))

    with store.connect() as con:
        _ensure_view(con, store)
        yr = _latest_year(con, year)
        if not yr:
            return {"loaded": False, "subject": subject, "rows": [],
                    "reason": ("no Medicare utilization is loaded — import a CMS "
                               "Physician & Other Practitioners file first"),
                    "note": SERVICE_LINE_NOTE}
        load_centroids(con, centroids_path)
        home = zip_code and str(zip_code).strip()[:5]
        if not home:
            r = con.execute("""
                SELECT mode(lpad(substr(trim(n.zip), 1, 5), 5, '0'))
                FROM rates r JOIN npi_directory n ON n.npi = r.npi
                WHERE r.tin_value IN (SELECT unnest(?::VARCHAR[]))
                  AND n.zip IS NOT NULL AND trim(n.zip) <> ''
            """, [tins]).fetchone()
            home = r[0] if r else None
        if not home:
            raise BenchmarkError(
                "this practice has no NPPES location on file, so its local "
                "peers cannot be found — run NPI enrichment or pass a ZIP")
        origin = con.execute("SELECT lat, lon FROM _zcta WHERE zip = ?",
                             [home]).fetchone()
        if origin is None:
            raise BenchmarkError(
                f"ZIP {home} is not in the Census ZCTA centroid list")
        miles = haversine_miles_sql("z.lat", "z.lon", origin[0], origin[1])
        in_radius = [r[0] for r in con.execute(
            f"SELECT z.zip FROM _zcta z WHERE {miles} <= ?", [radius]).fetchall()]

        mine = [n for (n,) in con.execute(
            "SELECT DISTINCT npi FROM rates WHERE tin_value IN "
            "(SELECT unnest(?::VARCHAR[])) AND npi IS NOT NULL", [tins]).fetchall()]
        therapy = therapy_taxonomy_sql("n.taxonomy_code", all_col="n.taxonomy_codes")
        cur = con.execute(f"""
            WITH peers AS (
                SELECT DISTINCT n.npi
                FROM npi_directory n
                WHERE lpad(substr(trim(n.zip), 1, 5), 5, '0')
                      IN (SELECT unnest(?::VARCHAR[]))
                  AND {therapy}
                  AND n.npi NOT IN (SELECT unnest(?::VARCHAR[]))
            )
            SELECT u.billing_code,
                   count(DISTINCT u.npi)  AS n_peers,
                   sum(u.services)        AS peer_units,
                   round(median(u.services), 0) AS median_units_each
            FROM utilization u JOIN peers p ON p.npi = u.npi
            WHERE u.data_year = ?
            GROUP BY u.billing_code
            HAVING count(DISTINCT u.npi) >= ?
            ORDER BY n_peers DESC, peer_units DESC
        """, [in_radius, mine, yr, min_peers])
        peer_codes = [dict(zip(["billing_code", "n_peers", "peer_units",
                                "median_units_each"], r)) for r in cur.fetchall()]

    my_codes = {c["billing_code"] for c in
                practice_utilization(store, tins, yr)["codes"]}
    gaps = [c for c in peer_codes if c["billing_code"] not in my_codes]
    for g in gaps:
        g["description"] = code_info(g["billing_code"])[0]
        g["peer_units"] = int(g["peer_units"] or 0)
        g["median_units_each"] = int(g["median_units_each"] or 0)
    return {
        "loaded": True, "subject": subject, "zip": home,
        "radius_miles": radius, "year": yr, "min_peers": min_peers,
        "rows": gaps[:100], "count": len(gaps),
        "n_peer_codes": len(peer_codes), "n_my_codes": len(my_codes),
        "reason": None if gaps else (
            "this practice already bills every therapy code its local peers "
            "bill in this release — no service-line gap to discuss"),
        "headline": (
            f"{len(gaps)} therapy code(s) that {min_peers}+ nearby practices "
            f"bill Medicare and this one does not"
            + (f", led by {gaps[0]['billing_code']} "
               f"({gaps[0]['description']}) at {gaps[0]['n_peers']} practices."
               if gaps else ".")),
        "note": SERVICE_LINE_NOTE,
    }
