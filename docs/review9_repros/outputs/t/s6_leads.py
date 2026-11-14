"""Leads / leaderboard / roster / size premium / visit economics on the messy fixture (MO)."""
from fxm import *  # noqa
import json, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = build()
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    d = c.post("/api/leads", json={"market": MO, "threshold_percentile": 99, "min_codes": 1}).json()
    print("LEADS", d.get("detail"), d.get("count"))
    for x in d.get("leads", []):
        print("   ", x["display_name"].ljust(14), x["tin_value"], "n_codes", x["n_codes"], "pct", x["median_percentile"], "gap", x["avg_gap_to_median"], "worst", x["worst_payer"], x["worst_payer_gap"], x["state"], x["states"])
    for sort in ("paid", "size", "footprint"):
        d = c.post("/api/leaderboard", json={"market": MO, "min_codes": 1, "sort": sort}).json()
        print("LEADERBOARD", sort, d.get("detail"), [k for k in d if k not in ("rows", "note")])
        for x in d.get("rows", d.get("leaders", [])):
            print("   ", json.dumps(x)[:230])
    d = c.post("/api/roster", json={"market": MO, "payer": "Alpha Health", "min_codes": 1}).json()
    print("ROSTER Alpha", {k: v for k, v in d.items() if k not in ("rows", "note")})
    for x in d.get("rows", []):
        print("   ", json.dumps(x)[:260])
    for subj in ("ACME REHAB", "BIG GROUP", "ACME"):
        d = c.post("/api/quality/size", json={"market": MO, "subject": subj}).json()
        print("SIZE", subj, json.dumps(d.get("subject")), "|", [(b["band"], b["n_practices"], b["avg_percentile"]) for b in d.get("bands", [])], d.get("detail"))
    for subj in ("ACME REHAB", "ACME REHAB · MO", S1, "ACME"):
        for body in ({"market": MO}, {"market": MO, "min_peers": 1}):
            d = c.post("/api/contract-gaps", json={"subject": subj, **body}).json()
            print("GAPS", subj, body.get("min_peers"), json.dumps(d)[:420])
