"""Multi-state subject: Benchmark/negotiation vs rate card 'your rate'."""
from fx import *

if __name__ == "__main__":
    cfg, store = mk()
    # Subject "ACME REHAB": TIN 610000001 in MO (paid 40), TIN 610000002 in KS (paid 60).
    # Peers MO: B 45, C 50, D 55, E 60  (TINs 62..65)
    tins = {"S1": "610000001", "S2": "610000002", "B": "620000001", "C": "630000001",
            "D": "640000001", "E": "650000001"}
    npis = {tins["S1"]: ["1610000001"], tins["S2"]: ["1610000002"], tins["B"]: ["1620000001"],
            tins["C"]: ["1630000001"], tins["D"]: ["1640000001"], tins["E"]: ["1650000001"]}
    pr = {"97110": {tins["S1"]: 40, tins["S2"]: 60, tins["B"]: 45, tins["C"]: 50,
                    tins["D"]: 55, tins["E"]: 60}}
    ingest(cfg, store, [("rho_2026-06.json", doc("Rho Health", "2026-06-01", pr, npis))])
    name(store, "1610000001", "ACME REHAB", "MO")
    name(store, "1610000002", "ACME REHAB", "KS")
    for k in "BCDE":
        name(store, npis[tins[k]][0], f"{k} REHAB", "MO")
    store.rebuild_rollups()
    from mrfx.benchmark import compute_benchmark, compute_payer_negotiation, resolve_subject_tins
    from mrfx.schedule import compute_fee_schedule
    print("subject tins", resolve_subject_tins(store, "ACME REHAB"))
    m = {"month": "latest", "state": "MO", "therapy_only": False}
    b = compute_benchmark(store, "ACME REHAB", m)
    print("benchmark rows", [(r["billing_code"], r["subject_rate"], r["p50"], r["subject_percentile"]) for r in b["rows"]])
    n = compute_payer_negotiation(store, "ACME REHAB", m)
    print("negotiation", [(s["payer"], [(r["subject_rate"]) for r in s["benchmark"]["rows"]]) for s in n["sections"]])
    fs = compute_fee_schedule(store, "ACME REHAB", m)
    print("rate card", [(c["billing_code"], {p: v["rate"] for p, v in c["rates"].items()}) for c in fs["codes"]])
    c = client(cfg, store)
    r = c.post("/api/benchmark", json={"subject": "ACME REHAB", "market": m})
    print("api benchmark", r.status_code, [(x["billing_code"], x["subject_rate"]) for x in r.json().get("rows", [])])
