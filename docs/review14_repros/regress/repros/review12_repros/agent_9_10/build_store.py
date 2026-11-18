"""Build a synthetic scratch store: NT tins x 4 payers x 8 codes x 2 months, with NPPES names."""
import sys, time, random
from pathlib import Path
from mrfx.config import MrfxConfig
from mrfx.store import Store
CODES = ["97110", "97140", "97530", "97112", "97161", "97162", "92507", "97535"]
PAYERS = ["Payer 00", "Payer 01", "Payer 02", "Payer 03"]
STATES = ["MO", "KS", "TX", "NY", "MA", "AL", "ND", "CA", "FL", "OH"]
def cfg_for(root: Path) -> MrfxConfig:
    c = MrfxConfig(inbox_dir=root/"inbox", processed_dir=root/"processed", failed_dir=root/"failed",
                   store_dir=root/"store", downloads_dir=root/"downloads", entity_map_path=root/"em.yaml")
    c.ensure_dirs(); return c
if __name__ == "__main__":
    root = Path(sys.argv[1]); nt = int(sys.argv[2])
    c = cfg_for(root); st = Store(c.store_dir)
    rnd = random.Random(7)
    t0 = time.monotonic()
    total = 0
    for pi, payer in enumerate(PAYERS):
        for month in ("2026-05", "2026-06"):
            fname = f"payer{pi}_{month}.json"
            rows = []
            for i in range(nt):
                tin = f"{300000000 + i:09d}"
                for ci, code in enumerate(CODES):
                    rows.append({"payer": payer, "source_file": fname, "file_month": month,
                        "schema_version": "1.0.0", "last_updated_on": month + "-01",
                        "tin_value": tin, "tin_type": "ein", "tin_is_really_npi": False,
                        "npi": f"1{tin}", "billing_code": code, "billing_code_type": "CPT",
                        "discipline": "SLP" if code == "92507" else "PT", "is_timed": code != "97161",
                        "billing_code_modifier": ["GP"], "negotiated_rate": round(20 + (i * 7 + ci * 3 + pi * 5) % 60 + rnd.random(), 2),
                        "negotiated_type": "negotiated", "is_dollar_rate": True,
                        "billing_class": "professional", "service_code": ["11"],
                        "expiration_date": "2027-01-01", "ingested_at": "2026-07-11"})
            with st.rates_part_writer(fname) as w:
                w.write_batch(rows)
            total += len(rows)
            st.upsert_file(fname, payer=payer, file_type="in_network", status="done",
                           rows_emitted=len(rows), file_month=month, finished_at="2026-07-11 00:00:00")
    print("parts written", total, "rows in %.1f s" % (time.monotonic() - t0), flush=True)
    npis = [{"npi": f"1{300000000 + i:09d}", "entity_type": "NPI-2", "org_name": f"PRACTICE {i} REHAB",
             "taxonomy_code": "261QP2000X", "city": f"CITY{i % 50}", "state": STATES[i % len(STATES)],
             "address": "x", "zip": "64105", "phone": None} for i in range(nt)]
    st.save_npis_bulk(npis)
    t1 = time.monotonic(); st.rebuild_rollups(); print("rollup %.1f s" % (time.monotonic() - t1), flush=True)
    st.close()
    print("DONE total rows", total)
