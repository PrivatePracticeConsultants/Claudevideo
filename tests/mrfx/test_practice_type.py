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
    # "Compare against: clinics by organization NPI taxonomy" — RIVER PT
    # (261QP2000X) and HILL THERAPY (a group NPI as 225100000X); not CITY
    # HOSPITAL (282N00000X) nor any practice without an org NPI
    org = compute_benchmark(typed, "500000005", {"month": "latest", "state": "TN",
                                                 "peer_group": "org"})
    row = next(r for r in org["rows"] if r["billing_code"] == "97110")
    assert (row["n_peers"], row["p50"]) == (2, 41.0)
    assert "organization NPI registered as a therapy clinic" in market_in_words(
        normalize_market({"peer_group": "org"}))
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


def test_a_city_list_leads_with_that_city(typed, monkeypatch):
    """A multi-site practice on the NASHVILLE list whose three biggest sites are
    elsewhere (2 Memphis, 2 Jackson, 2 Knoxville clinicians, 1 Nashville) is
    shown as 'NASHVILLE; …' — the reason it is on the list. was: its top
    three only, with no sign of Nashville (live: a Smyrna list read as
    Nashville / Chattanooga clinics)."""
    import mrfx.benchmark as bm
    from mrfx.benchmark import clinics_paid_more
    recs = []
    sites = [("MEMPHIS", "381030000"), ("MEMPHIS", "381030000"), ("JACKSON", "383010000"),
             ("JACKSON", "383010000"), ("KNOXVILLE", "379020000"), ("KNOXVILLE", "379020000"),
             ("NASHVILLE", "372030000")]
    for i, (city, z) in enumerate(sites, 1):
        npi = f"50000009{i:02d}"
        typed.save_npi(npi, f"PT {i}", PT, None, city, "TN", entity_type="NPI-1",
                       address=f"{i} MAIN ST", zip_code=z, taxonomy_codes=PT)
        recs.append(dict(RATE, tin_value="500000009", tin_type="ein", npi=npi,
                         negotiated_rate=45.0, source_file="tn2.json", state="TN"))
    with typed.rates_part_writer("tn2.json") as w:
        w.write_batch(recs)
    typed.upsert_file("tn2.json", payer="BCBS TN", file_type="in_network", status="done",
                      rows_emitted=len(recs), finished_at=FINISHED_NOW)
    typed.rebuild_rollups()
    typed.refresh_practice_names(force=True)
    top = typed.practice_names_for(["500000009"], "TN")["500000009"]["top_cities"]
    assert "NASHVILLE" not in top and len(top) == 3
    monkeypatch.setattr(bm, "PAID_MORE_MIN_CITY", 2)
    d = clinics_paid_more(typed, "500000001", "BCBS TN", {"month": "latest", "state": "TN"})
    assert (d["geography"], d["where"]) == ("city", "NASHVILLE, TN")
    delta = next(x for x in d["clinics"] if x["tin"] == "500000009")
    assert delta["cities"][0] == "NASHVILLE" and len(delta["cities"]) == 3
    assert delta["cities"][1:] == top[:2]


def test_cmsref_command_reclassifies(typed, monkeypatch, capsys):
    """`mrfx cmsref` fetches the CMS lists (here: stubbed, no network),
    reports each source in a sentence, and re-classifies every practice."""
    import argparse

    import mrfx.cmsref as cr
    from mrfx.cli import cmd_cmsref
    from mrfx.config import MrfxConfig
    monkeypatch.setattr(cr, "ensure_cmsref", lambda store, d: {
        "facilities": "current", "medicare": {"year": "2024", "rows": None, "fetched": False}})
    cfg = MrfxConfig(store_dir=typed.dir, cms_dac="off", cms_reference="off", port=1)
    assert cmd_cmsref(cfg, argparse.Namespace(refresh=False)) == 0
    out = capsys.readouterr().out
    assert "address lists: already current." in out
    assert "Medicare Physician & Other Practitioners, 2024: already current." in out
    assert "Practices re-classified: " in out and "outpatient_clinic 3" in out
    monkeypatch.setattr(cr, "ensure_cmsref", lambda store, d: {
        "facilities": "not loaded: HTTP 503", "medicare": "not loaded: HTTP 503"})
    assert cmd_cmsref(cfg, argparse.Namespace(refresh=False)) == 1
    assert "not loaded: HTTP 503" in capsys.readouterr().err


def test_merged_menu_entries_cover_every_screen():
    """My rates (Rate card + Benchmark) and Market rates (Explorer, Code,
    Markets, Payer roster) are one menu entry each; every member keeps its
    section, so old links (#benchmark, #roster, …) still open it."""
    import re
    from pathlib import Path
    web = Path(__file__).resolve().parents[2] / "mrfx" / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")
    nav = re.search(r"<nav.*?</nav>", html, re.S).group(0)
    assert 'data-group="myrates"' in nav and 'data-group="market"' in nav
    for gone in ('data-view="benchmark"', 'data-view="cpt"', 'data-view="markets"',
                 'data-view="roster"'):
        assert gone not in nav
    groups = re.search(r"const VIEW_GROUPS = \{(.*?)\n\};", js, re.S).group(1)
    members = re.findall(r'\["([a-z]+)", "', groups)
    assert members == ["ratecard", "benchmark", "explorer", "cpt", "markets", "roster"]
    for v in members:
        assert f'id="view-{v}"' in html
    assert "!!groupOf(v) ||" in js       # viewExists: a link to a member still opens it


def test_a_practice_found_by_its_legal_name_in_nppes(typed, tmp_path):
    """(org NPI taxonomy) Payers list clinicians, rarely the clinic's own
    organization NPI. A practice named from the CMS file is looked up by
    that legal name (normalized) among NPPES organization NPIs in its state:
    registered ONLY as therapy (225100000X) -> its organization is a
    therapy clinic (MAIN STREET PHYSICAL THERAPY, the GUS/HAL TIN); a
    multi-specialty registration (207Q00000X + 225100000X) is not (ELM
    GROUP); a namesake in another state never counts.
    was: only org NPIs a payer listed counted — 4 of 5,660 TN practices."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from mrfx.benchmark import compute_benchmark
    dac = tmp_path / "DAC_NationalDownloadableFile.csv"
    head = ("NPI,Ind_PAC_ID,Ind_enrl_ID,Provider Last Name,Provider First Name,Facility Name,"
            "org_pac_id,num_org_mem,adr_ln_1,City/Town,State,ZIP Code")
    rows = [("5000000501", "Main Street Physical Therapy, L.L.C."),
            ("5000000502", "Main Street Physical Therapy, L.L.C."),
            ("5000000701", "ELM GROUP PC"), ("5000000702", "ELM GROUP PC"),
            # therapists at a CMS-listed hospital address (typed hospital)
            ("5000000301", "CAL DEE REHAB INC"), ("5000000302", "CAL DEE REHAB INC")]
    dac.write_text(head + "\n" + "\n".join(
        f'{n},{9000 + i},I{i},L,F,"{o}",{5000 + i},2,"1 MAIN ST",NASHVILLE,TN,37203'
        for i, (n, o) in enumerate(rows)) + "\n")
    typed.load_dac(dac, release="2026-10-30", source="test")
    blank = dict(taxonomy_codes=None, city="NASHVILLE", address="1 MAIN ST", zip="37203",
                 phone=None, enumeration_date=None, deactivation_date=None, reactivation_date=None)
    orgs = [dict(blank, npi="1999999901", org_name="MAIN STREET PHYSICAL THERAPY LLC",
                 entity_type="NPI-2", taxonomy_code="225100000X", state="TN"),
            dict(blank, npi="1999999902", org_name="ELM GROUP, P.C.", entity_type="NPI-2",
                 taxonomy_code="207Q00000X", state="TN"),
            dict(blank, npi="1999999903", org_name="ELM GROUP PC", entity_type="NPI-2",
                 taxonomy_code="225100000X", state="TN"),
            dict(blank, npi="1999999905", org_name="CAL DEE REHAB, INC.", entity_type="NPI-2",
                 taxonomy_code="225100000X", state="TN"),
            # a namesake in another state, registered as a therapy clinic
            dict(blank, npi="1999999904", org_name="ELM GROUP", entity_type="NPI-2",
                 taxonomy_code="261QP2000X", state="KY")]
    pq.write_table(pa.Table.from_pylist(orgs), typed.dir / "nppes_cache.parquet")
    assert typed.refresh_practice_names() is True      # the NPPES file is in the stamp
    with typed.connect() as con:
        got = {t: (n, s, k, oc, why) for t, n, s, k, oc, why in con.execute(
            "SELECT tin_value, display_name, name_source, practice_type, org_clinic, type_reason "
            "FROM tin_directory WHERE tin_value IN ('500000003', '500000005', '500000007')"
        ).fetchall()}
    name, src, kind, oc, why = got["500000005"]
    assert (src, kind, oc) == ("cms_dac", "outpatient_clinic", True)
    assert "registered in NPPES only as a therapy clinic or therapist group (225100000X)" in why
    assert got["500000007"][2:4] == ("outpatient_clinic", False)    # Medicare evidence, not org
    with typed.connect() as con:      # matched (both TN registrations), then refused as mixed
        assert con.execute("SELECT org_taxonomies FROM tin_directory WHERE tin_value = "
                           "'500000007'").fetchone()[0] == ["207Q00000X", "225100000X"]
    # registered as therapy, but most of its addresses are a CMS hospital
    assert got["500000003"][2:4] == ("hospital", True)
    # "Compare against: clinics by organization NPI taxonomy" now holds it —
    # and not the hospital-typed one ($55)
    org = compute_benchmark(typed, "500000008", {"month": "latest", "state": "TN",
                                                 "peer_group": "org"})
    row = next(r for r in org["rows"] if r["billing_code"] == "97110")
    assert (row["n_peers"], row["p50"]) == (3, 40.0)        # $40, $42 and the $30 TIN
