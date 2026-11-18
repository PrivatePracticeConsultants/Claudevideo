"""An out-of-state subject (KS TIN) in a MO peer market: Benchmark keeps its rates (PEER_ONLY_KEYS).
Do contract gaps / size premium / changes / dossier / packets agree?"""
from fxm import *  # noqa
import json, re, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    e06 = {"97112": {B: 40, C: 41, D: 42, E: 43, F: 44, G: 45}}   # nobody named ACME prices 97112
    cfg, store = build([("eta_2026-06.json", doc("Eta Health", "2026-06-01", e06, NPIS))])
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    for subj in ("ACME REHAB · KS", S2, "K1 REHAB"):
        b = c.post("/api/benchmark/market", json={"subject": subj, "market": MO}).json()
        print(subj, "BENCH", [(x["billing_code"], x["subject_rate"], x["subject_percentile"]) for x in b["rows"]])
        r = c.post("/api/contract-gaps", json={"subject": subj, "market": MO, "min_peers": 3})
        g = r.json()
        print("   GAPS", r.status_code, g.get("detail"), [(x["billing_code"], x.get("n_peers")) for x in g.get("gaps", [])])
        r = c.post("/api/quality/size", json={"subject": subj, "market": MO})
        print("   SIZE", r.status_code, json.dumps(r.json().get("subject") or r.json())[:260])
        r = c.post("/api/report/dossier.json", json={"subject": subj, "market": MO})
        print("   DOSSIER", r.status_code, json.dumps(r.json().get("facts") or r.json())[:300], (r.json().get("skipped") or [])[:5])
        r = c.post("/api/quality/subject-types", json={"subject": subj, "market": MO})
        print("   TYPES", r.status_code, json.dumps(r.json())[:200])
        r = c.post("/api/leverage", json={"subject": subj, "market": MO})
        print("   LEVERAGE", r.status_code, json.dumps(r.json())[:300])
