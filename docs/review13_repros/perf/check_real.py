"""Independent recomputation of the real-store outputs from raw `rates`
(house.py) and comparison with the fetched bodies. usage: check_real.py <outdir> <tin> <state> <payer>"""
import csv
import io
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from house import house_ctes, open_ro, midrank  # noqa: E402
from mrfx.rounding import half_up as _hu  # noqa: E402


def half_up(x, d=0):
    return None if x is None else _hu(x, d)


S = "/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit13/perf/realstore/store"
PROBLEMS = []


def chk(label, got, exp, tol=0.0051):
    ok = (got is None and exp is None) or (got is not None and exp is not None and abs(float(got) - float(exp)) <= tol)
    if not ok:
        PROBLEMS.append((label, got, exp))
    print(("OK   " if ok else "DIFF ") + f"{label}: api={got} independent={exp}")


def qcont(s, q):
    s = sorted(s)
    if not s:
        return None

    pos = (len(s) - 1) * q
    lo, hi = int(pos // 1), int(-(-pos // 1))
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def main():
    out, tin, st, payer = sys.argv[1:5]
    out = Path(out)
    J = lambda n: json.load(open(out / f"{n}.json"))  # noqa: E731
    con = open_ro(S)
    con.execute(f"CREATE TEMP TABLE pm AS WITH {house_ctes()} SELECT * FROM pm")
    con.execute(f"CREATE TEMP TABLE pr AS WITH {house_ctes()} SELECT * FROM pr")
    con.execute("CREATE TEMP TABLE td AS SELECT tin_value, states, is_therapy, npi_count, display_name FROM tin_directory")
    q = lambda s, *a: con.execute(s, list(a)).fetchall()  # noqa: E731
    in_state = f"tin_value IN (SELECT tin_value FROM td WHERE list_contains(states, '{st}'))"

    # ---- 1. Benchmark (therapy_only False, state) --------------------------
    B = J("benchmark_nt")
    subj = {c: r for c, r in q(f"SELECT billing_code, rate FROM pr WHERE tin_value = ?", tin)}
    peers = {}
    for c, t, r in q(f"SELECT billing_code, tin_value, rate FROM pr WHERE {in_state} AND tin_value <> ?", tin):
        peers.setdefault(c, []).append(r)
    exact = []
    for r in B["rows"][:]:
        c = r["billing_code"]
        chk(f"benchmark {c} subject_rate", r["subject_rate"], half_up(subj[c], 2))
        p = peers.get(c, [])
        chk(f"benchmark {c} n_peers", r["n_peers"], len(p), 0)
        chk(f"benchmark {c} p50", r["p50"], half_up(qcont(p, .5), 2))
        chk(f"benchmark {c} p25", r["p25"], half_up(qcont(p, .25), 2))
        chk(f"benchmark {c} p75", r["p75"], half_up(qcont(p, .75), 2))
        mr = midrank(subj[c], p)
        chk(f"benchmark {c} percentile_exact", r["subject_percentile_exact"], mr, 1e-9)
        ranks = not (min(half_up(x, 2) for x in p) == half_up(subj[c], 2) == max(half_up(x, 2) for x in p))
        chk(f"benchmark {c} in_position", int(r["in_position"]), int(ranks), 0)
        if ranks:
            exact.append(mr)
    overall = half_up(statistics.median(exact)) if exact else 50
    print("independent overall position:", overall, "from", len(exact), "ranking codes")
    chk("quality/size position (overall)", J("quality_size")["subject"]["position"], overall, 0)
    chk("dossier median_percentile", J("dossier_json")["facts"]["median_percentile"], overall, 0)
    # within-payer average (quality/size avg_percentile): mid-rank within (payer, code) among the state's practices
    pcts = []
    rows_pm = q(f"SELECT payer, billing_code, tin_value, rate FROM pm WHERE {in_state}")
    by = {}
    for pay, c, t, r in rows_pm:
        by.setdefault((pay, c), {})[t] = r
    for (pay, c), d in by.items():
        if tin in d:
            others = [v for t, v in d.items() if t != tin]
            if len(others) >= 1 and len({half_up(v, 2) for v in d.values()}) >= 2:
                pcts.append(midrank(d[tin], others))
    print("within-payer positions n", len(pcts), "avg", half_up(statistics.mean(pcts), 1) if pcts else None,
          "api avg_percentile", J("quality_size")["subject"]["avg_percentile"])

    # ---- 2. Fee schedule + scorecard + negotiate compare -------------------
    F = J("schedule_fee_nodisc")["fee_schedule"]
    pm_subj = {(p, c): r for p, c, r in q("SELECT payer, billing_code, rate FROM pm WHERE tin_value = ?", tin)}
    for e in F["codes"]:
        c = e["billing_code"]
        for p, v in e["rates"].items():
            chk(f"fee {p} {c} rate", v["rate"], half_up(pm_subj[(p, c)], 2))
    # market_median per (payer, code): median of OTHER state practices' pm rates (>= 3 peers)
    for e in F["codes"][:12]:
        c = e["billing_code"]
        for p, v in e["rates"].items():
            others = [r for t, r in by.get((p, c), {}).items() if t != tin]
            exp = half_up(statistics.median(others), 2) if len(others) >= 3 else None
            chk(f"fee {p} {c} market_median", v["market_median"], exp)
            chk(f"fee {p} {c} n_peers", v["n_peers"], len(others), 0)
    N = J("negotiate_compare")
    for r in N["rows"][:12]:
        c = r["billing_code"]
        chk(f"negotiate {payer} {c} subject_rate", r["subject_rate"], half_up(pm_subj[(payer, c)], 2))
        others = [v for t, v in by.get((payer, c), {}).items() if t != tin]
        chk(f"negotiate {payer} {c} p50", r["p50"], half_up(qcont(others, .5), 2))
        chk(f"negotiate {payer} {c} pct_exact", r["subject_percentile_exact"], midrank(pm_subj[(payer, c)], others), 1e-9)
    P = J("negotiate_proposal")
    for r in P["rows"][:12]:
        c = r["billing_code"]
        others = [v for t, v in by.get((payer, c), {}).items() if t != tin]
        p50 = half_up(qcont(others, .5), 2)
        chk(f"proposal {c} proposed=max(current,p50)", r["proposed"], max(half_up(pm_subj[(payer, c)], 2), p50))
    O = J("opportunity")["opportunity"]
    for r in O["rows"]:
        c = r["billing_code"]
        p = peers.get(c, [])
        exp = max(0.0, half_up(qcont(p, .5), 2) - half_up(subj[c], 2)) * r["annual_units"]
        chk(f"opportunity {c} at target", r["opportunity_at_target"], half_up(exp, 2), 0.011)

    # ---- 3. Summary strip by_code (TIN grain, latest, dollar; all states) ----
    Sm = J("summary_tin")
    pr_all = {}
    for c, t, r in q("SELECT billing_code, tin_value, rate FROM pr"):
        pr_all.setdefault(c, []).append(r)
    for e in Sm["by_code"][:10]:
        c = e["billing_code"]
        chk(f"summary by_code {c} median", e["median"], half_up(qcont(pr_all[c], .5), 2))
        chk(f"summary by_code {c} entities", e["entities"], len(pr_all[c]), 0)
    # ---- 4. Code page payer_rank (professional, all states) ----------------
    C = J("code_97110")
    by_all = {}
    for pay, c, t, r in q("SELECT payer, billing_code, tin_value, rate FROM pm WHERE billing_code = '97110'"):
        by_all.setdefault(pay, []).append(r)
    for pr_ in C["payer_rank"]:
        vals = by_all[pr_["payer"]]
        chk(f"code 97110 payer_rank {pr_['payer']} median", pr_["median_rate"], qcont(vals, .5), 1e-6)
        chk(f"code 97110 payer_rank {pr_['payer']} n", pr_["n_entities"], len(vals), 0)
    # ---- 5. Overview -------------------------------------------------------
    Ov = J("overview")
    for e in Ov["top_codes"][:5]:
        c = e["billing_code"]
        chk(f"overview top_codes {c} median", e["median"], half_up(qcont(pr_all[c], .5), 2))
        chk(f"overview top_codes {c} practices", e["practices"], len(pr_all[c]), 0)
    for e in Ov["payer_index"]:
        pay = e["payer"]
        idx = []
        rows = q("SELECT billing_code, median(rate), count(*) FROM pm WHERE payer = ? GROUP BY 1 HAVING count(*) >= 3", pay)
        for c, pmed, n in rows:
            idx.append(pmed / qcont(pr_all[c], .5))
        chk(f"overview payer_index {pay}", e["index"], half_up(statistics.median(idx), 3), 0.0011)
        chk(f"overview payer_index {pay} codes", e["codes"], len(idx), 0)
    # ---- 6. Markets geography / negotiability / concentration / city -------
    G = J("market_geography")
    chk("geography national_median 97110", G["national_median"], half_up(qcont(pr_all["97110"], .5), 2))
    chk("geography national_practices", G["national_practices"], len(pr_all["97110"]), 0)
    for s_ in G["states"][:4]:
        vals = [r for (t, r) in q(f"SELECT tin_value, rate FROM pr WHERE billing_code='97110' AND tin_value IN (SELECT tin_value FROM td WHERE list_contains(states, ?))", s_["state"])]
        chk(f"geography {s_['state']} median", s_["median_rate"], half_up(qcont(vals, .5), 2))
        chk(f"geography {s_['state']} n", s_["n_practices"], len(vals), 0)
    Ng = J("market_negotiability")
    for pr_ in Ng["payers"]:
        vals = [r for (t, r) in q(f"SELECT tin_value, rate FROM pm WHERE billing_code='97110' AND payer=? AND {in_state}", pr_["payer"])]
        chk(f"negotiability {pr_['payer']} p50", pr_["p50"], half_up(qcont(vals, .5), 2))
        chk(f"negotiability {pr_['payer']} p90", pr_["p90"], half_up(qcont(vals, .9), 2))
        chk(f"negotiability {pr_['payer']} n", pr_["n_practices"], len(vals), 0)
    Cc = J("territory_concentration")
    tot = 0
    exp = {}
    for pay, n in q(f"SELECT payer, count(DISTINCT tin_value) FROM pm WHERE {in_state} GROUP BY 1"):
        exp[pay] = n
        tot += n
    for pr_ in Cc["payers"]:
        chk(f"concentration {pr_['payer']} n_practices", pr_["n_practices"], exp[pr_["payer"]], 0)
        chk(f"concentration {pr_['payer']} share", pr_["share_pct"], half_up(100 * exp[pr_["payer"]] / tot, 1))
    chk("concentration hhi", Cc["hhi"], half_up(sum((100 * n / tot) ** 2 for n in exp.values()), 0), 0.6)
    T = J("territory_local")
    chk("local state_median", T["state_median"], half_up(qcont([r for (t, r) in q(f"SELECT tin_value, rate FROM pr WHERE billing_code='97110' AND {in_state}")], .5), 2))
    for cty in T["cities"][:3]:
        vals = [r for (t, r) in q(f"""SELECT p.tin_value, p.rate FROM pr p WHERE p.billing_code='97110' AND {in_state.replace('tin_value', 'p.tin_value')}
            AND p.tin_value IN (SELECT DISTINCT r.tin_value FROM rates r JOIN npi_directory n ON n.npi = r.npi WHERE n.state = ? AND upper(trim(n.city)) = ?)""", st, cty["city"])]
        chk(f"city {cty['city']} median", cty["median_rate"], half_up(qcont(vals, .5), 2))
        chk(f"city {cty['city']} n", cty["n_practices"], len(vals), 0)
    # ---- 7. Leaderboard / roster / outreach positions ---------------------
    Lb = J("leaderboard")
    # position per practice: median over codes (>=2 practices, >=2 distinct rates) of mid-rank among others; p50 if none
    pos = {}
    codes_state = {}
    for c, t, r in q(f"SELECT billing_code, tin_value, rate FROM pr WHERE {in_state}"):
        codes_state.setdefault(c, {})[t] = r
    for c, d in codes_state.items():
        if len(d) >= 2 and len({half_up(v, 2) for v in d.values()}) >= 2:
            for t, v in d.items():
                pos.setdefault(t, []).append(midrank(v, [x for tt, x in d.items() if tt != t]))
    for e in Lb["practices"][:6]:
        t = e["tin_value"]
        exp_pos = half_up(statistics.median(pos[t])) if t in pos else 50
        chk(f"leaderboard {t} median_percentile", e["median_percentile"], exp_pos, 0)
        rates_t = [v for c, d in codes_state.items() for tt, v in d.items() if tt == t]
        chk(f"leaderboard {t} median_rate", e["median_rate"], half_up(statistics.median(rates_t), 2))
    R = J("roster")
    me = [o for o in R["organizations"] if o["tin_value"] == tin]
    if me:
        # roster position: within the payer's state market, mid-rank among other practices of that payer
        pp = {}
        for (pay, c), d in by.items():
            if pay == payer and len(d) >= 2 and len({half_up(v, 2) for v in d.values()}) >= 2 and tin in d:
                pp.setdefault(tin, []).append(midrank(d[tin], [x for tt, x in d.items() if tt != tin]))
        chk("roster subject median_percentile", me[0]["median_percentile"], half_up(statistics.median(pp[tin])) if tin in pp else 50, 0)
        chk("roster subject median_rate", me[0]["median_rate"], half_up(statistics.median([v for (p, c), v in pm_subj.items() if p == payer]), 2))
    txt = (out / "outreach_csv.txt").read_text(encoding="utf-8-sig")
    rd = list(csv.DictReader(io.StringIO(txt)))
    mine = [r for r in rd if tin in (r["TINS"] or "")]
    if mine:
        r = mine[0]
        for col in [k for k in r if k.endswith("_RATE")][:4]:
            c = col[1:-5]
            d = codes_state.get(c, {})
            others = [v for tt, v in d.items() if tt != tin]
            chk(f"outreach C{c}_RATE", float(r[col]) if r[col] else None, half_up(subj.get(c), 2) if c in subj else None)
            chk(f"outreach C{c}_MKT_MEDIAN", float(r[f"C{c}_MKT_MEDIAN"]) if r[f"C{c}_MKT_MEDIAN"] else None, half_up(statistics.median(others), 2) if others else None)
            chk(f"outreach C{c}_PCTL", float(r[f"C{c}_PCTL"]) if r[f"C{c}_PCTL"] else None, half_up(midrank(subj[c], others)) if others else None, 0)
    # ---- 8. Visit value in this market (state, PT, ortho preset) -----------
    try:
        V = J("visit_market")
    except Exception:
        V = None
    if V and "rows" in V:
        basket = [("97110", 1), ("97530", 1), ("97140", 1), ("97112", 1)]
        codes = [c for c, _ in basket]
        vals = {}
        # discipline PT filter: lines whose discipline is PT or unspecified — all four codes are PT/OT-shared ('unspecified' or 'PT')
        rows_v = q(f"""WITH {house_ctes()} SELECT h.payer, h.billing_code, h.tin_value, median(h.rate) FROM house h
                       JOIN rates_by_tin s ON s.payer=h.payer AND s.tin_value=h.tin_value AND s.billing_code=h.billing_code AND s.modifier_set=h.mods AND s.service_code_set=h.pos AND s.file_month=h.file_month AND s.billing_class=h.billing_class
                       WHERE h.{in_state} AND h.billing_code IN ('97110','97530','97140','97112') AND (s.discipline='PT' OR s.discipline='unspecified')
                       GROUP BY ALL""")
        for pay, c, t, r in rows_v:
            vals.setdefault((pay, t), {})[c] = r
        per_payer = {}
        for (pay, t), d in vals.items():
            if all(c in d for c in codes):
                per_payer.setdefault(pay, {})[t] = half_up(sum(half_up(1 * half_up(d[c], 2), 2) for c in codes), 2)
        for row in V["rows"]:
            pay = row["payer"]
            d = per_payer.get(pay, {})
            if row["suppressed"]:
                chk(f"visit {pay} suppressed (<5)", 1, int(len(d) < 5), 0)
                continue
            s = sorted(d.values())
            chk(f"visit {pay} practices", row["practices"], len(s), 0)
            chk(f"visit {pay} mean", row["mean"], half_up(statistics.fmean(s), 2))
            chk(f"visit {pay} median", row["median"], half_up(statistics.median(s), 2))
            chk(f"visit {pay} p25", row["p25"], half_up(qcont(s, .25), 2))
            chk(f"visit {pay} p75", row["p75"], half_up(qcont(s, .75), 2))
            if row.get("subject") and tin in d:
                others = [v for t, v in d.items() if t != tin]
                chk(f"visit {pay} subject value", row["subject"]["value"], d[tin])
                chk(f"visit {pay} subject percentile", row["subject"]["percentile"], int(half_up(100 * (sum(1 for x in others if x < d[tin]) + 0.5 * sum(1 for x in others if x == d[tin])) / len(others), 0)), 0)
        allv = {}
        for pay, d in per_payer.items():
            for t, v in d.items():
                allv.setdefault(t, []).append(v)
        allv = {t: half_up(statistics.median(v), 2) for t, v in allv.items()}
        s = sorted(allv.values())
        A = V["all_payers"]
        chk("visit ALL practices", A["practices"], len(s), 0)
        chk("visit ALL median", A["median"], half_up(statistics.median(s), 2))
        chk("visit ALL p25", A["p25"], half_up(qcont(s, .25), 2))
        chk("visit ALL p75", A["p75"], half_up(qcont(s, .75), 2))
        if A.get("subject"):
            print("visit ALL subject:", A["subject"], "| P25", A["p25"], "median", A["median"], "P75", A["p75"])
    print("\nPROBLEMS:", len(PROBLEMS))
    for p in PROBLEMS:
        print("  ", p)


if __name__ == "__main__":
    main()
