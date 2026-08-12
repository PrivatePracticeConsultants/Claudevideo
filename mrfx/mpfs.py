"""Locality-adjusted Medicare fee schedule (MPFS) import from the official
CMS RVU files.

WHY. "% of Medicare" is the lingua franca of every rate negotiation, but the
app previously needed a hand-built code,non_facility_rate CSV — and an
UNADJUSTED national number, which misstates Missouri by several percent. The
official inputs are public: CMS's annual RVU bundle (PPRRVU*.csv + GPCI*.csv
in one zip). The payment formula is published and exact:

    rate = (work_RVU x work_GPCI + nonfac_PE_RVU x PE_GPCI + MP_RVU x MP_GPCI)
           x conversion_factor

ACCURACY RULES:
- The conversion factor is NOT in the RVU files (CMS publishes it separately,
  and it changes mid-year some years). It is a REQUIRED input, validated to a
  sane range and echoed into the stored source string — never guessed from a
  hardcoded table that silently goes stale.
- Locality matters (a state has 1..n localities). Chosen by explicit name if
  given, else the state's "REST OF" locality, else the state's only locality —
  and the choice is disclosed in the source string every report prints.
- Only status-A (active, payable) rows with a blank modifier are loaded: the
  professional-component (26/TC) splits and non-payable statuses would
  misstate an office therapy rate.
- Header names drift slightly across years, so columns are matched by
  normalized tokens, and a file where the required columns can't ALL be found
  is refused with the header echoed — never guessed positionally.

The simple code,non_facility_rate CSV path still works unchanged.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)


class MpfsImportError(Exception):
    """A file that isn't what it claims to be — refuse it, don't guess."""


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _find_col(fieldnames: list[str], *want_all: str, exclude: tuple = ()) -> str | None:
    """First header whose normalized form contains every wanted token and no
    excluded one. Token matching, because CMS renames columns across years
    ('NON-FAC PE RVU' / 'TRANSITIONED NON-FACILITY PE RVU' / …)."""
    for f in fieldnames:
        n = _norm(f)
        if all(w in n for w in want_all) and not any(x in n for x in exclude):
            return f
    return None


def _read_rows(text: str) -> csv.DictReader:
    return csv.DictReader(io.StringIO(text))


def parse_pprrvu(text: str) -> dict[str, dict]:
    """HCPCS -> {work, pe_nonfac, mp} for payable, unmodified rows."""
    r = _read_rows(text)
    fields = list(r.fieldnames or [])
    col_code = _find_col(fields, "hcpcs")
    col_mod = _find_col(fields, "mod")
    col_status = _find_col(fields, "status")
    col_work = _find_col(fields, "work", "rvu")
    col_pe = (_find_col(fields, "non", "fac", "pe", "rvu", exclude=("opps",))
              or _find_col(fields, "nonfacility", "pe", "rvu", exclude=("opps",)))
    col_mp = (_find_col(fields, "mp", "rvu", exclude=("opps",))
              or _find_col(fields, "malpractice", "rvu", exclude=("opps",)))
    missing = [n for n, c in (("HCPCS", col_code), ("WORK RVU", col_work),
                              ("NON-FAC PE RVU", col_pe), ("MP RVU", col_mp))
               if c is None]
    if missing:
        raise MpfsImportError(
            "this does not look like a CMS PPRRVU file — could not find "
            f"column(s) {', '.join(missing)} in header: {fields[:12]}")
    out: dict[str, dict] = {}
    skipped_status = skipped_mod = 0
    for row in r:
        code = (row.get(col_code) or "").strip().upper()
        if not code:
            continue
        mod = (row.get(col_mod) or "").strip() if col_mod else ""
        if mod:                       # 26/TC/53… splits are not the office rate
            skipped_mod += 1
            continue
        status = (row.get(col_status) or "").strip().upper() if col_status else "A"
        if status != "A":             # only active, payable-under-MPFS rows
            skipped_status += 1
            continue
        try:
            out[code] = {"work": float(row.get(col_work) or 0),
                         "pe_nonfac": float(row.get(col_pe) or 0),
                         "mp": float(row.get(col_mp) or 0)}
        except ValueError:
            continue                  # a stray text row ("NOTE: …") in the file
    if not out:
        raise MpfsImportError("the PPRRVU file parsed but produced no payable "
                              "status-A rows — wrong file, or a layout change.")
    log.info("PPRRVU: %s payable codes (skipped %s non-A status, %s modifier rows)",
             f"{len(out):,}", f"{skipped_status:,}", f"{skipped_mod:,}")
    return out


def parse_gpci(text: str, state: str, locality_name: str | None = None) -> dict:
    """The GPCI row to price with: explicit locality name, else the state's
    'REST OF' locality, else the state's only locality."""
    r = _read_rows(text)
    fields = list(r.fieldnames or [])
    col_state = _find_col(fields, "state")
    col_locnum = _find_col(fields, "locality", "number") or _find_col(fields, "locality")
    col_locname = _find_col(fields, "locality", "name") or _find_col(fields, "name")
    col_w = _find_col(fields, "work", "gpci") or _find_col(fields, "pw", "gpci")
    col_pe = _find_col(fields, "pe", "gpci")
    col_mp = _find_col(fields, "mp", "gpci") or _find_col(fields, "malpractice", "gpci")
    missing = [n for n, c in (("state", col_state), ("work GPCI", col_w),
                              ("PE GPCI", col_pe), ("MP GPCI", col_mp)) if c is None]
    if missing:
        raise MpfsImportError(
            "this does not look like a CMS GPCI file — could not find "
            f"column(s) {', '.join(missing)} in header: {fields[:10]}")
    state = (state or "").strip().upper()
    if not state:
        raise MpfsImportError("a state is required to pick the GPCI locality "
                              "(e.g. --state MO)")
    rows = []
    for row in r:
        if (row.get(col_state) or "").strip().upper()[:2] != state[:2]:
            continue
        try:
            rows.append({
                "locality": (row.get(col_locnum) or "").strip() if col_locnum else "",
                "name": (row.get(col_locname) or "").strip() if col_locname else state,
                "work": float(row.get(col_w) or 0),
                "pe": float(row.get(col_pe) or 0),
                "mp": float(row.get(col_mp) or 0),
            })
        except ValueError:
            continue
    if not rows:
        raise MpfsImportError(f"no GPCI rows for state {state!r} — check the "
                              "state code and the file.")
    if locality_name:
        want = _norm(locality_name)
        hits = [x for x in rows if want in _norm(x["name"])]
        if not hits:
            raise MpfsImportError(
                f"no {state} locality matches {locality_name!r}; available: "
                + "; ".join(x["name"] for x in rows))
        return hits[0]
    if len(rows) == 1:
        return rows[0]
    rest = [x for x in rows if "rest of" in _norm(x["name"])]
    return (rest or rows)[0]


def compute_locality_rates(rvus: dict[str, dict], gpci: dict,
                           conversion_factor: float) -> list[dict]:
    if not (20.0 <= float(conversion_factor) <= 60.0):
        raise MpfsImportError(
            f"conversion factor {conversion_factor} is outside any plausible "
            "MPFS range (20-60 $/RVU) — check the CMS-published value for the "
            "file's year.")
    return [{
        "code": code,
        "locality": gpci.get("name", ""),
        "non_facility_rate": round(
            (v["work"] * gpci["work"] + v["pe_nonfac"] * gpci["pe"]
             + v["mp"] * gpci["mp"]) * float(conversion_factor), 2),
    } for code, v in sorted(rvus.items())]


def _pick_from_zip(data: bytes) -> tuple[str, str]:
    """(pprrvu_text, gpci_text) from a CMS RVU bundle zip."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise MpfsImportError("not a readable zip file")
    names = z.namelist()

    def read(patterns):
        for n in names:
            base = Path(n).name.lower()
            if any(base.startswith(p) and base.endswith((".csv", ".txt"))
                   for p in patterns):
                return z.read(n).decode("utf-8-sig", errors="replace")
        return None
    ppr = read(("pprrvu",))
    gp = read(("gpci",))
    if ppr is None or gp is None:
        raise MpfsImportError(
            "the zip does not contain the expected PPRRVU*.csv and GPCI*.csv "
            f"— found: {', '.join(Path(n).name for n in names[:12])}")
    return ppr, gp


def import_official(store, data: bytes, filename: str, *, conversion_factor: float,
                    state: str, locality_name: str | None = None,
                    gpci_data: bytes | None = None) -> dict:
    """Load an official CMS RVU delivery: a bundle zip, or a PPRRVU csv plus a
    separate GPCI csv. Returns what was loaded, locality and CF included —
    the same string every methodology sidecar will print."""
    fname = Path(filename or "upload").name
    if fname.lower().endswith(".zip") or data[:2] == b"PK":
        ppr_text, gpci_text = _pick_from_zip(data)
    else:
        ppr_text = data.decode("utf-8-sig", errors="replace")
        if gpci_data is None:
            raise MpfsImportError(
                "a PPRRVU csv needs its GPCI csv too (they ship in the same "
                "CMS zip — easiest is to load the whole zip).")
        gpci_text = gpci_data.decode("utf-8-sig", errors="replace")
    rvus = parse_pprrvu(ppr_text)
    gpci = parse_gpci(gpci_text, state, locality_name)
    rows = compute_locality_rates(rvus, gpci, conversion_factor)
    source = (f"{fname} · locality {gpci['name']} ({state.upper()}) · "
              f"CF ${float(conversion_factor):.4f} · locality-adjusted non-facility")
    n = store.load_mpfs(rows, source)
    log.info("MPFS: %s codes at %s", f"{n:,}", source)
    return {"rows": n, "source": source, "locality": gpci["name"],
            "conversion_factor": float(conversion_factor)}


def looks_official(data: bytes, filename: str) -> bool:
    """Route an upload: CMS delivery vs the simple code,rate CSV."""
    if (filename or "").lower().endswith(".zip") or data[:2] == b"PK":
        return True
    head = data[:2048].decode("utf-8-sig", errors="replace").lower()
    return "hcpcs" in head and "rvu" in head
