"""6.2 lines: rounding at the Medicaid boundary; a hospital file loaded
without --state; two same-name hospitals in one state."""
import csv, shutil, sys
from pathlib import Path
D = Path(__file__).resolve().parent.parent


def hosp_csv(p, name, rows):
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hospital_name", "last_updated_on", "version"])
        w.writerow([name, "2026-04-01", "2.0.0"])
        w.writerow(["description", "code|1", "code|1|type", "setting", "payer_name",
                    "plan_name", "standard_charge|gross", "standard_charge|discounted_cash",
                    "standard_charge|negotiated_dollar", "standard_charge|methodology",
                    "estimated_amount", "modifiers"])
        for code, payer, plan, rate in rows:
            w.writerow(["x", code, "CPT", "outpatient", payer, plan, "300", "150",
                        rate, "fee schedule", "", ""])
    return p


if __name__ == "__main__":
    import logging; logging.disable(logging.INFO)
    from mrfx.config import MrfxConfig
    from mrfx.store import Store
    from mrfx.anchors import medicaid_lines, hospital_lines
    from mrfx.floors import import_fee_schedule
    from mrfx.hospital import import_hospital_file
    work = D / "w_s6"; shutil.rmtree(work, ignore_errors=True); work.mkdir()
    cfg = MrfxConfig(inbox_dir=work / "inbox", processed_dir=work / "p", failed_dir=work / "f",
                     store_dir=work / "store", downloads_dir=work / "dl", entity_map_path=work / "em.yaml")
    cfg.ensure_dirs()
    s = Store(cfg.store_dir)
    (work / "mo.csv").write_text("Procedure Code,Maximum Allowable\n97110,30.00\n97112,25.00\n")
    import_fee_schedule(s, work / "mo.csv", kind="medicaid", state="MO", year="2026")
    for r in (29.996, 30.004, 29.9951):
        print("medicaid, practice rate", r, "->",
              medicaid_lines(s, {"state": "MO"}, {("Alpha", "97110"): r})["lines"][0]["text"])
    # hospital loaded WITHOUT --state (the CLI flag is optional)
    import_hospital_file(s, hosp_csv(work / "h1.csv", "Mercy Hospital",
                                     [("97110", "Alpha", "PPO", "120.00")]), cfg=cfg)
    print("hospital without state ->", hospital_lines(s, {"state": "MO"}, {("Alpha", "97110"): 40.0}))
    # two DIFFERENT MO hospitals that share a name (different cities)
    import_hospital_file(s, hosp_csv(work / "h2.csv", "Mercy Hospital",
                                     [("97112", "Alpha", "PPO", "80.00")]), state="MO", city="St. Louis", cfg=cfg)
    import_hospital_file(s, hosp_csv(work / "h3.csv", "Mercy Hospital",
                                     [("97112", "Alpha", "PPO", "200.00")]), state="MO", city="Springfield", cfg=cfg)
    print("two same-name MO hospitals ->",
          [x["text"] for x in hospital_lines(s, {"state": "MO"}, {("Alpha", "97112"): 40.0})["lines"]])
