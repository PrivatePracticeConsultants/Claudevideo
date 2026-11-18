import io, secrets, tempfile, zipfile, difflib
from pathlib import Path
import h
from fastapi.testclient import TestClient
if __name__ == "__main__":
    root = Path(tempfile.mkdtemp(dir=Path(__file__).parent, prefix="t09b_"))
    cfg = h.mkcfg(root); store = h.Store(cfg.store_dir)
    from tests.mrfx.test_review13 import _market
    T, _ = _market(cfg, store, "Alpha Health", "71000", [40, 50, 60, 70, 80]); store.rebuild_rollups()
    c = TestClient(h._api.create_app(cfg, store))
    u = "/api/export/outreach.zip?grain=tin&payer=Alpha%20Health&cpt=97110"
    def meth(hdr):
        z = zipfile.ZipFile(io.BytesIO(c.get(u, headers=hdr).content))
        return z.read([n for n in z.namelist() if n.endswith("methodology.txt")][0]).decode()
    a, b, t = meth({}), meth({}), meth({"X-Mrfx-Progress": secrets.token_hex(12)})
    print("plain vs plain identical:", a == b)
    print("".join(difflib.unified_diff(a.splitlines(1), t.splitlines(1), "no token", "with token", n=0)))
