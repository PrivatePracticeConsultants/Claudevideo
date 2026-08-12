"""Medicare public datasets: order/referring eligibility, and shared-patient
(referral-structure) pairs.

Why these live here. MRFs publish negotiated RATES and nothing else — they
cannot say whether a provider may order/refer for Medicare, nor where a
practice's patients come from. Two public CMS datasets fill exactly those gaps,
and both key on NPI, which this store already carries and already enriches with
NPPES names and taxonomies:

- **Order & Referring** (~2M rows, refreshed ~twice a week): NPI + five
  eligibility flags. Small, current, no licence restrictions.
- **Physician Shared Patient Patterns** (~35M pairs, 2015 FOIA release) or a
  licensed DocGraph Hop Teaming year (140–210M pairs): directed provider pairs
  with shared-patient counts — the structure of a referral market.

IMPORT, never download. The Medicare Order & Referring Tracker already fetches
and validates these, so re-downloading 70 MB — or 7–11 GB for a Hop Teaming
year — would be wasteful and would fork the provenance. We read the files that
tool already put on disk.

READ THE LIMITS (they are repeated into every export):
- Shared-patient is NOT referral. Labs, imaging and hospitals appear as
  "sources" purely from co-occurring care. Interpret by specialty.
- CMS suppresses pairs under 11 patients, so low-volume referrers are invisible.
- The free CMS release covers Jan–Sep 2015. It is market STRUCTURE, not current
  volume. Hop Teaming years run to 2022 and are CC BY-NC-SA (non-commercial) —
  commercial use needs a licence from CareSet.
- Patient counts sum per source; one patient sent by three sources counts three
  times. Rank with it; never call it a headcount.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from .store import Store, sql_path

log = logging.getLogger(__name__)

# The tracker validates this exact header and refuses anything else; so do we,
# rather than silently mis-column a 2M-row roster.
ORF_HEADER = ("NPI", "LAST_NAME", "FIRST_NAME", "PARTB", "DME", "HHA", "PMD", "HOSPICE")
ORF_FLAGS = ("partb", "dme", "hha", "pmd", "hospice")

# Shared-patient layouts, per the CMS and CareSet deliveries:
#   CMS  — headerless, 5 fields: npi1, npi2, pair_count, bene_count, same_day
#   HOP  — header row,  6 fields: from_npi, to_npi, patient_count,
#                                 transaction_count, average_day_wait, std_day_wait
# NOTE the column ORDER DIFFERS: the shared-PATIENT number is field 4 in the CMS
# file but field 3 in Hop Teaming. Reading them positionally as if they matched
# would silently report transaction counts as patients.
FMT_CMS, FMT_HOP = "cms-shared-patient", "hop-teaming"


class MedicareImportError(Exception):
    """A file that isn't what it claims to be — refuse it, don't guess."""


def _peek_format(path: Path) -> str:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            fields = line.split(",")
            head = fields[0].strip().strip('"').lower()
            if head == "from_npi":
                return FMT_HOP
            if len(fields) == 5:
                return FMT_CMS
            if len(fields) == 6:
                return FMT_HOP
            raise MedicareImportError(
                f"{path.name}: first data line has {len(fields)} comma-separated "
                "fields; a CMS shared-patient file has 5 and a Hop Teaming file "
                "has 6. This does not look like either.")
    raise MedicareImportError(f"{path.name} is empty.")


def import_orf_roster(store: Store, path: str | Path) -> dict:
    """Load a CMS Order & Referring snapshot (NPI + eligibility flags)."""
    path = Path(path)
    if not path.exists():
        raise MedicareImportError(f"no such file: {path}")
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        header = [c.strip().strip('"').upper() for c in (fh.readline() or "").split(",")]
    if tuple(header) != ORF_HEADER:
        raise MedicareImportError(
            f"{path.name} does not have the Order & Referring layout. Expected "
            f"{', '.join(ORF_HEADER)} but found {', '.join(header) or '(nothing)'}. "
            "If CMS changed the file, this refuses it rather than loading wrong "
            "columns.")
    # release date from the tracker's filename convention, else the file mtime
    stem = path.stem
    release = stem.split("_")[-1] if stem.lower().startswith("orderreferring_") else ""
    if not release:
        release = dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")

    p = sql_path(path)
    with store.write_lock, store.connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS medicare_orf (
                npi VARCHAR PRIMARY KEY, last_name VARCHAR, first_name VARCHAR,
                partb BOOLEAN, dme BOOLEAN, hha BOOLEAN, pmd BOOLEAN,
                hospice BOOLEAN, release VARCHAR, loaded_at TIMESTAMP)
        """)
        # One transaction for replace-with-snapshot. Connections are autocommit,
        # so an unwrapped DELETE + INSERT would commit the DELETE alone — and a
        # file that passes the header check but dies mid-body (truncated
        # download, ragged row) would WIPE the roster it was meant to replace.
        # The tracker's contract, kept here: a failed load leaves the previous
        # snapshot untouched.
        con.execute("BEGIN")
        try:
            con.execute("DELETE FROM medicare_orf")   # a snapshot REPLACES, never merges
            con.execute(f"""
                INSERT INTO medicare_orf
                SELECT trim(NPI), trim(LAST_NAME), trim(FIRST_NAME),
                       upper(trim(PARTB))   = 'Y', upper(trim(DME))  = 'Y',
                       upper(trim(HHA))     = 'Y', upper(trim(PMD))  = 'Y',
                       upper(trim(HOSPICE)) = 'Y', ?, now()
                FROM read_csv('{p}', header = true, all_varchar = true)
                WHERE NPI IS NOT NULL AND trim(NPI) <> ''
            """, [release])
            con.execute("COMMIT")
        except Exception as e:
            con.execute("ROLLBACK")
            raise MedicareImportError(
                f"{path.name} has the right header but could not be read past "
                f"it ({e}). The previously loaded roster is untouched.") from e
        n = con.execute("SELECT count(*) FROM medicare_orf").fetchone()[0]
    log.info("Medicare Order & Referring: loaded %s providers (release %s)",
             f"{n:,}", release)
    return {"providers": n, "release": release, "path": str(path)}


def import_shared_patients(store: Store, path: str | Path,
                           label: str | None = None, year: str | None = None) -> dict:
    """Load a shared-patient pair file into the store as Parquet.

    Streamed and converted by DuckDB itself — a 210M-row Hop Teaming year is
    never materialized in Python. Only pairs where at least one side is an NPI
    this store has rates for are kept: the point is to explain THIS book's
    practices, and keeping all 210M pairs would dwarf the rate data itself.
    """
    path = Path(path)
    if not path.exists():
        raise MedicareImportError(f"no such file: {path}")
    fmt = _peek_format(path)
    year = year or "".join(c for c in path.stem if c.isdigit())[:4] or "unknown"
    label = label or ("CMS shared-patient" if fmt == FMT_CMS else "DocGraph Hop Teaming")

    out_dir = Path(store.dir) / "referrals"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{fmt}_{year}.parquet"
    p = sql_path(path)

    # Column order differs by format — map explicitly, never positionally.
    if fmt == FMT_CMS:
        select = ("column0 AS source_npi, column1 AS target_npi, "
                  "TRY_CAST(column3 AS BIGINT) AS patients, "
                  "TRY_CAST(column2 AS BIGINT) AS transactions, "
                  "TRY_CAST(column4 AS BIGINT) AS same_day, "
                  "CAST(NULL AS DOUBLE) AS avg_day_wait")
        reader = f"read_csv('{p}', header = false, all_varchar = true)"
    else:
        select = ("from_npi AS source_npi, to_npi AS target_npi, "
                  "TRY_CAST(patient_count AS BIGINT) AS patients, "
                  "TRY_CAST(transaction_count AS BIGINT) AS transactions, "
                  "CAST(NULL AS BIGINT) AS same_day, "
                  "TRY_CAST(average_day_wait AS DOUBLE) AS avg_day_wait")
        reader = f"read_csv('{p}', header = true, all_varchar = true)"

    # Write to a .tmp beside the target and rename only on success — the
    # registered view globs *.parquet, so a COPY that dies partway (bad row
    # deep in a 7 GB file, disk full) must never leave a corrupt file where
    # the glob will read it: one bad import would blind the WHOLE
    # referral_pairs view, prior good datasets included. Same convention as
    # the parser workers' *.parquet.tmp handoff.
    tmp = out.with_suffix(out.suffix + ".tmp")
    with store.write_lock, store.connect() as con:
        known = con.execute("SELECT count(*) FROM (SELECT DISTINCT npi FROM rates)").fetchone()[0]
        if not known:
            raise MedicareImportError(
                "this store has no NPIs yet — ingest some rate files first, or "
                "the referral import would have nothing to attach to.")
        try:
            con.execute(f"""
                COPY (
                    SELECT {select}, '{label}' AS source_label, '{year}' AS data_year
                    FROM {reader} e
                    WHERE EXISTS (SELECT 1 FROM rates r WHERE r.npi = e.{'column0' if fmt == FMT_CMS else 'from_npi'})
                       OR EXISTS (SELECT 1 FROM rates r WHERE r.npi = e.{'column1' if fmt == FMT_CMS else 'to_npi'})
                ) TO '{sql_path(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise MedicareImportError(
                f"{path.name} looked like a {label} file but could not be read "
                f"through ({e}). Previously imported referral data is untouched.") from e
        tmp.replace(out)
        _register_referral_view(con, store)
        n = con.execute("SELECT count(*) FROM referral_pairs WHERE data_year = ?",
                        [year]).fetchone()[0]
    log.info("referrals: kept %s pairs touching this store's providers (%s %s)",
             f"{n:,}", label, year)
    return {"pairs": n, "format": fmt, "year": year, "label": label, "path": str(out)}


def _register_referral_view(con, store: Store) -> None:
    glob = sql_path(Path(store.dir) / "referrals" / "*.parquet")
    try:
        con.execute(f"CREATE OR REPLACE VIEW referral_pairs AS SELECT * FROM read_parquet('{glob}')")
    except Exception:  # noqa: BLE001 — no files yet: an empty view beats a hard error
        con.execute("CREATE OR REPLACE VIEW referral_pairs AS "
                    "SELECT NULL::VARCHAR AS source_npi, NULL::VARCHAR AS target_npi, "
                    "NULL::BIGINT AS patients, NULL::BIGINT AS transactions, "
                    "NULL::BIGINT AS same_day, NULL::DOUBLE AS avg_day_wait, "
                    "NULL::VARCHAR AS source_label, NULL::VARCHAR AS data_year WHERE FALSE")


def medicare_status(store: Store) -> dict:
    """What Medicare data is loaded — for `mrfx status` and the dashboard."""
    out = {"eligibility": None, "referrals": []}
    with store.connect() as con:
        try:
            r = con.execute("SELECT count(*), any_value(release), max(loaded_at) "
                            "FROM medicare_orf").fetchone()
            if r and r[0]:
                out["eligibility"] = {"providers": r[0], "release": r[1],
                                      "loaded_at": str(r[2])[:19]}
        except Exception:  # noqa: BLE001 — table absent = simply not loaded
            pass
        try:
            _register_referral_view(con, store)
            out["referrals"] = [
                {"label": a, "year": b, "pairs": c}
                for a, b, c in con.execute(
                    "SELECT source_label, data_year, count(*) FROM referral_pairs "
                    "GROUP BY 1, 2 ORDER BY 2").fetchall()]
        except Exception:  # noqa: BLE001
            pass
    return out


def npi_eligibility(store: Store, npis: list[str]) -> list[dict]:
    """Order & Referring status for each NPI, in the order given.

    An NPI that is simply ABSENT is reported as 'not on the list' — which is a
    real finding for a referring physician (a claims-denial risk) but is normal
    and expected for therapists and organizations, who are not on this roster at
    all. The caller must not read absence as a problem without knowing which."""
    if not npis:
        return []
    with store.connect() as con:
        try:
            rows = {r[0]: r for r in con.execute(
                "SELECT npi, last_name, first_name, partb, dme, hha, pmd, hospice, release "
                "FROM medicare_orf WHERE npi IN (SELECT unnest(?::VARCHAR[]))",
                [npis]).fetchall()}
        except Exception:  # noqa: BLE001 — not loaded
            return []
    out = []
    for npi in npis:
        r = rows.get(npi)
        out.append({
            "npi": npi,
            "on_list": bool(r),
            "name": f"{(r[2] or '').title()} {(r[1] or '').title()}".strip() if r else "",
            **{f: (bool(r[3 + i]) if r else None) for i, f in enumerate(ORF_FLAGS)},
            "release": r[8] if r else None,
        })
    return out


def active_dataset(store: Store, year: str | None = None) -> tuple[str, str] | None:
    """(label, year) of the dataset to answer with — the newest loaded unless
    pinned. ONE dataset at a time, never a blend: see org_referrals."""
    with store.connect() as con:
        try:
            _register_referral_view(con, store)
            rows = con.execute(
                "SELECT any_value(source_label), data_year FROM referral_pairs "
                "GROUP BY data_year ORDER BY data_year DESC").fetchall()
        except Exception:  # noqa: BLE001
            return None
    if not rows:
        return None
    if year:
        for label, y in rows:
            if y == year:
                return (label, y)
        raise MedicareImportError(
            f"no referral data loaded for {year}; have: "
            + ", ".join(f"{lbl} {y}" for lbl, y in rows))
    return (rows[0][0], rows[0][1])


def org_referrals(store: Store, npis: list[str], direction: str = "in",
                  limit: int = 100, year: str | None = None) -> dict:
    """Who shares patients INTO this practice (direction='in'), or where it
    shares them onward ('out'), aggregated over the practice's NPIs.

    ONE dataset at a time. Different releases measure different things over
    different windows — the free CMS file covers Jan–Sep 2015, a Hop Teaming
    year covers a full calendar year with a different attribution method — so
    adding a 2015 patient count to a 2022 one produces a number that describes
    no period at all. (Caught in testing: with both loaded, a source's inbound
    total was silently CMS + Hop.) Defaults to the newest loaded release and
    always reports which one answered.

    Names and specialties come from this store's own NPPES enrichment, so a
    source is identifiable without any extra dataset."""
    if direction not in ("in", "out"):
        raise ValueError("direction must be 'in' or 'out'")
    active = active_dataset(store, year) if npis else None
    if not npis or active is None:
        return {"direction": direction, "rows": [], "dataset": None, "data_year": None,
                "caveat": REFERRAL_CAVEAT}
    label, yr = active
    mine, theirs = (("target_npi", "source_npi") if direction == "in"
                    else ("source_npi", "target_npi"))
    with store.connect() as con:
        _register_referral_view(con, store)
        cur = con.execute(f"""
            SELECT p.{theirs}                             AS npi,
                   coalesce(n.org_name, '')               AS name,
                   coalesce(n.taxonomy_code, '')          AS taxonomy,
                   sum(p.patients)                        AS patients,
                   sum(p.transactions)                    AS transactions,
                   round(avg(p.avg_day_wait), 1)          AS avg_day_wait
            FROM referral_pairs p
            LEFT JOIN npi_directory n ON n.npi = p.{theirs}
            WHERE p.data_year = ?
              AND p.{mine} IN (SELECT unnest(?::VARCHAR[]))
              AND p.{theirs} NOT IN (SELECT unnest(?::VARCHAR[]))
            GROUP BY 1, 2, 3
            ORDER BY patients DESC NULLS LAST
            LIMIT {int(limit)}
        """, [yr, npis, npis])
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    return {"direction": direction, "rows": rows, "dataset": label, "data_year": yr,
            "caveat": REFERRAL_CAVEAT}


# Compact NUCC prefix -> readable family, for the dashboard's referral tables.
# Deliberately coarse: the point is "orthopedic surgeon vs lab vs hospital",
# which is what the shared-patient caveat says to interpret by. Unknown codes
# fall back to the raw code — never a guess.
_TAXONOMY_FAMILIES = [
    ("2251", "Physical therapist"), ("2252", "PT assistant"),
    ("225X", "Occupational therapist"), ("224Z", "OT assistant"),
    ("235Z", "Speech-language pathologist"), ("231H", "Audiologist"),
    ("261QP2000", "PT clinic"), ("261QR04", "Rehab clinic"),
    ("261QR08", "Radiology center"), ("261QU", "Urgent care"),
    ("261QM13", "Multi-specialty clinic"), ("261Q", "Clinic/center"),
    ("207X", "Orthopedic surgery"), ("2081", "Physiatry (PM&R)"),
    ("207Q", "Family medicine"), ("207R", "Internal medicine"),
    ("207T", "Neurosurgery"), ("2084", "Neurology/psychiatry"),
    ("207ZP", "Pathology"), ("2085", "Radiology"), ("2086", "Surgery"),
    ("207", "Physician"), ("208", "Physician"),
    ("363L", "Nurse practitioner"), ("363A", "Physician assistant"),
    ("111N", "Chiropractor"), ("213E", "Podiatrist"),
    ("291U", "Clinical laboratory"), ("29", "Laboratory"),
    ("282N", "General acute hospital"), ("28", "Hospital"),
    ("314000", "Skilled nursing facility"), ("31", "Nursing/custodial facility"),
    ("251E", "Home health agency"), ("25", "Agency"),
    ("332B", "DME supplier"), ("33", "Supplier"),
]


def taxonomy_label(code: str | None) -> str:
    """Readable family for an NPPES taxonomy code; the raw code if unknown."""
    if not code:
        return ""
    for prefix, label in _TAXONOMY_FAMILIES:
        if code.startswith(prefix):
            return label
    return code


REFERRAL_CAVEAT = (
    "Shared-patient data is NOT a referral record. A pair means two providers "
    "saw the same Medicare patient in sequence — labs, imaging and hospitals "
    "appear as 'sources' purely from co-occurring care, so interpret by "
    "specialty. CMS suppresses pairs under 11 patients, so low-volume referrers "
    "are invisible. Patient counts sum per source: one patient sent by three "
    "sources counts three times, so rank with it but never call it a headcount. "
    "It describes market structure for its data year, not current volume."
)
