"""Client digest cache ignores user edits (entity map)."""
from fx import *

if __name__ == "__main__":
    cfg, store = mk()
    T = {k: f"6{i}3000001" for i, k in enumerate("ABCDEFG", start=1)}
    npis = {t: [f"16{i}3000001"] for i, t in enumerate(T.values(), start=1)}
    # A prices 97110 only; B prices 97140 only; five peers price both
    pr = {"97110": {T["A"]: 40, **{T[k]: 50 for k in "CDEFG"}},
          "97140": {T["B"]: 35, **{T[k]: 45 for k in "CDEFG"}}}
    ingest(cfg, store, [("r_2026-06.json", doc("Rho Health", "2026-06-01", pr, npis))])
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", "MO", tax=PT)
    store.rebuild_rollups()
    store.set_entity_map({T["A"]: "ACME GROUP"})
    from mrfx.clients import add_client, client_digest
    add_client(store, "ACME GROUP")
    d1 = client_digest(store)
    print("before:", [(r["subject"], r.get("gaps")) for r in d1["clients"]])
    store.set_entity_map({T["A"]: "ACME GROUP", T["B"]: "ACME GROUP"})   # B joins the group
    d2 = client_digest(store)
    print("after map edit:", [(r["subject"], r.get("gaps")) for r in d2["clients"]])
    from mrfx.benchmark import contract_gaps
    print("Benchmark-tab contract gaps now:", [g["billing_code"] for g in contract_gaps(store, "ACME GROUP", {"month": "latest", "therapy_only": True})["gaps"]])
