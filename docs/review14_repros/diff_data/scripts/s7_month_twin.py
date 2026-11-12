"""5.2.1: two files, SAME decompressed content, names that carry different
months, header with no usable date (file_month comes from the name).
Old code (c8b80fc) vs new: which months reach the store?"""
import json, os, shutil, subprocess, sys
from pathlib import Path
D = Path(__file__).resolve().parent.parent

RUN = r'''
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
groups = [{"provider_groups": [{"npi": [1000000000 + i], "tin": {"type": "ein", "value": f"43{i:07d}"}}],
           "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 40.0,
              "billing_class": "professional", "service_code": ["11"], "billing_code_modifier": ["GP"]}]}
          for i in range(20)]
body = json.dumps({"reporting_entity_name": "Twin Health", "version": "1.0.0",
                   "in_network": [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                                   "billing_code": "97110", "negotiated_rates": groups}]}).encode()
(cfg.inbox_dir / "twin_2026-05-01_in-network-rates.json").write_bytes(body)
scan_inbox(cfg, s)
(cfg.inbox_dir / "twin_2026-06-01_in-network-rates.json.gz").write_bytes(gzip.compress(body, mtime=0))
scan_inbox(cfg, s)
with s.connect() as con:
    files = con.execute("SELECT filename, status, coalesce(error,'') FROM files ORDER BY filename").fetchall()
    months = con.execute("SELECT file_month, count(*) FROM rates GROUP BY 1 ORDER BY 1").fetchall()
print(json.dumps({"code": mrfx.__file__, "files": files, "months": months}))
'''

if __name__ == "__main__":
    for tag, root in (("old", str(D / "old")), ("new", None)):
        work = D / f"w_s7_{tag}"
        shutil.rmtree(work, ignore_errors=True); work.mkdir()
        env = dict(os.environ)
        if root:
            env["PYTHONPATH"] = root
        p = subprocess.run([sys.executable, "-c", RUN % {"work": str(work)}], env=env,
                           capture_output=True, text=True, cwd=str(D))
        out = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else p.stderr[-2000:]
        print(tag, json.dumps(out, indent=1) if isinstance(out, dict) else out)
