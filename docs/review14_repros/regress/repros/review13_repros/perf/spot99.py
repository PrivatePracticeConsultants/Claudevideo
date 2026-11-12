"""Hand checks on the 99M store (read-only, server stopped): recompute from
raw `rates` the numbers the saved bodies print for MO / TIN 100049428 /
entity 'PRACTICE 18 · OR', and compare."""
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from house import open_ro, midrank, BASE_MODS  # noqa: E402
from mrfx.rounding import half_up as _hu  # noqa: E402

S = "/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/scale4n/store"
B = Path(__file__).resolve().parent / "bodies"
TIN = "100049428"
PROBLEMS = []


def half_up(x, d=0):
    return None if x is None else _hu(x, d)


def chk(label, got, exp, tol=0.0051):
    ok = (got is None and exp is None) or (got is not None and exp is not None and abs(float(got) - float(exp)) <= tol)
    if not ok:
        PROBLEMS.append((label, got, exp))
    print(("OK   " if ok else "DIFF ") + f"{label}: api={got} independent={exp}", flush=True)


def qcont(s, q):
    s = sorted(s)
    if not s:
        return None
    pos = (len(s) - 1) * q
    lo, hi = int(pos // 1), int(-(-pos // 1))
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def main():
    con = open_ro(S)
    con.execute("SET threads TO 4")
    q = lambda s, *a: con.execute(s, list(a)).fetchall()  # noqa: E731
    t0 = time.perf_counter()
    # every variant (payer, tin, code, mods, class, pos, month, discipline) of MO TINs + the entity's TINs, from RAW rows
    con.execute("""CREATE TEMP TABLE keep AS SELECT tin_value FROM tin_directory WHERE list_contains(states, 'MO')
                   UNION SELECT unnest(['100000018', '100111018', '100049428'])""")
    con.execute(f"""CREATE TEMP TABLE var AS
        WITH raw AS (
            SELECT payer, tin_value, tin_is_really_npi, billing_code, npi, discipline,
                   coalesce(array_to_string(billing_code_modifier, '|'), '') AS mods,
                   billing_class, coalesce(array_to_string(service_code, '|'), '') AS pos,
                   file_month, negotiated_rate, is_dollar_rate
            FROM rates WHERE tin_value IN (SELECT tin_value FROM keep)
              AND (negotiated_rate IS NULL OR isfinite(negotiated_rate)))
        SELECT payer, tin_value, tin_is_really_npi, billing_code, mods, billing_class, pos, file_month, is_dollar_rate,
               min(discipline) AS discipline,
               round(coalesce(median(DISTINCT negotiated_rate) FILTER (NOT is_dollar_rate OR negotiated_rate > 0.01),
                              median(DISTINCT negotiated_rate)), 4) AS rate
        FROM raw GROUP BY ALL""")
    con.execute("""CREATE TEMP TABLE newest AS SELECT payer, tin_value, tin_is_really_npi, billing_code, max(file_month) m FROM var GROUP BY ALL""")
    con.execute(f"""CREATE TEMP TABLE house AS SELECT v.* FROM var v JOIN newest n USING (payer, tin_value, tin_is_really_npi, billing_code)
        WHERE v.file_month = n.m AND NOT v.tin_is_really_npi AND v.is_dollar_rate AND v.rate > 0.01
          AND v.billing_class = 'professional' AND v.mods IN {BASE_MODS}""")
    con.execute("CREATE TEMP TABLE pm AS SELECT payer, billing_code, tin_value, median(rate) rate FROM house GROUP BY ALL")
    con.execute("CREATE TEMP TABLE pr AS SELECT billing_code, tin_value, median(rate) rate FROM house GROUP BY ALL")
    print(f"raw scan + house tables: {time.perf_counter() - t0:.1f}s; house rows", q("SELECT count(*) FROM house"), flush=True)
    mo = "tin_value IN (SELECT tin_value FROM tin_directory WHERE list_contains(states, 'MO'))"

    # ---- Benchmark MO ------------------------------------------------------
    Bm = json.load(open(B / "benchmark_market_MO.json"))
    subj = dict(q("SELECT billing_code, rate FROM pr WHERE tin_value = ?", TIN))
    peers = {}
    for c, t, r in q(f"SELECT billing_code, tin_value, rate FROM pr WHERE {mo} AND tin_value <> ?", TIN):
        peers.setdefault(c, []).append(r)
    exact = []
    for r in Bm["rows"]:
        c = r["billing_code"]
        p = peers[c]
        chk(f"benchmark MO {c} subject_rate", r["subject_rate"], half_up(subj[c], 2))
        chk(f"benchmark MO {c} n_peers", r["n_peers"], len(p), 0)
        chk(f"benchmark MO {c} p25", r["p25"], half_up(qcont(p, .25), 2))
        chk(f"benchmark MO {c} p50", r["p50"], half_up(qcont(p, .5), 2))
        chk(f"benchmark MO {c} p75", r["p75"], half_up(qcont(p, .75), 2))
        mr = midrank(subj[c], p)
        chk(f"benchmark MO {c} pct_exact", r["subject_percentile_exact"], mr, 1e-9)
        if r["in_position"]:
            exact.append(mr)
    overall = half_up(statistics.median(exact))
    chk("quality/size overall position", json.load(open(B / "quality_size.json"))["subject"]["position"], overall, 0)
    chk("dossier median_percentile", json.load(open(B / "report_dossier_json.json"))["facts"]["median_percentile"], overall, 0)
    # ---- Fee schedule (rate card) per payer x code -------------------------
    F = json.load(open(B / "schedule_fee.json"))["fee_schedule"]
    pm_subj = {(p, c): r for p, c, r in q("SELECT payer, billing_code, rate FROM pm WHERE tin_value = ?", TIN)}
    by = {}
    for pay, c, t, r in q(f"SELECT payer, billing_code, tin_value, rate FROM pm WHERE {mo}"):
        by.setdefault((pay, c), {})[t] = r
    for e in F["codes"][:8]:
        c = e["billing_code"]
        for p, v in e["rates"].items():
            chk(f"fee {p} {c} rate", v["rate"], half_up(pm_subj.get((p, c)), 2))
            others = [r for t, r in by.get((p, c), {}).items() if t != TIN]
            chk(f"fee {p} {c} market_median", v["market_median"], half_up(statistics.median(others), 2) if len(others) >= 3 else None)
            chk(f"fee {p} {c} n_peers", v["n_peers"], len(others), 0)
    # ---- Negotiate compare (Payer 03, MO) ----------------------------------
    N = json.load(open(B / "negotiate_compare.json"))
    for r in N["rows"][:8]:
        c = r["billing_code"]
        others = [v for t, v in by.get(("Payer 03", c), {}).items() if t != TIN]
        chk(f"negotiate P03 {c} subject_rate", r["subject_rate"], half_up(pm_subj[("Payer 03", c)], 2))
        chk(f"negotiate P03 {c} p50", r["p50"], half_up(qcont(others, .5), 2))
        chk(f"negotiate P03 {c} pct_exact", r["subject_percentile_exact"], midrank(pm_subj[("Payer 03", c)], others), 1e-9)
    # ---- Leads MO (therapy_only, p25, min 3 codes) -------------------------
    L = json.load(open(B / "leads_MO.json"))
    codes_state = {}
    for c, t, r in q(f"SELECT billing_code, tin_value, rate FROM pr WHERE {mo}"):
        codes_state.setdefault(c, {})[t] = r
    for lead in L["leads"][:3]:
        t = lead["tin_value"]
        pcts, gaps = [], []
        for c, d in codes_state.items():
            if t in d and len(d) >= 2 and len({half_up(v, 2) for v in d.values()}) >= 2:
                others = [x for tt, x in d.items() if tt != t]
                pcts.append(midrank(d[t], others))
                gaps.append(max(0.0, statistics.median(others) - d[t]))
        chk(f"lead {t} median_pct", lead.get("median_pct", lead.get("median_percentile")), half_up(statistics.median(pcts)), 0)
        chk(f"lead {t} n_codes", lead["n_codes"], len(pcts), 0)
        chk(f"lead {t} avg_gap_to_median", lead.get("avg_gap_to_median", lead.get("avg_gap")), half_up(statistics.mean(gaps), 2))
    # ---- Overview MO -------------------------------------------------------
    Ov = json.load(open(B / "overview.json"))
    print("note: bodies/overview.json is the NATIONAL overview; the MO variant was not saved — checking national top_codes needs a national scan (skipped). Payer index MO recomputed and printed:")
    pr_mo = {}
    for c, t, r in q(f"SELECT billing_code, tin_value, rate FROM pr WHERE {mo}"):
        pr_mo.setdefault(c, []).append(r)
    for pay in ("Payer 03", "Payer 07"):
        idx = [pmed / qcont(pr_mo[c], .5) for c, pmed, n in q(f"SELECT billing_code, median(rate), count(*) FROM pm WHERE payer=? AND {mo} GROUP BY 1 HAVING count(*) >= 3", pay)]
        print(f"   independent MO payer index {pay}: {half_up(statistics.median(idx), 3)} over {len(idx)} codes")
    # ---- Rate changes: Payer 03, MO, 2026-10 vs 2026-08 -------------------
    Ch = json.load(open(B / "changes_MO_payer_prev.json"))
    rows = Ch.get("changes") or Ch.get("rows") or []
    print("changes rows", len(rows), "keys", list(Ch)[:12])
    for r in rows[:6]:
        t = r["tin_value"]
        c = r["billing_code"]
        ms = r["modifier_set"]
        pos = r.get("service_code_set") or ""
        old = q("SELECT median(rate) FROM var WHERE payer='Payer 03' AND tin_value=? AND billing_code=? AND mods=? AND pos=? AND billing_class='professional' AND file_month='2026-08' AND NOT tin_is_really_npi AND is_dollar_rate", t, c, ms, pos)[0][0]
        new = q("SELECT median(rate) FROM var WHERE payer='Payer 03' AND tin_value=? AND billing_code=? AND mods=? AND pos=? AND billing_class='professional' AND file_month='2026-10' AND NOT tin_is_really_npi AND is_dollar_rate", t, c, ms, pos)[0][0]
        chk(f"change {t} {c} {ms}|{pos} old", r["old_rate"], half_up(old, 2))
        chk(f"change {t} {c} {ms}|{pos} new", r["new_rate"], half_up(new, 2))
        chk(f"change {t} {c} pct", r["pct_change"], half_up(100.0 * (new - old) / old, 1) if old else None, 0.051)
    # ---- Entity drawer 'PRACTICE 18 · OR' (TINs 100000018, 100111018) ------
    E = json.load(open(B / "entity_drawer_entity.json"))
    print("entity tins:", [t["tin_value"] for t in E["tins"]], "rates rows", len(E["rates"]), "chart", len(E["chart"]))
    ent = ["100000018", "100111018"]
    n = bad = 0
    for r in E["rates"][:400]:
        vals = [x[0] for x in q("""SELECT rate FROM var v JOIN newest n USING (payer, tin_value, tin_is_really_npi, billing_code)
            WHERE v.file_month = n.m AND v.payer=? AND v.billing_code=? AND v.mods=? AND v.billing_class=? AND v.pos=? AND v.is_dollar_rate = ?
              AND v.tin_value IN (SELECT unnest(?::VARCHAR[]))""", r["payer"], r["billing_code"], r["modifier_set"], r["billing_class"], r["service_code_set"], r["is_dollar_rate"], ent)]
        exp = round(statistics.median(vals), 4) if vals else None
        n += 1
        if exp is None or abs(exp - r["negotiated_rate"]) > 0.00011:
            bad += 1
            if bad <= 5:
                print("DIFF entity row", r["payer"], r["billing_code"], r["modifier_set"], r["service_code_set"], "api", r["negotiated_rate"], "members", vals)
    print(f"entity drawer rows checked {n}, mismatches {bad}")
    for ch in E["chart"][:6]:
        vals = [x[0] for x in q(f"""SELECT median(rate) FROM house WHERE payer=? AND billing_code=? AND tin_value IN (SELECT unnest(?::VARCHAR[]))
            GROUP BY mods, pos""", ch["payer"], ch["billing_code"], ent)]
        # chart: median over the entity's latest base professional variants, each variant = median of member TINs
        exp = half_up(statistics.median(vals), 2) if vals else None
        chk(f"entity chart {ch['payer']} {ch['billing_code']}", ch["median_rate"], exp)
    # ---- Visit value MO / PT (schedule_visit_market.json) -----------------
    V = json.load(open(B / "schedule_visit_market.json"))
    codes = ["97110", "97530", "97140", "97112"]
    vals = {}
    for pay, c, t, r in q(f"""SELECT payer, billing_code, tin_value, median(rate) FROM house WHERE {mo} AND billing_code IN ('97110','97530','97140','97112')
                               AND (discipline = 'PT' OR discipline = 'unspecified') GROUP BY ALL"""):
        vals.setdefault((pay, t), {})[c] = r
    per_payer = {}
    for (pay, t), d in vals.items():
        if all(c in d for c in codes):
            per_payer.setdefault(pay, {})[t] = half_up(sum(half_up(1 * half_up(d[c], 2), 2) for c in codes), 2)
    for row in V["rows"][:5]:
        pay = row["payer"]
        s = sorted(per_payer.get(pay, {}).values())
        chk(f"visit MO {pay} practices", row["practices"], len(s), 0)
        chk(f"visit MO {pay} median", row["median"], half_up(statistics.median(s), 2))
        chk(f"visit MO {pay} mean", row["mean"], half_up(statistics.fmean(s), 2))
        chk(f"visit MO {pay} p25", row["p25"], half_up(qcont(s, .25), 2))
        if row.get("subject") and TIN in per_payer.get(pay, {}):
            others = [v for t, v in per_payer[pay].items() if t != TIN]
            v = per_payer[pay][TIN]
            chk(f"visit MO {pay} subject value", row["subject"]["value"], v)
            chk(f"visit MO {pay} subject percentile", row["subject"]["percentile"], int(half_up(100 * (sum(1 for x in others if x < v) + 0.5 * sum(1 for x in others if x == v)) / len(others), 0)), 0)
    allv = {}
    for pay, d in per_payer.items():
        for t, v in d.items():
            allv.setdefault(t, []).append(v)
    s = sorted(half_up(statistics.median(v), 2) for v in allv.values())
    A = V["all_payers"]
    chk("visit MO ALL practices", A["practices"], len(s), 0)
    chk("visit MO ALL median", A["median"], half_up(statistics.median(s), 2))
    chk("visit MO ALL mean", A["mean"], half_up(statistics.fmean(s), 2))
    print("visit ALL subject", A.get("subject"))
    # ---- Roster MO Payer 03: the subject's row ------------------------------
    R = json.load(open(B / "roster.json"))
    me = [o for o in R["organizations"] if o["tin_value"] == TIN]
    if me:
        pp = []
        for (pay, c), d in by.items():
            if pay == "Payer 03" and TIN in d and len(d) >= 2 and len({half_up(v, 2) for v in d.values()}) >= 2:
                pp.append(midrank(d[TIN], [x for tt, x in d.items() if tt != TIN]))
        chk("roster P03 subject median_percentile", me[0]["median_percentile"], half_up(statistics.median(pp)) if pp else 50, 0)
        chk("roster P03 subject median_rate", me[0]["median_rate"], half_up(statistics.median([v for (p, c), v in pm_subj.items() if p == "Payer 03"]), 2))
    else:
        print("subject not in the roster's first 500 rows (sorted by name)")
    print("\nPROBLEMS:", len(PROBLEMS), f"({time.perf_counter() - t0:.0f}s)")
    for p in PROBLEMS:
        print("  ", p)


if __name__ == "__main__":
    main()
