"""Per-visit value compares payers over DIFFERENT code subsets."""
from fx import *
import csv

if __name__ == "__main__":
    cfg, store = mk()
    S = "430000001"; npis = {S: ["1417594896"]}
    a = {"97530": {S: 50}}                       # A prices only 97530
    b = {"97110": {S: 40}, "97530": {S: 60}}     # B pays MORE on the shared code
    ingest(cfg, store, [("a_2026-06.json", doc("A Health", "2026-06-01", a, npis)),
                        ("b_2026-06.json", doc("B Health", "2026-06-01", b, npis))])
    name(store, "1417594896", "SUBJ REHAB", "MO")
    store.rebuild_rollups()
    p = cfg.store_dir.parent / "puf_2024.csv"
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Rndrng_NPI", "HCPCS_Cd", "Tot_Srvcs", "Tot_Benes", "Avg_Mdcr_Alowd_Amt", "Place_Of_Srvc"])
        w.writerow(["1417594896", "97110", 1000, 40, 30.0, "O"])
        w.writerow(["1417594896", "97530", 100, 20, 35.0, "O"])
    from mrfx.utilization import import_utilization
    import_utilization(store, p, year="2024")
    from mrfx.economics import visit_economics
    ve = visit_economics(store, "SUBJ REHAB", {"month": "latest", "state": "MO"}, units_per_visit=3)
    for r in ve["rows"]:
        print(r["payer"], r["value_per_billed_unit"], r["value_per_visit"], "coverage", r["mix_coverage_pct"])
    print(ve["headline"])
    print("best", ve["best_payer"], "worst", ve["worst_payer"])
