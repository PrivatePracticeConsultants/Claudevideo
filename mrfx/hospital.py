"""Hospital price transparency: what the SAME payer pays the hospital next door.

WHY. Every rate in this app comes from a payer's insurer MRF, so the only
comparison available is practice vs practice. The strongest lever in a therapy
rate negotiation is the other one: the hospital outpatient department across
town bills the same 97xxx codes to the same payer, and is paid a multiple of
what the private practice is paid. CMS requires every hospital to publish that
number in a machine-readable standard-charges file, so it is obtainable — it
just lives in a different file format, under a different regulation, with a
different unit of analysis.

WHAT THE NUMBER IS — AND IS NOT. This is the caveat that makes the feature
honest, and it is attached to every row, every table and every export:

- A hospital's negotiated rate for 97110 is a **FACILITY (HOPD) rate**. It pays
  for the department: space, equipment, overhead and staff. A private practice's
  professional rate covers the same visit but a different cost base, and a
  hospital often bills a separate professional line on top. The two numbers
  describe the same CPT code, NOT the same economics.
- So this is a **reference point for a negotiation** ("this payer already pays
  $X in this market for this code"), never a claim that the practice is
  underpaid BY that difference. The app never subtracts one from the other and
  calls the result an opportunity.
- Hospitals publish standard charges in several forms. Only **dollar amounts**
  are kept: a "percentage of billed charges" or an algorithm description is not
  a price, and estimated_amount (the hospital's own estimate behind an
  algorithm) is kept SEPARATELY and labelled as an estimate.
- File quality varies enormously — this is a young mandate. A file we cannot
  read is a refusal naming what was wrong, never a partial silent import.

FORMATS. Both CMS v2.x shapes are read: the tall CSV (what most hospitals
publish) and the JSON. The JSON is streamed with ijson exactly like an insurer
MRF — these files reach several GB and must never be json.load()ed.
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

from .catalog import CODE_CATALOG, code_info, resolve_discipline
from .sniff import open_stream
from .store import Store, sql_path

log = logging.getLogger(__name__)


class HospitalImportError(Exception):
    """Refuse a file we cannot read honestly, and say what was wrong."""


HOSPITAL_CAVEAT = (
    "Hospital rates come from CMS hospital price-transparency files and are "
    "FACILITY (outpatient department) rates: they pay for the department — "
    "space, equipment and overhead — not a private practice's professional "
    "fee, and a hospital often bills a separate professional line on top. The "
    "same CPT code, not the same economics. Use the gap as evidence of what "
    "this payer already pays in this market, never as an amount the practice "
    "is owed."
)

# Only outpatient-ish settings are comparable at all; an inpatient DRG-era row
# for a therapy code is a different product entirely.
_KEEP_SETTINGS = ("outpatient", "both", "")

_DOLLAR_COLS = (
    "standard_charge|negotiated_dollar",
    "standard_charge_negotiated_dollar",
    "standard_charge|negotiated|dollar",
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _slug(s: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "").strip())[:80].strip("_")
    return out or "hospital"


def _num(v) -> float | None:
    """A dollar amount, or None. '' / 'N/A' / '25%' are NOT dollar amounts."""
    if v is None:
        return None
    s = str(v).strip().replace("$", "").replace(",", "")
    if not s or s.endswith("%"):
        return None
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    # 0 and negatives are placeholders here exactly as they are in insurer MRFs
    return f if f > 0.01 else None


def _wanted_codes(codes=None) -> set[str]:
    return {str(c).upper() for c in (codes or CODE_CATALOG.keys())}


# ---------------------------------------------------------------- CSV (tall)

def _csv_columns(header: list[str]) -> dict:
    """Map the columns we need. Hospitals vary the spelling and the vendor
    templates drift, so match on normalized tokens rather than exact names."""
    idx = {}
    for i, raw in enumerate(header):
        idx.setdefault(_norm(raw), i)

    def find(*cands, contains=None):
        for c in cands:
            if _norm(c) in idx:
                return idx[_norm(c)]
        if contains:
            for k, i in idx.items():
                if all(t in k for t in contains):
                    return i
        return None

    return {
        "code": find("code|1", "code 1", "code", contains=("code", "1")),
        "code_type": find("code|1|type", "code 1 type", contains=("code", "type")),
        "description": find("description"),
        "payer": find("payer_name", "payer name", contains=("payer", "name")),
        "plan": find("plan_name", "plan name", contains=("plan", "name")),
        "setting": find("setting"),
        "dollar": find(*_DOLLAR_COLS, contains=("negotiated", "dollar")),
        "methodology": find("standard_charge|methodology", contains=("methodology",)),
        "estimate": find("estimated_amount", contains=("estimated", "amount")),
        "gross": find("standard_charge|gross", contains=("gross",)),
        "cash": find("standard_charge|discounted_cash", contains=("discounted", "cash")),
        "modifiers": find("modifiers"),
    }


def _csv_meta(path: Path) -> dict:
    """The CMS tall CSV carries hospital identity in the first two lines,
    ABOVE the real header row. Read what is there; never invent it."""
    meta = {"hospital_name": None, "last_updated_on": None,
            "version": None, "header_row": 0}
    try:
        with open_stream(path) as fh:
            text = fh.read(1 << 16).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return meta
    lines = text.splitlines()
    if len(lines) < 2:
        return meta
    keys = [_norm(k) for k in next(csv.reader(lines[:1]), [])]
    vals = next(csv.reader(lines[1:2]), [])
    if "hospital name" in keys or "hospital_name" in keys:
        pair = dict(zip(keys, vals))
        meta.update({
            "hospital_name": pair.get("hospital name") or pair.get("hospital_name"),
            "last_updated_on": pair.get("last updated on") or pair.get("last_updated_on"),
            "version": pair.get("version"),
            "header_row": 2,
        })
    return meta


def _read_csv_rows(path: Path, want: set[str]):
    """Yield normalized rows from a tall CSV. Streaming: these files run to
    millions of rows and hundreds of MB."""
    meta = _csv_meta(path)
    with open_stream(path) as raw:
        import io
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        reader = csv.reader(text)
        for _ in range(meta["header_row"]):
            next(reader, None)
        header = next(reader, None)
        if not header:
            raise HospitalImportError("the file has no header row")
        col = _csv_columns(header)
        if col["code"] is None or col["dollar"] is None:
            raise HospitalImportError(
                "this CSV has no code column and/or no negotiated-dollar column "
                "— it does not look like a CMS hospital standard-charges file "
                f"(columns seen: {', '.join(header[:12])}…)")

        def at(row, key):
            i = col[key]
            return (row[i] if i is not None and i < len(row) else None) or None

        for row in reader:
            code = str(at(row, "code") or "").strip().upper()
            if code not in want:
                continue
            setting = _norm(at(row, "setting") or "")
            if setting and setting not in _KEEP_SETTINGS:
                continue
            dollar = _num(at(row, "dollar"))
            estimate = _num(at(row, "estimate"))
            if dollar is None and estimate is None:
                continue
            yield {
                "hospital_name": meta["hospital_name"],
                "last_updated_on": meta["last_updated_on"],
                "billing_code": code,
                "code_type": str(at(row, "code_type") or "").strip().upper() or None,
                "payer_raw": str(at(row, "payer") or "").strip() or None,
                "plan_name": str(at(row, "plan") or "").strip() or None,
                "setting": setting or None,
                "rate": dollar,
                "estimated_amount": estimate,
                "methodology": str(at(row, "methodology") or "").strip() or None,
                "gross_charge": _num(at(row, "gross")),
                "cash_price": _num(at(row, "cash")),
                "modifiers": str(at(row, "modifiers") or "").strip() or None,
            }


# -------------------------------------------------------------------- JSON

def _read_json_rows(path: Path, want: set[str]):
    """Yield normalized rows from a CMS v2 JSON file, streamed with ijson.

    Never json.load: these reach several GB, the same reason the insurer
    parser is event-based."""
    import ijson

    meta = {"hospital_name": None, "last_updated_on": None}
    # a cheap first pass for the scalar header fields, then the big array
    try:
        with open_stream(path) as stream:
            for prefix, _event, value in ijson.parse(stream):
                if prefix == "hospital_name":
                    meta["hospital_name"] = str(value)[:200]
                elif prefix == "last_updated_on":
                    meta["last_updated_on"] = str(value)[:40]
                elif prefix.startswith("standard_charge_information"):
                    break
    except Exception as e:  # noqa: BLE001
        raise HospitalImportError(f"could not read the file header: {e}")

    with open_stream(path) as stream:
        try:
            items = ijson.items(stream, "standard_charge_information.item")
            for item in items:
                codes = [
                    str(c.get("code") or "").strip().upper()
                    for c in (item.get("code_information") or [])
                    if isinstance(c, dict)
                ]
                hit = [c for c in codes if c in want]
                if not hit:
                    continue
                ctype = next((str(c.get("type") or "").upper()
                              for c in (item.get("code_information") or [])
                              if isinstance(c, dict)
                              and str(c.get("code") or "").strip().upper() in hit),
                             None)
                for sc in (item.get("standard_charges") or []):
                    if not isinstance(sc, dict):
                        continue
                    setting = _norm(str(sc.get("setting") or ""))
                    if setting and setting not in _KEEP_SETTINGS:
                        continue
                    gross = _num(sc.get("gross_charge"))
                    cash = _num(sc.get("discounted_cash"))
                    for pi in (sc.get("payers_information") or []):
                        if not isinstance(pi, dict):
                            continue
                        dollar = _num(pi.get("standard_charge_dollar"))
                        estimate = _num(pi.get("estimated_amount"))
                        if dollar is None and estimate is None:
                            continue
                        for code in hit:
                            yield {
                                "hospital_name": meta["hospital_name"],
                                "last_updated_on": meta["last_updated_on"],
                                "billing_code": code,
                                "code_type": ctype,
                                "payer_raw": str(pi.get("payer_name") or "").strip() or None,
                                "plan_name": str(pi.get("plan_name") or "").strip() or None,
                                "setting": setting or None,
                                "rate": dollar,
                                "estimated_amount": estimate,
                                "methodology": str(
                                    pi.get("standard_charge_algorithm")
                                    or pi.get("methodology") or "").strip()[:120] or None,
                                "gross_charge": gross,
                                "cash_price": cash,
                                "modifiers": "|".join(
                                    str(m) for m in (sc.get("billing_code_modifier") or [])
                                ) or None,
                            }
        except HospitalImportError:
            raise
        except Exception as e:  # noqa: BLE001
            raise HospitalImportError(
                f"the JSON is not a CMS standard-charges file we can read: {e}")


def _looks_like_json(path: Path) -> bool:
    try:
        with open_stream(path) as fh:
            head = fh.read(4096).lstrip()
        return head[:1] in (b"{", b"[")
    except Exception:  # noqa: BLE001
        return path.suffix.lower().endswith("json")


def import_hospital_file(store: Store, path: str | Path, *,
                         hospital_name: str | None = None,
                         state: str | None = None, city: str | None = None,
                         codes=None, cfg=None) -> dict:
    """Import ONE hospital's standard-charges file, keeping therapy codes only.

    Written to a .tmp and renamed on success, so a failed import can never
    leave a partial parquet inside the view's glob (same rule as every other
    reference importer here). Re-importing the same hospital REPLACES its part,
    which keeps the import idempotent."""
    path = Path(path)
    if not path.exists():
        raise HospitalImportError(f"no such file: {path}")
    want = _wanted_codes(codes)

    rows: list[dict] = []
    reader = _read_json_rows if _looks_like_json(path) else _read_csv_rows
    n_seen = 0
    for r in reader(path, want):
        n_seen += 1
        rows.append(r)
        if len(rows) > 2_000_000:      # a therapy-filtered file is tiny; this is a guard
            raise HospitalImportError(
                "this file yielded more therapy rows than any real hospital "
                "publishes — it is probably not a single hospital's file")

    name = (hospital_name or "").strip() or next(
        (r["hospital_name"] for r in rows if r.get("hospital_name")), None)
    if not name:
        name = path.stem
    st = (state or "").strip().upper()[:2] or None
    ct = (city or "").strip() or None
    last_updated = next((r["last_updated_on"] for r in rows if r.get("last_updated_on")), None)

    normalize = cfg.normalize_payer if cfg is not None else (lambda s: s)
    for r in rows:
        r["hospital_name"] = name
        r["state"] = st
        r["city"] = ct
        r["payer"] = normalize(r.pop("payer_raw") or "") or "Unknown payer"
        r["source_file"] = path.name
        r["last_updated_on"] = last_updated
        # hospital files carry no GP/GO/GN modifier, so a shared code is
        # honestly 'unspecified' — the same rule the insurer parser applies
        r["discipline"] = resolve_discipline(r["billing_code"], [])

    if not rows:
        # An empty result is a legitimate outcome (many hospitals publish no
        # therapy line at all) — report it as such rather than as a failure,
        # and DON'T write an empty part that would look like data.
        return {"hospital": name, "rows": 0, "codes": 0, "payers": 0,
                "state": st, "file": path.name, "last_updated_on": last_updated,
                "note": ("no therapy codes with a negotiated dollar amount were "
                         "found in this file — many hospitals publish only "
                         "percentage-of-charges terms for these codes, or no "
                         "therapy line at all"),
                "caveat": HOSPITAL_CAVEAT}

    out_dir = Path(store.dir) / "hospital"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{_slug(name)}.parquet"
    tmp = out.with_suffix(".parquet.tmp")

    import pyarrow as pa
    import pyarrow.parquet as pq

    cols = ["hospital_name", "state", "city", "payer", "plan_name",
            "billing_code", "code_type", "discipline", "setting", "methodology",
            "modifiers", "source_file", "last_updated_on"]
    nums = ["rate", "estimated_amount", "gross_charge", "cash_price"]
    table = pa.table(
        {**{c: pa.array([r.get(c) for r in rows], pa.string()) for c in cols},
         **{c: pa.array([r.get(c) for r in rows], pa.float64()) for c in nums}})
    try:
        pq.write_table(table, tmp, compression="zstd")
        tmp.replace(out)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    with store.write_lock, store.connect() as con:
        _register_view(con, store)
        con.execute(
            "INSERT OR REPLACE INTO hospital_files "
            "(hospital_name, state, city, source_file, rows, last_updated_on, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, current_timestamp)",
            [name, st, ct, path.name, len(rows), last_updated])

    return {"hospital": name, "rows": len(rows),
            "codes": len({r["billing_code"] for r in rows}),
            "payers": len({r["payer"] for r in rows}),
            "state": st, "city": ct, "file": path.name,
            "last_updated_on": last_updated, "note": None,
            "caveat": HOSPITAL_CAVEAT}


def _register_view(con, store: Store) -> None:
    """(Re)point `hospital_rates` at whatever parts exist. Callers hold the
    write lock. An empty directory yields an EMPTY TYPED view, never a missing
    relation — every reader can then query unconditionally."""
    d = Path(store.dir) / "hospital"
    if any(d.glob("*.parquet")):
        con.execute("CREATE OR REPLACE VIEW hospital_rates AS SELECT * FROM "
                    f"read_parquet('{sql_path(d / '*.parquet')}')")
    else:
        con.execute(
            "CREATE OR REPLACE VIEW hospital_rates AS SELECT "
            "CAST(NULL AS VARCHAR) AS hospital_name, CAST(NULL AS VARCHAR) AS state, "
            "CAST(NULL AS VARCHAR) AS city, CAST(NULL AS VARCHAR) AS payer, "
            "CAST(NULL AS VARCHAR) AS plan_name, CAST(NULL AS VARCHAR) AS billing_code, "
            "CAST(NULL AS VARCHAR) AS code_type, CAST(NULL AS VARCHAR) AS discipline, "
            "CAST(NULL AS VARCHAR) AS setting, CAST(NULL AS VARCHAR) AS methodology, "
            "CAST(NULL AS VARCHAR) AS modifiers, CAST(NULL AS VARCHAR) AS source_file, "
            "CAST(NULL AS VARCHAR) AS last_updated_on, CAST(NULL AS DOUBLE) AS rate, "
            "CAST(NULL AS DOUBLE) AS estimated_amount, "
            "CAST(NULL AS DOUBLE) AS gross_charge, CAST(NULL AS DOUBLE) AS cash_price "
            "WHERE FALSE")


def ensure_view(store: Store) -> None:
    with store.write_lock, store.connect() as con:
        _register_view(con, store)


def hospital_status(store: Store) -> dict:
    """What hospital data is loaded. Never raises — the dashboard asks on load."""
    ensure_view(store)
    with store.connect() as con:
        try:
            files = [dict(zip(["hospital_name", "state", "city", "source_file",
                               "rows", "last_updated_on", "imported_at"], r))
                     for r in con.execute(
                         "SELECT hospital_name, state, city, source_file, rows, "
                         "last_updated_on, imported_at FROM hospital_files "
                         "ORDER BY hospital_name").fetchall()]
            n_rows, n_hosp, n_payers, n_codes = con.execute(
                "SELECT count(*), count(DISTINCT hospital_name), "
                "count(DISTINCT payer), count(DISTINCT billing_code) "
                "FROM hospital_rates").fetchone()
        except Exception as e:  # noqa: BLE001
            return {"loaded": False, "hospitals": [], "rows": 0,
                    "reason": f"hospital data unavailable ({e.__class__.__name__})",
                    "caveat": HOSPITAL_CAVEAT}
    for f in files:
        f["imported_at"] = str(f["imported_at"])[:19] if f["imported_at"] else None
    return {
        "loaded": bool(n_rows),
        "rows": n_rows, "n_hospitals": n_hosp, "n_payers": n_payers,
        "n_codes": n_codes, "hospitals": files,
        "reason": None if n_rows else (
            "no hospital price-transparency file has been imported yet — "
            "hospitals publish one on their own website (search the hospital's "
            "name plus 'price transparency'), then load it on the Files tab or "
            "with `mrfx hospital <file>`."),
        "caveat": HOSPITAL_CAVEAT,
    }


def hospital_parity(store: Store, market: dict | None = None, *,
                    payer: str | None = None, state: str | None = None,
                    subject: str | None = None, limit: int = 200) -> dict:
    """Practice rates vs hospital outpatient rates, same payer, same code.

    The comparison is deliberately conservative: matched only on payer AND
    billing code, reported per code, and never reduced to a single
    "you're owed $X" figure — the two rates pay for different things.
    """
    from .benchmark import (BenchmarkError, _market_where, _rates_relation,
                            normalize_market, resolve_plan_scope,
                            resolve_subject_tins)

    limit = max(1, min(int(limit), 2000))
    ensure_view(store)
    st = hospital_status(store)
    if not st["loaded"]:
        return {"loaded": False, "rows": [], "reason": st["reason"],
                "caveat": HOSPITAL_CAVEAT}

    m = resolve_plan_scope(store, normalize_market(market or {}))
    if payer:
        m = {**m, "payers": [payer]}
    if state:
        m = {**m, "state": state}
    where, params = _market_where(m, bool(m.get("include_assistant")),
                                  bool(m.get("include_non_dollar")))
    rel = _rates_relation(m)

    subject_tins: list[str] = []
    if subject:
        subject_tins = resolve_subject_tins(store, subject)
        if not subject_tins:
            raise BenchmarkError(f"no practice matches {subject!r}")

    hosp_where = ["h.rate IS NOT NULL"]
    hosp_params: list = []
    if payer:
        hosp_where.append("h.payer = ?")
        hosp_params.append(payer)
    if state or m.get("state"):
        hosp_where.append("h.state = ?")
        hosp_params.append(str(state or m["state"]).upper()[:2])

    sql = f"""
    WITH practice AS (
        SELECT t.payer, t.billing_code, t.tin_value,
               median(t.negotiated_rate) AS rate
        FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where}
        GROUP BY t.payer, t.billing_code, t.tin_value
    ),
    pmkt AS (
        SELECT payer, billing_code,
               round(median(rate), 2)    AS practice_median,
               count(DISTINCT tin_value) AS n_practices
        FROM practice GROUP BY payer, billing_code
    ),
    hosp AS (
        SELECT h.payer, h.billing_code,
               round(median(h.rate), 2)        AS hospital_median,
               round(min(h.rate), 2)           AS hospital_min,
               round(max(h.rate), 2)           AS hospital_max,
               count(DISTINCT h.hospital_name) AS n_hospitals
        FROM hospital_rates h
        WHERE {' AND '.join(hosp_where)}
        GROUP BY h.payer, h.billing_code
    )
    SELECT p.payer, p.billing_code, p.practice_median, p.n_practices,
           hs.hospital_median, hs.hospital_min, hs.hospital_max, hs.n_hospitals
    FROM pmkt p JOIN hosp hs USING (payer, billing_code)
    ORDER BY (hs.hospital_median / nullif(p.practice_median, 0)) DESC NULLS LAST,
             p.billing_code
    LIMIT {int(limit)}
    """
    with store.connect() as con:
        cur = con.execute(sql, [*params, *hosp_params])
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

        subj: dict[tuple, float] = {}
        if subject_tins:
            scur = con.execute(f"""
                WITH per_tin AS (
                    SELECT t.payer, t.billing_code, t.tin_value,
                           median(t.negotiated_rate) AS rate
                    FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
                    WHERE {where} AND t.tin_value IN (SELECT unnest(?::VARCHAR[]))
                    GROUP BY t.payer, t.billing_code, t.tin_value
                )
                SELECT payer, billing_code, round(median(rate), 2)
                FROM per_tin GROUP BY payer, billing_code
            """, [*params, subject_tins])
            subj = {(p, c): r for p, c, r in scur.fetchall()}

    for r in rows:
        r["description"] = code_info(r["billing_code"])[0]
        pm, hm = r["practice_median"], r["hospital_median"]
        # a RATIO, never a subtraction presented as money owed
        r["hospital_multiple"] = round(hm / pm, 2) if pm else None
        if subject_tins:
            s = subj.get((r["payer"], r["billing_code"]))
            r["subject_rate"] = s
            r["subject_multiple"] = round(hm / s, 2) if s else None

    with_mult = [r for r in rows if r.get("hospital_multiple")]
    med_mult = None
    if with_mult:
        vals = sorted(r["hospital_multiple"] for r in with_mult)
        mid = len(vals) // 2
        med_mult = round(vals[mid] if len(vals) % 2 else
                         (vals[mid - 1] + vals[mid]) / 2, 2)
    return {
        "loaded": True, "rows": rows, "count": len(rows),
        "payer": payer, "state": state or m.get("state"),
        "subject": subject,
        "median_hospital_multiple": med_mult,
        "market": {k: v for k, v in m.items() if not k.startswith("_")},
        "hospitals_loaded": st["n_hospitals"],
        "reason": None if rows else (
            "no code is priced by BOTH a hospital file and a payer MRF under "
            "this scope — check the payer name matches between the two sources, "
            "or widen the state/month filters"),
        "caveat": HOSPITAL_CAVEAT,
        "note": (
            "Matched on payer name AND billing code only. Hospital files name "
            "payers freely, so a payer whose name differs between the two "
            "sources will not match — the app never fuzzy-matches payer names "
            "into a comparison. The multiple is a ratio of published rates, not "
            "an amount owed."),
    }


def forget_hospital(store: Store, hospital_name: str) -> dict:
    """Remove one hospital's data — the same erase-cleanly rule as `mrfx
    forget` for rate files."""
    d = Path(store.dir) / "hospital"
    p = d / f"{_slug(hospital_name)}.parquet"
    existed = p.exists()
    p.unlink(missing_ok=True)
    with store.write_lock, store.connect() as con:
        con.execute("DELETE FROM hospital_files WHERE hospital_name = ?",
                    [hospital_name])
        _register_view(con, store)
    return {"removed": existed, "hospital": hospital_name}
