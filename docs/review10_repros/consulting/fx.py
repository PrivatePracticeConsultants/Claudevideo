"""Tiny-store builder for the audit (mirrors tests/mrfx/test_known_answer_leads.py)."""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, "/home/user/Claudevideo")
import mrfx.api as api
api.TRUSTED_HOSTS.add("testserver")
from mrfx.config import MrfxConfig
from mrfx.store import Store

PT, CLINIC, MD = "225100000X", "261QP2000X", "207R00000X"

def mk(tmp=None):
    Path("/tmp/mrfx_review10/consulting/tmp").mkdir(parents=True, exist_ok=True)
    tmp = Path(tmp or tempfile.mkdtemp(dir="/tmp/mrfx_review10/consulting/tmp"))
    c = MrfxConfig(inbox_dir=tmp/"inbox", processed_dir=tmp/"processed", failed_dir=tmp/"failed",
                   store_dir=tmp/"store", downloads_dir=tmp/"downloads",
                   entity_map_path=tmp/"entity_map.yaml", confirm_over_gb=5.0)
    c.ensure_dirs()
    return c, Store(c.store_dir)

def price(rate, mods=("GP",), pos=("11",)):
    return {"negotiated_type": "negotiated", "negotiated_rate": float(rate),
            "billing_class": "professional", "service_code": list(pos),
            "billing_code_modifier": list(mods), "expiration_date": "9999-12-31"}

def doc(payer, day, prices, npis):
    """prices: {code: {tin: rate | [price-dicts]}}; npis: {tin: [npi,...]}"""
    items = []
    for code, by_tin in prices.items():
        nr = []
        for tin, r in by_tin.items():
            ps = r if isinstance(r, list) else [price(r)]
            nr.append({"provider_groups": [{"npi": npis[tin], "tin": {"type": "ein", "value": tin}}],
                       "negotiated_prices": ps})
        items.append({"negotiation_arrangement": "ffs", "name": code, "billing_code_type": "CPT",
                      "billing_code_type_version": "2026", "billing_code": code,
                      "description": code, "negotiated_rates": nr})
    return {"reporting_entity_name": payer, "reporting_entity_type": "issuer",
            "last_updated_on": day, "version": "2.0.0", "provider_references": [],
            "in_network": items}

def ingest(cfg, store, docs):
    from mrfx.ingest import scan_inbox
    for name, d in docs:
        (cfg.inbox_dir / name).write_text(json.dumps(d))
    res = scan_inbox(cfg, store)
    assert all(r["status"] == "done" for r in res), res

def name(store, npi, label, state, tax=CLINIC, etype="NPI-2", city="Town", zipc="64105"):
    store.save_npi(npi, label, tax, "x", city, state, entity_type=etype, zip_code=zipc)

def client(cfg, store):
    from fastapi.testclient import TestClient
    from mrfx.api import create_app
    return TestClient(create_app(cfg, store))
