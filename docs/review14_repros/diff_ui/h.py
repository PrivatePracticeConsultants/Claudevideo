"""Shared helpers for the diff_ui audit repros: a small hand-built scratch store
(the same shapes tests/mrfx/test_review13.py uses). Never touches a real store."""
import json, sys, shutil, tempfile
from pathlib import Path
sys.path.insert(0, "/home/user/Claudevideo")
import mrfx.api as _api
_api.TRUSTED_HOSTS.add("testserver")
from mrfx.config import MrfxConfig
from mrfx.store import Store
from mrfx.ingest import scan_inbox
CLINIC = "261QP2000X"

def mkcfg(root: Path, **kw):
    root = Path(root)
    c = MrfxConfig(inbox_dir=root/"inbox", processed_dir=root/"processed", failed_dir=root/"failed",
                   store_dir=root/"store", downloads_dir=root/"downloads",
                   entity_map_path=root/"entity_map.yaml", confirm_over_gb=5.0, **kw)
    c.ensure_dirs()
    return c

def price(rate, mods=("GP",)):
    return {"negotiated_type": "negotiated", "negotiated_rate": float(rate),
            "billing_class": "professional", "service_code": ["11"],
            "billing_code_modifier": list(mods), "expiration_date": "9999-12-31"}

def doc(payer, day, prices, npis):
    items = []
    for code, by_tin in prices.items():
        nr = [{"provider_groups": [{"npi": npis[t], "tin": {"type": "ein", "value": t}}],
               "negotiated_prices": [price(r)]} for t, r in by_tin.items()]
        items.append({"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                      "billing_code": code, "negotiated_rates": nr})
    return {"reporting_entity_name": payer, "last_updated_on": day, "version": "2.0.0",
            "in_network": items}

def market(cfg, store, payer, prefix, n, codes=("97110", "97112", "97140", "97530"),
           day="2026-06-01", names=None, base=40.0, state="MO"):
    T = [f"{prefix}{i:04d}" for i in range(1, n + 1)]
    npis = {t: [f"1{t}"] for t in T}
    d = doc(payer, day, {c: {t: base + (i % 37) + j for i, t in enumerate(T)}
                         for j, c in enumerate(codes)}, npis)
    (cfg.inbox_dir / f"{prefix}_{day[:7]}.json").write_text(json.dumps(d))
    out = scan_inbox(cfg, store)
    for i, t in enumerate(T):
        nm = (names[i] if names and i < len(names) else f"{payer.upper()} P{i+1} REHAB")
        store.save_npi(npis[t][0], nm, CLINIC, "x", "Kansas City", state,
                       entity_type="NPI-2", zip_code="64105")
    return T, out

def small_store(root, n=40):
    cfg = mkcfg(root)
    store = Store(cfg.store_dir)
    A = market(cfg, store, "Alpha Health", "71", n, day="2026-05-01")[0]
    market(cfg, store, "Alpha Health", "71", n, day="2026-06-01", base=42.0)
    market(cfg, store, "Beta Health", "71", n, day="2026-06-01", base=45.0)
    market(cfg, store, "Gamma Health", "71", n, day="2026-06-01", base=50.0)
    store.rebuild_rollups()
    return cfg, store, A
