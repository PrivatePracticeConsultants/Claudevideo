"""Round 10 left-open: does Markets' state ranking still list a military mail code (AE)?"""
import sys, time
from pathlib import Path
from mrfx.store import Store
from mrfx.market import geographic_rates
sys.path.insert(0, str(Path(__file__).parent))
from build_store import cfg_for
if __name__ == "__main__":
    c = cfg_for(Path(sys.argv[1])); st = Store(c.store_dir)
    # practice 5 gets an extra NPI whose NPPES state is AE (APO address)
    st.save_npi("1300000005", "PRACTICE 5 REHAB", "261QP2000X", "x", "APO", "AE", entity_type="NPI-2", zip_code="09012")
    st.rebuild_rollups()
    with st.connect() as con:
        print("tin_directory with AE:", con.execute("select tin_value, states from tin_directory where list_contains(states, 'AE')").fetchall())
    out = geographic_rates(st, "97110", {"month": "latest"})
    rows = out.get("states") or out.get("rows") or []
    print("ranking states:", [r.get("state") for r in rows])
    print("AE listed:", any(r.get("state") == "AE" for r in rows), "| states_with_data:", out.get("states_with_data"), "| keys:", list(out.keys()))
    st.close()
