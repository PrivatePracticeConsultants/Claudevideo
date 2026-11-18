"""% of Medicare uses the locality of the view's / practice's state (round 3,
P0.8). Before: every screen took median(non_facility_rate) over whatever MPFS
was loaded — "REST OF MISSOURI" applied to a Kansas client — and the Explorer
showed the bare percentage with no anchor named.

Store: payer "Foxtrot Health", 97110 GP professional POS 11:
  TIN M1 (NPI in MO) $45      TIN K1 (NPI in KS) $60
Anchors: MO locality 97110 = $30.00, KS locality 97110 = $40.00.

By hand:
  Explorer state=MO: median 45 -> 45/30 = 150%;  state=KS: 60/40 = 150%.
  No state, both anchors loaded: refused (which locality?) — no %.
  Only MO loaded, state=KS: refused, never 60/30 = 200%.
  Benchmark subject K1 (main state KS), no market state: KS anchor ->
    subject 60/40 = 150%.
  A simple CSV with no state column: used, labelled "does not say which locality".
"""

from __future__ import annotations

import json

import pytest

M1, K1 = "820000001", "820000002"
NM, NK = "1820000001", "1820000002"


def _r(npi, tin, rate):
    return {"provider_groups": [{"npi": [npi], "tin": {"type": "ein", "value": tin}}],
            "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": rate,
                                   "billing_class": "professional", "service_code": ["11"],
                                   "billing_code_modifier": ["GP"],
                                   "expiration_date": "9999-12-31"}]}


FILE = {"reporting_entity_name": "Foxtrot Health", "reporting_entity_type": "issuer",
        "last_updated_on": "2026-05-01", "version": "2.0.0", "provider_references": [],
        "in_network": [{"negotiation_arrangement": "ffs", "name": "x", "billing_code_type": "CPT",
                        "billing_code_type_version": "2026", "billing_code": "97110",
                        "description": "x",
                        "negotiated_rates": [_r(NM, M1, 45.0), _r(NK, K1, 60.0)]}]}


@pytest.fixture
def kf(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from mrfx.ingest import scan_inbox
    (cfg.inbox_dir / "foxtrot_2026-05_in-network.json").write_text(json.dumps(FILE))
    list(scan_inbox(cfg, store))
    store.save_npi(NM, "MO CLINIC", "261QP2000X", "PT clinic", "Town", "MO", entity_type="NPI-2")
    store.save_npi(NK, "KS CLINIC", "261QP2000X", "PT clinic", "Town", "KS", entity_type="NPI-2")
    store.rebuild_rollups()
    return store, TestClient(create_app(cfg, store))


def _load(store, state, rate):
    store.load_mpfs([{"code": "97110", "locality": f"REST OF {state}", "non_facility_rate": rate}],
                    f"test PFS · locality REST OF {state} ({state})", state=state)


def _bycode(c, qs=""):
    s = c.get(f"/api/summary?grain=tin&cpt=97110{qs}").json()
    return s["by_code"][0]["pct_medicare"], s["medicare_anchor"], s["medicare_refusal"]


def test_each_state_uses_its_own_locality(kf):
    store, c = kf
    _load(store, "MO", 30.0)
    _load(store, "KS", 40.0)            # a second state's anchor does not replace MO's
    pct, anchor, refusal = _bycode(c, "&state=MO")
    assert (pct, refusal) == (150, None) and "REST OF MO" in anchor
    pct, anchor, _ = _bycode(c, "&state=KS")
    assert pct == 150 and "REST OF KS" in anchor
    d = c.get("/api/code/97110?grain=tin&state=KS").json()
    assert d["mpfs_rate"] == 40.0 and d["payer_rank"][0]["pct_medicare"] == 150


def test_no_state_with_two_anchors_is_refused_not_guessed(kf):
    store, c = kf
    _load(store, "MO", 30.0)
    _load(store, "KS", 40.0)
    pct, anchor, refusal = _bycode(c)
    assert pct is None and anchor is None and "pick a state" in refusal


def test_a_state_without_its_locality_is_refused(kf):
    store, c = kf
    _load(store, "MO", 30.0)
    pct, _, refusal = _bycode(c, "&state=KS")
    assert pct is None and "not KS" in refusal          # never 60/30 = 200%
    # with no state and one anchor loaded, it is used — and named
    pct, anchor, _ = _bycode(c)
    assert pct == round(100 * 52.5 / 30) and "REST OF MO" in anchor


def test_benchmark_uses_the_subjects_state(kf):
    store, c = kf
    _load(store, "MO", 30.0)
    _load(store, "KS", 40.0)
    r = c.post("/api/benchmark/market", json={
        "subject": K1, "market": {"payers": ["Foxtrot Health"], "codes": ["97110"]}}).json()
    row = r["rows"][0]
    assert (row["subject_rate"], row["subject_pct_medicare"]) == (60.0, 150)
    assert "REST OF KS" in r["mpfs_source"] and r["mpfs_refusal"] is None


def test_a_file_that_does_not_say_its_locality_is_used_and_labelled(kf):
    store, c = kf
    r = c.post("/api/mpfs/upload", files={"file": ("pfs.csv", b"code,non_facility_rate\n97110,35\n")})
    assert r.status_code == 200
    pct, anchor, _ = _bycode(c, "&state=KS")
    assert pct == round(100 * 60 / 35) and "does not say which locality" in anchor
