"""Scratch store for the live dashboard runs: synthetic payers (with hostile
org names and SSN-pattern TINs) + three small real MRFs. Absolute paths only."""
import json, shutil, sys
from pathlib import Path
import h

ROOT = Path(__file__).parent / "live"
REAL = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/real")
XSS = ['<img src=x onerror="window.__xss=(window.__xss||0)+1">ACME REHAB',
       '"><svg onload="window.__xss=(window.__xss||0)+1">',
       "O'BRIEN & SONS PT </script><script>window.__xss=(window.__xss||0)+1</script>",
       "ÉQUIPE RÉÉDUCATION 東京 ‮LTR",
       "PLAIN REHAB #1 ?subject=x&state=ZZ"]

def write_cfg():
    y = f"""inbox_dir: {ROOT}/inbox
processed_dir: {ROOT}/processed
failed_dir: {ROOT}/failed
store_dir: {ROOT}/store
downloads_dir: {ROOT}/downloads
entity_map_path: {ROOT}/entity_map.yaml
registry_overrides_path: {ROOT}/registry_overrides.yaml
port: 8791
monthly_refresh: false
monthly_recheck: false
backup_dir: {ROOT}/bk
packets_dir: {ROOT}/packets
enrichment:
  mode: "off"
  auto_download: false
  nppes_dir: {ROOT}/nppes
"""
    (ROOT / "mrfx.yaml").write_text(y)

if __name__ == "__main__":
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir()
    write_cfg()
    cfg = h.mkcfg(ROOT)
    store = h.Store(cfg.store_dir)
    names = XSS + [f"CLINIC {i} REHAB" for i in range(600)]
    codes = ("97110", "97112", "97140", "97530", "97161", "97162")
    h.market(cfg, store, "Alpha Health", "43000", 600, codes=codes, day="2026-05-01", names=names)
    h.market(cfg, store, "Alpha Health", "43000", 600, codes=codes, day="2026-06-01", names=names, base=42)
    h.market(cfg, store, "Beta Health", "43000", 600, codes=codes, day="2026-06-01", names=names, base=45)
    h.market(cfg, store, "Gamma <b>Health</b>", "43000", 600, codes=codes, day="2026-06-01", names=names, base=50)
    h.market(cfg, store, "Ssn Health", "07000", 3, codes=codes, day="2026-06-01",
             names=["SOLE PROP ONE PT", "SOLE PROP TWO PT", "SOLE PROP THREE PT"], base=60)
    for f in ("moda_ModaTXPriceTrans_20261101.json.gz",
              "bcbsms_2026-10_460_70J0_in-network-rates_03_of_13.json.gz",
              "uhc_2026-11-01_HEALTHSCOPE-BENEFITS_Third-Party-Administrator_SHO_U1_3342_in-network-rates.json.gz"):
        shutil.copy(REAL / f, cfg.inbox_dir / f)
    print(h.scan_inbox(cfg, store))
    store.rebuild_rollups()
    from mrfx.clients import add_client
    for s in ("430000001", "430000002", "430000010", "070000001"):
        try:
            add_client(store, s)
        except Exception as e:
            print("client", s, e)
    with store.connect() as con:
        print("rates:", con.execute("SELECT count(*) FROM rates").fetchone()[0],
              "payers:", con.execute("SELECT count(DISTINCT payer) FROM rates").fetchone()[0])
