"""Non-default basis flags: do Benchmark, Negotiate compare, the rate card, leads/roster agree on S1's Alpha 97110 rate?
S1 Alpha 97110 lines: GP $40 (base), GP+CQ $34, GP+KX $41, institutional $100, percentage 80, $0.01 placeholder.
hand: default 40 | include_assistant median(34,40)=37 | base_only false median(34,40,41)=40 | institutional 100"""
from fxm import *  # noqa
import json, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = build()
    c = client(cfg, store)
    base = {"month": "latest", "state": "MO"}
    for label, extra in (("default", {}), ("include_assistant", {"include_assistant": True}), ("base_only false", {"base_only": False}),
                         ("institutional", {"billing_class": "institutional"}), ("include_non_dollar", {"include_non_dollar": True}),
                         ("city Town", {"city": "Town"}), ("city NOPE", {"city": "Nowhere"}), ("therapy_only", {"therapy_only": True}),
                         ("codes [97140]", {"codes": ["97140"]}), ("month 2026-04", {"month": "2026-04"}), ("month 2026-05", {"month": "2026-05"}),
                         ("discipline PT", {"discipline": "PT"}), ("pos 11", {"pos": "11"}), ("target 75", {"target_percentile": 75})):
        m = {**base, **extra}
        out = {}
        r = c.post("/api/benchmark/market", json={"subject": S1, "market": {**m, "payers": ["Alpha Health"]}})
        out["bench[Alpha]"] = [(x["billing_code"], x["subject_rate"], x["subject_percentile"], x["n_peers"]) for x in r.json().get("rows", [])] if r.status_code == 200 else r.json()["detail"][:60]
        r = c.post("/api/negotiate/compare", json={"subject": S1, "payer": "Alpha Health", "market": m})
        out["compare"] = [(x["billing_code"], x["subject_rate"], x["subject_percentile"], x["n_peers"]) for x in r.json().get("rows", [])] if r.status_code == 200 else r.json()["detail"][:60]
        r = c.post("/api/schedule/fee", json={"subject": S1, "market": m})
        out["card"] = [(x["billing_code"], x["rates"].get("Alpha Health", {}).get("rate"), x["rates"].get("Alpha Health", {}).get("n_peers")) for x in r.json()["fee_schedule"]["codes"]] if r.status_code == 200 else r.json()["detail"][:60]
        r = c.post("/api/roster", json={"payer": "Alpha Health", "market": m, "min_codes": 1})
        out["roster"] = [(x["median_rate"], x["median_percentile"], x["n_codes"]) for x in r.json().get("organizations", []) if x["tin_value"] == S1] if r.status_code == 200 else r.json()["detail"][:60]
        r = c.post("/api/benchmark/market", json={"subject": S1, "market": m})
        out["bench[all payers]"] = [(x["billing_code"], x["subject_rate"], x["subject_percentile"], x["n_peers"]) for x in r.json().get("rows", [])] if r.status_code == 200 else r.json()["detail"][:60]
        r = c.post("/api/leads", json={"market": m, "threshold_percentile": 99, "min_codes": 1})
        out["leads"] = [(x["median_percentile"], x["n_codes"], x["avg_gap_to_median"]) for x in r.json().get("leads", []) if x["tin_value"] == S1] if r.status_code == 200 else r.json()["detail"][:60]
        print("==", label)
        for k, v in out.items():
            print(f"     {k:18s} {v}")
