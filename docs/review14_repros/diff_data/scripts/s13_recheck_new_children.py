"""5.2.5: monthly_recheck ON by default 'downloads only what changed'; queueing
NEW files 'stays the user's choice' (monthly_refresh, off). A pasted index
that next month lists a NEW file (the payer's next month) — downloaded?"""
import datetime as dt, gzip, json, shutil, sys
from pathlib import Path
D = Path(__file__).resolve().parent.parent
sys.path.insert(0, "/home/user/Claudevideo")

if __name__ == "__main__":
    import logging; logging.disable(logging.INFO)
    from mrfx import fetch
    from mrfx.config import MrfxConfig
    from mrfx.store import Store
    from tests.mrfx.test_review11 import Srv, drain, ranged, rate_doc
    work = D / "w_s13"; shutil.rmtree(work, ignore_errors=True); work.mkdir()
    cfg = MrfxConfig(inbox_dir=work / "inbox", processed_dir=work / "p", failed_dir=work / "f",
                     store_dir=work / "store", downloads_dir=work / "dl", entity_map_path=work / "em.yaml")
    cfg.ensure_dirs(); cfg.enrichment.mode = "off"
    print("defaults: monthly_refresh =", cfg.monthly_refresh, "| monthly_recheck =", cfg.monthly_recheck)
    s = Store(cfg.store_dir)
    srv = Srv({})
    nov = gzip.compress(rate_doc(n=30, month="2026-11-01"), mtime=0)
    dec = gzip.compress(rate_doc(n=30, rate=41.0, month="2026-12-01"), mtime=0)
    def toc(files):
        return json.dumps({"reporting_entity_name": "Twin Health", "reporting_structure": [{
            "reporting_plans": [{"plan_name": "PPO", "plan_id_type": "EIN", "plan_id": "1",
                                 "plan_market_type": "group"}],
            "in_network_files": [{"description": "x", "location": srv.base + f} for f in files]}]}).encode()
    try:
        srv.routes["/2026-11-01_twin_in-network-rates.json.gz"] = lambda r: ranged(nov, r, etag='"n"')
        srv.routes["/2026-12-01_twin_in-network-rates.json.gz"] = lambda r: ranged(dec, r, etag='"d"')
        cur = {"toc": toc(["/2026-11-01_twin_in-network-rates.json.gz"])}
        srv.routes["/index.json"] = lambda r: ranged(cur["toc"], r)
        fetch.add_urls(s, [srv.base + "/index.json"])
        drain(cfg, s)
        # next month the payer's index lists December's file (and still November's)
        cur["toc"] = toc(["/2026-11-01_twin_in-network-rates.json.gz",
                          "/2026-12-01_twin_in-network-rates.json.gz"])
        srv.log.clear()
        r = fetch.monthly_refresh_tick(cfg, s, dt.date(2026, 12, 3),
                                       dated_sources=cfg.monthly_refresh, recheck=cfg.monthly_recheck)
        print("tick:", r)
        drain(cfg, s)
        print("requests during the re-check:", [(p, st, rng, n) for p, st, rng, n in srv.log])
    finally:
        srv.close()
    with s.connect() as con:
        print("queue:", con.execute("SELECT regexp_replace(url, '.*/', ''), status, coalesce(error,'') FROM url_queue ORDER BY id").fetchall())
        print("months loaded:", con.execute("SELECT file_month, count(*) FROM rates GROUP BY 1 ORDER BY 1").fetchall())
