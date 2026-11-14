"""Visit value in this market — the Rate card's market block (review 12, Group 3).

The question a consultant is asked before any other: "what is a visit worth
around here?" One standard visit (a mrfx/visit.py basket, units by the CMS
8-minute rule) is priced at EVERY practice of one state and one discipline,
payer by payer, and the spread is described: how many practices, the average
(mean), the median, the 25th and 75th percentiles. A subject practice is
optional; when one is named it is placed in each payer's distribution.

THE RULES (AI_HANDOFF "Round 12 additions"; each is pinned by
tests/mrfx/test_review12.py's hand-worked case):

- Confined to ONE state and ONE discipline, both required — a visit value
  pooled across states or disciplines is not a market. Nothing else narrows
  the population (no city, no therapists-only switch); payers may be limited.
- Population = the Benchmark's market on the house basis: dollar rates,
  professional claims, base modifiers, each contract at its newest month (or
  the pinned month), NPI-in-the-TIN-slot rows excluded, the discipline's own
  lines plus untagged ones. A practice is a TIN located in the state (a
  multi-state TIN counts in each of its states, as everywhere else); its rate
  for a code with a payer is the median of its published rates — read from
  the materialised per-practice medians (benchmark.practice_base_sql), never a
  new scan of raw rates.
- A practice x payer pair counts only when that payer prices the practice on
  EVERY code of the basket. A partial basket is not a visit: such pairs are
  excluded and counted ("incomplete").
- Visit value of a pair = sum over the basket of units x the practice's rate,
  rounded exactly as the visit calculator prices a visit (each rate to the
  cent, each line to the cent, the total to the cent), so a one-TIN subject's
  own value is the same number in its row and in the population.
- Per payer: practices, mean, median, P25, P75 of those values (P25 / P75 by
  linear interpolation, DuckDB's quantile_cont). All payers: each practice
  contributes the MEDIAN of its own per-payer visit values — never a sum
  across payers (round 7 P0.2: cross-payer totals are not additive).
- A cell backed by fewer than MIN_PRACTICES practices is suppressed: no
  numbers, no count. No payer reaching the minimum is a refusal sentence.
- With a subject: its value per payer is price_visit's (the rate card's
  rates: median across its TINs; out-of-state TINs are its own — the round 9
  subject rule). Its position is the MID-RANK among the payer's practices
  with the subject counted once in place of its own TINs ("this practice
  included", Leads' wording); $ and % against the mean and the median are
  computed from the unrounded mean / median and rounded once; its quartile is
  read from the PRINTED percentile (<25 Q1, <50 Q2, <75 Q3, else Q4 — review
  13 item 4.1; P25 / median / P75 disagree with the percentile on ties).
- Every printed number goes through rounding.half_up; rows are listed by
  payer name (unique), the all-payers row last; the screen, the printable
  card and the CSV are rendered from this one dict.
"""

from __future__ import annotations

import html
import math
import statistics

from .rounding import half_up, signed_pct
from .store import Store, defuse_csv, generation_cached
from .visit import (DEFAULT_PRESET, PRESETS, UNITS_RULE, VALUE_NOTE, VisitError,
                    define_visit, parse_lines, price_visit)

# the house minimum behind any printed market statistic (market._MIN_CELL)
MIN_PRACTICES = 5

MARKET_NOTE = (
    "Visit value in this market: one standard visit priced at every practice of "
    "the state and discipline, with each payer, at that practice's own published "
    "rates. A practice counts for a payer only when the payer prices EVERY code "
    "of the visit for it — a partial visit is excluded and counted, never priced "
    "at $0. The average is the arithmetic mean; the median is the middle "
    "practice; P25 and P75 bound the middle half. 'All payers' gives each "
    "practice ONE value — the median of its own per-payer visit values — never a "
    "sum across payers. A payer backed by fewer than "
    f"{MIN_PRACTICES} practices is not shown. A practice is a TIN located in the "
    "state (its NPIs' NPPES addresses); a TIN in two states counts in both.")
SUBJECT_NOTE = (
    "This practice's position is its mid-rank among the practices priced for the "
    "payer, this practice included (counted once, in place of its own TINs): "
    "those paid less, plus half of those paid the same, as a share of the others. "
    "Quartile: read from that printed percentile — Q1 below 25, Q2 below 50, Q3 "
    "below 75, Q4 from 75 — so a practice tied at the median of a tied market "
    "reads the same on both cells.")


def _quantile_cont(s: list[float], q: float) -> float:
    """DuckDB's quantile_cont on a sorted list (linear interpolation)."""
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _stats(vals: list[float]) -> dict:
    s = sorted(vals)
    mean = math.fsum(s) / len(s)
    med = statistics.median(s)
    return {"_mean": mean, "_median": med,
            "practices": len(s), "mean": half_up(mean, 2), "median": half_up(med, 2),
            "p25": half_up(_quantile_cont(s, 0.25), 2), "p75": half_up(_quantile_cont(s, 0.75), 2)}


def _position(v: float, others: list[float], st: dict) -> dict:
    """The subject's place in a distribution (`others` excludes it)."""
    pop = others + [v]
    below = sum(1 for x in others if x < v)
    tied = sum(1 for x in others if x == v)
    pct = (100.0 * (below + 0.5 * tied) / (len(pop) - 1)) if len(pop) > 1 else None
    # the quartile is read from the PRINTED percentile (review 13 item 4.1):
    # read from P25 / median / P75 it said "Q3" beside "percentile 26" for a
    # practice AT a tied median — fee schedules tie heavily (1,668 TX
    # practices, four distinct visit values). No other number moves.
    printed = None if pct is None else int(half_up(pct, 0))
    if printed is None:
        quart = "Q1" if v < st["p25"] else "Q2" if v < st["median"] else \
            "Q3" if v < st["p75"] else "Q4"
    elif printed < 25:
        quart = "Q1"
    elif printed < 50:
        quart = "Q2"
    elif printed < 75:
        quart = "Q3"
    else:
        quart = "Q4"
    mean, med = st["_mean"], st["_median"]
    return {"value": v, "percentile": None if pct is None else int(half_up(pct, 0)),
            "ranked_among": len(pop), "quartile": quart,
            "vs_mean": half_up(v - mean, 2),
            "vs_mean_pct": half_up(100.0 * (v - mean) / mean, 1) if mean else None,
            "vs_median": half_up(v - med, 2),
            "vs_median_pct": half_up(100.0 * (v - med) / med, 1) if med else None}


def _basket(raw_lines, preset):
    key = None
    if raw_lines in (None, "", []):
        key = preset or DEFAULT_PRESET
        if key not in PRESETS:
            raise VisitError(f"unknown visit preset {key!r}")
        lines = parse_lines(PRESETS[key]["lines"])
        label, basis = PRESETS[key]["label"], PRESETS[key]["basis"]
    else:
        lines = parse_lines(raw_lines)
        label, basis = "Custom visit", "your own visit definition"
    return key, lines, label, basis


def _visit_value(rates: dict, billed: list[tuple[str, int]]) -> float:
    """price_visit's arithmetic: rate to the cent, line to the cent, total."""
    return half_up(math.fsum(half_up(u * half_up(rates[c], 2), 2) for c, u in billed), 2)


def _compute_values(store: Store, market: dict | None, raw_lines=None,
                    preset: str | None = None) -> dict:
    """Every practice's visit value per payer — the numbers both the block
    (market_visit_value) and its ranked practice lists stand on."""
    from .benchmark import DISCIPLINES, normalize_market, practice_base_sql
    m0 = market if isinstance(market, dict) else {}
    if not str(m0.get("state") or "").strip():
        raise VisitError("pick a state — a visit's value is a market figure, and a market "
                         "is one state (payers pay differently from state to state)")
    disc = str(m0.get("discipline") or "").strip().upper()
    if disc not in DISCIPLINES:
        raise VisitError("pick a discipline — PT, OT or SLP: a visit is priced on one "
                         "discipline's rates")
    m = normalize_market({"month": m0.get("month") or "latest", "state": m0["state"],
                          "discipline": disc, "payers": m0.get("payers") or []})
    key, lines, label, basis = _basket(raw_lines, preset)
    visit = define_visit(lines)
    # in the visit's own line order (the calculator's), each code once
    billed = [(r["code"], r["units"]) for r in visit["lines"] if r["units"]]
    if not billed:
        raise VisitError("this visit bills no unit — add minutes to its codes")
    codes = [c for c, _ in billed]
    # the visit's codes only (billing_code is a partition-key column, so
    # the newest-month rule is unchanged): the materialised table when it is
    # already built, else this scoped read — never a whole-spine build in
    # the request (benchmark.practice_base_sql, scoped_if_cold)
    in_c = ", ".join("?" for _ in codes)
    sql, params = practice_base_sql(store, {**m, "codes": codes}, per_payer=True,
                                    prefilter=f"billing_code IN ({in_c})",
                                    prefilter_params=list(codes), scoped_if_cold=True)
    with store.connect() as con:
        rows = con.execute(f"SELECT payer, billing_code, tin_value, rate FROM ({sql}) "
                           "ORDER BY payer, tin_value, billing_code", params).fetchall()
    pairs: dict[tuple[str, str], dict] = {}
    for payer, code, tin, rate in rows:
        if rate is not None:
            pairs.setdefault((payer, tin), {})[code] = float(rate)
    values: dict[str, dict[str, float]] = {}
    incomplete: dict[str, int] = {}
    for (payer, tin), rates in sorted(pairs.items()):
        if all(c in rates for c in codes):
            values.setdefault(payer, {})[tin] = _visit_value(rates, billed)
        else:
            incomplete[payer] = incomplete.get(payer, 0) + 1
            values.setdefault(payer, {})
    return {"m": m, "disc": disc, "key": key, "label": label, "basis": basis,
            "visit": visit, "billed": billed, "codes": codes, "pairs": pairs,
            "values": values, "incomplete": incomplete}


def _all_payer_values(values: dict[str, dict[str, float]]) -> dict[str, float]:
    """All payers: each practice's ONE value = the median of its own payers'."""
    per_tin: dict[str, list[float]] = {}
    for payer in sorted(values):
        for tin, v in values[payer].items():
            per_tin.setdefault(tin, []).append(v)
    return {t: half_up(statistics.median(v), 2) for t, v in per_tin.items()}


@generation_cached("visit_market.value")
def market_visit_value(store: Store, market: dict | None, raw_lines=None,
                       preset: str | None = None, subject: str | None = None) -> dict:
    """The block's numbers (see the module docstring for every rule)."""
    from .benchmark import basis_note_for, month_label, subject_tins_for_market
    b = _compute_values(store, market, raw_lines, preset)
    m, disc, key, label, basis = b["m"], b["disc"], b["key"], b["label"], b["basis"]
    visit, billed, values, incomplete = b["visit"], b["billed"], b["values"], b["incomplete"]
    payers = sorted(values)

    subj = None
    subject = str(subject or "").strip() or None
    stins: set[str] = set()
    if subject:
        stins = set(subject_tins_for_market(store, subject, m))
        with store.connect() as con:      # the resolver falls back to the text itself
            known = stins and con.execute(
                "SELECT 1 FROM tin_directory WHERE tin_value IN (SELECT unnest(?::VARCHAR[])) "
                "LIMIT 1", [sorted(stins)]).fetchone()
        if not known:
            raise VisitError(f"no practice in the store matches {subject!r} — check the "
                             "name, TIN or NPI, or leave the practice blank")
        sv = price_visit(store, subject, m, raw_lines, preset)
        subj = {r["payer"]: r["visit_value"] for r in sv["rows"] if r["complete"]}

    out_rows = []
    for payer in payers:
        vals = values[payer]
        row = {"payer": payer, "incomplete_excluded": incomplete.get(payer, 0)}
        if len(vals) < MIN_PRACTICES:
            row.update(suppressed=True, practices=None, mean=None, median=None,
                       p25=None, p75=None)
        else:
            st = _stats(list(vals.values()))
            row.update(suppressed=False, **{k: v for k, v in st.items() if not k.startswith("_")})
            row["_st"] = st
        if subj is not None:
            v = subj.get(payer)
            if v is None:
                row["subject"] = None
            elif row["suppressed"]:
                row["subject"] = {"value": v, "percentile": None}
            else:
                others = [x for t, x in vals.items() if t not in stins]
                row["subject"] = _position(v, others, row["_st"])
        out_rows.append(row)
    shown = [r for r in out_rows if not r["suppressed"]]
    if not shown:
        raise VisitError(
            f"no payer prices this visit for at least {MIN_PRACTICES} {disc} practices in "
            f"{m['state']} as of {month_label(m['month'])} — "
            + (f"{sum(len(v) for v in values.values())} practice-payer pair(s) price the "
               f"whole visit, {sum(incomplete.values())} price only part of it. "
               if values else "no practice there has a published rate for these codes. ")
            + "Try another visit, the latest month, or more payers.")

    # all payers: each practice's ONE value = the median of its own payers'
    all_vals = _all_payer_values(values)
    all_row = {"payer": "All payers", "incomplete_excluded": None}
    if len(all_vals) < MIN_PRACTICES:
        all_row.update(suppressed=True, practices=None, mean=None, median=None, p25=None, p75=None)
    else:
        st = _stats(list(all_vals.values()))
        all_row.update(suppressed=False, **{k: v for k, v in st.items() if not k.startswith("_")})
        all_row["_st"] = st
    if subj is not None:
        if not subj:
            all_row["subject"] = None
        else:
            v_all = half_up(statistics.median(list(subj.values())), 2)
            if all_row["suppressed"]:
                all_row["subject"] = {"value": v_all, "percentile": None}
            else:
                others = [x for t, x in all_vals.items() if t not in stins]
                all_row["subject"] = _position(v_all, others, all_row["_st"])
    for r in out_rows + [all_row]:
        r.pop("_st", None)

    units = visit["units"]
    basket_txt = " + ".join(f"{c}×{u}" for c, u in billed)
    head = (all_row if not all_row["suppressed"] else shown[0])
    headline = (f"One {disc} visit ({basket_txt}, {units} units) in {m['state']} is worth "
                f"${head['median']:,.2f} at the median practice"
                + (" across all payers" if head is all_row else f" with {head['payer']}")
                + f" — average (mean) ${head['mean']:,.2f}, middle half ${head['p25']:,.2f}"
                f" to ${head['p75']:,.2f}, over {head['practices']:,} practices.")
    as_of = month_label(m["month"])
    return {
        "state": m["state"], "discipline": disc, "month": m["month"], "as_of": as_of,
        # month_label already says "newest file per contract" for latest
        "as_of_rule": ("" if str(m["month"]).lower() == "latest"
                       else f"only the {as_of} files"),
        "payers_filter": m.get("payers") or [],
        "preset": key, "label": label, "basis": basis,
        "lines": visit["lines"], "units": units, "basket": basket_txt,
        "flags": visit["flags"],
        "basis_note": basis_note_for(m),
        "min_practices": MIN_PRACTICES,
        "rows": out_rows, "all_payers": all_row,
        "suppressed_payers": [r["payer"] for r in out_rows if r["suppressed"]],
        "incomplete_excluded": sum(incomplete.values()),
        "subject": subject, "subject_tins": len(stins) if subject else 0,
        "headline": headline, "note": MARKET_NOTE,
        "subject_note": SUBJECT_NOTE if subject else None,
        "units_rule": UNITS_RULE, "value_note": VALUE_NOTE,
    }


ALL_PAYERS = "All payers"
PRACTICES_PAGE = 50
PRACTICES_PAGE_MAX = 500
PRACTICES_NOTE = (
    "Every practice priced for this visit, by its visit value — the same values "
    "the row above summarises (a practice counts only when the payer prices EVERY "
    "code of the visit for it). Rank: 1 + the practices paid more (ties share a "
    "rank, listed by tax ID). Percentile: its mid-rank among the others — those "
    "paid less plus half of those paid the same, as a share of the others. "
    "'All payers': each practice's value is the median of its own per-payer "
    "visit values. A practice's name: its organization NPIs' legal business "
    "name (NPPES); else the group most of its clinicians are enrolled with "
    "(CMS Doctors and Clinicians file); else, for one provider, that "
    "provider; else 'Unnamed group' — never one clinician's name for a group "
    "(name_source says which). Cities: where most of its NPIs practise in "
    "the state.")


@generation_cached("visit_market.ranked")
def _ranked(store: Store, market: dict | None, raw_lines, preset, payer: str) -> dict:
    """One payer's (or All payers') practices, highest visit value first:
    [tin, value, rank, percentile, detail]. A suppressed cell (fewer than
    MIN_PRACTICES practices) has no list either — it would print the cell."""
    b = _compute_values(store, market, raw_lines, preset)
    values, billed = b["values"], b["billed"]
    if payer == ALL_PAYERS:
        vals = _all_payer_values(values)
        by_tin: dict[str, list[tuple[str, float]]] = {}
        for p_ in sorted(values):
            for tin, v in values[p_].items():
                by_tin.setdefault(tin, []).append((p_, v))
        detail = {t: {"payers": len(pv), "low": min(v for _p, v in pv),
                      "high": max(v for _p, v in pv)} for t, pv in by_tin.items()}
    else:
        if payer not in values:
            raise VisitError(f"{payer!r} prices no practice for this visit in "
                             f"{b['m']['state']} — pick a payer from the table")
        vals = values[payer]
        detail = {t: {"rates": [{"code": c, "units": u,
                                  "rate": half_up(b["pairs"][(payer, t)][c], 2)}
                                 for c, u in billed]} for t in vals}
    if len(vals) < MIN_PRACTICES:
        raise VisitError(f"{payer} prices this visit for fewer than {MIN_PRACTICES} "
                         f"practices in {b['m']['state']}, so its practices are not "
                         "listed (the house minimum behind any market figure)")
    st = _stats(list(vals.values()))
    ranked = sorted(vals.items(), key=lambda kv: (-kv[1], kv[0]))   # unique: tin
    n = len(ranked)
    allv = sorted(vals.values())
    import bisect
    rows = []
    for tin, v in ranked:
        below = bisect.bisect_left(allv, v)
        tied_others = bisect.bisect_right(allv, v) - below - 1
        higher = n - bisect.bisect_right(allv, v)
        pct = 100.0 * (below + 0.5 * tied_others) / (n - 1) if n > 1 else None
        rows.append({"tin": tin, "value": v, "rank": higher + 1,
                     "percentile": None if pct is None else int(half_up(pct, 0)),
                     "vs_median": half_up(v - st["_median"], 2),
                     "vs_median_pct": (half_up(100.0 * (v - st["_median"]) / st["_median"], 1)
                                       if st["_median"] else None),
                     **detail[tin]})
    return {"payer": payer, "state": b["m"]["state"], "discipline": b["disc"],
            "month": b["m"]["month"], "label": b["label"],
            "basket": " + ".join(f"{c}×{u}" for c, u in billed),
            "codes": [c for c, _ in billed], "median": st["median"], "mean": st["mean"],
            "rows": rows}


def market_visit_practices(store: Store, market: dict | None, raw_lines=None,
                           preset: str | None = None, payer: str = ALL_PAYERS,
                           subject: str | None = None, offset: int = 0,
                           limit: int | None = PRACTICES_PAGE, order: str = "desc") -> dict:
    """The practices behind one row of the block, ranked by visit value
    (highest first, or lowest first with order='asc'), one page at a time,
    each with its name, city, tax ID (SSN-pattern masked), NPIs and — per
    payer — the rates that make up its visit."""
    from .benchmark import subject_tins_for_market
    from .store import mask_tin
    payer = str(payer or ALL_PAYERS).strip() or ALL_PAYERS
    r = _ranked(store, market, raw_lines, preset, payer)
    rows = r["rows"]
    if order == "asc":
        rows = sorted(rows, key=lambda x: (x["value"], x["tin"]))
    offset = max(0, int(offset or 0))
    # None = every row in one pass (the CSV); a page is capped for the screen
    limit = len(rows) if limit is None else max(1, min(int(limit or PRACTICES_PAGE),
                                                        PRACTICES_PAGE_MAX))
    page = rows[offset:offset + limit]
    tins = [x["tin"] for x in page]
    stins: set[str] = set()
    subject = str(subject or "").strip() or None
    if subject:
        try:
            stins = set(subject_tins_for_market(store, subject, {"state": r["state"]}))
        except Exception:  # noqa: BLE001 — the list never fails on the highlight
            stins = set()
    info: dict[str, dict] = {}
    npis: dict[str, list[dict]] = {}
    if tins:
        with store.connect() as con:
            cur = con.execute(
                "SELECT tin_value, display_name, entity_kind, primary_state, cities, "
                "npi_count, therapy_npi_count, primary_discipline FROM tin_directory "
                "WHERE tin_value IN (SELECT unnest(?::VARCHAR[]))", [tins])
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                info[row[0]] = dict(zip(cols, row))
            for tin, npi, name, etype, city, st_, zip_ in con.execute(
                    f"SELECT p.tin_value, p.npi, n.org_name, n.entity_type, n.city, n.state, "
                    f"n.zip FROM (SELECT DISTINCT tin_value, npi FROM {store.pairs_relation()} "
                    "WHERE tin_value IN (SELECT unnest(?::VARCHAR[])) AND npi IS NOT NULL) p "
                    "LEFT JOIN npi_directory n USING (npi) "
                    "ORDER BY p.tin_value, (n.entity_type = 'NPI-2') DESC NULLS LAST, p.npi",
                    [tins]).fetchall():
                npis.setdefault(tin, []).append(
                    {"npi": npi, "name": name, "type": etype, "city": city, "state": st_,
                     "zip": (str(zip_)[:5] if zip_ else None)})
    # the practice's name and cities resolved for THIS export's state
    # (mrfx/naming.py: org NPIs, then the CMS file, then a solo provider,
    # else "Unnamed group"; in-state tie-breaks and cities)
    names = store.practice_names_for(tins, r["state"]) if tins else {}
    out = []
    for x in page:
        d = info.get(x["tin"], {})
        nm = names.get(x["tin"], {})
        all_npis = npis.get(x["tin"], [])
        out.append({
            **{k: v for k, v in x.items() if k != "tin"},
            "tin": mask_tin(x["tin"]),
            "name": nm.get("practice_name") or d.get("display_name") or f"TIN {mask_tin(x['tin'])}",
            "name_source": nm.get("name_source") or ("no_npi" if not all_npis else "pending"),
            "kind": d.get("entity_kind"),
            "state": d.get("primary_state"),
            "cities": list(nm.get("top_cities") or (d.get("cities") or [])[:3]),
            "npi_count": d.get("npi_count") if d.get("npi_count") is not None else len(all_npis),
            "therapy_npi_count": d.get("therapy_npi_count"),
            "discipline": d.get("primary_discipline"),
            "npis": all_npis[:5], "npis_more": max(0, len(all_npis) - 5),
            "is_subject": x["tin"] in stins})
    return {"payer": r["payer"], "state": r["state"], "discipline": r["discipline"],
            "month": r["month"], "label": r["label"], "basket": r["basket"],
            "codes": r["codes"], "median": r["median"], "mean": r["mean"],
            "order": "asc" if order == "asc" else "desc",
            "total": len(rows), "offset": offset, "limit": limit, "rows": out,
            "note": PRACTICES_NOTE}


def practices_csv(store: Store, market: dict | None, raw_lines=None,
                  preset: str | None = None, payer: str = ALL_PAYERS,
                  subject: str | None = None) -> str:
    """Every practice of one row, highest visit value first, as CSV."""
    import csv
    import io
    # every practice in ONE pass: the directory and NPI lookups read the
    # (TIN, NPI) pairs once — 500-row pages re-read them 30 times (34 s for
    # 14,674 practices on a 1.19 B-rate store)
    first = market_visit_practices(store, market, raw_lines, preset, payer, subject,
                                   0, None)
    rows = list(first["rows"])
    buf = io.StringIO()
    w = csv.writer(buf)
    for line in (f"Visit value by practice — {first['payer']}, {first['state']}, "
                 f"{first['discipline']}, as of {first['month']}",
                 f"Visit: {first['label']} — {first['basket']}", first["note"]):
        buf.write("# " + " ".join(str(line).split()) + "\n")     # a comment line, as-is
    per_code = first["payer"] != ALL_PAYERS
    head = ["rank", "practice", "name_source", "tax_id", "state", "cities", "npi_count",
            "therapy_npi_count", "npis", "visit_value", "vs_median", "vs_median_pct",
            "percentile", "this_practice"]
    head += [f"rate_{c}" for c in first["codes"]] if per_code else ["payers", "lowest_payer_value",
                                                                    "highest_payer_value"]
    w.writerow(head)
    for x in rows:
        line = [x["rank"], x["name"], x["name_source"], x["tin"], x["state"] or "",
                "; ".join(x["cities"]),
                x["npi_count"] if x["npi_count"] is not None else "",
                x["therapy_npi_count"] if x["therapy_npi_count"] is not None else "",
                "; ".join(n["npi"] for n in x["npis"]) + (f" (+{x['npis_more']} more)"
                                                          if x["npis_more"] else ""),
                f"{x['value']:.2f}", f"{x['vs_median']:.2f}",
                "" if x["vs_median_pct"] is None else f"{x['vs_median_pct']:.1f}",
                "" if x["percentile"] is None else x["percentile"],
                "yes" if x["is_subject"] else ""]
        if per_code:
            rates = {r_["code"]: r_["rate"] for r_ in x["rates"]}
            line += [f"{rates[c]:.2f}" for c in first["codes"]]
        else:
            line += [x["payers"], f"{x['low']:.2f}", f"{x['high']:.2f}"]
        w.writerow([defuse_csv(str(v)) for v in line])
    return buf.getvalue()


# ------------------------------------------------------------- rendering --

def _money(v) -> str:
    if v is None:
        return "–"
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def _signed_money(v) -> str:
    if v is None:
        return "–"
    return ("+" if v > 0 else "-" if v < 0 else "±") + f"${abs(v):,.2f}"


def _pct(v) -> str:
    return "–" if v is None else signed_pct(v, 1)


def method_lines(d: dict) -> list[str]:
    """The labels every surface carries (state, discipline, as-of rule, basket
    and units, basis, counts and exclusions, the mean spelled out)."""
    lines = [
        f"Visit value in this market — {d['state']}, {d['discipline']}, as of {d['as_of']}"
        + (f" ({d['as_of_rule']})." if d["as_of_rule"] else "."),
        f"Visit: {d['label']} — {d['basket']} = {d['units']} units ({d['basis']}).",
        UNITS_RULE,
        f"Basis: {d['basis_note']}",
        (f"Payers: limited to {', '.join(d['payers_filter'])}." if d["payers_filter"]
         else "Payers: every payer with published rates in the state."),
        f"Practice-payer pairs excluded because the payer does not price every code of "
        f"the visit: {d['incomplete_excluded']:,}.",
        (f"Not shown (fewer than {d['min_practices']} practices): "
         f"{', '.join(d['suppressed_payers'])}." if d["suppressed_payers"] else
         f"Every payer listed is backed by at least {d['min_practices']} practices."),
        d["note"],
    ]
    if d.get("subject"):
        lines.append(f"Subject: {d['subject']} ({d['subject_tins']} TIN(s); its own rates "
                     "wherever its TINs are). " + d["subject_note"])
    lines.append(VALUE_NOTE)
    return lines


def _cells(r: dict, with_subject: bool) -> list[str]:
    """One row's printed cells — shared by the CSV and the HTML table."""
    if r["suppressed"]:
        base = [f"fewer than {MIN_PRACTICES} practices — not shown", "", "", "", "", ""]
    else:
        base = [f"{r['practices']:,}", _money(r["mean"]), _money(r["median"]),
                _money(r["p25"]), _money(r["p75"]), ""]
    base[5] = "" if r.get("incomplete_excluded") is None else f"{r['incomplete_excluded']:,}"
    if not with_subject:
        return base
    s = r.get("subject")
    if not s:
        return base + ["no complete visit", "", "", "", ""]
    if s.get("percentile") is None:
        return base + [_money(s["value"]), "", "", "", ""]
    return base + [_money(s["value"]),
                   f"p{s['percentile']} of {s['ranked_among']}", s["quartile"],
                   f"{_signed_money(s['vs_mean'])} ({_pct(s['vs_mean_pct'])})",
                   f"{_signed_money(s['vs_median'])} ({_pct(s['vs_median_pct'])})"]


HEAD = ["Payer", "Practices", "Average (mean)", "Median", "P25", "P75",
        "Pairs excluded (partial visit)"]
SUBJECT_HEAD = ["This practice", "Percentile (this practice included)", "Quartile",
                "vs average (mean)", "vs median"]


def to_csv(d: dict) -> str:
    """Methodology as leading comment lines, then one row per payer and the
    all-payers row — the printed cells, money to the cent."""
    import csv
    import io
    out = io.StringIO()
    for ln in method_lines(d):
        out.write(f"# {ln}\n")
    w = csv.writer(out)
    subj = bool(d.get("subject"))
    w.writerow(["state", "discipline", "as_of", "visit", "units",
                "payer", "practices", "mean", "median", "p25", "p75", "pairs_excluded_partial_visit",
                "suppressed"]
               + (["subject_value", "subject_percentile", "ranked_among", "quartile",
                   "vs_mean_dollars", "vs_mean_pct", "vs_median_dollars", "vs_median_pct"]
                  if subj else []))

    def money(v):
        return "" if v is None else f"{v:.2f}"
    for r in d["rows"] + [d["all_payers"]]:
        row = [d["state"], d["discipline"], d["as_of"], d["basket"], d["units"],
               defuse_csv(r["payer"]), "" if r["practices"] is None else r["practices"],
               money(r["mean"]), money(r["median"]), money(r["p25"]), money(r["p75"]),
               "" if r.get("incomplete_excluded") is None else r["incomplete_excluded"],
               "yes" if r["suppressed"] else "no"]
        if subj:
            s = r.get("subject") or {}
            row += [money(s.get("value")),
                    "" if s.get("percentile") is None else str(s["percentile"]),
                    s.get("ranked_among", "") if s.get("percentile") is not None else "",
                    s.get("quartile", "") if s.get("percentile") is not None else "",
                    money(s.get("vs_mean")) if s.get("percentile") is not None else "",
                    "" if s.get("vs_mean_pct") is None else f"{s['vs_mean_pct']:.1f}",
                    money(s.get("vs_median")) if s.get("percentile") is not None else "",
                    "" if s.get("vs_median_pct") is None else f"{s['vs_median_pct']:.1f}"]
        w.writerow(row)
    return out.getvalue()


def block_html(d: dict) -> str:
    """The block as it appears in a printed document (rate card or its own)."""
    e = html.escape
    subj = bool(d.get("subject"))
    head = "".join(f"<th{' class=num' if i else ''}>{e(h)}</th>"
                   for i, h in enumerate(HEAD + (SUBJECT_HEAD if subj else [])))
    body = ""
    for r in d["rows"] + [d["all_payers"]]:
        cells = _cells(r, subj)
        cls = " class='allp'" if r is d["all_payers"] else ""
        if r["suppressed"]:
            tds = (f"<td colspan='5' class='sub'>{e(cells[0])}</td>"
                   f"<td class='num'>{e(cells[5])}</td>")
        else:
            tds = "".join(f"<td class='num'>{e(c)}</td>" for c in cells[:6])
        tds += "".join(f"<td class='num'>{e(c)}</td>" for c in cells[6:])
        body += f"<tr{cls}><td>{e(r['payer'])}</td>{tds}</tr>"
    flags = "".join(f"<p class='sub'>{e(f)}</p>" for f in d.get("flags") or [])
    return (f"<h2>Visit value in this market — {e(d['state'])}, {e(d['discipline'])}</h2>"
            f"<div class='meta vv-headline'>{e(d['headline'])}</div>"
            f"<div class='wrap'><table class='vv'><thead><tr>{head}</tr></thead>"
            f"<tbody>{body}</tbody></table></div>{flags}"
            f"<p class='sub vv-method'>{'<br>'.join(e(x) for x in method_lines(d))}</p>")


def render_report(cfg, store: Store, d: dict) -> str:
    """The block as its own printable page (no subject needed)."""
    from . import __version__
    from .benchmark import _brand_header
    e = html.escape
    import datetime as dt
    gen = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = f"Visit value — {d['state']} {d['discipline']}" + (
        f" — {d['subject']}" if d.get("subject") else "")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{e(title)}</title>
<style>
 body {{ font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #101828;
        max-width: 1100px; margin: 32px auto; padding: 0 24px; }}
 h1 {{ font-size: 20px; margin-bottom: 2px; }} h2 {{ font-size: 15px; margin-top: 28px; }}
 .brand {{ color: #475467; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }}
 .brandbar {{ display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }}
 .brandbar .brand {{ margin: 0; }} .logo {{ max-height: 40px; max-width: 200px; }}
 .meta {{ color: #475467; margin-bottom: 12px; }} .wrap {{ overflow-x: auto; }}
{BLOCK_CSS}
 footer {{ margin-top: 32px; border-top: 1px solid #d0d5dd; padding-top: 12px;
          color: #475467; font-size: 11px; white-space: pre-wrap; }}
 @page {{ size: landscape; margin: 10mm; }}
 @media print {{ body {{ margin: 0; max-width: none; }} table {{ font-size: 10.5px; }} th, td {{ padding: 3px 5px; }} * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }} }}
</style></head><body>
{_brand_header(cfg)}
<h1>{e(title)}</h1>
<div class="meta">What one standard visit is worth across the market, as of {e(d['as_of'])}.</div>
{block_html(d)}
<footer>Generated {e(gen)} by MRF Explorer v{e(__version__)}.{''.join(chr(10) + e(x) for x in store.analytics_note())}</footer>
</body></html>"""


BLOCK_CSS = """
 table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
 th, td {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid #eaecf0; vertical-align: top; white-space: nowrap; }}
 th {{ font-size: 11px; color: #667085; text-transform: uppercase; letter-spacing: .04em; }}
 .num {{ text-align: right; }} .sub {{ color: #667085; font-size: 11px; font-weight: 400; }}
 tr.allp td {{ font-weight: 700; border-top: 2px solid #d0d5dd; }}
 .vv-method {{ white-space: normal; }}
""".replace("{{", "{").replace("}}", "}")
