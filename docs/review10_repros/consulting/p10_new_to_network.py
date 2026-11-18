"""'New to network — just-signed practices' vs a payer's PARTIAL middle month.
Alpha publishes A..E in April ($40..), only C and D in May (one shard loaded), A..E again in June.
By hand: nobody signed anything new in June — A, B, E were in the April book. Expected rows: 0 (or a caveat)."""
from fx import *  # noqa
import warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = mk()
    T = {k: f"5{i}1000001" for i, k in enumerate("ABCDE", start=1)}
    npis = {t: ["1" + t] for t in T.values()}
    full = {"97110": {T[k]: 40 + i for i, k in enumerate("ABCDE")}}
    part = {"97110": {T["C"]: 42, T["D"]: 43}}
    ingest(cfg, store, [("a_2026-04.json", doc("Alpha Health", "2026-04-01", full, npis)),
                        ("a_2026-05.json", doc("Alpha Health", "2026-05-01", part, npis)),
                        ("a_2026-06.json", doc("Alpha Health", "2026-06-01", full, npis))])
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", "MO", tax=PT)
    store.rebuild_rollups()
    c = client(cfg, store)
    d = c.post("/api/contracts/new-to-network", json={"market": {"month": "latest", "state": "MO"}}).json()
    print("compared:", d.get("compared"), "| rows:", [(r["practice"], r["prev_month"], r["month"], r["median_rate"]) for r in d["rows"]])
    print("note:", d["note"][:330])
