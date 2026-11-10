"""fxm + every side dataset (MPFS anchor, Medicare PUF, floors, a hospital file, referrals, ACS) — hand-workable."""
from fxm import *  # noqa
import csv, io
from pathlib import Path

def build_full():
    cfg, store = build()
    tmp = Path(cfg.store_dir).parent
    store.load_mpfs([{"code": "97110", "locality": "REST OF MISSOURI", "non_facility_rate": 30.0},
                     {"code": "97140", "locality": "REST OF MISSOURI", "non_facility_rate": 28.0},
                     {"code": "97530", "locality": "REST OF MISSOURI", "non_facility_rate": 40.0}], "test MPFS 2026", state="MO")
    # Medicare PUF: S1 (MO office) 3000/1000/500 ; S2 (KS office, same NPPES name) 9000 of 97110 ; B 2000/2000
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["Rndrng_NPI", "HCPCS_Cd", "Tot_Srvcs", "Tot_Benes", "Avg_Mdcr_Alowd_Amt", "Place_Of_Srvc"])
    for npi, code, units in (("1" + S1, "97110", 3000), ("1" + S1, "97140", 1000), ("1" + S1, "97530", 500),
                             ("1" + S2, "97110", 9000), ("1" + B, "97110", 2000), ("1" + B, "97140", 2000),
                             ("1" + M1, "97110", 100), ("1" + M2, "97110", 300)):
        w.writerow([npi, code, units, 40, 30.0, "O"])
    p = tmp / "puf_2023.csv"; p.write_text(buf.getvalue())
    from mrfx.utilization import import_utilization
    import_utilization(store, p, year="2023")
    # floors: MO Medicaid 97110 25.00 / 97140 22.00 ; MO workers' comp 97110 55.00
    from mrfx.floors import import_fee_schedule
    p = tmp / "mo_medicaid.csv"; p.write_text("Procedure Code,Maximum Allowable\n97110,25.00\n97140,22.00\n")
    import_fee_schedule(store, p, kind="medicaid", state="MO", year="2026")
    p = tmp / "mo_wc.csv"; p.write_text("Procedure Code,Maximum Allowable\n97110,55.00\n")
    import_fee_schedule(store, p, kind="workers_comp", state="MO", year="2026")
    # hospital: Alpha Health pays St Mary's 128.40 (97110), 119.00 (97140)
    hdr = ["description", "code|1", "code|1|type", "setting", "payer_name", "plan_name", "standard_charge|gross",
           "standard_charge|discounted_cash", "standard_charge|negotiated_dollar", "standard_charge|methodology", "estimated_amount", "modifiers"]
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["hospital_name", "last_updated_on", "version"]); w.writerow(["St Mary's Regional", "2026-04-01", "2.0.0"]); w.writerow(hdr)
    w.writerow(["Therapeutic exercise", "97110", "CPT", "outpatient", "Alpha Health", "PPO", "310", "180", "128.40", "fee schedule", "", ""])
    w.writerow(["Manual therapy", "97140", "CPT", "outpatient", "Alpha Health", "PPO", "290", "165", "119.00", "fee schedule", "", ""])
    p = tmp / "st_marys.csv"; p.write_text(buf.getvalue())
    from mrfx.hospital import import_hospital_file
    import_hospital_file(store, p, state="MO", cfg=cfg)
    # ACS
    p = tmp / "acs.csv"
    p.write_text("GEO_ID,NAME,B01001_001E,B01001_020E,B01001_044E\n86000US64105,ZCTA5 64105,50000,4000,4200\n")
    from mrfx.demographics import import_demographics
    import_demographics(store, p)
    return cfg, store
