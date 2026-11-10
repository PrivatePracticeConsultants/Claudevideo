"""Time a few output endpoints on the 99M scratch store (first, second call)."""
import sys, time
from pathlib import Path
sys.path.insert(0, "/home/user/Claudevideo")

if __name__ == "__main__":
    from fastapi.testclient import TestClient
    import mrfx.api as api
    from mrfx.config import MrfxConfig
    from mrfx.store import Store
    d = Path("/tmp/mrfx_review9/_scratch/scale4n")
    cfg = MrfxConfig(inbox_dir=d / "in", processed_dir=d / "p", failed_dir=d / "f",
                     store_dir=d / "store", downloads_dir=d / "dl", entity_map_path=d / "em.yaml",
                     enrichment={"mode": "off"}, packets_dir=d / "packets_r6")
    st = Store(cfg.store_dir)
    api.TRUSTED_HOSTS.add("testserver")
    c = TestClient(api.create_app(cfg, st))
    with st.connect() as con:
        tin = con.execute(
            "SELECT t.tin_value FROM rates_by_tin t JOIN tin_directory d USING (tin_value) "
            "WHERE t.payer = 'Payer 03' AND list_contains(d.states, 'MO') "
            "GROUP BY 1 ORDER BY count(DISTINCT t.billing_code) DESC, count(*) DESC, 1 LIMIT 1").fetchone()[0]
    M = {"month": "latest", "state": "MO"}
    calls = [
        ("trajectory MO", "POST", "/api/trajectory", {"market": M}),
        ("pitch MO", "POST", "/api/report/pitch", {"subject": tin, "market": M}),
        ("ratecard MO", "POST", "/api/report/ratecard", {"subject": tin, "market": M}),
        ("outreach.zip MO (button defaults)", "GET", "/api/export/outreach.zip?grain=entity&dollar_only=1&state=MO", None),
    ]
    for label, meth, url, body in calls:
        ts = []
        for _ in range(2):
            t0 = time.perf_counter()
            r = c.post(url, json=body) if meth == "POST" else c.get(url)
            ts.append(time.perf_counter() - t0)
        print(f"{label}: {r.status_code} first {ts[0]:.2f}s second {ts[1]:.2f}s len {len(r.content)}", flush=True)
