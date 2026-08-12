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
import re
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

# CMS refreshes the Order & Referring roster about twice a week; past this age
# a loaded snapshot is called STALE by the CLI and dashboard, because "is this
# referrer still eligible" answered from an old roster is quietly wrong.
STALE_ROSTER_DAYS = 45


class MedicareImportError(Exception):
    """A file that isn't what it claims to be — refuse it, don't guess."""


def is_valid_npi(s: str) -> bool:
    """True when `s` is a structurally valid NPI: 10 digits, leading 1 or 2
    (the only prefixes NPPES issues), and a correct ISO-7812 Luhn check digit
    computed over the '80840' health-industry prefix. This is how a 10-digit
    phone number in pasted text is told apart from a provider id — a random
    10-digit number passes by chance only ~5% of the time."""
    if len(s) != 10 or not s.isdigit() or s[0] not in "12":
        return False
    total = 0
    for i, ch in enumerate(reversed("80840" + s)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


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
        con.execute("""
            CREATE TABLE IF NOT EXISTS medicare_orf_changes (
                prev_release VARCHAR, release VARCHAR, loaded_at TIMESTAMP,
                added BIGINT, removed BIGINT, partb_lost BIGINT, partb_gained BIGINT)
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS medicare_orf_lost (
                npi VARCHAR, last_name VARCHAR, first_name VARCHAR,
                kind VARCHAR, prev_release VARCHAR, release VARCHAR)
        """)
        diff = None
        con.execute("BEGIN")
        try:
            prev_release = con.execute(
                "SELECT any_value(release) FROM medicare_orf").fetchone()[0]
            con.execute("CREATE OR REPLACE TEMP TABLE _orf_prev AS "
                        "SELECT npi, last_name, first_name, partb FROM medicare_orf")
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
            # Change tracking vs the snapshot just replaced — the actionable
            # movement is "who LOST order/refer standing", so those NPIs are
            # kept by name (medicare_orf_lost) for the referral tables to flag.
            # Only the latest real change is kept: re-importing the SAME
            # release must not overwrite it with an all-zero diff, and a first
            # import has nothing to diff against.
            if prev_release is None:
                con.execute("DELETE FROM medicare_orf_changes")
                con.execute("DELETE FROM medicare_orf_lost")
            elif prev_release != release:
                con.execute("DELETE FROM medicare_orf_changes")
                con.execute("DELETE FROM medicare_orf_lost")
                added, removed, lost, gained = con.execute("""
                    SELECT
                      (SELECT count(*) FROM medicare_orf n
                       WHERE NOT EXISTS (SELECT 1 FROM _orf_prev p WHERE p.npi = n.npi)),
                      (SELECT count(*) FROM _orf_prev p
                       WHERE NOT EXISTS (SELECT 1 FROM medicare_orf n WHERE n.npi = p.npi)),
                      (SELECT count(*) FROM _orf_prev p JOIN medicare_orf n USING (npi)
                       WHERE p.partb AND NOT n.partb),
                      (SELECT count(*) FROM _orf_prev p JOIN medicare_orf n USING (npi)
                       WHERE NOT p.partb AND n.partb)
                """).fetchone()
                con.execute(
                    "INSERT INTO medicare_orf_changes VALUES (?, ?, now(), ?, ?, ?, ?)",
                    [prev_release, release, added, removed, lost, gained])
                con.execute("""
                    INSERT INTO medicare_orf_lost
                    SELECT p.npi, p.last_name, p.first_name, 'removed', ?, ?
                    FROM _orf_prev p
                    WHERE NOT EXISTS (SELECT 1 FROM medicare_orf n WHERE n.npi = p.npi)
                    UNION ALL
                    SELECT p.npi, p.last_name, p.first_name, 'lost_partb', ?, ?
                    FROM _orf_prev p JOIN medicare_orf n USING (npi)
                    WHERE p.partb AND NOT n.partb
                """, [prev_release, release, prev_release, release])
                diff = {"prev_release": prev_release, "added": added,
                        "removed": removed, "partb_lost": lost,
                        "partb_gained": gained}
            con.execute("COMMIT")
        except Exception as e:
            con.execute("ROLLBACK")
            raise MedicareImportError(
                f"{path.name} has the right header but could not be read past "
                f"it ({e}). The previously loaded roster is untouched.") from e
        n = con.execute("SELECT count(*) FROM medicare_orf").fetchone()[0]
    log.info("Medicare Order & Referring: loaded %s providers (release %s)",
             f"{n:,}", release)
    return {"providers": n, "release": release, "path": str(path), "diff": diff}


def import_shared_patients(store: Store, path: str | Path,
                           label: str | None = None, year: str | None = None,
                           interval: str | int | None = None) -> dict:
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
    # Both values are embedded in a COPY statement (which DuckDB cannot
    # parameterize) and `year` also names the output file — so they are
    # validated/sanitized, never trusted. A year of "2022'; DROP…" or "../x"
    # must die here, not in the SQL or the filesystem.
    year = year or "".join(c for c in path.stem if c.isdigit())[:4] or "unknown"
    if not (year == "unknown" or re.fullmatch(r"\d{4}", year)):
        raise MedicareImportError(
            f"'{year}' is not a data year — pass a 4-digit year (e.g. --year 2022).")
    # CMS publishes the SAME year at several windows (pspp_2015_days30 …
    # days365): different files, different pair counts, the same year. Without
    # the interval in the identity the second import would overwrite the first
    # under one name, and the tab would call whichever landed last "2015".
    if interval is None:
        m = re.search(r"days(\d{1,3})\b", path.stem, re.IGNORECASE)
        interval = m.group(1) if m else None
    if interval is not None:
        interval = str(interval).strip()
        if not re.fullmatch(r"\d{1,3}", interval):
            raise MedicareImportError(
                f"'{interval}' is not a day interval — pass a number (e.g. 180).")
    label = label or ("CMS shared-patient" if fmt == FMT_CMS else "DocGraph Hop Teaming")
    label = re.sub(r"[^A-Za-z0-9 ._-]", "", label)[:40] or "shared-patient"
    if interval and "day" not in label.lower():
        label = f"{label} {interval}-day"
    # The vintage identity everything groups by. Two datasets are the same
    # vintage only if format, year AND window match.
    dataset_id = f"{fmt}_{year}" + (f"_{interval}d" if interval else "")

    out_dir = Path(store.dir) / "referrals"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{dataset_id}.parquet"
    p = sql_path(path)

    # Column order differs by format — map explicitly, never positionally.
    # NPIs are trim()ed: the roster import and the rates parser both store
    # trimmed NPIs, and the keep-filter + every downstream join are exact
    # string matches — one space of padding in a delivery would otherwise
    # match NOTHING and silently drop every pair.
    if fmt == FMT_CMS:
        select = ("trim(column0) AS source_npi, trim(column1) AS target_npi, "
                  "TRY_CAST(column3 AS BIGINT) AS patients, "
                  "TRY_CAST(column2 AS BIGINT) AS transactions, "
                  "TRY_CAST(column4 AS BIGINT) AS same_day, "
                  "CAST(NULL AS DOUBLE) AS avg_day_wait")
        reader = f"read_csv('{p}', header = false, all_varchar = true)"
    else:
        select = ("trim(from_npi) AS source_npi, trim(to_npi) AS target_npi, "
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
    #
    # Deliberately UNSORTED output: sorting by target_npi for row-group
    # pruning was measured at 3x the write time (external sort) for a 12%
    # query gain — the hash-filtered scan is already ~3ms over 20M pairs, so
    # the queries were never the cost. Don't re-add it without new numbers.
    tmp = out.with_suffix(out.suffix + ".tmp")
    src, dst = ("column0", "column1") if fmt == FMT_CMS else ("from_npi", "to_npi")
    with store.write_lock, store.connect() as con:
        # existence probe, not a count: a full DISTINCT over the rates view
        # exists only to answer "is there at least one NPI", and on a 300M-row
        # store that is many seconds of scan for a boolean
        if con.execute("SELECT 1 FROM rates WHERE npi IS NOT NULL LIMIT 1").fetchone() is None:
            raise MedicareImportError(
                "this store has no NPIs yet — ingest some rate files first, or "
                "the referral import would have nothing to attach to.")
        try:
            # Materialize the store's DISTINCT NPIs ONCE, then keep pairs with
            # two hash semi-joins against that small table. The previous shape
            # — OR of two correlated EXISTS against the rates view — scanned
            # the full multi-hundred-million-row relation as the build side of
            # BOTH probes; measured 2.5x slower at 20M rows / 5M pairs, and the
            # gap grows with store size because it pays two full-width scans
            # where this pays one two-column scan. Same rows kept either way.
            con.execute("CREATE OR REPLACE TEMP TABLE _store_npis AS "
                        "SELECT DISTINCT npi FROM rates WHERE npi IS NOT NULL")
            con.execute(f"""
                COPY (
                    SELECT {select}, '{label}' AS source_label, '{year}' AS data_year,
                           '{dataset_id}' AS dataset_id
                    FROM {reader} e
                    WHERE trim(e.{src}) IN (SELECT npi FROM _store_npis)
                       OR trim(e.{dst}) IN (SELECT npi FROM _store_npis)
                ) TO '{sql_path(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise MedicareImportError(
                f"{path.name} looked like a {label} file but could not be read "
                f"through ({e}). Previously imported referral data is untouched.") from e
        finally:
            con.execute("DROP TABLE IF EXISTS _store_npis")
        tmp.replace(out)
        # Supersede the pre-interval file for this same format+year, if one is
        # left from an older build. Under that build every window of a year
        # collided into ONE window-less parquet — so that file IS a prior
        # import of this same delivery, and leaving it would list the same
        # data as two "vintages" in every picker.
        legacy = out_dir / f"{fmt}_{year}.parquet"
        if interval and legacy.exists():
            legacy.unlink()
            log.info("referrals: removed the legacy window-less %s "
                     "(superseded by %s)", legacy.name, out.name)
        _register_referral_view(con, store)
        n = con.execute("SELECT count(*) FROM referral_pairs WHERE dataset_id = ?",
                        [dataset_id]).fetchone()[0]
    # 0 kept is honest (the file imported cleanly, nothing matched) but it is
    # almost never what the user meant — a national claims file that shares no
    # provider with a store full of practices is nearly always the WRONG file.
    # The honesty contract keeps the 0; this makes sure it is SAID, not shown
    # as a quiet success the user reads as "done".
    warning = None
    if n == 0:
        warning = (f"0 of {path.name}'s pairs involve any provider in this "
                   "store. The file imported cleanly but matches nothing — "
                   "usually the wrong file (different region or population), "
                   "not a real absence of shared patients.")
        log.warning("referrals: %s", warning)
    else:
        log.info("referrals: kept %s pairs touching this store's providers (%s %s)",
                 f"{n:,}", label, year)
    return {"pairs": n, "format": fmt, "year": year, "label": label,
            "interval": interval, "dataset_id": dataset_id, "path": str(out),
            "warning": warning}


def _register_referral_view(con, store: Store) -> None:
    """(Re)write the referral_pairs view — a CATALOG WRITE, so this belongs on
    the import path (under store.write_lock). Read paths use _ensure below."""
    glob = sql_path(Path(store.dir) / "referrals" / "*.parquet")
    # union_by_name so a store holding BOTH pre-dataset_id parquet and new ones
    # still reads; the missing column is then synthesized below, so every row
    # has a vintage identity whether or not its file was written with one.
    reader = f"read_parquet('{glob}', union_by_name = true)"
    try:
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {reader}").fetchall()}
        sel = ("* REPLACE (coalesce(dataset_id, source_label || '_' || data_year) "
               "AS dataset_id)" if "dataset_id" in cols
               else "*, source_label || '_' || data_year AS dataset_id")
        con.execute(f"CREATE OR REPLACE VIEW referral_pairs AS SELECT {sel} FROM {reader}")
    except Exception:  # noqa: BLE001 — no files yet: an empty view beats a hard error
        con.execute("CREATE OR REPLACE VIEW referral_pairs AS "
                    "SELECT NULL::VARCHAR AS source_npi, NULL::VARCHAR AS target_npi, "
                    "NULL::BIGINT AS patients, NULL::BIGINT AS transactions, "
                    "NULL::BIGINT AS same_day, NULL::DOUBLE AS avg_day_wait, "
                    "NULL::VARCHAR AS source_label, NULL::VARCHAR AS data_year, "
                    "NULL::VARCHAR AS dataset_id WHERE FALSE")


def _ensure_referral_view(con, store: Store) -> None:
    """Read-path variant: probe first, register only when the view is missing
    or its parquet went away. Unconditionally running CREATE OR REPLACE on
    every read is a catalog write — two concurrent dashboard requests doing it
    can collide in DuckDB's catalog (write-write conflict) and 500 for no
    reason. The empty-view fallback only upgrades to real data on the import
    path, which re-registers under the write lock.

    The probe selects dataset_id BY NAME, not `SELECT 1`: views persist in the
    DuckDB catalog, so a store upgraded from before the dataset_id column
    still carries the OLD view definition. That view answers a bare probe
    happily while every real query dies on the missing column — and the broad
    not-loaded catches upstream would read that as "no referral data",
    silently hiding data the user already imported."""
    try:
        con.execute("SELECT dataset_id FROM referral_pairs LIMIT 0")
    except Exception:  # noqa: BLE001 — missing view, vanished parquet, or a
        # pre-upgrade view definition: all healed by re-registering
        _register_referral_view(con, store)


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
                # CMS refreshes this roster ~twice a week; a months-old
                # snapshot quietly answers eligibility wrong. Age is surfaced
                # so the UI/CLI can say "stale" instead of implying current.
                try:
                    out["eligibility"]["age_days"] = (
                        dt.date.today() - dt.date.fromisoformat(r[1])).days
                except (ValueError, TypeError):
                    out["eligibility"]["age_days"] = None
        except Exception:  # noqa: BLE001 — table absent = simply not loaded
            pass
        try:
            c = con.execute(
                "SELECT prev_release, release, added, removed, partb_lost, "
                "partb_gained FROM medicare_orf_changes LIMIT 1").fetchone()
            if c and out["eligibility"]:
                out["eligibility"]["last_change"] = {
                    "prev_release": c[0], "release": c[1], "added": c[2],
                    "removed": c[3], "partb_lost": c[4], "partb_gained": c[5]}
        except Exception:  # noqa: BLE001 — no change recorded yet
            pass
        try:
            _ensure_referral_view(con, store)
            out["referrals"] = [
                {"label": a, "year": b, "pairs": c, "dataset_id": d}
                for a, b, c, d in con.execute(
                    "SELECT any_value(source_label), any_value(data_year), count(*), "
                    "dataset_id FROM referral_pairs GROUP BY dataset_id "
                    "ORDER BY any_value(data_year), dataset_id").fetchall()]
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


def recent_losses(store: Store, npis: list[str]) -> dict[str, str]:
    """npi -> 'removed' | 'lost_partb' for NPIs that lost order/refer standing
    between the two most recent roster imports. The single most actionable
    Medicare fact for a practice: a referrer who just lost Part B standing
    means future claims ordered by them will deny."""
    if not npis:
        return {}
    with store.connect() as con:
        try:
            return {r[0]: r[1] for r in con.execute(
                "SELECT npi, kind FROM medicare_orf_lost "
                "WHERE npi IN (SELECT unnest(?::VARCHAR[]))", [npis]).fetchall()}
        except Exception:  # noqa: BLE001 — no change history yet
            return {}


def active_dataset(store: Store, year: str | None = None,
                   dataset_id: str | None = None) -> tuple[str, str, str] | None:
    """(label, year, dataset_id) of the dataset to answer with — the newest
    loaded unless pinned. ONE dataset at a time, never a blend: see
    org_referrals. Pin by `dataset_id` (exact) or `year` (rejected when the
    year is ambiguous — e.g. CMS 2015 loaded at both 30- and 180-day windows,
    which are different measurements of the same year)."""
    with store.connect() as con:
        try:
            _ensure_referral_view(con, store)
            rows = con.execute(
                "SELECT any_value(source_label), any_value(data_year), dataset_id "
                "FROM referral_pairs GROUP BY dataset_id "
                "ORDER BY any_value(data_year) DESC, dataset_id").fetchall()
        except Exception:  # noqa: BLE001
            return None
    if not rows:
        return None
    have = ", ".join(f"{lbl} {y} [{ds}]" for lbl, y, ds in rows)
    if dataset_id:
        for r in rows:
            if r[2] == dataset_id:
                return tuple(r)
        raise MedicareImportError(
            f"no referral dataset '{dataset_id}' is loaded; have: {have}")
    if year:
        hits = [r for r in rows if r[1] == year]
        if not hits:
            raise MedicareImportError(
                f"no referral data loaded for {year}; have: {have}")
        if len(hits) > 1:
            raise MedicareImportError(
                f"{year} is loaded as more than one dataset — they measure "
                f"different windows and must not be mixed. Pick one: "
                + ", ".join(f"{lbl} [{ds}]" for lbl, _y, ds in hits))
        return tuple(hits[0])
    return tuple(rows[0])


def org_referrals(store: Store, npis: list[str], direction: str = "in",
                  limit: int = 100, year: str | None = None,
                  dataset_id: str | None = None) -> dict:
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
    active = active_dataset(store, year, dataset_id) if npis else None
    if not npis or active is None:
        return {"direction": direction, "rows": [], "dataset": None, "data_year": None,
                "dataset_id": None, "total_partners": 0, "total_patients": 0,
                "caveat": REFERRAL_CAVEAT}
    label, yr, ds_id = active
    mine, theirs = (("target_npi", "source_npi") if direction == "in"
                    else ("source_npi", "target_npi"))
    with store.connect() as con:
        _ensure_referral_view(con, store)
        cur = con.execute(f"""
            SELECT p.{theirs}                             AS npi,
                   coalesce(n.org_name, '')               AS name,
                   coalesce(n.taxonomy_code, '')          AS taxonomy,
                   sum(p.patients)                        AS patients,
                   sum(p.transactions)                    AS transactions,
                   round(avg(p.avg_day_wait), 1)          AS avg_day_wait
            FROM referral_pairs p
            LEFT JOIN npi_directory n ON n.npi = p.{theirs}
            WHERE p.dataset_id = ?
              AND p.{mine} IN (SELECT unnest(?::VARCHAR[]))
              AND p.{theirs} NOT IN (SELECT unnest(?::VARCHAR[]))
            GROUP BY 1, 2, 3
            ORDER BY patients DESC NULLS LAST
            LIMIT {int(limit)}
        """, [ds_id, npis, npis])
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        # Totals over ALL partners, independent of the row limit — "no silent
        # caps": every consumer can say "top 100 of 217" instead of implying
        # the shown rows are the whole story, and summary numbers (the entity
        # drawer's glance) stay exact for practices with more partners than
        # any list shows. Same scan class as the query above (~ms).
        total_partners, total_patients = con.execute(f"""
            SELECT count(DISTINCT p.{theirs}), coalesce(sum(p.patients), 0)
            FROM referral_pairs p
            WHERE p.dataset_id = ?
              AND p.{mine} IN (SELECT unnest(?::VARCHAR[]))
              AND p.{theirs} NOT IN (SELECT unnest(?::VARCHAR[]))
        """, [ds_id, npis, npis]).fetchone()
    return {"direction": direction, "rows": rows, "dataset": label, "data_year": yr,
            "dataset_id": ds_id, "total_partners": total_partners,
            "total_patients": total_patients, "caveat": REFERRAL_CAVEAT}


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
