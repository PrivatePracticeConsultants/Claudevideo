import json, shutil, sys, time
from pathlib import Path
sys.path.insert(0, "/home/user/Claudevideo")
import mrfx.store as S
from mrfx.config import MrfxConfig
from mrfx.ingest import ingest_file
ROOT = Path("/tmp/mrfx_review9/_scratch/scale")
N = int(sys.argv[1])
def doc(i):
    return {"reporting_entity_name": "Acme", "version": "1", "last_updated_on": "2026-05-01",
            "in_network": [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code": c,
              "negotiated_rates": [{"provider_groups": [{"npi": [f"1{j:09d}"], "tin": {"type": "ein", "value": f"{100000000+j}"}}],
                 "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 40 + j % 7,
                   "billing_class": "professional", "billing_code_modifier": ["GP"], "service_code": ["11"]}]} for j in range(200)]}
              for c in ("97110", "97140", "97530")]}
if __name__ == "__main__":
    if not ROOT.exists():
        c = MrfxConfig(inbox_dir=ROOT/"in", processed_dir=ROOT/"p", failed_dir=ROOT/"f", store_dir=ROOT/"store",
                       downloads_dir=ROOT/"dl", entity_map_path=ROOT/"em.yaml"); c.ensure_dirs()
        st = S.Store(c.store_dir)
        p = c.inbox_dir / "seed_2026-05.json"; p.write_text(json.dumps(doc(0)))
        ingest_file(c, st, p, rebuild_rollups=False)
        seed = st.rates_dir / f"{S.file_key('seed_2026-05.json')}.parquet"
        with st.write_lock, st.connect() as con:
            for i in range(N):
                name = f"f{i:05d}_2026-05.json"
                shutil.copy(seed, st.rates_dir / f"{S.file_key(name)}.parquet")
                con.execute("INSERT INTO files (filename, status, file_type, row_rules) VALUES (?, 'done', 'in_network', 2)", [name])
        st.close()
    else:
        st = S.Store(ROOT/"store")
        with st.write_lock, st.connect() as con:
            con.execute("UPDATE files SET row_rules = 2")
        st.close()
    t = time.monotonic(); S.Store(ROOT/"store"); print("open took %.1f s" % (time.monotonic() - t))
