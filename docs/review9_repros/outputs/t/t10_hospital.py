"""Hospital parity: the hospital side is a median over raw plan rows, not per hospital."""
from fx import *
import csv

def hosp(path, hname, plans):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hospital_name", "last_updated_on", "version"])
        w.writerow([hname, "2026-07-01", "2.0.0"])
        hdr = ["description", "code|1", "code|1|type", "setting", "modifiers", "standard_charge|gross"]
        hdr += [f"standard_charge|Aetna|{p}|negotiated_dollar" for p, _ in plans]
        w.writerow(hdr)
        w.writerow(["Ther exercise", "97110", "CPT", "outpatient", "", "300"] + [str(r) for _, r in plans])

if __name__ == "__main__":
    cfg, store = mk()
    T = {k: f"6{i}5000001" for i, k in enumerate("ABC", start=1)}
    npis = {t: [f"16{i}5000001"] for i, t in enumerate(T.values(), start=1)}
    ingest(cfg, store, [("a_2026-06.json", doc("Aetna", "2026-06-01", {"97110": {t: 50 for t in T.values()}}, npis))])
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", "MO", tax=PT)
    store.rebuild_rollups()
    from mrfx.hospital import import_hospital_file, hospital_parity
    d = cfg.store_dir.parent
    hosp(d / "h1.csv", "Hospital One", [("PPO", 100), ("HMO", 100), ("EPO", 100)])
    hosp(d / "h2.csv", "Hospital Two", [("PPO", 200)])
    print(import_hospital_file(store, d / "h1.csv", state="MO"))
    print(import_hospital_file(store, d / "h2.csv", state="MO"))
    p = hospital_parity(store, {"month": "latest"}, payer="Aetna", state="MO")
    for r in p["rows"]:
        print(r["billing_code"], "practice", r["practice_median"], "hospital", r["hospital_median"],
              "n_hosp", r["n_hospitals"], "multiple", r["hospital_multiple"])
