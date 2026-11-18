"""Hand-workable fixture for the Rates/Practice findings (review 10).
Run: PYTHONPATH=/home/user/Claudevideo .venv/bin/python c2_hypo.py
Every expected value is worked in the comments; prints app vs expected."""
import json, sys, re
sys.path.insert(0, "/tmp/mrfx_review10/rates/repros")
from fx import *  # noqa

# 10 MO practices: A1..A5 in ALPHAVILLE, B1..B5 in BETATOWN; K1 in KS
A = [f"61000000{i}" for i in range(1, 6)]
B = [f"62000000{i}" for i in range(1, 6)]
K1 = "710000001"
ALL = A + B + [K1]
NPIS = {t: ["1" + t] for t in ALL}

def build():
    cfg, store = mk()
    # Alpha: 04, 05, 06.  97110
    #   A-practices 47.99 flat, B-practices 48.01 flat  -> state median 48.00, cities -0.02% / +0.02%
    #   except: A1 = 50 (04), 50 (05), 55 (06)   [drawer / changes]
    #           B1 = 40.00 (05) -> 40.01 (06)    [1-cent move]
    #           A2 (06): POS 11 50->55 and POS 02 40->44 on the same code+modifier  [twin rows]
    #           K1 (KS): 70 (05) -> 63 (06)      [subject outside the state]
    def alpha(month):
        d = {t: 47.99 for t in A}
        d.update({t: 48.01 for t in B})
        d[A[0]] = 55 if month == "06" else 50
        d[B[0]] = 40.01 if month == "06" else 40.00
        d[A[1]] = ([price(55, pos=("11",)), price(44, pos=("02",))] if month == "06"
                   else [price(50, pos=("11",)), price(40, pos=("02",))])
        d[K1] = 63 if month == "06" else 70
        return {"97110": d}
    # Beta: 04 and 06 only (skips 05).  A3 47 -> 40 (a 14.9% cut), others flat 47
    def beta(month):
        d = {t: 47 for t in A + B}
        if month == "06":
            d[A[2]] = 40
        return {"97110": d}
    docs = [(f"alpha_2026-{m}.json", doc("Alpha Health", f"2026-{m}-01", alpha(m), NPIS)) for m in ("04", "05", "06")]
    docs += [(f"beta_2026-{m}.json", doc("Beta Health", f"2026-{m}-01", beta(m), NPIS)) for m in ("04", "06")]
    ingest(cfg, store, docs)
    for t in A: name(store, "1" + t, f"A{A.index(t)+1} REHAB", "MO", city="ALPHAVILLE")
    for t in B: name(store, "1" + t, f"B{B.index(t)+1} REHAB", "MO", city="BETATOWN")
    name(store, "1" + K1, "K1 REHAB", "KS", city="KTOWN")
    store.rebuild_rollups()
    return cfg, store

def main():
    cfg, store = build()
    c = client(cfg, store)
    P = lambda u, b: c.post(u, json=b)
    M = {"month": "latest", "state": "MO"}

    print("\n== F: Code page — 'Which payer pays best' vs 'Median by month' (same payer, same month)")
    code = c.get("/api/code/97110?grain=tin").json()
    trend = c.get("/api/trend?cpt=97110&grain=tin").json()["rows"]
    for p in code["payer_rank"]:
        t = [r for r in trend if r["payer"] == p["payer"] and r["file_month"] == "2026-06"][0]
        print(f"   {p['payer']}: payer table median ${p['median_rate']:.2f} over {p['n_entities']} practices | month table 2026-06 ${t['median_rate']:.3f} over {t['entities']}")
    print("   hand (Alpha, 06, one rate per practice): A1 55, A2 median(55,44)=49.5, A3-5 47.99, B1 40.01, B2-5 48.01, K1 63 -> sorted 40.01 47.99x3 48.01x4 49.5 55 63 -> median 48.01")
    print("   hand (raw lines): the same + A2 counted twice (44, 55) -> 12 values, median (48.01+48.01)/2 = 48.01")

    print("\n== F: drawer chart blends months (A1: Alpha 50, 50, 55)")
    d = c.get("/api/entity/tin/" + A[0]).json()
    print("   drawer chart:", [(x["payer"], x["median_rate"]) for x in d["chart"]], " expected Alpha current rate 55.00")
    r = c.get("/api/rates?grain=tin&latest=1&cpt=97110&q=" + A[0]).json()["rows"]
    print("   Explorer latest rows:", [(x["payer"], x["file_month"], x["negotiated_rate"]) for x in r])
    b = P("/api/benchmark/market", {"subject": A[0], "market": {"month": "latest", "state": "MO", "payers": ["Alpha Health"]}}).json()
    print("   Benchmark (Alpha only) subject rate:", [(x["billing_code"], x.get("subject_rate")) for x in b.get("rows", [])])

    print("\n== F: Rate changes, month pinned to 2026-06, 'compare to' = auto")
    ch = P("/api/changes", {"market": {"month": "2026-06"}}).json()
    print("   compared:", ch.get("prev_month"), "->", ch.get("new_month"), "| payers with moves:", sorted({x["payer"] for x in ch["changes"]}),
          "| keys telling who was not compared:", [k for k in ch if "compared" in k])
    print("   expected: Beta's 47 -> 40 cut on A3 (its previous file is 2026-04) is reported, or Beta is named as not compared")
    chl = P("/api/changes", {"market": {"month": "latest"}}).json()
    print("   'latest' mode finds:", [(x["payer"], x["display_name"], x["old_rate"], x["new_rate"]) for x in chl["changes"]])

    print("\n== F: twin rows (same payer, practice, code, modifier; POS differs) and the 1-cent move")
    for x in chl["changes"]:
        if x["display_name"] in ("A2 REHAB", "B1 REHAB"):
            print("   ", {k: x[k] for k in ("display_name", "billing_code", "modifier_set", "old_rate", "new_rate", "pct_change")}, "| POS/class keys:", [k for k in x if "service" in k or "class" in k])
    print("   B1 40.00 -> 40.01 present?", any(x["display_name"] == "B1 REHAB" for x in chl["changes"]), "(expected: 1 increase of $0.01)")
    print("   n_increases:", chl["n_increases"], "n_cuts:", chl["n_cuts"], " hand: increases A1 (50->55), A2 x2, B1 (1 cent) = 4; cuts K1, A3 = 2")

    print("\n== F: Markets 'by state' with a state picked")
    g = P("/api/market/geography", {"code": "97110", "market": M}).json()
    print("   national_median:", g["national_median"], "national_practices:", g["national_practices"], "(the UI prints 'National median $X across N practices')")
    gn = P("/api/market/geography", {"code": "97110", "market": {"month": "latest"}}).json()
    print("   really national:", gn["national_median"], gn["national_practices"])

    print("\n== F: market report city table '-0.0%'")
    h = P("/api/report/market", {"market": M, "state": "MO"}).text
    print("   ", re.findall(r"<td>(Alphaville|Betatown)</td><td class='num'>([^<]*)</td><td class='num'>([^<]*)</td>", h))
    loc = P("/api/territory/local", {"code": "97110", "market": M, "state": "MO"}).json()
    print("   api:", [(x["city"], x["median_rate"], x["vs_state_pct"]) for x in loc["cities"]], "state median", loc["state_median"], "|", loc["headline"])

    print("\n== F: subject outside the state on Rate changes")
    for sub in (K1, "K1 REHAB"):
        r = P("/api/changes", {"market": M, "subject": sub}).json()
        print(f"   subject {sub!r} + state MO -> {r.get('count')} change(s);  without state ->", P("/api/changes", {"market": {"month": "latest"}, "subject": sub}).json().get("count"))
    bk = P("/api/benchmark/market", {"subject": K1, "market": M}).json()
    print("   Benchmark, same subject + state MO, shows its rate:", [(x["billing_code"], x.get("subject_rate")) for x in bk.get("rows", [])])

    print("\n== trajectory hand check (Alpha, national)")
    tr = P("/api/trajectory", {"market": {"month": "latest"}}).json()
    for p in tr["payers"]:
        print("   ", p["payer"], p["cumulative_pct"], "streak", p["cut_streak"], p["panel_codes"], p["panel_practices"], [(s["month"], s["index_pct"]) for s in p["series"]])
    print("   hand Alpha: 11 pairs; ratios A1 1.10, A2 median(55,44)/median(50,40)=49.5/45=1.10, B1 1.00025, K1 0.90, 7 flat -> median 1.0 -> 0.0%; steps 05->06 same -> 0 -> streak 0")
    print("   hand Beta: 10 pairs, one 40/47 -> median 1.0 -> 0.0%")
    tr2 = P("/api/trajectory", {"market": {"month": "2026-05"}}).json()
    print("   pinned 2026-05: months listed:", tr2["months"], "| series months:", [[s["month"] for s in p["series"]] for p in tr2["payers"]], "| payers:", [p["payer"] for p in tr2["payers"]])

if __name__ == "__main__":
    main()
