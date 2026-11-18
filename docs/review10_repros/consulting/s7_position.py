"""One practice's overall position on every surface that prints one.
9 MO practices, one payer. A: 97110 2nd lowest (1 of 8 others below -> 12.5), 97140 exact middle (-> 50).
variant 'flat' adds 97530 at $50 for everyone.
By hand: median(12.5, 50) = 31.25 -> p31 (Leads rule: all-tied code ranks no one)."""
from fx import *  # noqa
import json, re, sys, warnings
warnings.filterwarnings("ignore")

def run(flat):
    cfg, store = mk()
    names = "ABCDEFGHI"
    T = {k: f"5{i}1000001" for i, k in enumerate(names, start=1)}
    npis = {t: ["1" + t] for t in T.values()}
    r110 = dict(zip(names, [41, 40, 42, 43, 44, 45, 46, 47, 48]))
    r140 = dict(zip(names, [35, 31, 32, 33, 34, 36, 37, 38, 39]))
    prices = {"97110": {T[k]: v for k, v in r110.items()}, "97140": {T[k]: v for k, v in r140.items()}}
    if flat:
        prices["97530"] = {t: 50 for t in T.values()}
    ingest(cfg, store, [("p_2026-06.json", doc("P Health", "2026-06-01", prices, npis))])
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", "MO")
    store.rebuild_rollups()
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    out = {}
    d = c.post("/api/benchmark/market", json={"subject": "A REHAB", "market": MO}).json()
    out["benchmark per-code"] = [(x["billing_code"], x["subject_percentile"]) for x in d["rows"]]
    d = c.post("/api/leads", json={"market": MO, "threshold_percentile": 99, "min_codes": 1}).json()
    out["leads"] = [(x["median_percentile"], x["n_codes"]) for x in d["leads"] if x["display_name"] == "A REHAB"]
    d = c.post("/api/leaderboard", json={"market": MO, "min_codes": 1}).json()
    out["leaderboard"] = [(x["median_percentile"], x["n_codes"]) for x in d["practices"] if x["display_name"] == "A REHAB"]
    d = c.post("/api/roster", json={"market": MO, "payer": "P Health", "min_codes": 1}).json()
    out["payer roster"] = [(x["median_percentile"], x["n_codes"]) for x in d["organizations"] if x["display_name"] == "A REHAB"]
    d = c.post("/api/negotiate/compare", json={"subject": "A REHAB", "payer": "P Health", "market": MO}).json()
    out["negotiate compare headline"] = d["summary"]["headline_percentile"]
    h = c.post("/api/report/negotiation", json={"subject": "A REHAB", "market": MO}).text
    out["negotiation one-pager"] = re.findall(r"You sit at roughly <b>(p\d+)</b>", h)
    d = c.post("/api/report/dossier.json", json={"subject": "A REHAB", "market": MO}).json()
    out["dossier"] = d["facts"].get("median_percentile")
    h = c.post("/api/report/dossier", json={"subject": "A REHAB", "market": MO}).text
    out["dossier html"] = re.findall(r"median position\s+is <b>(p[\d.]+)</b>", h)
    d = c.post("/api/quality/size", json={"subject": "A REHAB", "market": MO}).json()
    out["size premium subject"] = d["subject"]["avg_percentile"]
    r = c.get("/api/export/outreach.csv?grain=tin&dollar_only=1&state=MO")
    import csv, io
    for x in csv.DictReader(io.StringIO(r.text.lstrip("﻿"))):
        if x["ORG_NAME"] == "A REHAB":
            out["outreach PCTL per code"] = {k: v for k, v in x.items() if k.endswith("_PCTL")}
    for k, v in out.items():
        print(f"   {k:28s} {v}")

if __name__ == "__main__":
    print("== no flat code (hand: p31)"); run(False)
    print("== with an all-tied code 97530 (hand, Leads rule: still p31)"); run(True)
