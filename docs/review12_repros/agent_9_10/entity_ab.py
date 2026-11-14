import sys, time
from pathlib import Path
import mrfx.api as api; api.TRUSTED_HOSTS.add("testserver")
from fastapi.testclient import TestClient
from mrfx.store import Store
sys.path.insert(0, str(Path(__file__).parent))
from build_store import cfg_for
if __name__ == "__main__":
    c = cfg_for(Path(sys.argv[1])); st = Store(c.store_dir, keep_warm=True); cl = TestClient(api.create_app(c, st))
    urls = ["/api/rates?grain=entity&latest=1", "/api/rates?grain=entity&latest=1&page=2", "/api/rates?grain=entity&latest=1&state=AL"]
    orig = api.entity_two_pass_ok
    for label, fn in (("two-pass", orig), ("one-pass", lambda fs, sort="negotiated_rate": False), ("two-pass again", orig)):
        api.entity_two_pass_ok = fn
        for u in urls:
            t0 = time.perf_counter(); r = cl.get(u); dt = time.perf_counter() - t0
            b = r.json(); print(f"{label:15s} {dt*1000:7.0f} ms {u} total={b.get('total')} first={b['rows'][0]['unit_id'] if b.get('rows') else None}")
    st.close()
