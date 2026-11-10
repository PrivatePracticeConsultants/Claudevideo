"""'Which payer pays this practice worst' on four screens, with a Medicare anchor loaded.
A REHAB: Alpha 97110 $45, 97140 $30, 97530 $44 ; Beta 97110 $42 (the ONLY code both price). Medicare MO: 30 / 28 / 40.
By hand, like-for-like (the rule Leads and per-visit economics state): on 97110 Alpha pays $45, Beta $42 -> Beta is the worse payer.
Rate-card scorecard: median % of Medicare over EACH PAYER'S OWN codes: Alpha median(150, 107, 110) = 110 ; Beta 140 -> names Alpha worst."""
from fx import *  # noqa
import csv, io, json, re, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = mk()
    names = "ABCDEF"
    T = {k: f"5{i}1000001" for i, k in enumerate(names, start=1)}
    npis = {t: ["1" + t] for t in T.values()}
    alpha = {"97110": {T[k]: v for k, v in zip(names, [45, 46, 47, 48, 49, 50])},
             "97140": {T[k]: v for k, v in zip(names, [30, 31, 32, 33, 34, 35])},
             "97530": {T[k]: v for k, v in zip(names, [44, 45, 46, 47, 48, 49])}}
    beta = {"97110": {T[k]: v for k, v in zip(names, [42, 43, 44, 45, 46, 47])}}
    ingest(cfg, store, [("a_2026-06.json", doc("Alpha Health", "2026-06-01", alpha, npis)),
                        ("b_2026-06.json", doc("Beta Health", "2026-06-01", beta, npis))])
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", "MO")
    store.load_mpfs([{"code": "97110", "locality": "REST OF MISSOURI", "non_facility_rate": 30.0},
                     {"code": "97140", "locality": "REST OF MISSOURI", "non_facility_rate": 28.0},
                     {"code": "97530", "locality": "REST OF MISSOURI", "non_facility_rate": 40.0}], "test MPFS", state="MO")
    tmp = Path(cfg.store_dir).parent
    p = tmp / "puf.csv"
    p.write_text("Rndrng_NPI,HCPCS_Cd,Tot_Srvcs,Tot_Benes,Avg_Mdcr_Alowd_Amt,Place_Of_Srvc\n" +
                 f"1{T['A']},97110,3000,40,30,O\n1{T['A']},97140,1000,40,28,O\n1{T['A']},97530,500,40,40,O\n")
    from mrfx.utilization import import_utilization
    import_utilization(store, p, year="2023")
    store.rebuild_rollups()
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}; S = {"subject": "A REHAB", "market": MO}
    d = c.post("/api/schedule/fee", json=S).json()["scorecard"]
    print("rate card scorecard:", [(r["rank"], r["payer"], r["median_pct_medicare"], r["median_rate"]) for r in d["rows"]], "| best", d["best_payer"], "| worst", d["worst_payer"])
    h = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c.post("/api/report/ratecard", json=S).text))
    print("rate card document :", re.findall(r"Best-paying payer: [A-Za-z ]+", h))
    d = c.post("/api/leads", json={"market": MO, "threshold_percentile": 99, "min_codes": 1}).json()
    print("Leads              :", [(x["worst_payer"], x["worst_payer_gap"], x["worst_payer_codes"], x["worst_payer_basis"]) for x in d["leads"] if x["display_name"] == "A REHAB"])
    d = c.post("/api/economics/visit", json=S).json()
    print("per-visit economics: best", d.get("best_payer"), "| worst", d.get("worst_payer"), "|", d.get("headline"))
    d = c.post("/api/report/dossier.json", json=S).json()
    print("dossier 'weakest'  :", d["facts"].get("weakest_payer"))
    r = c.get("/api/export/outreach.csv?grain=tin&dollar_only=1&state=MO")
    for x in csv.DictReader(io.StringIO(r.text.lstrip("﻿"))):
        if x["ORG_NAME"] == "A REHAB":
            print("outreach           :", {k: v for k, v in x.items() if "PAYER" in k or "WORST" in k or "GAP" in k})
