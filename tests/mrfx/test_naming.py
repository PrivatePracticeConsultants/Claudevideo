"""0.8.5 — practice names and cities (mrfx/naming.py), one rule for every surface.

Field report: in a BCBS Tennessee visit-value export, a tax ID with no
organization NPI was named after its ALPHABETICALLY FIRST clinician ("ALEC
YATES", "ABBEY MCCRORY" — ~26% of names began with "A"), and its cities were
the first three alphabetically. Each test fails on 0.8.4 (b00957e) with the
value in its docstring (no name_source, no resolver, the old names).
"""
from __future__ import annotations

import json

import pytest

from mrfx.naming import DAC_MIN_SHARE
from mrfx.store import FINISHED_NOW

RATE = dict(billing_code="97110", billing_code_type="CPT", discipline="PT", is_timed=True,
            billing_class="professional", negotiated_rate=50.0, negotiated_type="negotiated",
            is_dollar_rate=True, modifier_set=["GP"], service_code=["11"],
            file_month="2026-06", last_updated_on="2026-06-01", expiration_date=None,
            schema_version="2.0.0", tin_is_really_npi=False, state="TN")

# tin -> [(npi, entity_type, name, city, state)]
PRACTICES = {
    # 1. organization NPIs: ACME (2 org NPIs, two spellings of one name) beats
    #    ZED REHAB (1); individuals never name it
    "111111111": [("1111111101", "NPI-2", "ACME PHYSICAL THERAPY, LLC", "MEMPHIS", "TN"),
                  ("1111111102", "NPI-2", "Acme Physical Therapy LLC", "MEMPHIS", "TN"),
                  ("1111111103", "NPI-2", "ZED REHAB INC", "NASHVILLE", "TN"),
                  ("1111111104", "NPI-1", "AARON ABLE", "MEMPHIS", "TN")],
    # 1b. one org NPI in TN, one in KY: the export's state breaks the tie
    "121212121": [("1212121201", "NPI-2", "YORK REHAB PC", "KNOXVILLE", "TN"),
                  ("1212121202", "NPI-2", "ALPHA REHAB PC", "LEXINGTON", "KY")],
    # 2. only individuals; the CMS file puts 3 of 4 in MIDTOWN
    "222222222": [("2222222201", "NPI-1", "ABBEY MCCRORY", "CHATTANOOGA", "TN"),
                  ("2222222202", "NPI-1", "BEN BLAKE", "CHATTANOOGA", "TN"),
                  ("2222222203", "NPI-1", "CARA CRUZ", "CLEVELAND", "TN"),
                  ("2222222204", "NPI-1", "DAN DOE", "CHATTANOOGA", "TN")],
    # 2b. only individuals; the CMS file splits them 1/1/1/1 (25% < 30%)
    "232323232": [("2323232301", "NPI-1", "ALAN AMES", "JACKSON", "TN"),
                  ("2323232302", "NPI-1", "BETH BOLT", "JACKSON", "TN"),
                  ("2323232303", "NPI-1", "CARL COX", "JACKSON", "TN"),
                  ("2323232304", "NPI-1", "DEB DAY", "JACKSON", "TN")],
    # 3. individuals, none in the CMS file: 2 in MEMPHIS, 1 NASHVILLE, 1 ATLANTA GA
    "333333333": [("3333333301", "NPI-1", "ALEC YATES", "NASHVILLE", "TN"),
                  ("3333333302", "NPI-1", "ZACH ZELL", "MEMPHIS", "TN"),
                  ("3333333303", "NPI-1", "MIA MOORE", "MEMPHIS", "TN"),
                  ("3333333304", "NPI-1", "ADA ALLEN", "ATLANTA", "GA")],
    # 4. a true solo practice
    "444444444": [("4444444401", "NPI-1", "JANE DOE", "FRANKLIN", "TN")],
}
DAC_ROWS = [
    # npi, group, city, state
    ("2222222201", "MIDTOWN PHYSICAL THERAPY PLLC", "CHATTANOOGA", "TN"),
    ("2222222202", "MIDTOWN PHYSICAL THERAPY PLLC", "CHATTANOOGA", "TN"),
    ("2222222203", "Midtown Physical Therapy, P.L.L.C.", "CLEVELAND", "TN"),
    ("2222222204", "OTHER GROUP LLC", "CHATTANOOGA", "TN"),
    ("2323232301", "GROUP ONE", "JACKSON", "TN"),
    ("2323232302", "GROUP TWO", "JACKSON", "TN"),
    ("2323232303", "GROUP THREE", "JACKSON", "TN"),
    ("2323232304", "GROUP FOUR", "JACKSON", "TN"),
]


def _dac_csv(path, rows):
    """A file in the CMS layout (2026 header names, quoted commas)."""
    head = ("NPI,Ind_PAC_ID,Ind_enrl_ID,Provider Last Name,Provider First Name,Facility Name,"
            "org_pac_id,num_org_mem,adr_ln_1,City/Town,State,ZIP Code")
    lines = [head]
    for i, (npi, org, city, st) in enumerate(rows):
        lines.append(f'{npi},{9000 + i},I{i},LAST,FIRST,"{org}",{5000 + i},3,"1 MAIN ST, STE 2",'
                     f"{city},{st},37000")
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def named(store, tmp_path):
    recs = []
    for tin, npis in PRACTICES.items():
        for npi, etype, name, city, st in npis:
            store.save_npi(npi, name, "225100000X", "Physical Therapist", city, st,
                           entity_type=etype, zip_code="37000")
            recs.append(dict(RATE, payer="BCBS TN", tin_value=tin, tin_type="ein", npi=npi,
                             source_file="tn.json"))
    with store.rates_part_writer("tn.json") as w:
        w.write_batch(recs)
    store.upsert_file("tn.json", payer="BCBS TN", file_type="in_network", status="done",
                      rows_emitted=len(recs), finished_at=FINISHED_NOW)
    n = store.load_dac(_dac_csv(tmp_path / "DAC_NationalDownloadableFile.csv", DAC_ROWS),
                       release="2026-10-30", source="test")
    assert n == len(DAC_ROWS)
    store.rebuild_rollups()
    return store


def _resolved(store, state="TN"):
    return store.practice_names_for(list(PRACTICES), state)


def test_a_tin_with_org_npis_is_named_by_their_normalized_legal_name(named):
    """(rule 1) Two spellings of ACME's legal name are ONE name (case,
    punctuation, LLC) held by 2 org NPIs and beat ZED REHAB (1); the
    individual never names it. A TN / KY tie goes to the export's state.
    was: no resolver (AttributeError: practice_names_for)."""
    r = _resolved(named)
    assert (r["111111111"]["practice_name"], r["111111111"]["name_source"]) == (
        "ACME PHYSICAL THERAPY, LLC", "nppes_org")
    assert r["121212121"]["practice_name"] == "YORK REHAB PC"
    ky = named.practice_names_for(["121212121"], "KY")
    assert ky["121212121"]["practice_name"] == "ALPHA REHAB PC"


def test_a_tin_of_individuals_is_named_by_the_cms_file_group_of_most_clinicians(named):
    """(rule 2) 3 of the 4 clinicians are enrolled with MIDTOWN (two
    spellings, one name) in the CMS file: 75% >= 30% -> MIDTOWN; a TIN whose
    clinicians split 1/1/1/1 (25%) falls through, never naming a clinician.
    was: 'ABBEY MCCRORY' (the alphabetically first clinician)."""
    r = _resolved(named)
    assert (r["222222222"]["practice_name"], r["222222222"]["name_source"]) == (
        "MIDTOWN PHYSICAL THERAPY PLLC", "cms_dac")
    assert DAC_MIN_SHARE == 0.30
    assert (r["232323232"]["practice_name"], r["232323232"]["name_source"]) == (
        "Unnamed group — 4 clinicians (JACKSON)", "unnamed_group")


def test_a_multi_npi_tin_with_no_name_is_an_unnamed_group_with_ranked_cities(named):
    """(rule 3) No org NPI, nobody in the CMS file: 'Unnamed group — 4
    clinicians (MEMPHIS)' — never 'ADA ALLEN' / 'ALEC YATES'. Cities rank by
    NPIs there, in the export's state first: MEMPHIS (2), NASHVILLE (1);
    ATLANTA GA is not a TN city.
    was: display_name 'ADA ALLEN', cities ['ATLANTA', 'MEMPHIS', 'NASHVILLE']."""
    r = _resolved(named)["333333333"]
    assert (r["practice_name"], r["name_source"]) == (
        "Unnamed group — 4 clinicians (MEMPHIS)", "unnamed_group")
    assert r["top_cities"] == ["MEMPHIS", "NASHVILLE"]
    assert r["cities"] == ["MEMPHIS", "NASHVILLE", "ATLANTA"]


def test_a_true_solo_practice_is_named_by_its_one_provider(named):
    """(rule 3) Exactly one NPI and no group: that provider's name."""
    r = _resolved(named)["444444444"]
    assert (r["practice_name"], r["name_source"], r["top_cities"]) == (
        "JANE DOE", "solo_npi", ["FRANKLIN"])


def test_every_screen_reads_the_resolved_names_through_the_directory(named):
    """(all exports) The directory every screen and export reads carries the
    resolved name, ranked cities and name_source once the names table is
    built; before it, the directory's own values with name_source 'pending'.
    was: the alphabetical clinician and alphabetical cities everywhere."""
    with named.connect() as con:
        before = dict(con.execute(
            "SELECT tin_value, name_source FROM tin_directory").fetchall())
    assert set(before.values()) == {"pending"}
    assert named.refresh_practice_names() is True
    assert named.refresh_practice_names() is False          # unchanged inputs: kept
    with named.connect() as con:
        got = {t: (n, s, c) for t, n, s, c in con.execute(
            "SELECT tin_value, display_name, name_source, cities FROM tin_directory").fetchall()}
    assert got["333333333"] == ("Unnamed group — 4 clinicians (MEMPHIS)", "unnamed_group",
                                ["MEMPHIS", "NASHVILLE", "ATLANTA"])
    assert got["222222222"][:2] == ("MIDTOWN PHYSICAL THERAPY PLLC", "cms_dac")
    assert got["111111111"][:2] == ("ACME PHYSICAL THERAPY, LLC", "nppes_org")
    assert got["444444444"][:2] == ("JANE DOE", "solo_npi")
    # a name never starts with a clinician's initial by accident of sorting
    assert not any(n in {"AARON ABLE", "ABBEY MCCRORY", "ADA ALLEN", "ALEC YATES", "ALAN AMES"}
                   for n, _s, _c in got.values())


def test_the_cms_file_is_read_by_its_header_names_and_its_release_kept(named, tmp_path):
    """(the file) Read by header name — 2026's 'Facility Name' / 'City/Town' /
    'State' or the older 'org_nm' / 'cty' / 'st' — quoted commas intact,
    release date kept; a file of another layout is refused with a sentence."""
    with named.connect() as con:
        assert named._meta_get(con, "dac_release") == "2026-10-30"
        rows = con.execute("SELECT npi, org_nm, city, state FROM dac_orgs "
                           "WHERE npi = '2222222203'").fetchall()
    assert rows == [("2222222203", "MIDTOWN PHYSICAL THERAPY, P.L.L.C.", "CLEVELAND", "TN")]
    old = tmp_path / "old.csv"
    old.write_text("NPI,org_nm,cty,st\n2222222201,OLD NAME GROUP,CHATTANOOGA,TN\n")
    assert named.load_dac(old, release="2023-01-01") == 1
    bad = tmp_path / "bad.csv"
    bad.write_text("foo,bar\n1,2\n")
    with pytest.raises(ValueError, match="not the CMS Doctors and Clinicians file"):
        named.load_dac(bad)


def test_the_visit_value_export_carries_name_source(named):
    """(export) The visit-value practice list and CSV carry name_source and
    the in-state ranked cities.
    was: no name_source column; cities alphabetical."""
    from mrfx.visit_market import market_visit_practices, practices_csv
    m = {"state": "TN", "discipline": "PT", "month": "latest"}
    lines = "97110, 15"
    with pytest.MonkeyPatch.context() as mp:
        import mrfx.visit_market as vm
        mp.setattr(vm, "MIN_PRACTICES", 1)
        d = market_visit_practices(named, m, lines, None, "BCBS TN", None, 0, None)
        by = {x["name"]: x for x in d["rows"]}
        assert by["Unnamed group — 4 clinicians (MEMPHIS)"]["name_source"] == "unnamed_group"
        assert by["Unnamed group — 4 clinicians (MEMPHIS)"]["cities"] == ["MEMPHIS", "NASHVILLE"]
        text = practices_csv(named, m, lines, None, "BCBS TN")
    head = next(ln for ln in text.splitlines() if not ln.startswith("#"))
    assert head.split(",")[:3] == ["rank", "practice", "name_source"]
    assert json.dumps(sorted(x["name_source"] for x in d["rows"])) == json.dumps(
        ["cms_dac", "nppes_org", "nppes_org", "solo_npi", "unnamed_group", "unnamed_group"])
