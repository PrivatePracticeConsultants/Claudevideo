"""Mapped entity BIG GROUP (M1 44/46, M2 52) across surfaces."""
from fxm import *  # noqa
import json, re, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = build()
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    for subj in ("BIG GROUP", "big group", "NORTH CLINIC", M1, "BIG"):
        d = c.post("/api/benchmark/market", json={"subject": subj, "market": MO}).json()
        print(subj, d.get("subject_tins"), [(x["billing_code"], x["subject_rate"], x["p50"], x["subject_percentile"], x["n_peers"]) for x in d.get("rows", [])][:1], d.get("detail"))
        d = c.post("/api/schedule/fee", json={"subject": subj, "market": MO}).json()
        print("   card", [(x["billing_code"], {p: (v["rate"], v["market_median"], v["n_peers"]) for p, v in x["rates"].items()}) for x in d.get("fee_schedule", {}).get("codes", [])])
    d = c.post("/api/negotiate/compare", json={"subject": "ACME REHAB", "payer": "Alpha Health", "market": MO,
                                              "comparables": ["BIG GROUP", "B REHAB", "ACME REHAB · KS", "K1 REHAB", "TWO STATE PT"]}).json()
    for r in d["rows"]:
        print("compare", r["billing_code"], "subj", r["subject_rate"], "pct", r["subject_percentile"], "p50", r["p50"], "others", r["other_payers_rate"], r["n_other_payers"], "comps", r["comp_rates"], r["best_comparable"])
    print(json.dumps(d["comparables"]))
    r = c.get("/api/rates?grain=entity&dollar_only=1&cpt=97110&month=2026-06&page_size=200&q=big")
    for x in r.json()["rows"]:
        print("  explorer", x.get("display_name"), x.get("unit_id"), x.get("payer"), x.get("negotiated_rate"), x.get("rate_min"), x.get("rate_max"), x.get("tin_count"))
