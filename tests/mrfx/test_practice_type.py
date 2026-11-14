"""0.8.6 — which practices are outpatient rehab clinics (mrfx/naming.py
practice_type), and comparing against them by default.

Hand-built practices, one per rule; each test fails on 0.8.5 (6ce0eee): no
practice_type column, no peer_group / org_taxonomy filter, no CMS lists.
"""
from __future__ import annotations

import pytest

from mrfx.benchmark import BenchmarkError, market_in_words, normalize_market
from mrfx.cmsref import load_facilities
from mrfx.store import FINISHED_NOW

RATE = dict(billing_code="97110", billing_code_type="CPT", discipline="PT", is_timed=True,
            billing_class="professional", negotiated_type="negotiated", is_dollar_rate=True,
            modifier_set=["GP"], service_code=["11"], file_month="2026-06",
            last_updated_on="2026-06-01", expiration_date=None, schema_version="2.0.0",
            tin_is_really_npi=False, payer="BCBS TN")
PT, MD = "225100000X", "207Q00000X"
HOSP_ADDR, SNF_ADDR = "100 HOSPITAL DRIVE", "5 CARE CENTER ROAD"

# tin -> (rate, [(npi, entity_type, name, taxonomy, address)])
PRACTICES = {
    # org NPI registered as a PT clinic (261QP2000X) -> clinic
    "500000001": (40.0, [("5000000101", "NPI-2", "RIVER PT LLC", "261QP2000X", "1 RIVER ST"),
                         ("5000000102", "NPI-1", "ANN PT", PT, "1 RIVER ST")]),
    # a GROUP NPI registered as a physical therapist (225100000X) -> clinic
    "500000002": (42.0, [("5000000201", "NPI-2", "HILL THERAPY PC", PT, "2 HILL AVE"),
                         ("5000000202", "NPI-1", "BOB PT", PT, "2 HILL AVE")]),
    # therapists, but practising at a CMS-listed hospital address -> hospital
    "500000003": (55.0, [("5000000301", "NPI-1", "CAL PT", PT, "100 Hospital Dr. Suite 3"),
                         ("5000000302", "NPI-1", "DEE PT", PT, "100 HOSPITAL DR")]),
    # therapists at a CMS-listed nursing home -> facility (contract therapy)
    "500000004": (35.0, [("5000000401", "NPI-1", "EVE PT", PT, "5 Care Center Rd"),
                         ("5000000402", "NPI-1", "FAY PT", PT, "5 CARE CENTER RD STE 1")]),
    # physicians only -> physician / other
    "500000005": (30.0, [("5000000501", "NPI-1", "GUS MD", MD, "9 MAIN ST"),
                         ("5000000502", "NPI-1", "HAL MD", MD, "9 MAIN ST")]),
    # a hospital organization NPI with therapists -> hospital
    "500000006": (60.0, [("5000000601", "NPI-2", "CITY HOSPITAL", "282N00000X", "7 OAK ST"),
                         ("5000000602", "NPI-1", "IVY PT", PT, "7 OAK ST")]),
    # clinicians NPPES has not classified, billing Medicare as PTs in
    # private practice -> clinic (Medicare evidence)
    "500000007": (38.0, [("5000000701", "NPI-1", "JON X", None, "3 ELM ST"),
                         ("5000000702", "NPI-1", "KIM X", None, "3 ELM ST")]),
    # nothing known -> unknown
    "500000008": (33.0, [("5000000801", "NPI-1", "LEE X", None, "4 PINE ST")]),
}
EXPECT = {"500000001": "outpatient_clinic", "500000002": "outpatient_clinic",
          "500000003": "hospital", "500000004": "facility", "500000005": "physician_other",
          "500000006": "hospital", "500000007": "outpatient_clinic", "500000008": "unknown"}


def _cms_lists(tmp_path):
    hosp = tmp_path / "hosp.csv"
    hosp.write_text('"Facility ID","Facility Name","Address","City/Town","State","ZIP Code"\n'
                    f'"440001","CITY GENERAL","{HOSP_ADDR}","NASHVILLE","TN","37203"\n')
    nh = tmp_path / "nh.csv"
    nh.write_text('"CMS Certification Number (CCN)","Provider Name","Provider Address",'
                  '"City/Town","State","ZIP Code"\n'
                  f'"445001","OAK CARE CENTER","{SNF_ADDR}","NASHVILLE","TN","37203"\n')
    return {"hospital": {"path": str(hosp), "release": "2026-10-26"},
            "nursing_home": {"path": str(nh), "release": "2026-10-01"}}


def _medicare(store):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from mrfx.utilization import _register_view
    d = store.dir / "utilization"
    d.mkdir(exist_ok=True)
    rows = {"npi": ["5000000701", "5000000702"], "billing_code": ["97110", "97110"],
            "services": [120.0, 80.0], "beneficiaries": [30, 20], "avg_allowed": [25.0, 25.0],
            "place_of_service": ["O", "O"], "data_year": ["2024", "2024"],
            "provider_type": ["Physical Therapist in Private Practice"] * 2,
            "entity_code": ["I", "I"], "state": ["TN", "TN"], "zip5": ["37203", "37203"]}
    pq.write_table(pa.table(rows), d / "2024.parquet")
    with store.write_lock, store.connect() as con:
        _register_view(con, store)


@pytest.fixture
def typed(store, tmp_path):
    recs = []
    for tin, (rate, npis) in PRACTICES.items():
        for npi, etype, name, tax, addr in npis:
            store.save_npi(npi, name, tax, None, "NASHVILLE", "TN", entity_type=etype,
                           address=addr, zip_code="372030000", taxonomy_codes=tax)
            recs.append(dict(RATE, tin_value=tin, tin_type="ein", npi=npi, negotiated_rate=rate,
                             source_file="tn.json", state="TN"))
    with store.rates_part_writer("tn.json") as w:
        w.write_batch(recs)
    store.upsert_file("tn.json", payer="BCBS TN", file_type="in_network", status="done",
                      rows_emitted=len(recs), finished_at=FINISHED_NOW)
    assert load_facilities(store, _cms_lists(tmp_path)) == 2
    _medicare(store)
    store.rebuild_rollups()
    assert store.refresh_practice_names() is True
    return store


def _types(store):
    with store.connect() as con:
        return {t: (k, r) for t, k, r in con.execute(
            "SELECT tin_value, practice_type, type_reason FROM tin_directory").fetchall()}


def test_each_rule_classifies_its_practice(typed):
    """(classification) Org taxonomy, a group registered as a therapist, a
    hospital / nursing-home ADDRESS (suite and spelling normalized), a
    hospital org, physicians, Medicare private-practice billing, nothing.
    was: no practice_type (BinderException)."""
    got = _types(typed)
    assert {t: k for t, (k, _r) in got.items()} == EXPECT
    # two NPIs, one address once the suite and "Dr." / "DRIVE" are normalized
    assert "1 of 1 practice addresses are CMS-listed hospitals" in got["500000003"][1]
    assert "CMS-listed nursing homes" in got["500000004"][1]
    assert "bill as therapists in private practice, 100% of the services (100% in the office)" \
        in got["500000007"][1]
    assert "registered as a therapy clinic" in got["500000001"][1]


def test_comparisons_default_to_outpatient_clinics_when_asked(typed):
    """(peer group) peer_group 'clinics' compares a practice with the other
    outpatient rehab clinics only (500000002 at $42 and 500000007 at $38 ->
    median $40), not the hospitals ($55, $60), the nursing home ($35), the
    physicians ($30) or the unknown ($33); 'all' keeps everyone (median of
    the 7 others = $38).
    was: market.peer_group was ignored — every practice pooled."""
    from mrfx.benchmark import compute_benchmark
    base = {"month": "latest", "state": "TN"}
    clinics = compute_benchmark(typed, "500000001", {**base, "peer_group": "clinics"})
    every = compute_benchmark(typed, "500000001", base)
    row_c = next(r for r in clinics["rows"] if r["billing_code"] == "97110")
    row_a = next(r for r in every["rows"] if r["billing_code"] == "97110")
    assert (row_c["n_peers"], row_c["p50"]) == (2, 40.0)
    assert (row_a["n_peers"], row_a["p50"]) == (7, 38.0)
    assert "compared with outpatient rehab clinics" in market_in_words(
        normalize_market({**base, "peer_group": "clinics"}))


def test_org_npi_taxonomy_filter_and_validation(typed):
    """(filter) org_taxonomy selects practices by their organization NPIs'
    taxonomies: 261QP2000X -> only RIVER PT; a bad peer group is refused in
    words.
    was: org_taxonomy ignored; peer_group 'nursing' accepted silently."""
    from mrfx.benchmark import compute_benchmark
    got = compute_benchmark(typed, "500000002", {"month": "latest", "state": "TN",
                                                 "org_taxonomy": ["261qp2000x"]})
    row = next(r for r in got["rows"] if r["billing_code"] == "97110")
    assert (row["n_peers"], row["p50"]) == (1, 40.0)
    with pytest.raises(BenchmarkError, match="peer_group must be one of"):
        normalize_market({"peer_group": "nursing"})


def test_before_the_classification_runs_the_directory_flags_decide(store, tmp_path):
    """(no empty market) A store whose practice types have not been computed
    yet still has a practice_type for every TIN — the directory's own
    therapy / hospital / facility flags — so the clinics default never
    empties a market.
    was: no practice_type."""
    recs = [dict(RATE, tin_value="500000009", tin_type="ein", npi="5000000901",
                 negotiated_rate=40.0, source_file="x.json", state="TN")]
    store.save_npi("5000000901", "ZED PT", PT, None, "NASHVILLE", "TN", entity_type="NPI-1",
                   taxonomy_codes=PT)
    with store.rates_part_writer("x.json") as w:
        w.write_batch(recs)
    store.upsert_file("x.json", payer="BCBS TN", file_type="in_network", status="done",
                      rows_emitted=1, finished_at=FINISHED_NOW)
    store.rebuild_rollups()
    with store.connect() as con:
        got = con.execute("SELECT practice_type, name_source FROM tin_directory").fetchall()
    assert got == [("outpatient_clinic", "pending")]


def test_negotiate_lists_the_clinics_the_payer_pays_more(typed):
    """(Negotiate) RIVER PT is paid $40.00 for 97110. Among outpatient rehab
    clinics only HILL THERAPY ($42.00, +5.0%) is paid more (JON/KIM's $38 is
    not); fewer than 5 clinics in NASHVILLE -> the state. Among all
    practices: CITY HOSPITAL $60 (+50%), the hospital-address group $55
    (+37.5%), HILL THERAPY $42 (+5%) — ranked by codes paid more, then gap.
    was: no such list (404)."""
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from mrfx.benchmark import clinics_paid_more
    d = clinics_paid_more(typed, "500000001", "BCBS TN", {"month": "latest", "state": "TN"})
    assert (d["geography"], d["where"], d["practices_compared"], d["clinics_paid_more"]) == (
        "state", "TN", 2, 1)
    (x,) = d["clinics"]
    assert (x["name"], x["codes_paid_more"], x["median_gap_pct"], x["practice_type"]) == (
        "HILL THERAPY PC", 1, 5.0, "outpatient_clinic")
    assert d["codes"] == [{"code": "97110", "description": d["codes"][0]["description"],
                           "your_rate": 40.0, "practices": 2, "median": 40.0, "highest": 42.0,
                           "paid_more": 1, "gap_to_median": 0.0}]
    every = clinics_paid_more(typed, "500000001", "BCBS TN",
                              {"month": "latest", "state": "TN", "peer_group": "all"})
    assert [(c["rank"], c["median_gap_pct"]) for c in every["clinics"]] == [
        (1, 50.0), (2, 37.5), (3, 5.0)]
    from mrfx.config import MrfxConfig
    cfg = MrfxConfig(store_dir=typed.dir, cms_dac="off")
    client = TestClient(create_app(cfg, typed), base_url="http://localhost")
    body = {"subject": "500000001", "payer": "BCBS TN", "market": {"month": "latest", "state": "TN"}}
    assert client.post("/api/negotiate/paid-more", json=body).json()["clinics_paid_more"] == 1
    csv = client.post("/api/negotiate/paid-more.csv", json=body).text
    assert "HILL THERAPY PC" in csv and "YOUR RATE" in csv and "rate_97110" in csv
