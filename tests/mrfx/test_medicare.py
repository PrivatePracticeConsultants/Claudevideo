"""The two CMS layers MRFs cannot supply: order/refer eligibility, and
shared-patient (referral-structure) pairs."""

import pytest

from mrfx.medicare import (MedicareImportError, import_orf_roster,
                           import_shared_patients, medicare_status,
                           npi_eligibility, org_referrals)

MINE = ["1417594896", "1234567893"]
DOCS = ["1901234567", "1811223344", "1722334455"]


def _seed_rates(store):
    rows = [dict(
        payer=p, tin_value="431234567", tin_type="ein", npi=npi, source_file="m.json",
        billing_code=c, billing_code_type="CPT", discipline="PT", is_timed=True,
        billing_class="professional", negotiated_rate=55.0, negotiated_type="negotiated",
        is_dollar_rate=True, billing_code_modifier=[], service_code=["11"],
        file_month="2026-06", last_updated_on="2026-06-01", expiration_date=None,
        schema_version="2.0.0", tin_is_really_npi=False, state=None)
        for npi in MINE for p in ("Aetna", "BCBS") for c in ("97110", "92507")]
    with store.rates_part_writer("m.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk(
        [dict(npi=n, org_name="Gateway Therapy", entity_type="NPI-2",
              taxonomy_code="261QP2000X", city="StL", state="MO", address="1 Main",
              zip="63101", phone=None) for n in MINE] +
        [dict(npi=d, org_name=f"Dr Ortho {i}", entity_type="NPI-1",
              taxonomy_code="207X00000X", city="StL", state="MO", address="2 Oak",
              zip="63101", phone=None) for i, d in enumerate(DOCS)])
    store.rebuild_rollups()


def _orf(tmp_path, name="OrderReferring_2026-07-17.csv"):
    p = tmp_path / name
    p.write_text("NPI,LAST_NAME,FIRST_NAME,PARTB,DME,HHA,PMD,HOSPICE\n"
                 + "".join(f"{d},DOC{i},ANNA,Y,{'Y' if i == 0 else 'N'},N,N,N\n"
                           for i, d in enumerate(DOCS)))
    return p


def test_orf_roster_loads_and_refuses_a_wrong_layout(store, tmp_path):
    r = import_orf_roster(store, _orf(tmp_path))
    assert r["providers"] == len(DOCS)
    assert r["release"] == "2026-07-17"          # parsed from the tracker's filename

    flags = {e["npi"]: e for e in npi_eligibility(store, DOCS)}
    assert all(flags[d]["on_list"] and flags[d]["partb"] for d in DOCS)
    assert flags[DOCS[0]]["dme"] is True and flags[DOCS[1]]["dme"] is False

    # A practice's OWN NPIs are absent from this roster by design (it lists
    # order/refer-eligible providers; therapists and orgs are not on it). That
    # must read as "not on the list", never as an error.
    assert all(e["on_list"] is False for e in npi_eligibility(store, MINE))

    bad = tmp_path / "wrong.csv"
    bad.write_text("NPI,NAME,FLAG\n1234567893,x,Y\n")
    with pytest.raises(MedicareImportError, match="Order & Referring layout"):
        import_orf_roster(store, bad)


def test_both_pair_formats_map_their_columns_correctly(store, tmp_path):
    """The two deliveries put the shared-PATIENT count in DIFFERENT positions:
    field 4 in the headerless CMS file, field 3 in Hop Teaming. Reading them
    positionally as if they matched would silently report transaction counts
    as patients."""
    _seed_rates(store)

    cms = tmp_path / "cms_2015.csv"           # npi1,npi2,pair,bene,sameday
    cms.write_text("".join(f"{d},{MINE[0]},{300 + i},{40 + i},3\n"
                           for i, d in enumerate(DOCS))
                   + "5555555555,6666666666,900,90,0\n")   # unrelated pair
    r = import_shared_patients(store, cms)
    assert r["format"] == "cms-shared-patient" and r["year"] == "2015"
    # the unrelated pair touches none of this store's NPIs and is dropped
    assert r["pairs"] == len(DOCS)

    hop = tmp_path / "docgraph_2022.csv"      # header; patients in field 3
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   + "".join(f"{d},{MINE[1]},{70 + i},{500 + i},12.5,3.1\n"
                             for i, d in enumerate(DOCS)))
    r2 = import_shared_patients(store, hop)
    assert r2["format"] == "hop-teaming" and r2["year"] == "2022"

    # CMS: patients must be the BENE column (40..42), not the pair column (300+)
    cms_rows = {x["npi"]: x for x in
                org_referrals(store, MINE, "in", year="2015")["rows"]}
    assert {cms_rows[d]["patients"] for d in DOCS} == {40, 41, 42}
    # Hop: patients must be patient_count (70..72), not transaction_count (500+)
    hop_rows = {x["npi"]: x for x in
                org_referrals(store, MINE, "in", year="2022")["rows"]}
    assert {hop_rows[d]["patients"] for d in DOCS} == {70, 71, 72}
    assert {hop_rows[d]["transactions"] for d in DOCS} == {500, 501, 502}


def test_vintages_are_never_blended(store, tmp_path):
    """Different releases measure different things over different windows — the
    free CMS file is Jan-Sep 2015, a Hop Teaming year is a full calendar year
    with a different attribution method. Adding them produces a number that
    describes no period at all. (Caught in testing: a source's inbound total
    was silently CMS + Hop.)"""
    _seed_rates(store)
    cms = tmp_path / "cms_2015.csv"
    cms.write_text(f"{DOCS[0]},{MINE[0]},300,40,3\n")
    hop = tmp_path / "docgraph_2022.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   f"{DOCS[0]},{MINE[0]},70,500,12.5,3.1\n")
    import_shared_patients(store, cms)
    import_shared_patients(store, hop)

    res = org_referrals(store, MINE, "in")        # no year: newest wins
    assert res["data_year"] == "2022" and res["dataset"] == "DocGraph Hop Teaming"
    assert [r["patients"] for r in res["rows"]] == [70], "must not be 40+70=110"
    assert res["caveat"] and "NOT a referral record" in res["caveat"]

    pinned = org_referrals(store, MINE, "in", year="2015")
    assert [r["patients"] for r in pinned["rows"]] == [40]
    with pytest.raises(MedicareImportError, match="no referral data loaded for 1999"):
        org_referrals(store, MINE, "in", year="1999")

    st = medicare_status(store)
    assert {d["year"] for d in st["referrals"]} == {"2015", "2022"}


def test_medicare_api_and_dashboard_tab(cfg, store, tmp_path):
    """The dashboard tab's three endpoints: status, one-practice lookup (with
    eligibility joined onto every referral row), and the batch NPI check."""
    from fastapi.testclient import TestClient
    from mrfx.api import create_app

    _seed_rates(store)
    client = TestClient(create_app(cfg, store))

    # Nothing imported: status says so, the practice lookup still answers with
    # its NPIs (the UI shows import instructions), and the batch check is a
    # clear refusal rather than an empty table.
    assert client.get("/api/medicare/status").json() == {
        "eligibility": None, "referrals": []}
    d = client.post("/api/medicare/org", json={"subject": "431234567"}).json()
    assert set(d["npis"]) == set(MINE)
    assert d["eligibility"] == [] and d["referrals_in"]["rows"] == []
    r = client.post("/api/medicare/eligibility", json={"text": MINE[0]})
    assert r.status_code == 422 and "no Order & Referring roster" in r.json()["detail"]

    import_orf_roster(store, _orf(tmp_path))
    hop = tmp_path / "docgraph_2022.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   + "".join(f"{d},{MINE[0]},{70 + i},{500 + i},12.5,3.1\n"
                             for i, d in enumerate(DOCS)))
    import_shared_patients(store, hop)

    st = client.get("/api/medicare/status").json()
    assert st["eligibility"]["providers"] == len(DOCS)
    assert st["referrals"] == [
        {"label": "DocGraph Hop Teaming", "year": "2022", "pairs": len(DOCS)}]

    d = client.post("/api/medicare/org", json={"subject": "431234567"}).json()
    rows = d["referrals_in"]["rows"]
    assert [x["npi"] for x in rows] == [DOCS[2], DOCS[1], DOCS[0]]  # by patients desc
    # the payoff on one row: volume + current Part B eligibility + a readable specialty
    assert all(x["on_orf"] and x["partb"] for x in rows)
    assert rows[0]["specialty"] == "Orthopedic surgery"           # 207X00000X
    assert d["referrals_in"]["data_year"] == "2022"
    # the practice's own NPIs read as not-on-list (expected for therapists/orgs)
    assert all(e["on_list"] is False for e in d["eligibility"])

    # an unloaded year and an unknown practice are refusals, not empty screens
    r = client.post("/api/medicare/org", json={"subject": "431234567", "year": "1999"})
    assert r.status_code == 422 and "no referral data loaded for 1999" in r.json()["detail"]
    r = client.post("/api/medicare/org", json={"subject": "No Such Clinic LLC"})
    assert r.status_code == 422 and "no practice with NPIs" in r.json()["detail"]

    # batch check: NPIs are pulled out of any pasted text, deduplicated
    r = client.post("/api/medicare/eligibility",
                    json={"text": f"call {DOCS[0]} and {MINE[0]}; also {DOCS[0]} again"})
    d = r.json()
    assert d["checked"] == 2 and d["on_list"] == 1
    by = {x["npi"]: x for x in d["rows"]}
    assert by[DOCS[0]]["on_list"] is True and by[MINE[0]]["on_list"] is False
    r = client.post("/api/medicare/eligibility", json={"text": "no npis here"})
    assert r.status_code == 422


def test_bundle_gains_the_medicare_layers_and_says_so(cfg, store, tmp_path):
    """With the layers imported the bundle must carry them AND stop claiming it
    contains no eligibility/referral data — that disclaimer is true only while
    they are absent."""
    from mrfx.orgprofile import compute_org_profile, org_bundle_files

    _seed_rates(store)
    before = org_bundle_files(store, compute_org_profile(
        store, "431234567", {"month": "2026-06"}))
    assert "eligibility.csv" not in before
    assert "WHAT THIS BUNDLE DOES NOT CONTAIN" in before["profile.txt"]

    import_orf_roster(store, _orf(tmp_path))
    hop = tmp_path / "docgraph_2022.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   + "".join(f"{d},{MINE[0]},{70 + i},{500 + i},12.5,3.1\n"
                             for i, d in enumerate(DOCS)))
    import_shared_patients(store, hop)

    after = org_bundle_files(store, compute_org_profile(
        store, "431234567", {"month": "2026-06"}))
    assert {"eligibility.csv", "referral_sources.csv"} <= set(after)
    # the payoff: each referring physician carries BOTH volume and whether they
    # are still eligible to refer
    src = after["referral_sources.csv"]
    assert "still_eligible_partb" in src and "Dr Ortho 0" in src
    assert src.count(",Y,") >= 1
    assert "NOT a referral record" in src            # caveat travels with the data
    # and the disclaimer flipped rather than lying
    assert "WHAT THIS BUNDLE DOES NOT CONTAIN" not in after["profile.txt"]
    assert "already carries eligibility and referral structure" in after["profile.txt"]
