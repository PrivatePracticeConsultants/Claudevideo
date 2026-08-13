"""Underpayment check: what a payer PAID versus what it PUBLISHED.

The store already knows every payer's negotiated rate for a practice. A remit
says what actually landed. Lines paid below the published contract rate are
the most immediately billable finding this data can produce — and the one
place the app compares its rates against the real world, so the caveats matter
more here than anywhere else.

WHAT SITS BETWEEN A PUBLISHED RATE AND A PAID AMOUNT (all disclosed on every
result, because a "recovery" number that ignores them is malpractice):

- **MPPR** — Medicare-style multiple-procedure reduction on the practice
  expense of additional units/procedures in a visit. Many commercial payers
  apply it, so a second/third code on the same date legitimately pays less.
- **Assistant modifiers (CQ/CO)** — a statutory percentage reduction.
- **Patient cost-share** — deductible/copay/coinsurance is the PATIENT's
  share of the allowed amount; the payer's cheque is smaller by design.
  If the remit's paid column is the payer-paid amount rather than the total
  ALLOWED amount, every line will look underpaid. This is the single most
  common misreading, so the caller states which column they pasted.
- **Sequestration / withholds**, secondary-payer coordination, and contract
  terms not in the MRF (carve-outs, per-visit caps).

So a flagged line is a QUESTION for the payer, never a proven underpayment —
the wording throughout says exactly that.
"""

from __future__ import annotations

import csv
import io
import logging
import re

from .benchmark import (BenchmarkError, _market_where, _rates_relation,
                        normalize_market, resolve_subject_tins)
from .parser import clean_code
from .store import Store

log = logging.getLogger(__name__)

AMOUNT_BASIS = ("allowed", "paid")

CAVEAT = (
    "A flagged line is a QUESTION to take to the payer, not a proven "
    "underpayment. MPPR (multiple-procedure reduction), assistant-modifier "
    "(CQ/CO) reductions, sequestration, secondary-payer coordination and "
    "contract terms that never appear in a machine-readable file can all make "
    "a correct payment fall below the published rate. If you pasted the "
    "PAYER-PAID column rather than the ALLOWED amount, patient deductible and "
    "copay alone will make nearly every line look short."
)

_NUM = re.compile(r"-?[\d,]*\.?\d+")


def _money(raw) -> float | None:
    if raw is None:
        return None
    m = _NUM.search(str(raw).replace("$", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_remit(text: str) -> tuple[list[dict], list[str]]:
    """Accept what a billing system actually exports: CSV/TSV with a header
    row naming code / payer / amount (+ optional units, date, modifiers), or
    loose 'code, payer, amount' lines. Returns (rows, problems) — a line we
    cannot read is REPORTED, never dropped in silence."""
    text = (text or "").strip()
    if not text:
        raise BenchmarkError("paste some remit lines first")
    problems: list[str] = []
    rows: list[dict] = []

    sample = text.splitlines()[0]
    delim = "\t" if "\t" in sample else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    records = [r for r in reader if any((c or "").strip() for c in r)]
    if not records:
        raise BenchmarkError("no readable lines in that paste")

    header = [(c or "").strip().lower() for c in records[0]]

    def find(*names, exclude=()):
        for i, h in enumerate(header):
            if any(n in h for n in names) and not any(x in h for x in exclude):
                return i
        return None

    # exclusions matter more than matches here: real remit exports carry
    # denial_reason_code, adjustment_group_code, paid_date, unit_price —
    # substring-matching those as the code/amount/units column would silently
    # score garbage (a paid_date "2026-01-05" reads as $2026.00)
    i_code = find("code", "cpt", "hcpcs",
                  exclude=("modifier", "zip", "denial", "reason", "remark",
                           "adjust", "group"))
    # ALLOWED wins over PAID when a remit export carries both — it is the
    # column comparable to a published rate. `is not None`, never `or`: the
    # column is often at index 0, and 0 is falsy.
    i_allowed = find("allowed", exclude=("date",))
    i_amt = i_allowed if i_allowed is not None else find(
        "paid", "payment", "amount", exclude=("date",))
    has_header = i_code is not None and i_amt is not None
    if has_header:
        i_payer = find("payer", "carrier", "insurer", "plan")
        i_units = find("unit", "qty", "quantity",
                       exclude=("price", "amount", "charge", "rate"))
        i_mods = find("modifier", "mod")
        i_date = find("date", "dos", "service")
        body = records[1:]
    else:
        i_code, i_payer, i_amt, i_units, i_mods, i_date = 0, 1, 2, 3, None, None
        body = records

    for lineno, rec in enumerate(body, start=2 if has_header else 1):
        def cell(idx):
            return rec[idx].strip() if idx is not None and idx < len(rec) else ""
        code = clean_code(cell(i_code))
        amount = _money(cell(i_amt))
        if not code or amount is None:
            problems.append(f"line {lineno}: could not read a code and amount "
                            f"from {','.join(rec)[:70]!r}")
            continue
        units = _money(cell(i_units))
        rows.append({
            "line": lineno, "billing_code": code,
            "payer": cell(i_payer) or None, "amount": amount,
            "units": units if units and units > 0 else 1.0,
            "modifiers": cell(i_mods) or "", "service_date": cell(i_date) or "",
        })
    if not rows:
        raise BenchmarkError(
            "no lines had both a billing code and an amount. Expected columns "
            "like: code, payer, allowed_amount[, units][, modifiers][, date]. "
            + (problems[0] if problems else ""))
    return rows, problems


def check_underpayments(store: Store, subject: str, text: str,
                        market: dict | None = None,
                        basis: str = "allowed", tolerance: float = 0.01) -> dict:
    """Compare each remit line against what THAT payer published for THIS
    practice and code. Lines below the published rate by more than `tolerance`
    are flagged with the shortfall; lines we cannot price are reported as
    unmatched (never silently dropped, and never counted as fine)."""
    if basis not in AMOUNT_BASIS:
        raise BenchmarkError(f"basis must be one of {', '.join(AMOUNT_BASIS)}")
    subject = str(subject or "").strip()
    if not subject:
        raise BenchmarkError("pick the practice whose remit this is")
    tins = resolve_subject_tins(store, subject)
    market = normalize_market(market or {"month": "latest"})
    lines, problems = parse_remit(text)

    where, params = _market_where(market, False, False)
    rel = _rates_relation(market)
    with store.connect() as con:
        cur = con.execute(f"""
            SELECT t.payer, t.billing_code, median(t.negotiated_rate) AS rate,
                   count(*) AS n
            FROM {rel} t
            WHERE {where} AND t.tin_value IN (SELECT unnest(?::VARCHAR[]))
            GROUP BY 1, 2
        """, [*params, tins])
        published: dict[tuple, float] = {}
        by_code: dict[str, list[float]] = {}
        real_name: dict[str, str] = {}   # lowercase key -> the payer's real name
        for payer, code, rate, _n in cur.fetchall():
            published[(payer.lower(), code)] = rate
            real_name[payer.lower()] = payer
            by_code.setdefault(code, []).append(rate)
    if not published:
        raise BenchmarkError(
            f"no published rates for '{subject}' under the current month/basis "
            "— pick the practice from the suggestions, or widen the month")

    payers = sorted({p for p, _c in published})
    flagged, matched, unmatched = [], [], []
    total_short = 0.0
    for ln in lines:
        code = ln["billing_code"]
        key_payer = (ln["payer"] or "").lower().strip()
        rate = None
        matched_payer = None
        if key_payer:
            # exact, then a forgiving contains-match ("BCBS MO" vs "Anthem BCBS MO")
            for p in payers:
                if p == key_payer or key_payer in p or p in key_payer:
                    if (p, code) in published:
                        rate, matched_payer = published[(p, code)], p
                        break
        if rate is None and not key_payer and len(by_code.get(code, [])) == 1:
            # no payer column, but the practice has exactly one published rate
            # for this code — unambiguous, so use it and say which payer
            rate = by_code[code][0]
            matched_payer = next(p for (p, c) in published
                                 if c == code and published[(p, c)] == rate)
        if rate is None:
            unmatched.append({**ln, "reason": (
                f"no published rate for {code}"
                + (f" with a payer matching {ln['payer']!r}" if ln["payer"] else
                   " (and the code is priced by more than one payer, so the "
                   "payer column is needed to pick one)"))})
            continue
        expected = round(rate * (ln["units"] or 1.0), 2)
        short = round(expected - ln["amount"], 2)
        # report the payer's REAL name, not the lowercased match key
        row = {**ln, "matched_payer": real_name.get(matched_payer, matched_payer),
               "published_rate": round(rate, 2),
               "expected": expected, "shortfall": short,
               "pct_short": round(100.0 * short / expected, 1) if expected else None}
        if short > tolerance:
            total_short += short
            flagged.append(row)
        else:
            matched.append(row)
    flagged.sort(key=lambda r: -r["shortfall"])
    return {
        "subject": subject, "market": market, "basis": basis,
        "flagged": flagged, "ok": matched, "unmatched": unmatched,
        "problems": problems,
        "summary": {
            "lines_read": len(lines), "flagged": len(flagged),
            "ok": len(matched), "unmatched": len(unmatched),
            "total_shortfall": round(total_short, 2),
            "basis_note": (
                "You told the app these amounts are the ALLOWED amount."
                if basis == "allowed" else
                "You told the app these are PAYER-PAID amounts — patient "
                "deductible, copay and coinsurance are NOT included in them, so "
                "shortfalls here are expected and are not evidence of "
                "underpayment on their own."),
        },
        "caveat": CAVEAT,
    }


def underpayment_csv(result: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["status", "line", "billing_code", "payer_on_remit",
                "matched_payer", "units", "amount", "published_rate",
                "expected", "shortfall", "pct_short", "service_date", "note"])
    for r in result["flagged"]:
        w.writerow(["UNDER", r["line"], r["billing_code"], r.get("payer") or "",
                    r["matched_payer"], r["units"], r["amount"],
                    r["published_rate"], r["expected"], r["shortfall"],
                    r["pct_short"], r.get("service_date", ""), ""])
    for r in result["ok"]:
        w.writerow(["ok", r["line"], r["billing_code"], r.get("payer") or "",
                    r["matched_payer"], r["units"], r["amount"],
                    r["published_rate"], r["expected"], r["shortfall"],
                    r["pct_short"], r.get("service_date", ""), ""])
    for r in result["unmatched"]:
        w.writerow(["unmatched", r["line"], r["billing_code"],
                    r.get("payer") or "", "", r["units"], r["amount"],
                    "", "", "", "", r.get("service_date", ""), r["reason"]])
    for p in result["problems"]:
        w.writerow(["unreadable", "", "", "", "", "", "", "", "", "", "", "", p])
    w.writerow([])
    w.writerow([f"NOTE: {result['summary']['basis_note']}"])
    w.writerow([f"NOTE: {result['caveat']}"])
    return out.getvalue()
