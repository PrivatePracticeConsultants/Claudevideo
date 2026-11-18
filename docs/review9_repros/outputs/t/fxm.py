"""Review 9 messy fixture: hand-workable store with multi-state same-name,
mapped entity, two-state TIN, 3 months (Beta skips the middle one, Gamma only
the last, Delta only the first), CQ / KX / institutional / percentage / $0.01
lines on the subject."""
from fx import *  # noqa

S1, S2 = "610000001", "610000002"       # ACME REHAB (MO) / ACME REHAB (KS)  -- same NPPES name, not mapped
M1, M2 = "620000001", "620000002"       # NORTH CLINIC / SOUTH CLINIC (MO)   -- mapped to "BIG GROUP"
B, C, D, E, F, G, H = (f"6{i}0000001" for i in range(3, 10))   # B..H REHAB (MO)
K1, K2, K3 = "710000001", "720000001", "730000001"             # K1..K3 REHAB (KS)
X = "740000001"                         # TWO STATE PT: one NPI in MO, two in KS
ALL = [S1, S2, M1, M2, B, C, D, E, F, G, H, K1, K2, K3, X]
NPIS = {t: ["1" + t] for t in ALL}
NPIS[X] = ["1740000001", "1740000002", "1740000003"]
LABEL = {S1: "ACME REHAB", S2: "ACME REHAB", M1: "NORTH CLINIC", M2: "SOUTH CLINIC",
         B: "B REHAB", C: "C REHAB", D: "D REHAB", E: "E REHAB", F: "F REHAB", G: "G REHAB",
         H: "H REHAB", K1: "K1 REHAB", K2: "K2 REHAB", K3: "K3 REHAB", X: "TWO STATE PT"}
STATE = {t: "MO" for t in ALL}
STATE.update({S2: "KS", K1: "KS", K2: "KS", K3: "KS"})


def pct(v):
    return {"negotiated_type": "percentage", "negotiated_rate": float(v),
            "billing_class": "professional", "service_code": ["11"],
            "billing_code_modifier": ["GP"], "expiration_date": "9999-12-31"}


def inst(v):
    d = price(v)
    d["billing_class"] = "institutional"
    return d


def build(extra_docs=(), entity_map=True):
    cfg, store = mk()
    # ---- Alpha: 3 months
    a04 = {"97110": {S1: 40, B: 45, C: 40, D: 50, E: 60, H: 20}}
    a05 = {"97110": {C: 50, D: 50}}
    a06 = {"97110": {S1: [price(40), price(34, mods=("GP", "CQ")), price(41, mods=("GP", "KX")),
                          inst(100), pct(80), price(0.01)],
                     S2: 60, M1: 44, M2: 52, B: 45, C: 50, D: 55, E: 60, F: 50, G: 50,
                     K1: 70, K2: 72, K3: 74, X: 48},
           "97140": {S1: 30, S2: 36, B: 31, C: 32, D: 33, E: 34, K1: 40},
           "97530": {S1: 50, B: 50, C: 50, D: 50, E: 50}}          # flat
    # ---- Beta: 04 and 06 (skips 05)
    b04 = {"97110": {S1: 42, B: 47, C: 47}}
    b06 = {"97110": {S1: 42, S2: 58, B: 47, C: 47, D: 47, E: 47, M1: 46}}
    # ---- Gamma: 06 only
    g06 = {"97110": {S2: 66, B: 49, K1: 71}}
    # ---- Delta: 04 only
    d04 = {"97110": {B: 39, C: 41}}
    docs = [("alpha_2026-04.json", doc("Alpha Health", "2026-04-01", a04, NPIS)),
            ("alpha_2026-05.json", doc("Alpha Health", "2026-05-01", a05, NPIS)),
            ("alpha_2026-06.json", doc("Alpha Health", "2026-06-01", a06, NPIS)),
            ("beta_2026-04.json", doc("Beta Health", "2026-04-01", b04, NPIS)),
            ("beta_2026-06.json", doc("Beta Health", "2026-06-01", b06, NPIS)),
            ("gamma_2026-06.json", doc("Gamma Health", "2026-06-01", g06, NPIS)),
            ("delta_2026-04.json", doc("Delta Health", "2026-04-01", d04, NPIS)),
            *extra_docs]
    ingest(cfg, store, docs)
    for t in ALL:
        for i, n in enumerate(NPIS[t]):
            st = STATE[t]
            if t == X:
                st = "MO" if i == 0 else "KS"
            name(store, n, LABEL[t], st)
    if entity_map:
        from mrfx.entities import update_entity
        update_entity(cfg, store, "BIG GROUP", add_tins=[M1, M2])
    store.rebuild_rollups()
    return cfg, store
