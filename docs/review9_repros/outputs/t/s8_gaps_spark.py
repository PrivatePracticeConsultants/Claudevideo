"""(1) contract gaps vs Benchmark for a same-name multi-state subject;
   (2) sparkline % vs changes monitor % on a half (80.00 -> 81.00 = +1.25 %)."""
from fxm import *  # noqa
import json, re, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    # Eta prices 97112 for the KS ACME TIN (S2) and 6 MO peers, NOT for the MO ACME TIN (S1)
    e06 = {"97112": {S2: 44, B: 40, C: 41, D: 42, E: 43, F: 44, G: 45}}
    # Theta: one contract 80 -> 81 for S1 on 97116
    t05 = {"97116": {S1: 80.00, B: 80, C: 82, D: 84, E: 86, F: 88}}
    t06 = {"97116": {S1: 81.00, B: 80, C: 82, D: 84, E: 86, F: 88}}
    extra = [("eta_2026-06.json", doc("Eta Health", "2026-06-01", e06, NPIS)),
             ("theta_2026-05.json", doc("Theta Health", "2026-05-01", t05, NPIS)),
             ("theta_2026-06.json", doc("Theta Health", "2026-06-01", t06, NPIS))]
    cfg, store = build(extra)
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    for subj in ("ACME REHAB", "ACME REHAB · MO", S1):
        b = c.post("/api/benchmark/market", json={"subject": subj, "market": MO}).json()
        g = c.post("/api/contract-gaps", json={"subject": subj, "market": MO, "min_peers": 5}).json()
        fs = c.post("/api/schedule/fee", json={"subject": subj, "market": MO}).json()["fee_schedule"]
        print(subj, "| benchmark tins", b["subject_tins"], "97112 your rate:", [x["subject_rate"] for x in b["rows"] if x["billing_code"] == "97112"],
              "| rate card has 97112:", any(x["billing_code"] == "97112" for x in fs["codes"]),
              "| contract-gaps tins", g["subject_tins"], "gaps:", [(x["billing_code"], x.get("n_peers"), x.get("market_median")) for x in g["gaps"]])
    # spark vs changes
    ch = c.post("/api/changes", json={"subject": S1, "market": {"month": "2026-06", "prev_month": "2026-05", "payers": ["Theta Health"]}}).json()
    print("changes:", [(x["billing_code"], x["old_rate"], x["new_rate"], x["pct_change"]) for x in ch["changes"]])
    tr = c.post("/api/trajectory", json={"market": {"payers": ["Theta Health"]}}).json()
    print("trajectory series:", [(p["payer"], p["series"]) for p in tr["payers"]])
    for ep in ("/api/report/pitch", "/api/report/ratecard"):
        h = c.post(ep, json={"subject": S1, "market": {"month": "latest", "state": "MO", "payers": ["Theta Health"]}}).text
        print(ep, re.findall(r"<title>([^<]*97116[^<]*)</title>", h), re.findall(r"aria-label=\"([^\"]*)\"", h)[:2])
