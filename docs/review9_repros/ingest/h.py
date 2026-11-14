"""Shared harness for review9: cfg/store under review9/ingest/runs/<name>."""
import gzip, json, shutil, sys, os
from pathlib import Path
sys.path.insert(0, "/home/user/Claudevideo")
from mrfx.config import MrfxConfig
from mrfx.store import Store
import mrfx.api as _api
_api.TRUSTED_HOSTS.add("testserver")

BASE = Path("/tmp/mrfx_review9/ingest/runs")

def mkcfg(name, fast=True, fresh=True, **kw):
    root = BASE / name
    if fresh and root.exists():
        shutil.rmtree(root)
    c = MrfxConfig(inbox_dir=root/"inbox", processed_dir=root/"processed", failed_dir=root/"failed",
                   store_dir=root/"store", downloads_dir=root/"downloads",
                   entity_map_path=root/"entity_map.yaml", fast_scan=fast, **kw)
    c.ensure_dirs()
    return c

def put(cfg, fname, obj, gz=False):
    data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    p = cfg.inbox_dir / fname
    p.write_bytes(gzip.compress(data) if gz else data)
    return p

def q(store, sql, *a):
    with store.connect() as con:
        return con.execute(sql, list(a)).fetchall()

HDR = {"reporting_entity_name": "Acme Health", "version": "1.0", "last_updated_on": "2026-05-01"}

def item(code, groups=None, refs=None, prices=None, arr="ffs", ctype="CPT", rate=50.0, ntype="negotiated"):
    prices = prices or [{"negotiated_type": ntype, "negotiated_rate": rate,
                         "billing_class": "professional", "service_code": ["11"]}]
    nr = {"negotiated_prices": prices}
    if groups is not None: nr["provider_groups"] = groups
    if refs is not None: nr["provider_references"] = refs
    return {"negotiation_arrangement": arr, "billing_code_type": ctype, "billing_code": code,
            "negotiated_rates": [nr]}

def grp(npis, tin, ttype="ein"):
    return {"npi": npis, "tin": {"type": ttype, "value": tin}}

def rss_mb():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS"):
                return int(line.split()[1]) / 1024
def peak_mb():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmHWM"):
                return int(line.split()[1]) / 1024
