"""5.2.1 on an UPGRADED store: a .json.gz loaded by 0.7.6 (c8b80fc), then the
same document saved decompressed (a browser) dropped in after the upgrade."""
import json, os, shutil, subprocess, sys
from pathlib import Path
D = Path(__file__).resolve().parent.parent

COMMON = r'''
import sys, json, gzip
from pathlib import Path
import mrfx
from mrfx.config import MrfxConfig
from mrfx.store import Store
from mrfx.ingest import scan_inbox
w = Path(%(work)r)
cfg = MrfxConfig(inbox_dir=w/"inbox", processed_dir=w/"p", failed_dir=w/"f", store_dir=w/"store",
                 downloads_dir=w/"dl", entity_map_path=w/"em.yaml")
cfg.ensure_dirs()
s = Store(cfg.store_dir)
sys.path.insert(0, %(tests)r)
groups = [{"provider_groups": [{"npi": [1000000000 + i], "tin": {"type": "ein", "value": f"43{i:07d}"}}],
           "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 40.0,
              "billing_class": "professional", "service_code": ["11"], "billing_code_modifier": ["GP"]}]}
          for i in range(60)]
body = json.dumps({"reporting_entity_name": "Twin Health", "last_updated_on": "2026-05-01", "version": "2.0.0",
                   "in_network": [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                                   "billing_code": "97110", "negotiated_rates": groups}]}).encode()
'''
STEP1 = COMMON + r'''
(cfg.inbox_dir / "twin_in-network-rates.json.gz").write_bytes(gzip.compress(body, mtime=0))
scan_inbox(cfg, s)
print("old load:", mrfx.__file__)
'''
STEP2 = COMMON + r'''
from mrfx.ingest import backfill_identities
print("backfilled:", backfill_identities(cfg, s))
with s.connect() as con:
    print("fingerprint of the 0.7.6 file:", con.execute("SELECT filename, full_sha IS NOT NULL, content_sha FROM content_fingerprints").fetchall())
(cfg.inbox_dir / "twin_in-network-rates (1).json").write_bytes(b"\xef\xbb\xbf" + body)
scan_inbox(cfg, s)
with s.connect() as con:
    print("files:", con.execute("SELECT filename, status FROM files ORDER BY 1").fetchall())
    print("rate rows:", con.execute("SELECT count(*) FROM rates").fetchone()[0], "(one document = 60)")
print("new code:", mrfx.__file__)
'''

if __name__ == "__main__":
    work = D / "w_s8"
    shutil.rmtree(work, ignore_errors=True); work.mkdir()
    kw = {"work": str(work), "tests": "/home/user/Claudevideo"}
    for code, root in ((STEP1, str(D / "old")), (STEP2, None)):
        env = dict(os.environ)
        if root:
            env["PYTHONPATH"] = root
        p = subprocess.run([sys.executable, "-c", code % kw], env=env, capture_output=True,
                           text=True, cwd=str(D))
        print(p.stdout.strip() or p.stderr[-2000:])
