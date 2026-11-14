"""Hand-worked visit-market store (review 13 of the round-12 diff, item 1).

MO / PT / ortho basket (97110 + 97530 + 97140 + 97112, one unit each -> value
= sum of the four rates). TINs T1..T11; every rate below is on all four codes
unless said otherwise.

Payer A, May file (bare = no modifier -> 'unspecified' discipline):
  T1 bare 10 (superseded by June GP 20), T2 25, T3 30, T4 35, T5 40, T6 45,
  T7 50 (TIN with one NPI in MO and one in KS), T8 60 under tin type 'npi'
  (NPI in the TIN slot -> excluded), T9 55 but 97140 as a PERCENTAGE row
  (incomplete), T10 70 (KS only), T11 65 on GO lines only (OT -> excluded).
Payer A, June file: T1 GP 20.
  latest A (PT, MO): T1 80, T2 100, T3 120, T4 140, T5 160, T6 180, T7 200
    n 7, mean 140, median 140, P25 110 (pos 1.5), P75 170 (pos 4.5); incomplete 1 (T9)
  pinned 2026-05: T1 40 instead -> 40 100 120 140 160 180 200
    n 7, mean 134.29, median 140, P25 110, P75 170
Payer B, June: T1 30, T2 30 (97140 missing), T3 35, T4 40, T5 50, T6 50, T7 45
  complete: T1 120, T3 140, T4 160, T5 200, T6 200, T7 180
    n 6, mean 166.67, median 170, P25 145 (pos 1.25), P75 195 (pos 3.75); incomplete 1 (T2)
Payer C, June: T1 10, T2 10 -> suppressed (2 < 5)
All payers (median of each TIN's complete payer values; C counts):
  T1 med(80,120,40)=80, T2 med(100,40)=70, T3 130, T4 150, T5 180, T6 190, T7 190
    sorted 70 80 130 150 180 190 190: n 7, mean 141.43, median 150, P25 105, P75 185
Entity map: DUO REHAB = T5 + T6; SPAN REHAB = T4 + T10 (KS).
"""
import copy
import csv
import io
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/home/user/Claudevideo")

ROOT = Path(__file__).parent / "scratch_vm1"
ORTHO = ("97110", "97530", "97140", "97112")
TIN = {f"T{i}": f"{73000000 + i:09d}" for i in range(1, 12)}
NPI = {k: f"18{v[1:]}" for k, v in TIN.items()}            # 10 digits
NPI_KS = "1899999997"                                       # T7's second NPI, in KS


def doc(payer, day, lines):
    """lines: (tinkey, rate, mods, ntype, tin_type, codes)"""
    items = []
    for code in ORTHO:
        groups = []
        for k, rate, mods, ntype, tin_type, codes in lines:
            if code not in codes:
                continue
            npis = [NPI[k]] + ([NPI_KS] if k == "T7" else [])
            tin = {"type": tin_type, "value": NPI[k] if tin_type == "npi" else TIN[k]}
            groups.append({"provider_groups": [{"npi": npis, "tin": tin}],
                           "negotiated_prices": [{"negotiated_type": ntype,
                                                  "negotiated_rate": float(rate),
                                                  "billing_class": "professional",
                                                  "service_code": ["11"],
                                                  "billing_code_modifier": list(mods),
                                                  "expiration_date": "9999-12-31"}]})
        if groups:
            items.append({"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                          "billing_code": code, "negotiated_rates": groups})
    return {"reporting_entity_name": payer, "last_updated_on": day,
            "version": "2.0.0", "in_network": items}


def L(k, rate, mods=("GP",), ntype="negotiated", tin_type="ein", codes=ORTHO):
    return (k, rate, mods, ntype, tin_type, codes)


def build():
    from mrfx.config import MrfxConfig
    from mrfx.ingest import scan_inbox
    from mrfx.store import Store
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    cfg = MrfxConfig(inbox_dir=ROOT / "inbox", processed_dir=ROOT / "processed",
                     failed_dir=ROOT / "failed", store_dir=ROOT / "store",
                     downloads_dir=ROOT / "downloads", entity_map_path=ROOT / "em.yaml",
                     confirm_over_gb=5.0)
    cfg.enrichment.mode = "off"
    cfg.ensure_dirs()
    may_a = [L("T1", 10, mods=()), L("T2", 25), L("T3", 30), L("T4", 35), L("T5", 40),
             L("T6", 45), L("T7", 50), L("T8", 60, tin_type="npi"),
             L("T9", 55, codes=("97110", "97530", "97112")),
             L("T9", 80, ntype="percentage", codes=("97140",)),
             L("T10", 70), L("T11", 65, mods=("GO",))]
    docs = {"a_2026-05.json": doc("Payer A", "2026-05-01", may_a),
            "a_2026-06.json": doc("Payer A", "2026-06-01", [L("T1", 20)]),
            "b_2026-06.json": doc("Payer B", "2026-06-01", [
                L("T1", 30), L("T2", 30, codes=("97110", "97530", "97112")), L("T3", 35),
                L("T4", 40), L("T5", 50), L("T6", 50), L("T7", 45)]),
            "c_2026-06.json": doc("Payer C", "2026-06-01", [L("T1", 10), L("T2", 10)])}
    for name, d in docs.items():
        (cfg.inbox_dir / name).write_text(json.dumps(d))
    store = Store(cfg.store_dir)
    res = scan_inbox(cfg, store)
    assert all(r["status"] == "done" for r in res), res
    states = {k: "MO" for k in TIN}
    states["T10"] = "KS"
    for k, npi in NPI.items():
        store.save_npi(npi, f"{k} REHAB", "225100000X", "x", "Town", states[k],
                       entity_type="NPI-2", zip_code="64105")
    store.save_npi(NPI_KS, "T7 REHAB", "225100000X", "x", "Town", "KS",
                   entity_type="NPI-2", zip_code="66101")
    from mrfx.entities import update_entity
    update_entity(cfg, store, "DUO REHAB", add_tins=[TIN["T5"], TIN["T6"]])
    update_entity(cfg, store, "SPAN REHAB", add_tins=[TIN["T4"], TIN["T10"]])
    store.rebuild_rollups()
    return cfg, store


FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok' if ok else 'FAIL'}] {label}: got {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok:
        FAILS.append(label)


def stats(r):
    return (r["practices"], r["mean"], r["median"], r["p25"], r["p75"],
            r["incomplete_excluded"], r["suppressed"])


def pos(s):
    return (s["value"], s["percentile"], s["ranked_among"], s["quartile"],
            s["vs_mean"], s["vs_mean_pct"], s["vs_median"], s["vs_median_pct"])


def rows_of(d):
    return {r["payer"]: r for r in d["rows"] + [d["all_payers"]]}


def main():
    from fastapi.testclient import TestClient

    import mrfx.api as api
    from mrfx import market as mk
    from mrfx.api import create_app
    from mrfx.visit_market import MIN_PRACTICES, _position, market_visit_value, to_csv
    api.TRUSTED_HOSTS.add("testserver")
    cfg, store = build()
    c = TestClient(create_app(cfg, store), base_url="http://localhost")
    M = {"state": "MO", "discipline": "PT", "month": "latest"}
    print("directory:", sorted(store_q(store, "SELECT tin_value, states, primary_state FROM tin_directory ORDER BY tin_value")))
    print("spine disciplines:", store_q(store, "SELECT discipline, count(*) FROM rates_by_tin GROUP BY 1 ORDER BY 1"))
    print("npi-slot rows:", store_q(store, "SELECT count(*) FROM rates_by_tin WHERE tin_is_really_npi"))
    check("MIN_PRACTICES == market._MIN_CELL", MIN_PRACTICES, mk._MIN_CELL)

    print("\n== population (no subject) ==")
    d0 = c.post("/api/schedule/visit/market", json={"market": M, "preset": "ortho"})
    assert d0.status_code == 200, d0.text
    d0 = d0.json()
    R = rows_of(d0)
    check("row order", list(R), ["Payer A", "Payer B", "Payer C", "All payers"])
    check("A", stats(R["Payer A"]), (7, 140.0, 140.0, 110.0, 170.0, 1, False))
    check("B", stats(R["Payer B"]), (6, 166.67, 170.0, 145.0, 195.0, 1, False))
    check("C suppressed", stats(R["Payer C"]), (None, None, None, None, None, 0, True))
    check("All", stats(R["All payers"]), (7, 141.43, 150.0, 105.0, 185.0, None, False))
    check("incomplete total", d0["incomplete_excluded"], 2)

    print("\n== subject T3 (one TIN) ==")
    d3 = c.post("/api/schedule/visit/market", json={"market": M, "preset": "ortho", "subject": TIN["T3"]}).json()
    R3 = rows_of(d3)
    check("T3 A", pos(R3["Payer A"]["subject"]), (120.0, 33, 7, "Q2", -20.0, -14.3, -20.0, -14.3))
    check("T3 B", pos(R3["Payer B"]["subject"]), (140.0, 20, 6, "Q1", -26.67, -16.0, -30.0, -17.6))
    check("T3 C", R3["Payer C"]["subject"], None)
    check("T3 All", pos(R3["All payers"]["subject"]), (130.0, 33, 7, "Q2", -11.43, -8.1, -20.0, -13.3))
    check("market unchanged with subject", {k: stats(v) for k, v in R3.items()}, {k: stats(v) for k, v in R.items()})

    print("\n== subject T4 (value == A median, == All P25? no: 150) ==")
    d4 = c.post("/api/schedule/visit/market", json={"market": M, "preset": "ortho", "subject": TIN["T4"]}).json()
    R4 = rows_of(d4)
    # A: v 140 == median 140 -> Q3 by the stated rule; others 80 100 120 160 180 200 -> 3/6 = p50
    check("T4 A (v == median)", pos(R4["Payer A"]["subject"]), (140.0, 50, 7, "Q3", 0.0, 0.0, 0.0, 0.0))

    print("\n== subject DUO REHAB (T5 + T6, both in MO) ==")
    dd = c.post("/api/schedule/visit/market", json={"market": M, "preset": "ortho", "subject": "DUO REHAB"}).json()
    RD = rows_of(dd)
    check("DUO tins", dd["subject_tins"], 2)
    # A: rates median(40,45)=42.5 -> 170; others 80 100 120 140 200 -> 4/5 = p80 of 6; p75 170 -> Q4
    check("DUO A", pos(RD["Payer A"]["subject"]), (170.0, 80, 6, "Q4", 30.0, 21.4, 30.0, 21.4))
    # B: 200; others 120 140 160 180 -> p100 of 5; Q4
    check("DUO B", pos(RD["Payer B"]["subject"]), (200.0, 100, 5, "Q4", 33.33, 20.0, 30.0, 17.6))
    # All: med(170,200)=185; others 70 80 130 150 190 -> 4/5 = p80 of 6; p75 185 -> Q4
    check("DUO All", pos(RD["All payers"]["subject"]), (185.0, 80, 6, "Q4", 43.57, 30.8, 35.0, 23.3))
    check("DUO: market practices still count T5 and T6 separately", RD["Payer A"]["practices"], 7)

    print("\n== subject SPAN REHAB (T4 in MO + T10 in KS) ==")
    ds = c.post("/api/schedule/visit/market", json={"market": M, "preset": "ortho", "subject": "SPAN REHAB"}).json()
    RS = rows_of(ds)
    check("SPAN tins", ds["subject_tins"], 2)
    # A: median(35,70)=52.5 -> 210 (the KS TIN is its own); others exclude T4: 80 100 120 160 180 200 -> p100 of 7
    check("SPAN A", pos(RS["Payer A"]["subject"]), (210.0, 100, 7, "Q4", 70.0, 50.0, 70.0, 50.0))
    # B: T4 only -> 160; others 120 140 180 200 200 -> 2/5 = p40 of 6; 145 <= 160 < 170 -> Q2
    check("SPAN B", pos(RS["Payer B"]["subject"]), (160.0, 40, 6, "Q2", -6.67, -4.0, -10.0, -5.9))

    print("\n== subject T10 (KS only; not in the market) ==")
    dk = c.post("/api/schedule/visit/market", json={"market": M, "preset": "ortho", "subject": TIN["T10"]}).json()
    RK = rows_of(dk)
    check("T10 A (ranked among 7 + itself)", pos(RK["Payer A"]["subject"]), (280.0, 100, 8, "Q4", 140.0, 100.0, 140.0, 100.0))
    check("T10 B", RK["Payer B"]["subject"], None)
    check("T10 All", pos(RK["All payers"]["subject"])[:4], (280.0, 100, 8, "Q4"))

    print("\n== payers filter ==")
    dp = c.post("/api/schedule/visit/market", json={"market": {**M, "payers": ["Payer B"]}, "preset": "ortho", "subject": TIN["T3"]}).json()
    RP = rows_of(dp)
    check("payers rows", list(RP), ["Payer B", "All payers"])
    check("payers All == B", stats(RP["All payers"])[:5], (6, 166.67, 170.0, 145.0, 195.0))
    check("payers subject All == B value", RP["All payers"]["subject"]["value"], 140.0)

    print("\n== pinned month 2026-05 ==")
    dm = c.post("/api/schedule/visit/market", json={"market": {**M, "month": "2026-05"}, "preset": "ortho", "subject": TIN["T1"]}).json()
    RM = rows_of(dm)
    check("pinned rows", list(RM), ["Payer A", "All payers"])
    check("pinned A", stats(RM["Payer A"]), (7, 134.29, 140.0, 110.0, 170.0, 1, False))
    check("pinned T1 value (May bare $10)", RM["Payer A"]["subject"]["value"], 40.0)
    check("pinned as_of_rule", dm["as_of_rule"], "only the 2026-05 files")

    print("\n== refusals ==")
    for market, words in (({"discipline": "PT"}, "pick a state"), ({"state": "MO"}, "pick a discipline"),
                          ({"state": "KS", "discipline": "PT"}, "no payer prices"),
                          ({"state": "MO", "discipline": "SLP"}, "no payer prices")):
        r = c.post("/api/schedule/visit/market", json={"market": market, "preset": "ortho"})
        check(f"refusal {market}", (r.status_code, words in r.json().get("detail", "")), (422, True))
    r = c.post("/api/schedule/visit/market", json={"market": M, "preset": "ortho", "lines": [{"code": "99999", "minutes": 15}]})
    print("  custom unknown code ->", r.status_code, r.json().get("detail", "")[:140])

    print("\n== CSV == JSON == HTML (DUO case, negatives + a no-visit payer) ==")
    body = {"market": M, "preset": "ortho", "subject": "DUO REHAB"}
    text = c.post("/api/schedule/visit/market.csv", json=body).text.lstrip("﻿")
    recs = list(csv.DictReader(io.StringIO("\n".join(ln for ln in text.splitlines() if not ln.startswith("#")))))
    page = c.post("/api/report/visitvalue", json=body).text
    table = re.search(r"<table class='vv'>.*?</table>", page, re.S).group(0)
    printed = [[re.sub(r"<[^>]+>", "", x).replace("&amp;", "&") for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
               for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table.split("<tbody>")[1], re.S)]
    for r, rec, p in zip(dd["rows"] + [dd["all_payers"]], recs, printed):
        s = r.get("subject")
        if r["suppressed"]:
            check(f"csv/html suppressed {r['payer']}", (rec["suppressed"], "not shown" in p[1]), ("yes", True))
            if s is None:
                check(f"csv/html no visit {r['payer']}", (rec["subject_value"], p[-5]), ("", "no complete visit"))
            continue
        check(f"csv stats {r['payer']}", (rec["mean"], rec["median"], rec["p25"], rec["p75"], rec["practices"]),
              (f"{r['mean']:.2f}", f"{r['median']:.2f}", f"{r['p25']:.2f}", f"{r['p75']:.2f}", str(r["practices"])))
        check(f"html stats {r['payer']}", (p[2], p[3], p[4], p[5], p[1]),
              (f"${r['mean']:,.2f}", f"${r['median']:,.2f}", f"${r['p25']:,.2f}", f"${r['p75']:,.2f}", f"{r['practices']:,}"))
        if s:
            check(f"csv subject {r['payer']}", (rec["subject_value"], rec["subject_percentile"], rec["quartile"], rec["vs_mean_dollars"], rec["vs_mean_pct"]),
                  (f"{s['value']:.2f}", str(s["percentile"]), s["quartile"], f"{s['vs_mean']:.2f}", f"{s['vs_mean_pct']:.1f}"))
            check(f"html subject {r['payer']}", (p[7], p[8], p[9]), (f"${s['value']:,.2f}", f"p{s['percentile']} of {s['ranked_among']}", s["quartile"]))
    # the T3 case has negative deltas: HTML prints a signed figure
    page3 = c.post("/api/report/visitvalue", json={"market": M, "preset": "ortho", "subject": TIN["T3"]}).text
    check("html negative vs mean", "-$20.00 (-14.3%)" in page3, True)
    print("  html T3 row sample:", re.findall(r"<tr><td>Payer A</td>.*?</tr>", page3, re.S)[0][:400])

    print("\n== determinism & caching ==")
    b1 = c.post("/api/schedule/visit/market", json=body).content
    b2 = c.post("/api/schedule/visit/market", json=body).content
    check("two calls identical", b1 == b2, True)
    store.rebuild_rollups()
    b3 = c.post("/api/schedule/visit/market", json=body).content
    check("identical after rollup regeneration", b1 == b3, True)
    # entity-map edit: T7 joins DUO -> A = median(40,45,50)=45 -> 180; others 80 100 120 140 -> p100 of 5
    from mrfx.entities import update_entity
    update_entity(cfg, store, "DUO REHAB", add_tins=[TIN["T7"]])   # the UI's edit path
    de = c.post("/api/schedule/visit/market", json=body).json()
    check("entity edit invalidates the cache (DUO A)", pos(rows_of(de)["Payer A"]["subject"])[:4], (180.0, 100, 5, "Q4"))
    check("entity edit: subject_tins", de["subject_tins"], 3)
    # a new file changes T3's A rate: 30 -> 50 (July) -> T3 A 200; the per-discipline table must rebuild
    (cfg.inbox_dir / "a_2026-07.json").write_text(json.dumps(doc("Payer A", "2026-07-01", [L("T3", 50)])))
    from mrfx.ingest import scan_inbox
    assert all(r["status"] == "done" for r in scan_inbox(cfg, store))
    store.rebuild_rollups()
    dn = c.post("/api/schedule/visit/market", json={"market": M, "preset": "ortho", "subject": TIN["T3"]}).json()
    check("spine change rebuilds the table (T3 A now 200)", dn["rows"][0]["subject"]["value"], 200.0)
    # A: 80 100 140 160 180 200 200 -> mean 151.43 median 160
    check("spine change: A stats", stats(dn["rows"][0])[:3], (7, 151.43, 160.0))

    print("\n== _position boundaries (pure function) ==")
    st = {"p25": 100.0, "median": 120.0, "p75": 140.0, "_mean": 120.0, "_median": 120.0}
    for v, want in ((99.99, "Q1"), (100.0, "Q2"), (119.99, "Q2"), (120.0, "Q3"), (139.99, "Q3"), (140.0, "Q4")):
        check(f"quartile v={v}", _position(v, [90.0, 110.0, 130.0, 150.0], st)["quartile"], want)
    st2 = {"p25": 120.0, "median": 120.0, "p75": 140.0, "_mean": 120.0, "_median": 120.0}
    check("p25 == median, v == both -> Q3", _position(120.0, [120.0, 120.0, 120.0, 150.0], st2)["quartile"], "Q3")
    p = _position(120.0, [120.0, 120.0, 120.0, 150.0], st2)
    check("ties: 0 below + half of 3 tied over 4 others = 37.5 -> 38", p["percentile"], 38)
    check("no others -> percentile None", _position(5.0, [], st)["percentile"], None)

    print("\nFAILS:", FAILS or "none")
    return 1 if FAILS else 0


def store_q(store, sql):
    with store.connect() as con:
        return con.execute(sql).fetchall()


if __name__ == "__main__":
    sys.exit(main())
