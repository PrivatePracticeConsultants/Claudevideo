"""Shared harness for review 11 (ingest): cfg/store under /tmp/mrfx_review11/ingest/runs/<name>."""
import gzip, json, shutil, sys, os, io, zipfile, time
from pathlib import Path
sys.path.insert(0, "/home/user/Claudevideo")
from mrfx.config import MrfxConfig
from mrfx.store import Store
import mrfx.api as _api
_api.TRUSTED_HOSTS.add("testserver")

BASE = Path("/tmp/mrfx_review11/ingest/runs")
LOGS = Path("/tmp/mrfx_review11/ingest/logs")
BASE.mkdir(parents=True, exist_ok=True); LOGS.mkdir(parents=True, exist_ok=True)


def mkcfg(name, fast=True, fresh=True, **kw):
    root = BASE / name
    if fresh and root.exists():
        shutil.rmtree(root)
    c = MrfxConfig(inbox_dir=root/"inbox", processed_dir=root/"processed", failed_dir=root/"failed",
                   store_dir=root/"store", downloads_dir=root/"downloads",
                   entity_map_path=root/"entity_map.yaml", fast_scan=fast,
                   enrichment={"mode": "off"}, **kw)
    c.ensure_dirs()
    return c


def put(cfg, fname, obj, gz=False, where=None):
    data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    p = (where or cfg.inbox_dir) / fname
    p.write_bytes(gzip.compress(data) if gz else data)
    return p


def q(store, sql, *a):
    with store.connect() as con:
        return con.execute(sql, list(a)).fetchall()


HDR = {"reporting_entity_name": "Acme Health", "version": "1.0.0", "last_updated_on": "2026-05-01"}


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


def doc(n=3, codes=("97110", "97112", "97140"), payer="Acme Health", month="2026-05-01", seed=0,
        pad_mb=0.0, inline=True):
    """n items of target codes with inline groups; pad_mb adds a non-target item with a big description."""
    items = []
    for i in range(n):
        c = codes[i % len(codes)]
        items.append(item(c, groups=[grp([str(1000000000 + seed * 100000 + i)], f"{100000000 + seed * 1000 + i}")],
                          rate=10.0 + i + seed))
    if pad_mb:
        items.append({**item("99213", groups=[grp(["1234567893"], "123456789")]),
                      "description": "x" * int(pad_mb * 1e6)})
    return {"reporting_entity_name": payer, "version": "1.0.0", "last_updated_on": month,
            "in_network": items}


def zipped(members: dict, inner_zip: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in members.items():
            z.writestr(k, v)
        if inner_zip:
            z.writestr("inner.zip", zipped(inner_zip))
    return buf.getvalue()


def files_rows(store):
    return q(store, "SELECT filename, status, file_type, rows_emitted, error FROM files ORDER BY 1")


def rate_counts(store):
    return dict(q(store, "SELECT source_file, count(*) FROM rates GROUP BY 1 ORDER BY 1"))


def where_is(cfg, name):
    out = []
    for d, tag in ((cfg.inbox_dir, "inbox"), (cfg.processed_dir, "processed"), (cfg.failed_dir, "failed"),
                   (cfg.inbox_dir / ".held", "held"), (cfg.inbox_dir / ".replacing", "replacing")):
        if (d / name).exists():
            out.append(tag)
    return "+".join(out) or "gone"


def parts(store):
    return sorted(p.name for p in store.rates_dir.iterdir())


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
