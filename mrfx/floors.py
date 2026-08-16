"""Medicaid and workers'-comp fee schedules: the floor and the ceiling.

Commercial rates mean much more when a client can see what sits under and over
them:

- **Medicaid** is the floor. "Your commercial rate is 4% above Missouri
  Medicaid" ends a conversation about whether a rate is defensible faster than
  any percentile does.
- **Workers' comp** is usually the ceiling. Most state WC schedules pay therapy
  well above commercial, which turns "we can't pay more" into "you already pay
  more, under a different schedule".

Neither is published in a machine-readable national file the way MRFs are:
every state posts its own PDF or spreadsheet on its own cadence, in its own
layout. So this module does NOT pretend to fetch them. The user brings a
schedule — a two-column code/rate export is all that is needed — and labels it
with the state, the kind and the year. That label IS the provenance, and it is
printed with every number derived from it.

HONESTY:
- These are the user's own uploaded numbers. The app never invents a Medicaid
  or WC rate and never interpolates a missing code — a code the schedule does
  not list is ABSENT from the comparison, not zero.
- A state Medicaid schedule is a maximum allowable for fee-for-service
  Medicaid. Managed-Medicaid plans commonly pay a percentage of it, so the
  real floor for a practice may be lower. Stated on every result.
- WC schedules carry their own billing rules (different modifiers, treatment
  caps, authorization). A ratio to a WC rate is a talking point, not a
  contract term.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from pathlib import Path

from .catalog import CODE_CATALOG, code_info
from .sniff import open_stream
from .store import Store

log = logging.getLogger(__name__)


class FloorImportError(Exception):
    """Refuse a schedule we cannot read, and say what was missing."""


KINDS = {"medicaid": "Medicaid", "workers_comp": "Workers' comp",
         "other": "Other schedule"}

FLOOR_NOTE = (
    "Medicaid and workers'-comp rates are the schedules YOU loaded, labelled "
    "with the state, kind and year you gave them — the app never fetches or "
    "invents them. A state Medicaid schedule is the fee-for-service maximum "
    "allowable; managed-Medicaid plans commonly pay a percentage of it, so a "
    "practice's real floor can be lower. Workers'-comp schedules carry their "
    "own billing rules, caps and authorization requirements, so a ratio to a "
    "WC rate is a talking point, not a contract term. A code the schedule does "
    "not list is absent from the comparison — never treated as zero."
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _money(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace("$", "").replace(",", "")
    if not s or s.endswith("%"):
        return None
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _pick_columns(header: list[str], code_col=None, rate_col=None) -> tuple[int, int]:
    idx = {}
    for i, h in enumerate(header):
        idx.setdefault(_norm(h), i)

    def find(explicit, exact, contains):
        if explicit:
            if _norm(explicit) in idx:
                return idx[_norm(explicit)]
            raise FloorImportError(
                f"no column named {explicit!r} — columns are: "
                f"{', '.join(header[:15])}")
        for name in exact:
            if name in idx:
                return idx[name]
        for k, i in idx.items():
            if any(t in k for t in contains):
                return i
        return None

    ci = find(code_col, ("code", "hcpcs", "cpt", "procedure code", "proc code",
                         "hcpcs code", "cpt code", "billing code"),
              ("hcpcs", "cpt", "code"))
    ri = find(rate_col, ("rate", "amount", "fee", "allowed", "max fee",
                         "maximum allowable", "allowable", "non facility",
                         "price"),
              ("rate", "allow", "fee", "amount", "price"))
    if ci is None or ri is None:
        raise FloorImportError(
            "could not find a code column and a rate column in this file "
            f"(columns are: {', '.join(header[:15])}). Re-export it with two "
            "columns named 'code' and 'rate', or name the columns explicitly.")
    return ci, ri


def import_fee_schedule(store: Store, path: str | Path, *, kind: str,
                        state: str, label: str | None = None,
                        year: str | None = None, code_col: str | None = None,
                        rate_col: str | None = None, codes=None) -> dict:
    """Load one state schedule, keeping only therapy codes."""
    kind = str(kind or "").strip().lower().replace("-", "_").replace(" ", "_")
    if kind not in KINDS:
        raise FloorImportError(
            f"kind must be one of {', '.join(KINDS)} — got {kind!r}")
    st = str(state or "").strip().upper()[:2]
    if len(st) != 2:
        raise FloorImportError("a two-letter state is required — a fee schedule "
                               "is a state document and means nothing without it")
    path = Path(path)
    if not path.exists():
        raise FloorImportError(f"no such file: {path}")
    want = {str(c).upper() for c in (codes or CODE_CATALOG.keys())}

    with open_stream(path) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace",
                                newline="")
        reader = csv.reader(text)
        header = next(reader, None)
        if not header:
            raise FloorImportError("the file is empty")
        ci, ri = _pick_columns(header, code_col, rate_col)
        seen: dict[str, float] = {}
        skipped_no_rate = 0
        for row in reader:
            if ci >= len(row):
                continue
            code = str(row[ci] or "").strip().upper()
            if code not in want:
                continue
            rate = _money(row[ri] if ri < len(row) else None)
            if rate is None:
                skipped_no_rate += 1
                continue
            # a schedule listing a code twice (modifier variants, facility vs
            # non-facility) keeps the HIGHEST — the maximum allowable is what a
            # schedule promises, and picking arbitrarily would be unrepeatable
            seen[code] = max(rate, seen.get(code, 0.0))

    if not seen:
        raise FloorImportError(
            "no therapy codes with a usable rate were found in this file — "
            "check that it lists CPT codes (97110, 97140, 92507…) and a dollar "
            "amount, and that the right columns were picked")

    lbl = (label or "").strip() or f"{st} {KINDS[kind]}{' ' + year if year else ''}"
    yr = str(year or "").strip() or None
    with store.write_lock, store.connect() as con:
        con.execute("DELETE FROM floor_schedules WHERE kind = ? AND state = ? "
                    "AND coalesce(year, '') = coalesce(?, '')", [kind, st, yr])
        con.executemany(
            "INSERT INTO floor_schedules (kind, state, year, label, code, rate) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [[kind, st, yr, lbl, c, r] for c, r in sorted(seen.items())])
    log.info("floors: %s codes for %s", len(seen), lbl)
    return {"kind": kind, "kind_label": KINDS[kind], "state": st, "year": yr,
            "label": lbl, "codes": len(seen),
            "skipped_no_rate": skipped_no_rate,
            "file": path.name, "note": FLOOR_NOTE}


def floor_status(store: Store) -> dict:
    """Which schedules are loaded. Never raises — the dashboard asks on load."""
    try:
        with store.connect() as con:
            rows = con.execute(
                "SELECT kind, state, year, any_value(label), count(*), "
                "round(min(rate), 2), round(max(rate), 2) "
                "FROM floor_schedules GROUP BY kind, state, year "
                "ORDER BY state, kind, year").fetchall()
    except Exception as e:  # noqa: BLE001 — a store predating the table
        return {"loaded": False, "schedules": [], "reason": (
            f"fee-schedule storage unavailable ({e.__class__.__name__})"),
            "note": FLOOR_NOTE}
    schedules = [{"kind": k, "kind_label": KINDS.get(k, k), "state": s,
                  "year": y, "label": lbl, "codes": n,
                  "min_rate": lo, "max_rate": hi}
                 for k, s, y, lbl, n, lo, hi in rows]
    return {
        "loaded": bool(schedules), "schedules": schedules,
        "states": sorted({s["state"] for s in schedules}),
        "reason": None if schedules else (
            "no Medicaid or workers'-comp schedule loaded yet — export your "
            "state's schedule to a CSV with a code column and a rate column, "
            "then load it here or with `mrfx floors <file> --kind medicaid "
            "--state MO`"),
        "note": FLOOR_NOTE,
    }


def floor_comparison(store: Store, market: dict | None = None, *,
                     subject: str | None = None, state: str | None = None,
                     payer: str | None = None) -> dict:
    """Commercial rates against the loaded Medicaid / WC schedules, per code."""
    from .benchmark import (BenchmarkError, _market_where, _rates_relation,
                            month_label, normalize_market, resolve_plan_scope,
                            resolve_subject_tins)

    st = floor_status(store)
    if not st["loaded"]:
        return {"loaded": False, "rows": [], "reason": st["reason"],
                "note": FLOOR_NOTE}

    m = resolve_plan_scope(store, normalize_market(market or {}))
    scope_state = (state or m.get("state") or "").strip().upper()[:2]
    if not scope_state:
        # a fee schedule is a STATE document; comparing a national commercial
        # median to one state's Medicaid would be an accidental apples-oranges
        raise BenchmarkError(
            "a floor comparison needs a state — Medicaid and workers'-comp "
            "schedules are state documents, so a national commercial median "
            "cannot be compared to one")
    m = {**m, "state": scope_state}
    if payer:
        m = {**m, "payers": [payer]}
    where, params = _market_where(m, bool(m.get("include_assistant")),
                                  bool(m.get("include_non_dollar")))
    rel = _rates_relation(m)
    subject_tins = resolve_subject_tins(store, subject) if subject else []

    with store.connect() as con:
        cur = con.execute(f"""
            WITH per_tin AS (
                SELECT t.billing_code, t.tin_value,
                       median(t.negotiated_rate) AS rate
                FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
                WHERE {where}
                GROUP BY t.billing_code, t.tin_value
            )
            SELECT billing_code,
                   round(median(rate), 2)    AS commercial_median,
                   count(DISTINCT tin_value) AS n_practices
            FROM per_tin GROUP BY billing_code
        """, params)
        commercial = {r[0]: {"commercial_median": r[1], "n_practices": r[2]}
                      for r in cur.fetchall()}
        subj = {}
        if subject_tins:
            scur = con.execute(f"""
                WITH per_tin AS (
                    SELECT t.billing_code, t.tin_value,
                           median(t.negotiated_rate) AS rate
                    FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
                    WHERE {where} AND t.tin_value IN (SELECT unnest(?::VARCHAR[]))
                    GROUP BY t.billing_code, t.tin_value
                )
                SELECT billing_code, round(median(rate), 2)
                FROM per_tin GROUP BY billing_code
            """, [*params, subject_tins])
            subj = dict(scur.fetchall())
        floors = con.execute(
            "SELECT kind, code, rate, any_value(label) FROM floor_schedules "
            "WHERE state = ? GROUP BY kind, code, rate ORDER BY code",
            [scope_state]).fetchall()

    if not floors:
        return {"loaded": False, "rows": [],
                "reason": (f"no schedule loaded for {scope_state} — schedules "
                           f"are loaded per state, and this store has "
                           f"{', '.join(st['states']) or 'none'}"),
                "note": FLOOR_NOTE}

    by_code: dict[str, dict] = {}
    labels: dict[str, str] = {}
    for kind, code, rate, label in floors:
        by_code.setdefault(code, {})[kind] = rate
        labels[kind] = label

    rows = []
    for code in sorted(set(by_code) & set(commercial)):
        c = commercial[code]
        f = by_code[code]
        row = {
            "billing_code": code, "description": code_info(code)[0],
            "commercial_median": c["commercial_median"],
            "n_practices": c["n_practices"],
            "subject_rate": subj.get(code),
            "medicaid": f.get("medicaid"),
            "workers_comp": f.get("workers_comp"),
            "other": f.get("other"),
        }
        for kind in ("medicaid", "workers_comp", "other"):
            base = f.get(kind)
            row[f"pct_of_{kind}"] = (
                round(100.0 * c["commercial_median"] / base, 1) if base else None)
            row[f"subject_pct_of_{kind}"] = (
                round(100.0 * row["subject_rate"] / base, 1)
                if base and row["subject_rate"] else None)
        rows.append(row)

    def _median(vals):
        v = sorted(x for x in vals if x is not None)
        if not v:
            return None
        mid = len(v) // 2
        return round(v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2, 1)

    med_pct = _median(r["pct_of_medicaid"] for r in rows)
    wc_pct = _median(r["pct_of_workers_comp"] for r in rows)
    subj_med = _median(r["subject_pct_of_medicaid"] for r in rows)

    parts = []
    if med_pct is not None:
        parts.append(
            f"Commercial rates here run at {med_pct:g}% of {scope_state} "
            "Medicaid" + (f" — this practice at {subj_med:g}%" if subj_med else ""))
    if wc_pct is not None:
        parts.append(f"and {wc_pct:g}% of workers' comp")
    headline = (". ".join(parts) + "." if parts else
                f"No code is priced by both a payer and a {scope_state} schedule.")

    return {
        "loaded": True, "rows": rows, "count": len(rows), "state": scope_state,
        "as_of": month_label(m["month"]), "payer": payer, "subject": subject,
        "schedule_labels": labels,
        "median_pct_of_medicaid": med_pct,
        "median_pct_of_workers_comp": wc_pct,
        "subject_median_pct_of_medicaid": subj_med,
        "codes_not_in_schedule": sorted(set(commercial) - set(by_code))[:50],
        "headline": headline,
        "reason": None if rows else (
            f"no code is priced by both a payer file and a {scope_state} "
            "schedule under this scope"),
        "note": FLOOR_NOTE,
    }


def forget_schedule(store: Store, kind: str, state: str,
                    year: str | None = None) -> dict:
    with store.write_lock, store.connect() as con:
        n = con.execute(
            "SELECT count(*) FROM floor_schedules WHERE kind = ? AND state = ? "
            "AND coalesce(year, '') = coalesce(?, '')",
            [kind, str(state).upper()[:2], year]).fetchone()[0]
        con.execute(
            "DELETE FROM floor_schedules WHERE kind = ? AND state = ? "
            "AND coalesce(year, '') = coalesce(?, '')",
            [kind, str(state).upper()[:2], year])
    return {"removed": n, "kind": kind, "state": str(state).upper()[:2],
            "year": year}
