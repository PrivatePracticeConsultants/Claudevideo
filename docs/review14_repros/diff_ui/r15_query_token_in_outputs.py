"""The iframe downloads carry the token in the URL (?progress=): does it leak
into the delivered files (methodology sidecar, outreach) or change them?"""
import io, re, secrets, tempfile, zipfile, difflib
from pathlib import Path
import h
from fastapi.testclient import TestClient
if __name__ == "__main__":
    root = Path(tempfile.mkdtemp(dir=Path(__file__).parent, prefix="t15_"))
    cfg = h.mkcfg(root); store = h.Store(cfg.store_dir)
    from tests.mrfx.test_review13 import _market
    T, _ = _market(cfg, store, "Alpha Health", "71000", [40, 50, 60, 70, 80]); store.rebuild_rollups()
    c = TestClient(h._api.create_app(cfg, store))
    for u in ("/api/export.zip?grain=tin&payer=Alpha%20Health&cpt=97110",
              "/api/export/outreach.zip?grain=tin&payer=Alpha%20Health&cpt=97110"):
        tok = secrets.token_hex(12)
        a = zipfile.ZipFile(io.BytesIO(c.get(u).content)); b = zipfile.ZipFile(io.BytesIO(c.get(u + "&progress=" + tok).content))
        for n in a.namelist():
            x = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.+]+", "D", a.read(n).decode("utf-8", "replace"))
            m = [k for k in b.namelist() if k == n][0]
            y = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.+]+", "D", b.read(m).decode("utf-8", "replace"))
            print(u.split("?")[0], n, "identical" if x == y else "DIFFERENT", "| token inside:", tok in y)
