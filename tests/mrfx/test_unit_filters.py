"""Practice-attribute filters find their practices first (round 5, P1.4) and
the Explorer's spine-only strip lives for a data generation (P1.3).

P1.4: state / city / therapy / name and partial-ID search used to be tested
AFTER three dimension tables were joined onto every spine row (2.9 s / 3.9 s
at 99M rates). rel_sql now computes the practice columns once per (TIN,
NPI-in-slot flag), keeps the passing practices, and reads only their rows.
The answer must be the old one, row for row and column for column: each case
here is checked against the old formulation (joined, then filtered), kept
below as the reference. The same comparison ran over the full relation of a
1.4M-row named store and a 79M-row synthetic one (21 cases each: IDENTICAL)."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

import pytest

PT, CLINIC, MD = "225100000X", "261QP2000X", "207R00000X"
TINS = {"A": "610000001", "B": "620000001", "C": "630000001", "M": "640000001"}
SOLO = "1880000001"                   # an NPI the payer published in the TIN slot
NPIS = {"A": [("1610000001", "NPI-2", CLINIC, "MO", "KANSAS CITY"),
              ("1610000002", "NPI-1", PT, "MO", "KANSAS CITY")],
        "B": [("1620000001", "NPI-2", CLINIC, "MO", "SAINT LOUIS")],
        "C": [("1630000001", "NPI-2", CLINIC, "KS", "OVERLAND PARK"),
              ("1630000002", "NPI-1", PT, "MO", "KANSAS CITY")],
        "M": [("1640000001", "NPI-1", MD, "MO", "SAINT LOUIS")]}
JUNE = {"97110": dict(A=30, B=40, C=50, M=5, S=44), "97140": dict(A=25, B=900, C=45, S=41)}
MAY = {"97110": dict(A=20, B=40, C=55, S=39)}


def _file(payer, day, prices):
    def group(k):
        if k == "S":
            return {"npi": [SOLO], "tin": {"type": "npi", "value": SOLO}}
        return {"npi": [n for n, *_ in NPIS[k]], "tin": {"type": "ein", "value": TINS[k]}}
    return {"reporting_entity_name": payer, "reporting_entity_type": "issuer",
            "last_updated_on": day, "version": "2.0.0", "provider_references": [],
            "in_network": [{
                "negotiation_arrangement": "ffs", "name": code, "billing_code_type": "CPT",
                "billing_code_type_version": "2026", "billing_code": code, "description": code,
                "negotiated_rates": [{"provider_groups": [group(k)], "negotiated_prices": [{
                    "negotiated_type": "negotiated", "negotiated_rate": float(rate),
                    "billing_class": "professional", "service_code": ["11"],
                    "billing_code_modifier": ["GP"], "expiration_date": "9999-12-31"}]}
                    for k, rate in by.items()]} for code, by in prices.items()]}


@pytest.fixture
def world(cfg, store):
    from mrfx.ingest import scan_inbox
    for name, doc in (("rho_2026-05_in-network.json", _file("Rho Health", "2026-05-01", MAY)),
                      ("rho_2026-06_in-network.json", _file("Rho Health", "2026-06-01", JUNE))):
        (cfg.inbox_dir / name).write_text(json.dumps(doc))
    assert all(r["status"] == "done" for r in scan_inbox(cfg, store))
    for k, npis in NPIS.items():
        for npi, et, tax, st, city in npis:
            store.save_npi(npi, f"{k} REHAB" if et == "NPI-2" else f"CLINICIAN {npi}",
                           tax, "x", city, st, entity_type=et, zip_code="64105")
    store.save_npi(SOLO, "SOLO THERAPY", PT, "x", "KANSAS CITY", "MO",
                   entity_type="NPI-1", zip_code="64105")
    store.set_entity_map({TINS["A"]: "ALPHA GROUP", TINS["B"]: "ALPHA GROUP"})
    store.rebuild_rollups()
    return store


def _old_rel(grain, fs):
    """rel_sql for the TIN / entity grains before round 5: join, then filter."""
    import mrfx.api as api

    def sub(w):
        return (w.replace(api._IDENT_TOKEN, f"({api._ident_clause(grain, fs.ident_kind)})")
                if fs.ident_kind else w)
    tin_rel = api._TIN_REL_LATEST if fs.latest_only else api._TIN_REL
    if grain == "entity":
        inner = f"SELECT * FROM ({tin_rel}) WHERE {fs.where_of('key', 'unit')}"
        base = (f"SELECT * FROM ({api._entity_rel(inner, fs.latest_only)}) "
                f"WHERE {sub(fs.where_of('ident', 'row'))}")
    else:
        base = f"WITH base AS ({tin_rel}) SELECT * FROM base WHERE {sub(fs.where)}"
    return api._outlier_band(base) if fs.hide_outliers else base


CASES = [  # (grain, query, rows expected — a floor, so a case can't pass empty)
    ("tin", "state=MO", 1), ("tin", "state=KS", 1), ("tin", "latest=1&state=MO", 1),
    ("tin", "city=kansas city", 1), ("tin", "therapy_only=1", 1),
    ("tin", "latest=1&therapy_only=1&state=MO&cpt=97110", 1),
    ("tin", "q=rehab", 1), ("tin", "q=solo", 1), ("tin", "q=alpha", 1), ("tin", "q=6200", 1),
    ("tin", f"q={TINS['A']}&state=MO", 1), ("tin", f"q={SOLO}&city=Kansas City", 1),
    ("tin", "latest=1&state=MO&rate_max=45&hide_outliers=1", 1),
    ("tin", "state=MO&dollar_only=0&month=2026-05", 1), ("tin", "hide_tin_npi=1&state=MO", 1),
    ("tin", "state=ZZ", 0), ("tin", "therapy_only=1&q=1880000001", 1),
    ("entity", "state=MO", 1), ("entity", "latest=1&state=KS", 1),
    ("entity", "latest=1&therapy_only=1", 1), ("entity", "city=Saint Louis&q=alpha", 1),
    ("entity", "month=2026-06&state=MO&rate_min=30", 1),
]


@pytest.mark.parametrize("grain,qs,floor", CASES)
def test_finding_practices_first_is_the_same_answer(world, grain, qs, floor):
    import mrfx.api as api
    fs = api.FilterSet(dict(parse_qsl(qs)))
    new_sql = api.rel_sql(grain, fs)
    if fs.selective_dims and not fs.codes and (fs.has("unit") or (grain == "tin" and not fs.ident_kind)):
        assert "SEMI JOIN" in new_sql, "the case must exercise the rewrite"
    if not fs.selective_dims or fs.codes:
        assert "SEMI JOIN" not in new_sql
    with world.connect() as con:
        new = con.execute(f"SELECT * FROM ({new_sql})", fs.params).fetchall()
        old = con.execute(f"SELECT * FROM ({_old_rel(grain, fs)})", fs.params).fetchall()
    assert sorted(map(repr, new)) == sorted(map(repr, old))
    assert len(new) >= floor


def test_the_strip_off_the_spine_lives_for_the_generation(world, cfg, monkeypatch):
    """Unfiltered (spine-only) strip: computed once per data generation, not
    every 30 s. A joined view (state) still expires. The Medicare % is applied
    per request, so an MPFS import shows at once without a new generation."""
    from fastapi.testclient import TestClient

    import mrfx.api as api
    c = TestClient(api.create_app(cfg, world))
    scans = {"n": 0}
    real = api.stat_rel

    def counting(*a, **k):
        scans["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(api, "stat_rel", counting)
    monkeypatch.setattr(api, "SUMMARY_TTL_SECONDS", 0.0)     # every TTL entry is stale

    first = c.get("/api/summary?grain=tin").json()
    assert scans["n"] == 1 and first["n"] > 0 and first["mpfs_loaded"] is False
    world.load_mpfs([{"code": "97110", "non_facility_rate": 31.0}], source="test MPFS",
                    state="MO")
    again = c.get("/api/summary?grain=tin").json()
    assert scans["n"] == 1, "same generation: no rescan"
    assert {k: again[k] for k in ("n", "median", "entities")} == {
        k: first[k] for k in ("n", "median", "entities")}
    assert again["mpfs_loaded"] and any(x["pct_medicare"] for x in again["by_code"])

    c.get("/api/summary?grain=tin&state=MO")
    c.get("/api/summary?grain=tin&state=MO")
    assert scans["n"] == 3, "a joined view keeps its TTL"
    world.rebuild_rollups()                                  # new generation
    c.get("/api/summary?grain=tin")
    assert scans["n"] == 4
