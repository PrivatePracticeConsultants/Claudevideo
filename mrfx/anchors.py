"""Medicaid floors and hospital parity inside the negotiation documents
(review 13 Group 6.2).

`mrfx floors` and `mrfx hospital` load a state's Medicaid schedule and
hospitals' standard charges; until 0.7.7 neither reached the documents a
practice takes into a renewal. This module turns them into one line per code:

    Medicaid pays $30.00 for 97110 in MO; Alpha Health pays you $28.00
    (below Medicaid), Beta Health $35.00 (116.7% of Medicaid).

    97110: Alpha Health pays St. Luke's Hospital $95.00 (you: $40.00);
    Beta Health pays 3 MO hospitals a median $88.00 ($80.00-$110.00; you: $35.00).

The practice's rate on each line is the rate the DOCUMENT already prints for
that payer and code (passed in, never recomputed), so a line can never
disagree with the table above it. The Medicaid side is the newest loaded year
of the state's schedule (floors.floor_comparison's rule); the hospital side is
one rate per hospital (the median of its plan rows) under the same base-
modifier rule as the parity table (hospital.hospital_filters). Nothing loaded,
no state, or no shared code is a sentence saying so — never an empty heading
(the dossier's rule)."""
from __future__ import annotations

import html

from .rounding import half_up
from .store import Store

ANCHOR_NOTE = (
    "Medicaid is a floor, not a target: a commercial rate below the state's "
    "Medicaid rate for the same code is a concrete argument in a renewal. "
    "Hospital rates are FACILITY rates (they cover a hospital's overhead) for "
    "the same code from the same payer, matched on the exact payer name; the "
    "gap is an argument about relative value, not an amount owed.")


def _money(v) -> str:
    return "–" if v is None else f"${v:,.2f}"


def _state(market: dict) -> str | None:
    from .states import state_code_or_none
    return state_code_or_none(market.get("state")) if market.get("state") else None


def medicaid_lines(store: Store, market: dict,
                   subject_rates: dict[tuple[str, str], float]) -> dict:
    """{"lines": [...], "reason": str | None, "source": str | None}."""
    from .floors import floor_status
    st = _state(market)
    out: dict = {"lines": [], "reason": None, "source": None}
    if not st:
        out["reason"] = ("needs a state — a Medicaid schedule is a state "
                         "document, and this document is not scoped to one")
        return out
    status = floor_status(store)
    if not status["loaded"]:
        out["reason"] = (f"no Medicaid schedule is loaded — load {st}'s with "
                         f"`mrfx floors <file> --kind medicaid --state {st}`")
        return out
    with store.connect() as con:
        rows = con.execute("""
            WITH newest AS (SELECT max(year) AS year FROM floor_schedules
                            WHERE state = ? AND kind = 'medicaid')
            SELECT f.code, median(f.rate), min(f.label), f.year
            FROM floor_schedules f JOIN newest n ON n.year IS NOT DISTINCT FROM f.year
            WHERE f.state = ? AND f.kind = 'medicaid'
            GROUP BY f.code, f.year ORDER BY f.code
        """, [st, st]).fetchall()
    if not rows:
        have = sorted({s["state"] for s in status["schedules"] if s["kind"] == "medicaid"})
        out["reason"] = (f"no Medicaid schedule is loaded for {st}"
                         + (f" (loaded: {', '.join(have)})" if have else ""))
        return out
    sched = {code: float(rate) for code, rate, _l, _y in rows if rate is not None}
    label, year = rows[0][2], rows[0][3]
    out["source"] = f"{label}" + (f" ({year})" if year and str(year) not in str(label) else "")
    by_code: dict[str, list[tuple[str, float]]] = {}
    for (payer, code), rate in subject_rates.items():
        if rate is not None and code in sched:
            by_code.setdefault(code, []).append((payer, float(rate)))
    for code in sorted(by_code):
        floor = sched[code]
        payers = []
        for payer, rate in sorted(by_code[code], key=lambda x: (x[1], x[0])):
            pct = half_up(100.0 * rate / floor, 1) if floor else None
            payers.append({"payer": payer, "rate": half_up(rate, 2),
                           "pct_of_medicaid": pct, "below": rate < floor})

        def said(p, first):
            how = ("below Medicaid" if p["below"] else
                   f"{p['pct_of_medicaid']:g}% of Medicaid" if p["pct_of_medicaid"] is not None
                   else "")
            lead = f"{p['payer']} pays you " if first else f"{p['payer']} "
            return f"{lead}{_money(p['rate'])}" + (f" ({how})" if how else "")
        text = (f"Medicaid pays {_money(half_up(floor, 2))} for {code} in {st}; "
                + ", ".join(said(p, i == 0) for i, p in enumerate(payers)) + ".")
        out["lines"].append({"billing_code": code, "medicaid": half_up(floor, 2),
                             "payers": payers, "text": text})
    if not out["lines"]:
        out["reason"] = (f"none of this practice's billed codes is on the {st} "
                         f"Medicaid schedule ({out['source']})")
    return out


def hospital_lines(store: Store, market: dict,
                   subject_rates: dict[tuple[str, str], float]) -> dict:
    """{"lines": [...], "reason": str | None}: what the SAME payer pays the
    state's hospitals for the same code, beside what it pays the practice."""
    from .benchmark import normalize_market
    from .hospital import ensure_view, hospital_filters, hospital_status
    st = _state(market)
    out: dict = {"lines": [], "reason": None}
    if not st:
        out["reason"] = ("needs a state — hospital rates are compared within the "
                         "document's state")
        return out
    ensure_view(store)
    status = hospital_status(store)
    if not status["loaded"]:
        out["reason"] = ("no hospital standard-charges file is loaded — load one "
                         "with `mrfx hospital <file>`")
        return out
    pairs = sorted((p, c) for (p, c), r in subject_rates.items() if r is not None)
    if not pairs:
        out["reason"] = "this document prices no code for this practice"
        return out
    m = normalize_market(market)
    where, params = hospital_filters(m, None, st)
    with store.connect() as con:
        rows = con.execute(f"""
            WITH want AS (SELECT unnest(?::VARCHAR[]) AS payer,
                                 unnest(?::VARCHAR[]) AS billing_code),
            per_hosp AS (
                SELECT h.payer, h.billing_code, h.hospital_name, median(h.rate) AS rate
                FROM hospital_rates h JOIN want w USING (payer, billing_code)
                WHERE {' AND '.join(where)}
                GROUP BY h.payer, h.billing_code, h.hospital_name
            )
            SELECT payer, billing_code, count(*), median(rate), min(rate), max(rate),
                   min(hospital_name)
            FROM per_hosp GROUP BY payer, billing_code
            ORDER BY billing_code, payer
        """, [[p for p, _ in pairs], [c for _, c in pairs], *params]).fetchall()
    by_code: dict[str, list[dict]] = {}
    for payer, code, n, med, lo, hi, one in rows:
        you = subject_rates[(payer, code)]
        by_code.setdefault(code, []).append({
            "payer": payer, "n_hospitals": int(n), "hospital_median": half_up(med, 2),
            "hospital_min": half_up(lo, 2), "hospital_max": half_up(hi, 2),
            "hospital_name": one if n == 1 else None, "you": half_up(you, 2)})
    for code in sorted(by_code):
        parts = []
        for h in by_code[code]:
            if h["n_hospitals"] == 1:
                parts.append(f"{h['payer']} pays {h['hospital_name']} "
                             f"{_money(h['hospital_median'])} (you: {_money(h['you'])})")
            else:
                parts.append(f"{h['payer']} pays {h['n_hospitals']} {st} hospitals a median "
                             f"{_money(h['hospital_median'])} ({_money(h['hospital_min'])}"
                             f"–{_money(h['hospital_max'])}; you: {_money(h['you'])})")
        out["lines"].append({"billing_code": code, "payers": by_code[code],
                             "text": f"{code}: " + "; ".join(parts) + "."})
    if not out["lines"]:
        out["reason"] = (f"no loaded hospital file in {st} lists a rate from this "
                         "practice's payers for these codes (payer names must match "
                         "exactly between the two sources)")
    return out


def negotiation_anchors(store: Store, market: dict,
                        subject_rates: dict[tuple[str, str], float]) -> dict:
    """Both halves. A failure of either is its reason, never a broken document."""
    res = {}
    for key, fn in (("medicaid", medicaid_lines), ("hospital", hospital_lines)):
        try:
            res[key] = fn(store, market, subject_rates)
        except Exception as e:  # noqa: BLE001 — a supporting line never fails a report
            res[key] = {"lines": [], "reason": f"could not be built ({e})"}
    res["note"] = ANCHOR_NOTE
    return res


def medicaid_html(a: dict) -> str:
    e = html.escape
    if not a["lines"]:
        return ""
    src = f"<p class='note'>Source: {e(a['source'])}.</p>" if a.get("source") else ""
    return ("<h2>Against the Medicaid floor</h2><ul class='anchors'>"
            + "".join(f"<li>{e(x['text'])}</li>" for x in a["lines"]) + "</ul>" + src)


def hospital_html(a: dict) -> str:
    e = html.escape
    if not a["lines"]:
        return ""
    return ("<h2>What the same payer pays hospitals</h2><ul class='anchors'>"
            + "".join(f"<li>{e(x['text'])}</li>" for x in a["lines"]) + "</ul>")


def anchors_html(res: dict) -> str:
    """The negotiation one-pager's / proposal's section: the lines that exist,
    and a sentence for each half that is omitted."""
    e = html.escape
    body = medicaid_html(res["medicaid"]) + hospital_html(res["hospital"])
    omitted = [f"{name}: {res[k]['reason']}" for k, name in
               (("medicaid", "Medicaid floor"), ("hospital", "Hospital parity"))
               if not res[k]["lines"] and res[k].get("reason")]
    om = ("<p class='note'>Not included, and why — "
          + "; ".join(e(x) for x in omitted) + ".</p>") if omitted else ""
    note = f"<p class='note'>{e(res['note'])}</p>" if body else ""
    return body + note + om
