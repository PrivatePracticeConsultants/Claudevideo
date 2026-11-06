"""A standard therapy visit, priced per payer from a practice's own rates.

A practice owner asks "what is an Aetna visit worth to me?", not "what does
Aetna pay for 97110?". This prices ONE follow-up visit — a fixed basket of
timed codes with their minutes — against every payer that publishes rates for
the practice, using the rate card's own per-code rates (schedule.compute_fee_
schedule: the practice's median published rate per payer and code).

UNITS ARE COUNTED THE WAY MEDICARE REQUIRES (CMS Pub. 100-04, Medicare Claims
Processing Manual, ch. 5, sec. 20.2 "Reporting of Service Units With HCPCS",
Rev. 3670, eff. 01-01-17) — the "8-minute rule":
- the TOTAL timed minutes of the day fix the number of timed units
  (8-22 min = 1, 23-37 = 2, 38-52 = 3, 53-67 = 4, ... every 15 more = 1 more);
- each code first gets its whole 15-minute units; units still left go to the
  codes with the most minutes remaining (sec. 20.2 examples 1-5 — reproduced
  exactly in tests/mrfx/test_standard_visit.py);
- untimed codes are one unit and their minutes do not count.
Many commercial payers follow the same rule; some count each code on its own
("rule of eights", >= 8 minutes per code per unit). Both counts are returned,
and when they differ the visit says so — it is never silently re-counted.

COMPLIANCE, stated with the numbers rather than assumed:
- one 15-minute interval bills ONE therapy service (NCCI Policy Manual for
  Medicare, ch. XI, sec. P.2, rev. 1/1/2026); a timed pair that NCCI edits may
  be billed together with modifier 59 or XU only when performed in DIFFERENT
  timed intervals. 97140 + 97530 is the pair therapy billers most often meet
  (CMS's pair tables are AMA-licensed and not bundled here) — the visit flags
  it, and a preset without it exists for payers that deny it outright.
- Medicare's multiple procedure payment reduction (ch. 5, sec. 10.7: 50% of the
  practice-expense part of every unit after the highest-paid one) cannot be
  applied to a commercial rate, and the loaded Medicare fee schedule keeps no
  practice-expense split — so no Medicare visit total is shown rather than an
  overstated one.

WHAT THE NUMBER IS: the CONTRACTED value of the visit (payer + patient share)
at the practice's published rates — the ceiling on net revenue per visit
(NRPV). Actual NRPV (collections / visits) is lower by any multiple-procedure
reduction a payer applies, unit caps, denials and unpaid patient balances.
"""

from __future__ import annotations

from .catalog import code_info

# The default basket. Shares of all physical-medicine & rehabilitation
# procedures on all-payer claims, Jan-Sep 2023 (Definitive Healthcare, "Top
# rehabilitative services procedure codes", Nov 2023): 97110 28.7%, 97530
# 19.8%, 97140 16.3%, 97112 15.1% — the next timed code, 97116, is 2.7%.
# Four units (industry planning figures for adult orthopedic outpatient visits
# run ~3.8-4.2; several commercial plans cap a visit at 4 timed units) split by
# those shares, largest remainder, is exactly one unit of each.
PRESETS: dict[str, dict] = {
    "ortho": {
        "label": "Adult orthopedic follow-up — 97110, 97530, 97140, 97112, 15 min each (4 units)",
        "lines": [("97110", 15), ("97530", 15), ("97140", 15), ("97112", 15)],
        "basis": ("the four most-billed timed therapy codes on all-payer claims "
                  "(97110 28.7%, 97530 19.8%, 97140 16.3%, 97112 15.1% of physical-"
                  "medicine procedures, Jan-Sep 2023), one 15-minute unit each"),
    },
    "ortho_no_pair": {
        "label": "Same visit without the 97140 + 97530 pair — 97110 30 min, 97140, 97112 (4 units)",
        "lines": [("97110", 30), ("97140", 15), ("97112", 15)],
        "basis": ("the default visit with therapeutic activities replaced by a "
                  "second unit of therapeutic exercise — for payers that deny "
                  "97140 with 97530 even with modifier 59"),
    },
    "gait": {
        "label": "Gait / balance follow-up — 97116, 97112, 97530, 97140, 15 min each (4 units)",
        "lines": [("97116", 15), ("97112", 15), ("97530", 15), ("97140", 15)],
        "basis": "a lower-extremity / gait-focused follow-up, one 15-minute unit of each code",
    },
}
DEFAULT_PRESET = "ortho"

# timed pairs therapy billers most often see denied as NCCI edits; the full
# column-one/column-two tables are CMS's (AMA-licensed), so this is a flag to
# check, not a ruling
PAIRS_TO_FLAG = {frozenset(("97140", "97530")): (
    "97140 with 97530 is commonly edited as an NCCI pair: bill both only when "
    "each was performed in its own 15-minute interval, with modifier 59 or XU "
    "on the edit's column-two code (NCCI Policy Manual ch. XI, sec. P.2). "
    "Some commercial payers have denied the pair even with the modifier — the "
    "preset without it shows what the visit is worth there.")}

MAX_LINES = 8
MAX_MINUTES_PER_LINE = 120

UNITS_RULE = (
    "Units follow the CMS 8-minute rule (Medicare Claims Processing Manual, "
    "ch. 5, sec. 20.2): total timed minutes set the number of units "
    "(8-22 = 1, 23-37 = 2, 38-52 = 3, 53-67 = 4, ...), each code gets its whole "
    "15-minute units, and units left go to the codes with the most minutes "
    "remaining. Untimed codes are one unit.")
VALUE_NOTE = (
    "Contracted value per visit = units x this practice's published rate for "
    "each code with that payer (the rate card's rate: latest month, base "
    "modifiers, professional claims unless the filters say otherwise). It is "
    "the CEILING on net revenue per visit: actual NRPV (collections / visits) "
    "is lower by any multiple-procedure reduction a payer applies to 2nd+ "
    "therapy units, visit unit caps, denials and unpaid patient balances, and "
    "includes the patient's cost-sharing. A payer that publishes no rate for "
    "one of the visit's codes is listed without a total rather than priced "
    "at $0. No Medicare total is shown: Medicare reduces the practice-expense "
    "part of every unit after the highest-paid one by 50% (ch. 5, sec. 10.7), "
    "which needs the RVU split the loaded fee schedule does not keep.")


class VisitError(ValueError):
    """A visit definition that cannot be billed as entered (plain-language)."""


def units_by_8_minute_rule(lines: list[tuple[str, int]]) -> dict[str, int]:
    """CMS Pub. 100-04 ch. 5 sec. 20.2 for TIMED codes: {code: units}.

    `lines` are (code, minutes) with each code once. Total minutes fix the
    units; whole 15-minute units first; the rest by most minutes remaining —
    ties go to the line listed first (sec. 20.2 example 2: "choose either")."""
    total = sum(m for _, m in lines)
    n = (total + 7) // 15 if total >= 8 else 0
    units = {c: m // 15 for c, m in lines}
    left = n - sum(units.values())
    order = sorted(range(len(lines)), key=lambda i: (-(lines[i][1] % 15), i))
    for i in order[:max(left, 0)]:
        units[lines[i][0]] += 1
    return units


def units_per_code(lines: list[tuple[str, int]]) -> dict[str, int]:
    """Each code counted on its own (">= 8 minutes = 1 unit", the rule of
    eights some commercial payers use): {code: units}."""
    return {c: (m + 7) // 15 if m >= 8 else 0 for c, m in lines}


def parse_lines(raw) -> list[tuple[str, int]]:
    """[(code, minutes)] from a list of {code, minutes} / (code, minutes), or
    text with one "97110, 15" per line. Repeated codes are merged (one claim
    line per code and date). Refuses what cannot be billed as entered."""
    items: list[tuple[str, object]] = []
    if isinstance(raw, str):
        for ln in raw.replace(";", "\n").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            parts = [p for p in ln.replace("\t", ",").replace(" ", ",").split(",") if p]
            if len(parts) != 2:
                raise VisitError(f"'{ln}': write one code and its minutes per line, e.g. 97110, 15")
            items.append((parts[0], parts[1]))
    else:
        for it in raw or []:
            if isinstance(it, dict):
                items.append((it.get("code"), it.get("minutes")))
            else:
                items.append(tuple(it))
    merged: dict[str, int] = {}
    for code, minutes in items:
        code = str(code or "").strip().upper()
        if not code:
            continue
        desc = code_info(code)[0]
        if not desc:
            raise VisitError(f"{code} is not a therapy code this app knows — check the code")
        try:
            m = int(float(minutes))
        except (TypeError, ValueError):
            raise VisitError(f"{code}: minutes must be a number, e.g. 15")
        if not 1 <= m <= MAX_MINUTES_PER_LINE:
            raise VisitError(f"{code}: minutes must be between 1 and {MAX_MINUTES_PER_LINE}")
        merged[code] = merged.get(code, 0) + m
    if not merged:
        raise VisitError("the visit has no codes")
    if len(merged) > MAX_LINES:
        raise VisitError(f"a visit of more than {MAX_LINES} different codes is not a standard visit")
    return list(merged.items())


def define_visit(lines: list[tuple[str, int]]) -> dict:
    """The billable visit: units per code (CMS rule, and the per-code count
    beside it), totals and compliance flags. No pricing."""
    timed = [(c, m) for c, m in lines if code_info(c)[2]]
    cms = units_by_8_minute_rule(timed)
    each = units_per_code(timed)
    total_timed = sum(m for _, m in timed)
    rows = []
    for c, m in lines:
        is_timed = c in cms
        rows.append({"code": c, "description": code_info(c)[0], "minutes": m,
                     "timed": is_timed,
                     "units": cms[c] if is_timed else 1,
                     "units_per_code_rule": each[c] if is_timed else 1})
    flags = []
    codes = {c for c, _ in lines}
    for pair, text in PAIRS_TO_FLAG.items():
        if pair <= codes and all(r["units"] for r in rows if r["code"] in pair):
            flags.append(text)
    unbilled = [r["code"] for r in rows if r["timed"] and r["units"] == 0]
    if unbilled:
        flags.append(
            f"{', '.join(unbilled)}: performed but not billable — the day's total "
            "timed minutes allow no unit for it (sec. 20.2 example 4); document it, "
            "do not bill it.")
    n_cms = sum(r["units"] for r in rows)
    n_each = sum(r["units_per_code_rule"] for r in rows)
    if n_each != n_cms:
        flags.append(
            f"Counted code by code (the rule of eights some commercial payers use) "
            f"this visit is {n_each} units, not {n_cms}: priced here at the CMS "
            "count — check the payer's own policy before quoting it.")
    if timed and total_timed and n_cms and total_timed / max(sum(cms.values()), 1) < 15:
        flags.append(
            "Under 15 minutes per billed unit on average: CMS expects each unit to "
            "average 15 minutes and flags a consistent practice of less for review "
            "(sec. 20.2).")
    return {"lines": rows, "total_timed_minutes": total_timed,
            "units": n_cms, "units_per_code_rule": n_each, "flags": flags}


def price_visit(store, subject: str, market: dict | None, raw_lines=None,
                preset: str | None = None) -> dict:
    """The visit, priced per payer from the practice's own rate card."""
    from .schedule import compute_fee_schedule

    key = None
    if raw_lines in (None, "", []):
        key = preset or DEFAULT_PRESET
        if key not in PRESETS:
            raise VisitError(f"unknown visit preset {key!r}")
        lines, basis, label = PRESETS[key]["lines"], PRESETS[key]["basis"], PRESETS[key]["label"]
        lines = parse_lines(lines)
    else:
        lines, basis, label = parse_lines(raw_lines), "your own visit definition", "Custom visit"
    visit = define_visit(lines)
    billed = [r for r in visit["lines"] if r["units"]]

    fs = compute_fee_schedule(store, subject, market or {})
    rate = {(p, e["billing_code"]): cell["rate"]
            for e in fs["codes"] for p, cell in (e.get("rates") or {}).items()
            if cell.get("rate") is not None}
    rows = []
    for payer in fs["payers"]:
        parts, missing = [], []
        for r in billed:
            v = rate.get((payer, r["code"]))
            if v is None:
                missing.append(r["code"])
            else:
                parts.append({"code": r["code"], "units": r["units"], "rate": v,
                              "amount": round(r["units"] * v, 2)})
        if not parts:
            continue                       # this payer prices none of the visit
        complete = not missing
        rows.append({
            "payer": payer,
            "visit_value": round(sum(p["amount"] for p in parts), 2) if complete else None,
            "priced_value": round(sum(p["amount"] for p in parts), 2),
            "priced_units": sum(p["units"] for p in parts),
            "parts": parts, "missing_codes": missing, "complete": complete,
        })
    rows.sort(key=lambda r: (not r["complete"], -(r["visit_value"] or 0), r["payer"]))
    full = [r for r in rows if r["complete"]]
    basket = " + ".join(f"{r['code']}×{r['units']}" for r in billed)
    if len(full) >= 2:
        hi, lo = full[0], full[-1]
        headline = (f"One visit ({basket}, {visit['units']} units) is worth "
                    f"${hi['visit_value']:,.2f} from {hi['payer']} and "
                    f"${lo['visit_value']:,.2f} from {lo['payer']} at this practice's "
                    f"contracted rates — ${hi['visit_value'] - lo['visit_value']:,.2f} "
                    "a visit apart for the same care.")
    elif full:
        headline = (f"One visit ({basket}, {visit['units']} units) is worth "
                    f"${full[0]['visit_value']:,.2f} from {full[0]['payer']} at this "
                    "practice's contracted rates.")
    elif rows:
        headline = ("No payer publishes a rate for every code in this visit for this "
                    "practice — the partial values below cover only the codes priced.")
    else:
        headline = "This practice has no published rate for any code in this visit."
    return {
        "subject": subject, "preset": key,
        "label": label, "basis": basis, **visit,
        "rows": rows, "count": len(rows), "complete_count": len(full),
        "market": fs.get("market"), "month": fs.get("month"),
        "headline": headline, "units_rule": UNITS_RULE, "note": VALUE_NOTE,
    }


def presets_public() -> list[dict]:
    return [{"key": k, "label": v["label"], "basis": v["basis"],
             "lines": [{"code": c, "minutes": m} for c, m in v["lines"]]}
            for k, v in PRESETS.items()]
