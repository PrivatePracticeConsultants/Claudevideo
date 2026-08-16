"""Sub-state geography, payer concentration, and steal-share targeting.

Each of these is easy to state too strongly. A city map can turn four
practices into "the market"; an HHI over three ingested payers calls every
market concentrated; a shared-patient count can be read as a referral. The
tests are mostly about those three failure modes.
"""

import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.benchmark import BenchmarkError
from mrfx.territory import (MIN_CELL, local_rate_map, payer_concentration,
                            steal_share)

MARKET = {"month": "2026-06", "therapy_only": False}


def _row(tin, npi, rate, *, payer="Aetna", code="97110"):
    return dict(payer=payer, tin_value=tin, tin_type="ein", npi=npi,
                source_file="a.json", billing_code=code, billing_code_type="CPT",
                discipline="PT", is_timed=True, billing_class="professional",
                negotiated_rate=rate, negotiated_type="negotiated",
                is_dollar_rate=True, billing_code_modifier=[], service_code=["11"],
                file_month="2026-06", last_updated_on="2026-06-01",
                expiration_date=None, schema_version="2.0.0",
                tin_is_really_npi=False, state="MO")


def _cities(store, spec):
    """spec: {city: [(tin, npi, rate), ...]} — one NPI per TIN, all in MO."""
    rows, npis = [], []
    for city, entries in spec.items():
        for tin, npi, rate in entries:
            rows.append(_row(tin, npi, rate))
            npis.append((npi, city))
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk([
        dict(npi=n, org_name=f"Clinic {n[-4:]}", entity_type="NPI-2",
             taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X",
             city=city, state="MO", address="x", zip="63103", phone=None)
        for n, city in npis])
    store.rebuild_rollups()


def _band(city, base, n, start_tin, start_npi):
    return [(str(start_tin + i), str(start_npi + i), base + i) for i in range(n)]


def test_city_medians_rank_inside_a_state(cfg, store):
    _cities(store, {
        "St Louis": _band("St Louis", 60.0, 6, 431000000, 1417594896),
        "Rolla": _band("Rolla", 40.0, 6, 432000000, 1417594996),
    })
    m = local_rate_map(store, "97110", MARKET, state="MO")
    by = {c["city"]: c for c in m["cities"]}
    assert by["ST LOUIS"]["median_rate"] > by["ROLLA"]["median_rate"]
    assert by["ST LOUIS"]["n_practices"] == 6
    assert m["spread_pct"] and m["spread_pct"] > 0
    assert "pays" in m["headline"] and "more than" in m["headline"]
    assert m["cities"][0]["vs_state_pct"] is not None


def test_a_thin_city_is_suppressed_and_counted_not_shown(cfg, store):
    """Four practices are not a city market. Dropping them silently would read
    as 'nothing there'; the count says otherwise."""
    _cities(store, {
        "St Louis": _band("St Louis", 60.0, 6, 431000000, 1417594896),
        "Hamlet": _band("Hamlet", 90.0, MIN_CELL - 1, 433000000, 1417595096),
    })
    m = local_rate_map(store, "97110", MARKET, state="MO")
    assert {c["city"] for c in m["cities"]} == {"ST LOUIS"}
    assert m["suppressed_cities"] == 1, "the thin city is counted, not hidden"


def test_a_sub_state_map_without_a_state_refuses(cfg, store):
    """Pooling cities across states would bury the far larger state effect."""
    _cities(store, {"St Louis": _band("St Louis", 60.0, 6, 431000000, 1417594896)})
    with pytest.raises(BenchmarkError, match="needs a state"):
        local_rate_map(store, "97110", {"month": "2026-06", "therapy_only": False})
    with pytest.raises(BenchmarkError, match="billing code"):
        local_rate_map(store, "", MARKET, state="MO")


def _payer_mix(store, shares):
    """shares: {payer: n_practices}. One code, distinct TINs per payer."""
    rows, npis = [], []
    tin, npi = 431000000, 1417594896
    for payer, n in shares.items():
        for _ in range(n):
            rows.append(_row(str(tin), str(npi), 50.0, payer=payer))
            npis.append(str(npi))
            tin += 1
            npi += 1
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk([
        dict(npi=n, org_name=f"C{n[-4:]}", entity_type="NPI-2",
             taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X",
             city="StL", state="MO", address="x", zip="63103", phone=None)
        for n in npis])
    store.rebuild_rollups()


def test_concentration_is_computed_and_banded(cfg, store):
    """60/10/10/10/10 → HHI = 3600+100*4 = 4000, 'highly concentrated'."""
    _payer_mix(store, {"BigCo": 60, "B": 10, "C": 10, "D": 10, "E": 10})
    c = payer_concentration(store, MARKET)
    assert c["total_relationships"] == 100
    assert c["top_payer"] == "BigCo" and c["top_share_pct"] == 60.0
    assert c["hhi"] == 4000
    assert c["band"] == "highly concentrated" and c["thin"] is False
    assert "60% of the contracted practice relationships" in c["headline"]
    assert "not.*market share" or "NOT" in c["note"]
    assert "covered lives" in c["note"], "the caveat that makes this honest"


def test_too_few_payers_refuses_to_call_a_market_concentrated(cfg, store):
    """With three payers the minimum possible HHI is 3333 — the index would
    call every market concentrated by construction. Say that instead."""
    _payer_mix(store, {"A": 34, "B": 33, "C": 33})
    c = payer_concentration(store, MARKET)
    assert c["thin"] is True
    assert c["band"] is None, "no band, because the number is an artifact"
    assert "too few for a concentration index" in c["headline"]
    assert c["hhi"] is not None, "the raw number is still shown, just unbadged"


def _referral_world(store, tmp_path):
    """A client, two local rivals, and three physicians with different habits."""
    me, rival1, rival2 = "1417594896", "1999999992", "1215555554"
    docs = ["1901234561", "1811223340", "1722334459"]
    rows = [_row("431234567", me, 50.0),
            _row("437654321", rival1, 50.0),
            _row("431111111", rival2, 50.0)]
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk([
        dict(npi=n, org_name=nm, entity_type=et, taxonomy_code=tx,
             taxonomy_codes=tx, city="Springfield", state="MO", address="x",
             zip="65801", phone="4175551212")
        for n, nm, et, tx in (
            (me, "My Therapy", "NPI-2", "261QP2000X"),
            (rival1, "Rival One", "NPI-2", "261QP2000X"),
            (rival2, "Rival Two", "NPI-2", "261QP2000X"),
            (docs[0], "Dr Cold", "NPI-1", "207X00000X"),
            (docs[1], "Dr Warm", "NPI-1", "207X00000X"),
            (docs[2], "Dr Loyal", "NPI-1", "207X00000X"))])
    store.rebuild_rollups()

    from mrfx.medicare import import_shared_patients
    p = tmp_path / "pspp_2015_days30.txt"
    p.write_text(
        f"{docs[0]},{rival1},900,120,3\n"       # sends a lot, none to me
        f"{docs[1]},{rival2},400,60,3\n"        # sends elsewhere...
        f"{docs[1]},{me},100,15,3\n"            # ...and a little to me
        f"{docs[2]},{me},500,80,3\n")           # loyal: only to me
    import_shared_patients(store, p)
    return me, docs


def test_steal_share_ranks_the_referrals_going_elsewhere(cfg, store, tmp_path):
    _referral_world(store, tmp_path)
    r = steal_share(store, "431234567")
    by = {x["npi"]: x for x in r["rows"]}

    assert "1901234561" in by, "the physician sending only to rivals"
    cold = by["1901234561"]
    assert cold["patients_to_me"] == 0 and cold["patients_elsewhere"] == 120
    assert cold["gap"] == 120 and cold["already_a_partner"] is False
    assert cold["share_missed_pct"] == 100.0

    warm = by["1811223340"]
    assert warm["patients_to_me"] == 15 and warm["patients_elsewhere"] == 60
    assert warm["gap"] == 45 and warm["already_a_partner"] is True
    assert warm["share_missed_pct"] == 60.0     # 45 of 75

    assert "1722334459" not in by, "a source that sends only to me is not a target"
    assert r["rows"][0]["npi"] == "1901234561", "biggest gap first"
    assert r["n_cold"] == 1
    assert "send patients to therapy practices" in r["headline"]
    assert "proxy for referral, not a record of one" in r["note"]
    assert r["dataset"] and r["dataset_id"]


def test_steal_share_refuses_a_practice_it_cannot_place(cfg, store, tmp_path):
    _referral_world(store, tmp_path)
    # an unknown subject resolves to its own raw string, so the refusal comes
    # from the NPI lookup — it still has to name the likely cause
    with pytest.raises(BenchmarkError, match="check the practice name"):
        steal_share(store, "not a practice at all")


def test_steal_share_says_why_when_no_referral_release_is_loaded(cfg, store):
    with store.rates_part_writer("a.json") as w:
        w.write_batch([_row("431234567", "1417594896", 50.0)])
    store.save_npis_bulk([dict(
        npi="1417594896", org_name="My Therapy", entity_type="NPI-2",
        taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X", city="StL",
        state="MO", address="x", zip="63103", phone=None)])
    store.rebuild_rollups()
    r = steal_share(store, "431234567")
    assert r["rows"] == [] and "no CMS referral dataset is loaded" in r["reason"]


def test_the_territory_endpoints_answer_and_refuse(cfg, store, tmp_path):
    _referral_world(store, tmp_path)
    c = TestClient(create_app(cfg, store))
    assert c.post("/api/territory/concentration",
                  json={"market": MARKET}).status_code == 200
    assert c.post("/api/territory/steal-share",
                  json={"subject": "431234567"}).json()["count"] >= 1
    # missing state, unknown subject, junk market: refusals, never 500s
    assert c.post("/api/territory/local",
                  json={"code": "97110", "market": MARKET}).status_code == 422
    assert c.post("/api/territory/steal-share",
                  json={"subject": "nobody"}).status_code == 422
    assert c.post("/api/territory/concentration",
                  json={"market": "everything"}).status_code == 422
