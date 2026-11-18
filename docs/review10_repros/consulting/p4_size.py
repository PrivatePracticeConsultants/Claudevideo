"""Size premium's subject position vs the ONE overall position.
9 MO practices, ONE payer, THREE ranked codes. A is lowest on 97110 (p0), 2nd-lowest on 97140 (12.5), top on 97530 (100).
Hand: per-code mid-ranks 0, 12.5, 100 -> overall position = median = 12.5 -> p13 (half-up).  Mean would be 37.5."""
from fx import *  # noqa
import json, re, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = mk()
    names = "ABCDEFGHI"
    T = {k: f"5{i}1000001" for i, k in enumerate(names, start=1)}
    npis = {t: ["1" + t] for t in T.values()}
    prices = {"97110": {T[k]: v for k, v in zip(names, [40, 41, 42, 43, 44, 45, 46, 47, 48])},
              "97140": {T[k]: v for k, v in zip(names, [31, 30, 32, 33, 34, 36, 37, 38, 39])},
              "97530": {T[k]: v for k, v in zip(names, [59, 50, 51, 52, 53, 54, 55, 56, 57])}}
    ingest(cfg, store, [("p_2026-06.json", doc("P Health", "2026-06-01", prices, npis))])
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", "MO")
    store.rebuild_rollups()
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    S = {"subject": "A REHAB", "market": MO}
    d = c.post("/api/benchmark/market", json=S).json()
    print("benchmark per-code", [(x["billing_code"], x["subject_percentile_exact"]) for x in d["rows"]])
    d = c.post("/api/leads", json={"market": MO, "threshold_percentile": 99, "min_codes": 1}).json()
    print("leads      ", [x["median_percentile"] for x in d["leads"] if x["display_name"] == "A REHAB"])
    d = c.post("/api/leaderboard", json={"market": MO, "min_codes": 1}).json()
    print("leaderboard", [x["median_percentile"] for x in d["practices"] if x["display_name"] == "A REHAB"])
    d = c.post("/api/roster", json={"market": MO, "payer": "P Health"}).json()
    print("roster     ", [x["median_percentile"] for x in d["organizations"] if x["display_name"] == "A REHAB"])
    d = c.post("/api/negotiate/compare", json={**S, "payer": "P Health"}).json()
    print("compare    ", d["summary"]["headline_percentile"])
    d = c.post("/api/report/dossier.json", json=S).json()
    print("dossier    ", d["facts"].get("median_percentile"))
    d = c.post("/api/economics/weighted-position", json={**S, "volumes": {"97110": 1, "97140": 1, "97530": 1}}).json()
    print("weighted   ", {k: d.get(k) for k in ("median_percentile", "unweighted_percentile", "weighted_percentile")})
    d = c.post("/api/quality/size", json=S).json()
    print("SIZE       ", d["subject"])
    print("SIZE headline:", d["headline"])
