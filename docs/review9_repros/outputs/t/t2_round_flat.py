"""(a) one code median printed on several screens (45.125 case);
(b) a flat fee schedule: outreach PCTL vs leaderboard/benchmark position."""
from fx import *
import csv, io

if __name__ == "__main__":
    cfg, store = mk()
    T = {k: f"7{i}0000001" for i, k in enumerate("ABCD", start=1)}
    npis = {t: [f"17{i}0000001"] for i, t in enumerate(T.values(), start=1)}
    # 97110: A 40.25, B 40.25, C 50, D 50 -> market median 45.125 (half-up 45.13)
    # 97140: flat 35 for all four; 97530: flat 50 for all four
    pr = {"97110": {T["A"]: 40.25, T["B"]: 40.25, T["C"]: 50, T["D"]: 50},
          "97140": {t: 35 for t in T.values()},
          "97530": {t: 50 for t in T.values()}}
    ingest(cfg, store, [("rho_2026-06.json", doc("Rho Health", "2026-06-01", pr, npis))])
    for i, (k, t) in enumerate(T.items(), start=1):
        name(store, npis[t][0], f"{k} REHAB", "MO")
    store.rebuild_rollups()
    c = client(cfg, store)
    ov = c.get("/api/overview", params={"state": "MO"}).json()
    print("overview top_codes", [(x["billing_code"], x["median"]) for x in ov["top_codes"]])
    from mrfx.market import medicare_index, geographic_rates
    m = {"month": "latest", "state": "MO"}
    print("markets/medicare market_median", medicare_index(store, "97110", m)["market_median"])
    print("markets/geo national_median", geographic_rates(store, "97110", m)["national_median"])
    from mrfx.benchmark import compute_benchmark
    # an outside subject so every one of A..D is a peer
    b = compute_benchmark(store, "999999999", m)
    print("benchmark p50 (subject outside)", [(r["billing_code"], r["p50"]) for r in b["rows"]])
    r = c.get("/api/export/outreach.csv", params={"state": "MO", "grain": "tin", "latest": "1"})
    print("outreach", r.status_code)
    rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))
    for x in rows:
        print("  ", x["ORG_NAME"], {k: v for k, v in x.items() if k.startswith("C97")})
    from mrfx.leads import compute_leaderboard
    lb = compute_leaderboard(store, {"month": "latest", "state": "MO", "therapy_only": False}, min_codes=1)
    print("leaderboard", [(p["display_name"], p["median_percentile"], p["median_rate"]) for p in lb["practices"]])
    b = compute_benchmark(store, "C REHAB", m)
    print("benchmark C", [(r["billing_code"], r["subject_rate"], r["subject_percentile"]) for r in b["rows"]])
