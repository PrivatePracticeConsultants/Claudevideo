"""4.2: is PRACTICE_RATE_RULE printed with every all-payer practice figure?"""
import shutil, sys
from pathlib import Path
D = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(D / "scripts"))

if __name__ == "__main__":
    import logging; logging.disable(logging.INFO)
    from fastapi.testclient import TestClient
    import mrfx.api as api
    from mrfx.config import MrfxConfig
    from mrfx.store import Store
    import s4_seed
    work = D / "w_s5"; shutil.rmtree(work, ignore_errors=True); work.mkdir()
    cfg = MrfxConfig(inbox_dir=work / "inbox", processed_dir=work / "p", failed_dir=work / "f",
                     store_dir=work / "store", downloads_dir=work / "dl",
                     entity_map_path=work / "em.yaml")
    cfg.ensure_dirs()
    s = Store(cfg.store_dir); s4_seed.seed(s); s.rebuild_rollups()
    api.TRUSTED_HOSTS.add("testserver")
    c = TestClient(api.create_app(cfg, s))
    M = {"month": "latest", "state": "MO"}
    SUBJ = "430000001"
    probes = [
        ("POST", "/api/report/pitch", {"subject": SUBJ, "market": M}),
        ("POST", "/api/report/ratecard", {"subject": SUBJ, "market": M}),
        ("POST", "/api/schedule/fee.csv", {"subject": SUBJ, "market": M}),
        ("POST", "/api/report/market", {"state": "MO", "code": "97110", "market": M}),
        ("POST", "/api/leads.csv", {"market": M}),
        ("POST", "/api/report/visitvalue", {"subject": SUBJ, "market": {**M, "discipline": "PT"},
                                            "lines": "97110, 15\n97140, 15"}),
        ("POST", "/api/schedule/visit/market.csv", {"market": {**M, "discipline": "PT"},
                                                   "lines": "97110, 15\n97140, 15"}),
        ("POST", "/api/report/dossier", {"subject": SUBJ, "market": M}),
        ("GET", "/api/export.zip?grain=tin&state=MO", None),
        ("GET", "/api/export/outreach.zip?state=MO", None),
    ]
    import io, zipfile
    for meth, route, body in probes:
        r = c.post(route, json=body) if meth == "POST" else c.get(route)
        txt = r.text if not route.endswith(".zip") and ".zip?" not in route else ""
        if ".zip" in route and r.status_code == 200:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            txt = "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())
        has = ("median of its published base lines" in txt
               or "place-of-service set counts" in txt or "place-of-service sets counts" in txt)
        print(f"{r.status_code} {route:38s} rule printed: {has}")
