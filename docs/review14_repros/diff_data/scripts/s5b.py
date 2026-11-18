import sys, logging
from pathlib import Path
D = Path(__file__).resolve().parent.parent
if __name__ == "__main__":
    logging.disable(logging.INFO)
    from fastapi.testclient import TestClient
    import mrfx.api as api
    from mrfx.config import MrfxConfig
    from mrfx.store import Store
    work = D / "w_s5"
    cfg = MrfxConfig(inbox_dir=work / "inbox", processed_dir=work / "p", failed_dir=work / "f",
                     store_dir=work / "store", downloads_dir=work / "dl", entity_map_path=work / "em.yaml")
    s = Store(cfg.store_dir); api.TRUSTED_HOSTS.add("testserver")
    c = TestClient(api.create_app(cfg, s))
    M = {"month": "latest", "state": "MO"}
    r = c.post("/api/report/visitvalue", json={"subject": "430000001", "market": {**M, "discipline": "PT"}, "lines": "97110, 15\n97140, 15"})
    print(r.status_code, r.text[:300])
    r = c.post("/api/schedule/visit/market", json={"subject": "430000001", "market": {**M, "discipline": "pt"}, "lines": "97110, 15\n97140, 15"})
    print(r.status_code, r.text[:300])
    for route in ("/api/report/ratecard", "/api/report/market", "/api/leads.csv"):
        body = {"subject": "430000001", "market": M} if "ratecard" in route else ({"state": "MO", "code": "97110", "market": M} if "market" in route else {"market": M})
        t = c.post(route, json=body).text
        import re
        txt = re.sub("<[^>]+>", " ", t); txt = re.sub(r"\s+", " ", txt)
        i = txt.find("median")
        print("==", route, "|", txt[max(0, i-200):i+200])
