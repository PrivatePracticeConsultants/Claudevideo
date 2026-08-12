"""The consultant's-book features: client watchlist digest, proposal builder,
referral leaderboard (ZIP+radius), backup/verify."""

import pytest

from mrfx.benchmark import BenchmarkError, compute_rate_proposal, render_proposal_report
from mrfx.clients import add_client, client_digest, remove_client, watchlist

GW = "431234567"            # subject: Gateway (2 NPIs)
P1, P2 = "437654321", "439988776"
GW_N = ["1417594896", "1234567893"]
DOCS = ["1901234561", "1811223340"]
# five peers, because contract_gaps' tab default needs >=5 peers pricing a code
EXTRA_PEERS = [("434440001", "1876543219"), ("434440002", "1765432196"),
               ("434440003", "1654321987")]


def _seed(store, months=("2026-05", "2026-06")):
    rows = []
    for month in months:
        for tin, npis, mult in ((GW, GW_N, 1.0), (P1, ["1999999992"], 1.15),
                                (P2, ["1667788990"], 0.9),
                                *((t, [n], 1.02) for t, n in EXTRA_PEERS)):
            for npi in npis:
                for payer in ("Aetna", "BCBS"):
                    codes = {"97110": 30.0, "97140": 28.0}
                    if tin != GW:
                        codes["97530"] = 36.0     # peers price it; subject doesn't
                    for code, base in codes.items():
                        rate = base * mult * (1.1 if payer == "BCBS" else 1.0)
                        # the subject's 97110/Aetna moves +10% in the new month
                        if (tin, code, payer) == (GW, "97110", "Aetna") \
                                and month == months[-1]:
                            rate *= 1.10
                        rows.append(dict(
                            payer=payer, tin_value=tin, tin_type="ein", npi=npi,
                            source_file=f"s_{month}.json", billing_code=code,
                            billing_code_type="CPT", discipline="PT", is_timed=True,
                            billing_class="professional",
                            negotiated_rate=round(rate, 2),
                            negotiated_type="negotiated", is_dollar_rate=True,
                            billing_code_modifier=[], service_code=["11"],
                            file_month=month, last_updated_on=f"{month}-01",
                            expiration_date=None, schema_version="2.0.0",
                            tin_is_really_npi=False, state=None))
    for month in months:
        with store.rates_part_writer(f"s_{month}.json") as w:
            w.write_batch([r for r in rows if r["file_month"] == month])
    store.save_npis_bulk(
        [dict(npi=n, org_name="Gateway Therapy", entity_type="NPI-2",
              taxonomy_code="261QP2000X", city="StL", state="MO", address="1 Main",
              zip="63103", phone=None) for n in GW_N] +
        [dict(npi="1999999992", org_name="Peer One", entity_type="NPI-2",
              taxonomy_code="261QP2000X", city="StC", state="MO", address="2 Oak",
              zip="63301", phone=None),
         dict(npi="1667788990", org_name="Peer Two", entity_type="NPI-2",
              taxonomy_code="261QP2000X", city="Spr", state="MO", address="3 Elm",
              zip="65806", phone=None)] +
        [dict(npi=n, org_name=f"Extra Peer {i}", entity_type="NPI-2",
              taxonomy_code="261QP2000X", city="StL", state="MO", address="5 Firs",
              zip="63110", phone=None) for i, (_t, n) in enumerate(EXTRA_PEERS)] +
        [dict(npi=d, org_name=f"Dr Ref {i}", entity_type="NPI-1",
              taxonomy_code="207X00000X", city="StL", state="MO", address="4 Ash",
              zip="63110", phone=None) for i, d in enumerate(DOCS)])
    store.rebuild_rollups()


# ---------------------------------------------------------------- proposal --

def test_proposal_math_is_reproducible_and_floored(store):
    _seed(store)
    market = {"month": "2026-06", "state": "MO"}
    prop = compute_rate_proposal(store, GW, "Aetna", market,
                                 {"kind": "p50"}, volumes={"97140": 1000})
    rows = {r["billing_code"]: r for r in prop["rows"]}
    # 97110 moved +10%: subject 33.0 vs peers' median — above it, so KEPT
    # (a proposal must never ask to go DOWN)
    r110 = rows["97110"]
    assert r110["proposed"] >= r110["current"]
    # 97140: subject 28.0; five peers' medians [32.2, 25.2, 28.56 x3]
    # -> p50 = 28.56 (reproducible from the seed by hand)
    r140 = rows["97140"]
    assert r140["current"] == 28.0 and r140["proposed"] == 28.56
    assert r140["delta"] == 0.56 and r140["delta_pct"] == 2.0
    assert r140["annual_value"] == 560.0            # 0.56 x 1000 units
    assert prop["summary"]["total_annual_value"] == 560.0
    assert prop["summary"]["has_volumes"] is True

    # % of Medicare without MPFS loaded is a refusal, not a zero
    with pytest.raises(BenchmarkError, match="MPFS"):
        compute_rate_proposal(store, GW, "Aetna", market,
                              {"kind": "pct_medicare", "value": 110})
    store.load_mpfs([{"code": "97110", "non_facility_rate": 31.0},
                     {"code": "97140", "non_facility_rate": 27.0}], "test MPFS")
    prop2 = compute_rate_proposal(store, GW, "Aetna", market,
                                  {"kind": "pct_medicare", "value": 120})
    rows2 = {r["billing_code"]: r for r in prop2["rows"]}
    assert rows2["97140"]["proposed"] == round(27.0 * 1.2, 2)   # 32.40
    # 97110 at 120% MCR = 37.20 > current 33.0 -> raised to exactly that
    assert rows2["97110"]["proposed"] == round(31.0 * 1.2, 2)
    # implausible targets refused
    for bad in ({"kind": "p90"}, {"kind": "pct_medicare", "value": 999},
                {"kind": "pct_medicare", "value": "x"}):
        with pytest.raises(BenchmarkError):
            compute_rate_proposal(store, GW, "Aetna", market, bad)


def test_proposal_report_renders_with_basis_and_methodology(cfg, store):
    _seed(store)
    prop = compute_rate_proposal(store, GW, "Aetna",
                                 {"month": "2026-06", "state": "MO"},
                                 {"kind": "p50"}, volumes={"97140": 1000})
    html = render_proposal_report(cfg, store, prop)
    assert "Rate proposal" in html and "Aetna" in html
    assert "median" in html            # the stated basis
    assert "METHODOLOGY" in html and "never fall below the" in html
    assert "$560.00" in html           # the volume-priced ask
    with pytest.raises(BenchmarkError, match="pinned as-of month"):
        render_proposal_report(cfg, store, {**prop, "market": {}})


# ---------------------------------------------------------------- watchlist --

def test_watchlist_digest_composes_the_tabs(cfg, store, tmp_path):
    _seed(store)
    assert watchlist(store) == []
    add_client(store, "Gateway Therapy")
    add_client(store, "No Such Practice LLC")
    assert watchlist(store) == ["Gateway Therapy", "No Such Practice LLC"]
    with pytest.raises(BenchmarkError):
        add_client(store, "   ")

    # medicare layers so the referral-alert column lights up: a source who
    # loses Part B between two roster snapshots
    from mrfx.medicare import import_orf_roster, import_shared_patients
    v1 = tmp_path / "OrderReferring_2026-08-01.csv"
    v1.write_text("NPI,LAST_NAME,FIRST_NAME,PARTB,DME,HHA,PMD,HOSPICE\n"
                  + "".join(f"{d},REF{i},ANNA,Y,N,N,N,N\n"
                            for i, d in enumerate(DOCS)))
    import_orf_roster(store, v1)
    v2 = tmp_path / "OrderReferring_2026-08-09.csv"
    v2.write_text("NPI,LAST_NAME,FIRST_NAME,PARTB,DME,HHA,PMD,HOSPICE\n"
                  f"{DOCS[0]},REF0,ANNA,Y,N,N,N,N\n"
                  f"{DOCS[1]},REF1,ANNA,N,N,N,N,N\n")
    import_orf_roster(store, v2)
    hop = tmp_path / "hop_2022.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   + "".join(f"{d},{GW_N[0]},{50 + i},200,12.0,3.0\n"
                             for i, d in enumerate(DOCS)))
    import_shared_patients(store, hop)

    d = client_digest(store)
    assert d["can_diff"] and d["month"] == "2026-06" and d["have_medicare"]
    by = {c["subject"]: c for c in d["clients"]}
    gw = by["Gateway Therapy"]
    assert gw["error"] is None
    # the +10% move on 97110/Aetna is the headline
    assert gw["changes"]["n"] >= 1
    assert gw["changes"]["biggest_code"] == "97110"
    assert gw["changes"]["biggest_payer"] == "Aetna"
    assert 9.5 <= gw["changes"]["biggest_pct"] <= 10.5
    # peers price 97530; the client doesn't -> a gap
    assert "97530" in gw["gaps"]["top"]
    # the Part B loss lands on the digest row with the source's name
    assert [x["kind"] for x in gw["lost_referrers"]] == ["lost_partb"]
    assert gw["lost_referrers"][0]["name"] == "Dr Ref 1"
    # the unknown client is an error CELL, not a dead digest
    bad = by["No Such Practice LLC"]
    assert bad["error"] and "no longer matches" in bad["error"]

    remove_client(store, "No Such Practice LLC")
    assert watchlist(store) == ["Gateway Therapy"]

    # API round-trip
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    client = TestClient(create_app(cfg, store))
    assert client.get("/api/clients").json()["clients"] == ["Gateway Therapy"]
    assert client.post("/api/clients", json={"subject": "Peer One"}).status_code == 200
    dig = client.get("/api/clients/digest").json()
    assert {c["subject"] for c in dig["clients"]} == {"Gateway Therapy", "Peer One"}
    assert client.post("/api/clients", json={"subject": ""}).status_code == 422


# ---------------------------------------------------------------- leaders --

def test_referral_leaders_zip_radius_and_honesty(cfg, store, tmp_path):
    from mrfx.medicare import (MedicareImportError, import_shared_patients,
                               referral_leaders)

    _seed(store)
    hop = tmp_path / "hop_2022.csv"
    # inbound to Gateway (63103), Peer One (63301, ~21 mi), Peer Two (65806,
    # ~190 mi), an orthopedist (non-therapy), and one practice with a ZIP the
    # centroid list doesn't know
    store.save_npis_bulk([dict(npi="1555666770", org_name="Nowhere Rehab",
                               entity_type="NPI-2", taxonomy_code="261QP2000X",
                               city="X", state="MO", address="9", zip="00000",
                               phone=None)])
    # every pair touches a store NPI (the import keeps nothing else): the
    # doc and the unmappable-ZIP practice enter via a store-NPI source
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   f"{DOCS[0]},{GW_N[0]},200,900,12.0,3.0\n"
                   f"{DOCS[0]},1999999992,120,500,12.0,3.0\n"
                   f"{DOCS[0]},1667788990,80,300,12.0,3.0\n"
                   f"{GW_N[1]},{DOCS[0]},60,200,12.0,3.0\n"        # -> a physician
                   f"{GW_N[1]},1555666770,40,150,12.0,3.0\n")
    import_shared_patients(store, hop)

    centroids = tmp_path / "zcta.csv"
    centroids.write_text("zip,lat,lon\n"
                         "63103,38.6333,-90.2190\n"
                         "63301,38.7920,-90.4885\n"
                         "65806,37.2058,-93.2923\n"
                         "63110,38.6238,-90.2585\n")

    # unscoped: therapy practices only, ranked by patients received
    d = referral_leaders(store, therapy_only=True, centroids_path=centroids)
    names = [r["practice"] for r in d["rows"]]
    assert names == ["Gateway Therapy", "Peer One", "Peer Two", "Nowhere Rehab"]
    assert d["rows"][0]["in_store"] and d["rows"][0]["tin"] == GW
    assert d["rows"][3]["in_store"] is False    # no MRF rates -> honest badge
    assert "Dr Ref 0" not in names          # 207X receives pairs but isn't therapy
    allkinds = [r["practice"] for r in referral_leaders(
        store, therapy_only=False, centroids_path=centroids)["rows"]]
    assert "Dr Ref 0" in allkinds           # untick the filter and they appear

    # radius: 25 miles of 63103 keeps Gateway + Peer One, drops Peer Two;
    # the unmappable-ZIP practice is EXCLUDED AND COUNTED, never silent
    d25 = referral_leaders(store, zip_code="63103", radius_miles=25,
                           centroids_path=centroids)
    assert [r["practice"] for r in d25["rows"]] == ["Gateway Therapy", "Peer One"]
    assert d25["rows"][1]["miles"] == pytest.approx(18.2, abs=0.5)   # haversine of the fixture coords
    assert d25["unplaced"] == 1 and d25["total"] == 4

    with pytest.raises(MedicareImportError, match="5-digit ZIP"):
        referral_leaders(store, zip_code="6310", radius_miles=25,
                         centroids_path=centroids)
    with pytest.raises(MedicareImportError, match="centroid"):
        referral_leaders(store, zip_code="99999", radius_miles=25,
                         centroids_path=centroids)

    # API shape (bundled centroid file)
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    client = TestClient(create_app(cfg, store))
    r = client.post("/api/medicare/leaders",
                    json={"zip": "63103", "radius_miles": 25})
    assert r.status_code == 200
    got = r.json()
    assert got["rows"][0]["practice"] == "Gateway Therapy"
    assert client.post("/api/medicare/leaders",
                       json={"zip": "abcde", "radius_miles": 25}).status_code == 422
