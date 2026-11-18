"""The same 'market median' and 'gap to median' for one practice on Benchmark vs Leads vs outreach (p8's store)."""
import p8_worst_payer  # noqa (fixture code reused below)
from fx import *  # noqa
import csv, io, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = mk()
    names = "ABCDEF"
    T = {k: f"5{i}1000001" for i, k in enumerate(names, start=1)}
    npis = {t: ["1" + t] for t in T.values()}
    alpha = {"97110": {T[k]: v for k, v in zip(names, [40, 46, 47, 48, 49, 50])},
             "97140": {T[k]: v for k, v in zip(names, [30, 31, 32, 33, 34, 35])}}
    ingest(cfg, store, [("a_2026-06.json", doc("Alpha Health", "2026-06-01", alpha, npis))])
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", "MO")
    store.rebuild_rollups()
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    d = c.post("/api/benchmark/market", json={"subject": "A REHAB", "market": MO}).json()
    print("hand: 97110 A=40, the other five 46..50 -> median 48, gap 8.00 ; 97140 A=30, others 31..35 -> median 33, gap 3.00 ; average gap 5.50")
    print("Benchmark :", [(r["billing_code"], r["subject_rate"], "median", r["p50"], "gap", r["gap_to_target"]) for r in d["rows"]])
    d = c.post("/api/leads", json={"market": MO, "threshold_percentile": 99, "min_codes": 1}).json()
    print("Leads     : avg_gap_to_median", [x["avg_gap_to_median"] for x in d["leads"] if x["display_name"] == "A REHAB"], "worst_payer_gap", [x["worst_payer_gap"] for x in d["leads"] if x["display_name"] == "A REHAB"])
    r = c.get("/api/export/outreach.csv?grain=tin&dollar_only=1&state=MO")
    for x in csv.DictReader(io.StringIO(r.text.lstrip("﻿"))):
        if x["ORG_NAME"] == "A REHAB":
            print("Outreach  :", {k: v for k, v in x.items() if k.startswith("C971")})
