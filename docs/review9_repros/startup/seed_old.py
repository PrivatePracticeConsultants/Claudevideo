"""Runs under the OLD build (0.7.0): a user's install with their own settings and data."""
import json, sys
from pathlib import Path
I = Path(sys.argv[1]); sys.path.insert(0, str(I))
import mrfx; assert mrfx.__version__ == "0.7.0", mrfx.__version__
from mrfx.config import load_mrfx_config as load_config
from mrfx.store import Store
from mrfx.ingest import scan_inbox
if __name__ == "__main__":
    import os; os.chdir(I)
    y = I / "config" / "mrfx.yaml"
    y.write_text(y.read_text() + "\n# MY OWN NOTE - keep me\nport: 8655\n")
    cfg = load_config(y); cfg.ensure_dirs()
    def doc(payer, ntype, rate, arr="ffs"):
        return {"reporting_entity_name": payer, "version": "1.0", "last_updated_on": "2026-05-01",
                "in_network": [{"negotiation_arrangement": arr, "billing_code_type": "CPT", "billing_code": "97110",
                  "negotiated_rates": [{"provider_groups": [{"npi": [f"1{i:09d}"], "tin": {"type": "ein", "value": f"{810000000+i}"}}],
                     "negotiated_prices": [{"negotiated_type": ntype, "negotiated_rate": rate + i, "billing_class": "professional",
                                            "billing_code_modifier": ["GP"], "service_code": ["11"]}]} for i in range(6)]}]}
    (cfg.inbox_dir / "alpha_2026-05.json").write_text(json.dumps(doc("Alpha Health", "negotiated", 40.0)))
    (cfg.inbox_dir / "beta_2026-05.json").write_text(json.dumps(doc("Beta Health", "fee_schedule", 50.0)))
    st = Store(cfg.store_dir)
    print("old ingest:", [(r["file"], r["status"], r.get("rows")) for r in scan_inbox(cfg, st)])
    with st.connect() as con:
        print("old rows:", con.execute("SELECT payer, negotiated_type, is_dollar_rate, count(*) FROM rates GROUP BY ALL ORDER BY 1").fetchall())
        print("old files:", con.execute("SELECT filename, row_rules FROM files ORDER BY 1").fetchall())
        print("old market median 97110 (dollar rows):", con.execute("SELECT median(negotiated_rate) FROM rates_by_tin WHERE is_dollar_rate").fetchone())
