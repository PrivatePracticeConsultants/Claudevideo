"""Leads 'worst payer' averages in codes the position excludes (all-tied codes)."""
from fx import *

if __name__ == "__main__":
    cfg, store = mk()
    T = {k: f"8{i}0000001" for i, k in enumerate("ABCDE", start=1)}
    npis = {t: [f"18{i}0000001"] for i, t in enumerate(T.values(), start=1)}
    rho = {"97110": dict(zip(T.values(), (30, 40, 50, 60, 70))),
           "97530": {t: 50 for t in T.values()}}
    sig = {"97110": {T["A"]: 34}}
    ingest(cfg, store, [("rho_2026-06.json", doc("Rho Health", "2026-06-01", rho, npis)),
                        ("sig_2026-06.json", doc("Sigma Health", "2026-06-01", sig, npis))])
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", "MO")
    store.rebuild_rollups()
    from mrfx.leads import compute_leads
    L = compute_leads(store, {"month": "latest", "state": "MO", "therapy_only": False},
                      threshold_percentile=25, min_codes=1)
    for x in L["leads"]:
        print(x["display_name"], x["median_percentile"], x["n_codes"], x["avg_gap_to_median"],
              "worst:", x["worst_payer"], x["worst_payer_gap"])
    from mrfx.leads import compute_leaderboard
    lb = compute_leaderboard(store, {"month": "latest", "state": "MO", "therapy_only": False}, min_codes=1)
    print("leaderboard", [(p["display_name"], p["median_percentile"], p["n_codes"]) for p in lb["practices"]])
