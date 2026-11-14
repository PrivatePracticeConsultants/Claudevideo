"""URL queue: a rate file that needs the payer's standalone provider-reference
file is downloaded and loaded BEFORE that reference file (queue order = link
order). Is it re-ingested once the references land?"""
import json
from dl import *
import mrfx.fetch as F

H = {"reporting_entity_name": "Acme Health", "version": "1.0", "last_updated_on": "2026-05-01"}
RATES = {**H, "in_network": [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code": "97110",
         "negotiated_rates": [{"provider_references": [1, 2], "negotiated_prices": [
             {"negotiated_type": "negotiated", "negotiated_rate": 50.0, "billing_class": "professional", "service_code": ["11"]}]}]}]}
REFS = {**H, "provider_references": [
    {"provider_group_id": 1, "provider_groups": [{"npi": [1111111111], "tin": {"type": "ein", "value": "111111111"}}]},
    {"provider_group_id": 2, "provider_groups": [{"npi": [2222222222], "tin": {"type": "ein", "value": "222222222"}}]}]}

if __name__ == "__main__":
    for keep_raw in (False, True):
        cfg, store = mk_cfg(f"d6_{keep_raw}")
        cfg.delete_raw_after_ingest = not keep_raw
        cfg.parallel_ingests = 1
        srv = Srv({"/acme_rates_2026-05.json": json.dumps(RATES).encode(), "/acme_refs_2026-05.json": json.dumps(REFS).encode()})
        try:
            ur, uf = srv.base + "/acme_rates_2026-05.json", srv.base + "/acme_refs_2026-05.json"
            F.add_urls(store, [ur]); F.run_queue(cfg, store, drain=True)
            print(f"[delete_raw_after_ingest={cfg.delete_raw_after_ingest}] rate file first ->", row(store, ur)["status"], "rows", row(store, ur)["rows_emitted"])
            F.add_urls(store, [uf]); F.run_queue(cfg, store, drain=True)
            print("   reference file ->", row(store, uf)["status"], "ids", row(store, uf)["rows_emitted"])
            F.run_queue(cfg, store, drain=True)
            r = row(store, ur)
            print("   rate link now:", r["status"], "rows", r["rows_emitted"], "| error:", r["error"])
            print("   files:", q(store, "SELECT filename, file_type, status, rows_emitted, ref_groups_skipped FROM files ORDER BY 1"))
            print("   rates rows:", q(store, "SELECT count(*) FROM rates")[0][0], "(expected 2)",
                  "| GETs for the rate file:", len(srv.hits("/acme_rates_2026-05.json")))
        finally:
            srv.close()
