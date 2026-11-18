"""practice_base_sql: materialised tables vs live query on a messy fixture."""
from fx import *
import json

if __name__ == "__main__":
    cfg, store = mk()
    T = {k: f"6{i}6000001" for i, k in enumerate("ABCDEFG", start=1)}
    npis = {t: [f"16{i}6000001", f"16{i}6000002"] for i, t in enumerate(T.values(), start=1)}
    inst = dict(price(100)); inst["billing_class"] = "institutional"
    pct = dict(price(150)); pct["negotiated_type"] = "percentage"
    def lines(base):
        return [price(base), price(base * 0.85, mods=("GP", "CQ")), price(base + 9, mods=("GP", "KX")),
                price(base + 3, pos=("22",)), inst, pct, price(0.01)]
    may = {c: {t: lines(30 + 5 * i + j) for i, t in enumerate(T.values())} for j, c in enumerate(("97110", "97140", "97530"))}
    jun = {c: {t: lines(31 + 5 * i + j) for i, t in enumerate(list(T.values())[:5])} for j, c in enumerate(("97110", "97140"))}
    sig = {"97110": {T["A"]: 33, T["B"]: 47, T["F"]: 52}, "97140": {T["A"]: 28}}
    ingest(cfg, store, [("r_2026-05.json", doc("Rho Health", "2026-05-01", may, npis)),
                        ("r_2026-06.json", doc("Rho Health", "2026-06-01", jun, npis)),
                        ("s_2026-06.json", doc("Sigma Health", "2026-06-01", sig, npis))])
    for i, (k, t) in enumerate(T.items()):
        st = "KS" if k in "FG" else "MO"
        name(store, npis[t][0], f"{k} REHAB", st, tax=PT if k != "G" else MD, city="Town" if i % 2 else "Ville")
        name(store, npis[t][1], f"CLIN {k}", "KS" if k == "C" else st, tax=PT, etype="NPI-1")
    store.rebuild_rollups()
    store.save_peer_set("PS", ["626000001", "636000001", "646000001"]) if hasattr(store, "save_peer_set") else None
    import mrfx.benchmark as bm
    from mrfx.benchmark import (compute_benchmark, compute_payer_negotiation, compute_payer_comparison,
                                contract_gaps_book)
    from mrfx.leads import compute_leaderboard, compute_leads, compute_payer_roster
    from mrfx.quality import size_premium
    from mrfx.schedule import compute_fee_schedule
    markets = [{"month": "latest", "state": "MO", "therapy_only": True},
               {"month": "latest", "allow_national": True},
               {"month": "latest", "payers": ["Sigma Health"]},
               {"month": "latest", "codes": ["97110"], "state": "KS"},
               {"month": "latest", "city": "Ville"},
               {"month": "latest", "state": "MO", "target_percentile": 75}]
    def run():
        out = []
        for m in markets:
            for s in ("A REHAB", "C REHAB", "F REHAB"):
                for f in (lambda: compute_benchmark(store, s, m),
                          lambda: compute_payer_negotiation(store, s, m, volumes={"97110": 100}),
                          lambda: compute_payer_comparison(store, s, "Rho Health", m, comparables=["B REHAB"]),
                          lambda: compute_fee_schedule(store, s, m),
                          lambda: contract_gaps_book(store, [s], m, min_peers=1)):
                    try:
                        out.append(f())
                    except Exception as e:
                        out.append(repr(e))
            out.append(compute_leads.uncached(store, m, threshold_percentile=60, min_codes=1))
            out.append(compute_leaderboard.uncached(store, m, min_codes=1))
            out.append(compute_payer_roster.uncached(store, "Rho Health", m, min_codes=1))
            out.append(size_premium.uncached(store, m, min_practices=1))
        return json.loads(json.dumps(out, sort_keys=True, default=str))
    a = run()
    orig = bm._house_eligible
    bm._house_eligible = lambda m: False
    b = run()
    bm._house_eligible = orig
    diffs = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    print("results", len(a), "differences", diffs)
    for i in diffs[:3]:
        print(json.dumps(a[i])[:600]); print(json.dumps(b[i])[:600])
