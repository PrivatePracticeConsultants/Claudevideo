"""Wrong-typed / odd input on every Consulting+Growth POST route: any 5xx, or a body with SQL / a path / a Python repr."""
from fxm import *  # noqa
import json, re, warnings, itertools
warnings.filterwarnings("ignore")
ROUTES = {
 "/api/benchmark/market": ["subject", "market"], "/api/benchmark/opportunity": ["subject", "market", "volumes", "conservative_percentile"],
 "/api/report/pitch": ["subject", "market", "volumes", "conservative_percentile"], "/api/report/negotiation": ["subject", "market", "volumes"],
 "/api/negotiate/compare": ["subject", "payer", "market", "comparables"], "/api/report/payer-compare": ["subject", "payer", "market", "comparables"],
 "/api/negotiate/proposal": ["subject", "payer", "market", "target", "volumes", "comparables"], "/api/report/proposal": ["subject", "payer", "market", "target", "volumes"],
 "/api/schedule/fee": ["subject", "market"], "/api/schedule/fee.csv": ["subject", "market"], "/api/report/ratecard": ["subject", "market"],
 "/api/schedule/visit": ["subject", "market", "lines", "preset"],
 "/api/leads": ["market", "threshold_percentile", "min_codes", "limit", "exclude_subject"], "/api/leads.csv": ["market", "threshold_percentile", "min_codes", "limit"],
 "/api/leaderboard": ["market", "min_codes", "limit", "sort"], "/api/roster": ["payer", "market", "min_codes", "limit", "sort"], "/api/roster.csv": ["payer", "market", "min_codes"],
 "/api/contract-gaps": ["subject", "market", "min_peers", "limit"], "/api/quality/size": ["market", "subject", "min_practices"],
 "/api/quality/types": ["market"], "/api/quality/posture": ["market", "min_codes"], "/api/quality/subject-types": ["market", "subject"],
 "/api/leverage": ["subject", "payer", "market", "radius_miles"], "/api/leverage/summary": ["subject", "market", "radius_miles"],
 "/api/economics/visit": ["subject", "market", "units_per_visit", "year"], "/api/economics/weighted-position": ["subject", "market", "volumes", "year"],
 "/api/report/dossier": ["subject", "market", "radius_miles"], "/api/report/dossier.json": ["subject", "market", "radius_miles"],
 "/api/report/market": ["state", "code", "zip", "radius_miles", "market"], "/api/report/org-bundle.zip": ["subject", "market"],
 "/api/medicare/org": ["subject", "year", "dataset_id", "limit"], "/api/medicare/leaders": ["zip", "radius_miles", "limit", "dataset_id", "therapy_only"],
 "/api/medicare/eligibility": ["text"], "/api/contracts/renewals": ["subjects", "market", "within_days"],
 "/api/contracts/new-to-network": ["market", "zip", "radius_miles", "therapy_only"], "/api/hospital/parity": ["market", "payer", "state", "subject", "limit"],
 "/api/hospital/cash": ["state", "codes", "limit"], "/api/floors/compare": ["market", "subject", "state", "payer"],
 "/api/trajectory": ["market", "assume_inflation_pct"], "/api/inflation": ["values", "index_name", "source"], "/api/market/sizing": ["zip", "radius_miles", "therapy_only"],
 "/api/utilization/volumes": ["subject", "year", "multiplier"], "/api/utilization/growth": ["subject", "from_year", "to_year"],
 "/api/utilization/service-lines": ["subject", "zip", "radius_miles", "year", "min_peers"], "/api/newclinics": ["zip", "radius_miles", "days", "therapy_only", "state"],
 "/api/closures": ["zip", "radius_miles", "days", "state"], "/api/remits/check": ["subject", "text", "market", "basis"],
 "/api/engagements/baseline": ["subject", "market", "label"], "/api/engagements/compare": ["subject", "label", "market", "volumes"],
 "/api/changes": ["market", "subject", "min_pct"], "/api/territory/steal-share": ["subject", "limit", "min_patients", "dataset_id"],
 "/api/packets/build": ["subjects", "month"], "/api/clients": ["subject"],
}
GOOD = {"subject": "ACME REHAB", "market": {"month": "latest", "state": "MO"}, "payer": "Alpha Health", "volumes": {"97110": 100},
        "state": "MO", "code": "97110", "zip": "64105", "text": "1610000001", "target": {"kind": "p50"}, "label": "x"}
BAD = [None, 5, -1, 1e308, "x" * 5000, "'; DROP TABLE rates;--", [], [1, 2], {}, {"a": {"b": [1]}}, True, "\u0000", "NaN", float("1e30"), ["a", None], {"month": 5}, {"state": ["MO"]},
       {"month": "latest", "state": "MO", "payers": "Alpha Health"}, {"month": "2026-13"}, {"month": "latest", "codes": [97110]}, {"month": "latest", "target_percentile": "50"}, {"month": "latest", "target_percentile": 33}, {"kind": "pct_medicare", "value": "abc"}, {"97110": -5}, {"97110": "many"}]
LEAK = re.compile(r"SELECT |FROM rates|duckdb|Traceback|/tmp/|/home/|Binder|Conversion Error|line \d+|<class|object at 0x", re.I)
if __name__ == "__main__":
    cfg, store = build()
    c = client(cfg, store)
    n = 0; bad = []
    for url, keys in ROUTES.items():
        for k in keys:
            for v in BAD:
                body = {x: GOOD[x] for x in keys if x in GOOD}
                body[k] = v
                try:
                    r = c.post(url, json=body)
                except Exception as e:
                    bad.append((url, k, repr(v)[:40], "EXC", repr(e)[:160])); continue
                n += 1
                if r.status_code >= 500 or (r.status_code != 200 and LEAK.search(r.text)):
                    bad.append((url, k, repr(v)[:40], r.status_code, r.text[:200]))
    print(n, "requests;", len(bad), "bad")
    seen = set()
    for b in bad:
        key = (b[0], b[1], b[3], b[4][:60])
        if key in seen: continue
        seen.add(key); print(b)
