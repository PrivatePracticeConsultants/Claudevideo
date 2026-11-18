"""Does the NPI drawer's 'current rate' chart use the contract-level latest rule
(round 3 P0.2: newest month per payer x provider x code) or a per-modifier one?
May: TIN T / NPI N, 97110, no modifier, $40.  June: same contract, GP, $42.
Contract rule -> current = $42 (June). Per-modifier partition keeps May's '' row."""
import json, pathlib, shutil, sys, tempfile
from mrfx.config import MrfxConfig
from mrfx.store import Store

TIN, NPI = "123456789", "1234567893"

def _doc(payer, day, items):
    return {"reporting_entity_name": payer, "reporting_entity_type": "issuer",
            "last_updated_on": day, "version": "2.0.0", "provider_references": [],
            "in_network": [{
                "negotiation_arrangement": "ffs", "name": code, "billing_code_type": "CPT",
                "billing_code_type_version": "2026", "billing_code": code, "description": code,
                "negotiated_rates": [{"provider_groups": [{"npi": [NPI], "tin": {"type": "ein", "value": TIN}}],
                                      "negotiated_prices": [{
                                          "negotiated_type": "negotiated", "negotiated_rate": rate,
                                          "billing_class": "professional", "service_code": ["11"],
                                          "billing_code_modifier": mods,
                                          "expiration_date": "9999-12-31"}]}]}
                for code, rate, mods in items]}

def main():
    from fastapi.testclient import TestClient
    from mrfx.api import create_app
    from mrfx.ingest import scan_inbox
    tmp = pathlib.Path(tempfile.mkdtemp(dir=pathlib.Path(__file__).parent))
    try:
        cfg = MrfxConfig(inbox_dir=tmp/"inbox", processed_dir=tmp/"processed", failed_dir=tmp/"failed",
                         store_dir=tmp/"store", downloads_dir=tmp/"downloads",
                         entity_map_path=tmp/"entity_map.yaml", confirm_over_gb=5.0)
        cfg.ensure_dirs()
        store = Store(cfg.store_dir)
        (cfg.inbox_dir/"p_2026-05_in-network.json").write_text(json.dumps(_doc("Payer P", "2026-05-01", [("97110", 40.0, [])])))
        (cfg.inbox_dir/"p_2026-06_in-network.json").write_text(json.dumps(_doc("Payer P", "2026-06-01", [("97110", 42.0, ["GP"])])))
        res = scan_inbox(cfg, store)
        assert all(r["status"] == "done" for r in res), res
        store.rebuild_rollups()
        c = TestClient(create_app(cfg, store), base_url="http://localhost")
        for grain, uid in (("npi", NPI), ("tin", TIN)):
            resp = c.get(f"/api/entity/{grain}/{uid}")
            d = resp.json()
            print(grain, "drawer status", resp.status_code, "chart:", d.get("chart"), "| rates:", [(x.get("billing_code"), x.get("modifier_set"), x.get("file_month"), x.get("negotiated_rate")) for x in d.get("rates", [])][:6], "| detail:", d.get("detail"))
        r = c.get("/api/rates", params={"grain": "npi", "latest": "1", "q": NPI}).json()
        print("explorer npi grain latest rows:", [(x["billing_code"], x["modifier_set"], x["file_month"], x["negotiated_rate"]) for x in r.get("rows", [])], r.get("detail"))
        r = c.get("/api/rates", params={"grain": "tin", "latest": "1", "q": TIN}).json()
        print("explorer tin grain latest rows:", [(x["billing_code"], x["modifier_set"], x["file_month"], x["negotiated_rate"]) for x in r.get("rows", [])], r.get("detail"))
        r = c.get("/api/code/97110", params={"grain": "npi", "payer": "Payer P"}).json()
        print("code page npi grain keys:", {k: r[k] for k in r if k in ("payer_rank", "ranked")} )
    finally:
        try: store.close()
        except Exception: pass
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    main()
