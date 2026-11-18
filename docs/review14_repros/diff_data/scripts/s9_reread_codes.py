"""5.2.4: re-read for added codes — rows replaced (not doubled), analytics follow;
a file whose copy is gone and that came from no link."""
import json, shutil, sys
from pathlib import Path
D = Path(__file__).resolve().parent.parent


def doc(codes, n=10, day="2026-05-01"):
    items = []
    for c in codes:
        items.append({"negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code": c,
                      "negotiated_rates": [{"provider_groups": [{"npi": [1000000000 + i],
                          "tin": {"type": "ein", "value": f"43{i:07d}"}}],
                          "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 40.0 + i,
                              "billing_class": "professional", "service_code": ["11"],
                              "billing_code_modifier": ["GP"]}]} for i in range(n)]})
    return json.dumps({"reporting_entity_name": "Recode Health", "last_updated_on": day,
                       "version": "2.0.0", "in_network": items}).encode()


if __name__ == "__main__":
    import logging; logging.disable(logging.INFO)
    from mrfx.config import MrfxConfig
    from mrfx.store import Store
    from mrfx.ingest import scan_inbox, code_set_key, reread_for_new_codes
    work = D / "w_s9"; shutil.rmtree(work, ignore_errors=True); work.mkdir()
    cfg = MrfxConfig(inbox_dir=work / "inbox", processed_dir=work / "p", failed_dir=work / "f",
                     store_dir=work / "store", downloads_dir=work / "dl", entity_map_path=work / "em.yaml")
    cfg.ensure_dirs()
    s = Store(cfg.store_dir)
    q = lambda sql: s.connect().__enter__().execute(sql).fetchall()
    cfg.codes.cpt_codes = ["97112"]
    (cfg.inbox_dir / "a.json").write_bytes(doc(["97110", "97112"]))
    (cfg.inbox_dir / "b.json").write_bytes(doc(["97110", "97112"], day="2026-06-01"))
    scan_inbox(cfg, s)
    s.rebuild_rollups()
    print("loaded with [97112]:", q("SELECT source_file, billing_code, count(*) FROM rates GROUP BY ALL ORDER BY ALL"))
    (cfg.processed_dir / "b.json").unlink()        # b's copy is gone, and it came from no link
    cfg.codes.cpt_codes = ["97110", "97112"]
    s.current_code_set = code_set_key(cfg)
    print("missing:", s.files_missing_codes())
    out = reread_for_new_codes(cfg, s)
    print("reread:", out)
    print("rows now:", q("SELECT source_file, billing_code, count(*) FROM rates GROUP BY ALL ORDER BY ALL"))
    print("spine 97110 rows:", q("SELECT count(*) FROM rates_by_tin_tbl WHERE billing_code='97110'"),
          "stale:", s.rollups_stale())
    print("b row:", q("SELECT filename, status, error FROM files WHERE filename='b.json'"))
    print("missing after:", s.files_missing_codes())
    # remove 97110 again and re-add: nothing to re-read for a
    cfg.codes.cpt_codes = ["97112"]; s.current_code_set = code_set_key(cfg)
    print("after removing 97110, missing:", s.files_missing_codes())
    (cfg.inbox_dir / "c.json").write_bytes(doc(["97110", "97112"], day="2026-07-01"))
    scan_inbox(cfg, s)
    cfg.codes.cpt_codes = ["97110", "97112"]; s.current_code_set = code_set_key(cfg)
    print("re-added 97110, missing:", s.files_missing_codes())
