"""The three reference datasets: Medicare utilization (volumes), Census ACS
ZCTA demographics (market sizing), and NPPES enumeration dates (new clinics)."""

import datetime as dt

import pytest

GW = "431234567"
GW_N = ["1417594896", "1234567893"]
PEER = ("437654321", "1999999992")


def _row(**kw):
    base = dict(payer="Aetna", tin_value=GW, tin_type="ein", npi=GW_N[0],
                source_file="s.json", billing_code="97110", billing_code_type="CPT",
                discipline="PT", is_timed=True, billing_class="professional",
                negotiated_rate=30.0, negotiated_type="negotiated",
                is_dollar_rate=True, billing_code_modifier=[], service_code=["11"],
                file_month="2026-06", last_updated_on="2026-06-01",
                expiration_date=None, schema_version="2.0.0",
                tin_is_really_npi=False, state=None)
    base.update(kw)
    return base


def _seed(store):
    rows = []
    for tin, npi in [(GW, GW_N[0]), (GW, GW_N[1]), PEER]:
        for code, rate in (("97110", 30.0), ("97140", 28.0)):
            rows.append(_row(tin_value=tin, npi=npi, billing_code=code,
                             negotiated_rate=rate))
    with store.rates_part_writer("s.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk(
        [dict(npi=n, org_name="Gateway Therapy", entity_type="NPI-2",
              taxonomy_code="261QP2000X", city="StL", state="MO",
              address="1 Main", zip="63103", phone=None) for n in GW_N]
        + [dict(npi=PEER[1], org_name="Peer PT", entity_type="NPI-2",
                taxonomy_code="2251X0800X", city="StL", state="MO",
                address="2 Oak", zip="63103", phone=None)])
    store.rebuild_rollups()


# --------------------------------------------------------- utilization --

PUF_MODERN = (
    "Rndrng_NPI,Rndrng_Prvdr_Type,HCPCS_Cd,HCPCS_Desc,Place_Of_Srvc,"
    "Tot_Benes,Tot_Srvcs,Avg_Mdcr_Alowd_Amt\n"
    f"{GW_N[0]},Physical Therapist,97110,Therapeutic exercise,O,120,4800,32.50\n"
    f"{GW_N[0]},Physical Therapist,97140,Manual therapy,O,80,1600,28.00\n"
    f"{GW_N[1]},Physical Therapist,97110,Therapeutic exercise,O,40,1200,32.50\n"
    f"{GW_N[0]},Physical Therapist,97110,Therapeutic exercise,F,15,300,26.00\n"
    f"{GW_N[0]},Physical Therapist,99213,Office visit,O,50,500,90.00\n"
    f"{PEER[1]},Physical Therapist,97110,Therapeutic exercise,O,200,9000,32.50\n"
)
# the 2013-era layout: every column renamed
PUF_LEGACY = (
    "npi,provider_type,hcpcs_code,hcpcs_description,place_of_service,"
    "bene_unique_cnt,line_srvc_cnt,average_Medicare_allowed_amt\n"
    f"{GW_N[0]},Physical Therapist,97110,Therapeutic exercise,O,100,4000,30.00\n"
)


def test_utilization_import_and_volume_prefill(store, tmp_path):
    """The PUF pre-fills a volumes box. Office-only, therapy codes only, and
    the Medicare-floor caveat rides on every answer."""
    from mrfx.utilization import (import_utilization, practice_utilization,
                                  suggested_volumes, utilization_status)

    _seed(store)
    f = tmp_path / "MUP_PHY_R24_P05_V10_D23_Prov_Svc.csv"
    f.write_text(PUF_MODERN)
    res = import_utilization(store, f, year="2023")
    assert res["year"] == "2023"
    # 99213 is not a therapy code -> never imported
    assert res["rows"] == 5 and res["providers"] == 3

    u = practice_utilization(store, [GW], year="2023")
    by = {c["billing_code"]: c for c in u["codes"]}
    # 4800 + 1200 across the practice's two NPIs; the facility row is excluded
    assert by["97110"]["units"] == 6000
    assert by["97140"]["units"] == 1600
    assert "99213" not in by, "non-therapy codes are not imported"
    assert u["summary"]["matched_npis"] == 2
    assert u["summary"]["total_units"] == 7600

    v = suggested_volumes(store, GW)
    assert v["volumes"] == {"97110": 6000, "97140": 1600}
    assert v["multiplier"] == 1.0
    assert "FLOOR" in v["note"] and "Medicare fee-for-service" in v["note"]

    # a multiplier is an ASSUMPTION and must say so
    v3 = suggested_volumes(store, GW, multiplier=3)
    assert v3["volumes"]["97110"] == 18000
    assert "assumption, not data" in v3["note"]
    from mrfx.benchmark import BenchmarkError
    with pytest.raises(BenchmarkError):
        suggested_volumes(store, GW, multiplier=0)

    st = utilization_status(store)
    assert st["loaded"] and st["latest"] == "2023"
    assert st["years"][0]["providers"] == 3


def test_utilization_reads_the_old_column_layout(store, tmp_path):
    """CMS renamed every column between the 2013 and 2020 layouts; matching
    tokens (not positions) is what keeps an older download working."""
    from mrfx.utilization import import_utilization, practice_utilization

    _seed(store)
    f = tmp_path / "Medicare_Provider_Util_Payment_PUF_CY2015.csv"
    f.write_text(PUF_LEGACY)
    res = import_utilization(store, f)
    assert res["year"] == "2015"
    u = practice_utilization(store, [GW])
    assert u["codes"][0]["units"] == 4000


def test_utilization_refuses_a_file_that_is_not_the_puf(store, tmp_path):
    from mrfx.utilization import UtilizationImportError, import_utilization

    _seed(store)
    bad = tmp_path / "rates_2023.csv"
    bad.write_text("payer,rate\nAetna,30.00\n")
    with pytest.raises(UtilizationImportError, match="does not look like"):
        import_utilization(store, bad)
    # a year that is neither in the name nor passed is a refusal, not a guess
    noyear = tmp_path / "puf.csv"
    noyear.write_text(PUF_MODERN)
    with pytest.raises(UtilizationImportError, match="which year"):
        import_utilization(store, noyear)
    # binary garbage refuses in plain language, never a raw DuckDB traceback
    binary = tmp_path / "puf_2023.zip"
    binary.write_bytes(b"PK\x03\x04" + bytes(range(256)) * 40)
    with pytest.raises(UtilizationImportError, match="could not be read"):
        import_utilization(store, binary)


def test_utilization_reads_the_real_cms_filename_year(store, tmp_path):
    """The actual download is named MUP_PHY_R24_P05_V10_D23_Prov_Svc.csv —
    no 4-digit year anywhere; D23 IS the data year. Refusing THE file the CMS
    page hands out would be pointless friction."""
    from mrfx.utilization import import_utilization

    _seed(store)
    f = tmp_path / "MUP_PHY_R24_P05_V10_D23_Prov_Svc.csv"
    f.write_text(PUF_MODERN)
    assert import_utilization(store, f)["year"] == "2023"
    # but an explicit year still wins over the filename
    f2 = tmp_path / "MUP_PHY_R24_P05_V10_D23_Prov_Svc_copy.csv"
    f2.write_text(PUF_MODERN)
    assert import_utilization(store, f2, year="2022")["year"] == "2022"


def test_utilization_quotes_hostile_column_names(store, tmp_path):
    """A CSV header may contain a double quote. Inlined undoubled it ENDS the
    SQL identifier and the rest of the header becomes SQL — verified to
    restructure the SELECT before this was fixed."""
    import csv as _csv

    from mrfx.utilization import import_utilization, practice_utilization

    _seed(store)
    f = tmp_path / "puf_2023.csv"
    with open(f, "w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["Rndrng_NPI", "HCPCS_Cd", 'Tot_Srvcs" AS zzz, 999 AS "boom',
                    "Place_Of_Srvc"])
        w.writerow([GW_N[0], "97110", "4000", "O"])
    res = import_utilization(store, f)
    assert res["rows"] == 1
    u = practice_utilization(store, [GW])
    assert u["codes"][0]["units"] == 4000, "the real column, not an injected 999"


def test_utilization_never_zeroes_a_practice_on_place_of_service(store, tmp_path):
    """CMS codes place-of-service 'O'/'F'. A differently-coded export would
    make the office filter match nothing — which must fall back to all
    settings and SAY so, never report 'this practice bills no therapy'."""
    from mrfx.utilization import import_utilization, practice_utilization

    _seed(store)
    f = tmp_path / "puf_2023.csv"
    f.write_text(
        "Rndrng_NPI,HCPCS_Cd,Tot_Srvcs,Place_Of_Srvc\n"
        f"{GW_N[0]},97110,4000,Office\n")     # not 'O'
    import_utilization(store, f)
    u = practice_utilization(store, [GW])
    assert u["codes"] and u["codes"][0]["units"] == 4000
    assert u["summary"]["office_only"] is False
    assert "ALL settings" in u["note"]


# -------------------------------------------------------- demographics --

ACS = (
    "GEO_ID,NAME,B01001_001E,B01001_020E,B01001_021E,B01001_044E,B19013_001E\n"
    "86000US63103,ZCTA5 63103,12000,400,350,500,48000\n"
    "86000US63104,ZCTA5 63104,20000,900,800,1100,52000\n"
    "86000US99999,ZCTA5 99999,500,10,10,10,31000\n"
)
ACS_HUMAN = (
    "Geography,Geographic Area Name,Estimate!!Total:,"
    "Estimate!!Total:!!Male:!!65 to 66 years,Estimate!!Total:!!Female:!!67 to 69 years,"
    "Estimate!!Median household income\n"
    "86000US63103,ZCTA5 63103,12000,400,350,48000\n"
)


def test_demographics_import_and_market_sizing(store, tmp_path):
    from mrfx.demographics import (demographics_status, import_demographics,
                                   market_sizing)

    _seed(store)
    f = tmp_path / "ACSDT5Y2023.B01001-Data.csv"
    f.write_text(ACS)
    res = import_demographics(store, f)
    assert res["zctas"] == 3 and res["vintage"] == "2023"

    st = demographics_status(store)
    assert st["loaded"] and st["zctas"] == 3

    m = market_sizing(store, "63103", radius_miles=25)
    # 63103 + 63104 are ~1 mile apart; 99999 (Alaska) is far away
    assert m["population"] == 32000, m
    assert m["pop_65_plus"] == 4050          # 400+350+500 + 900+800+1100
    assert m["practices"] == 2, "Gateway + Peer, TIN grain"
    assert m["seniors_per_practice"] == 2025
    assert m["median_income"] == 50000
    assert m["zctas_unmatched"] >= 0 and m["loaded"]
    assert "upper bound" in m["note"]
    assert [z["zip"] for z in m["top_zips"]][0] == "63104"   # most seniors first


def test_demographics_survives_a_repeated_zcta(store, tmp_path):
    """zip is the primary key, and real exports do repeat a ZCTA (two
    geography rows, an appended file). One duplicate must not abort the whole
    import — last row wins, as the row-by-row upsert used to do."""
    from mrfx.demographics import import_demographics

    _seed(store)
    f = tmp_path / "acs_2023.csv"
    f.write_text("GEO_ID,B01001_001E,B01001_020E,B19013_001E\n"
                 "86000US63103,12000,400,48000\n"
                 "86000US63103,15000,500,51000\n"
                 "86000US63104,20000,900,52000\n")
    res = import_demographics(store, f)
    assert res["zctas"] == 2
    with store.connect() as con:
        pop = con.execute("SELECT population FROM zip_demographics WHERE zip = ?",
                          ["63103"]).fetchone()[0]
    assert pop == 15000, "the last row for a ZCTA wins"


def test_market_sizing_works_before_any_demographics_are_loaded(store):
    """The read path must PROBE for the table, never CREATE it: a
    CREATE-IF-NOT-EXISTS on a read is a catalog write two dashboard requests
    can collide on. With nothing loaded the answer is an honest reason."""
    from mrfx.demographics import demographics_status, market_sizing

    _seed(store)
    assert demographics_status(store)["loaded"] is False
    m = market_sizing(store, "63103", radius_miles=25)
    assert m["loaded"] is False and "no ZIP demographics" in m["reason"]
    assert m["population"] == 0 and m["top_zips"] == []
    # the practice count still works — it comes from the store, not the Census
    assert m["practices"] == 2
    with store.connect() as con:
        assert con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = 'zip_demographics'").fetchone()[0] == 0, \
            "a read must not have created the table"


def test_demographics_reads_the_human_labeled_export(store, tmp_path):
    """data.census.gov ships a second header row of labels; dropping it
    silently would import one junk ZCTA per file."""
    from mrfx.demographics import import_demographics, parse_acs_csv

    rows, problems = parse_acs_csv(ACS_HUMAN)
    assert len(rows) == 1
    assert rows[0]["zip"] == "63103" and rows[0]["population"] == 12000
    assert rows[0]["pop_65_plus"] == 750
    assert rows[0]["median_income"] == 48000

    f = tmp_path / "acs_2023.csv"
    f.write_text(ACS_HUMAN)
    assert import_demographics(store, f)["zctas"] == 1


def test_demographics_never_reads_suppression_as_zero(store, tmp_path):
    """ACS marks suppressed cells '-' / '(X)' / negative annotations. Reading
    one as 0 would invent an empty market."""
    from mrfx.demographics import DemographicsImportError, parse_acs_csv

    rows, _ = parse_acs_csv(
        "GEO_ID,B01001_001E,B01001_020E,B19013_001E\n"
        "86000US63103,-,(X),-666666666\n"
        "86000US63104,20000,900,52000\n")
    assert [r["zip"] for r in rows] == ["63104"], \
        "a row of pure suppression markers carries no data at all"
    assert rows[0]["population"] == 20000
    with pytest.raises(DemographicsImportError, match="does not look like"):
        parse_acs_csv("payer,rate\nAetna,30\n")


# ---------------------------------------------------------- new clinics --

def _write_cache(store, rows):
    """Stand in for the NPPES bulk cache the enrichment pass builds."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from mrfx.enrich import _NPPES_CACHE_COLS
    cols = {c: [r.get(c) for r in rows] for c in _NPPES_CACHE_COLS}
    pq.write_table(pa.table(cols, schema=pa.schema(
        [(c, pa.string()) for c in _NPPES_CACHE_COLS])),
        str(store.dir / "nppes_cache.parquet"))


def test_new_enumerations_finds_brand_new_therapy_clinics(store):
    from mrfx.nppes import feed_status, new_enumerations

    _seed(store)
    st = feed_status(store)
    assert not st["ready"] and "bulk" in st["reason"], \
        "with no cache the feed explains itself instead of returning nothing"

    recent = (dt.date.today() - dt.timedelta(days=20)).strftime("%m/%d/%Y")
    old = (dt.date.today() - dt.timedelta(days=900)).strftime("%m/%d/%Y")
    _write_cache(store, [
        {"npi": "1730153216", "org_name": "Brand New PT", "entity_type": "NPI-2",
         "taxonomy_code": "261QP2000X", "city": "StL", "state": "MO",
         "zip": "63103", "address": "9 New St", "phone": "3145550000",
         "enumeration_date": recent},
        {"npi": "1841281600", "org_name": "Old Established PT", "entity_type": "NPI-2",
         "taxonomy_code": "261QP2000X", "city": "StL", "state": "MO",
         "zip": "63103", "address": "8 Old St", "phone": None,
         "enumeration_date": old},
        {"npi": "1053308064", "org_name": "New Cardiology", "entity_type": "NPI-2",
         "taxonomy_code": "207RC0000X", "city": "StL", "state": "MO",
         "zip": "63103", "address": "7 Heart St", "phone": None,
         "enumeration_date": recent},
        {"npi": GW_N[0], "org_name": "Gateway Therapy", "entity_type": "NPI-2",
         "taxonomy_code": "261QP2000X", "city": "StL", "state": "MO",
         "zip": "63103", "address": "1 Main", "phone": None,
         "enumeration_date": recent},
    ])
    assert feed_status(store)["ready"]

    d = new_enumerations(store, days=90)
    names = {r["org_name"] for r in d["rows"]}
    assert "Brand New PT" in names
    assert "Old Established PT" not in names, "outside the window"
    assert "New Cardiology" not in names, "not a therapy taxonomy"
    # a practice already carrying published rates is a LATER-stage lead, so it
    # is kept but flagged rather than mixed in unmarked
    gw = next(r for r in d["rows"] if r["npi"] == GW_N[0])
    assert gw["in_store"] is True
    assert next(r for r in d["rows"] if r["org_name"] == "Brand New PT")["in_store"] is False
    assert "issued" in d["note"].lower()

    near = new_enumerations(store, zip_code="63103", radius_miles=25, days=90)
    assert {r["org_name"] for r in near["rows"]} == names
    far = new_enumerations(store, zip_code="99801", radius_miles=25, days=90)
    assert far["rows"] == []
    assert new_enumerations(store, days=90, state="TX")["rows"] == []


def test_new_enumerations_reports_its_scan_window(store):
    """A radius filter runs AFTER the pull, so a capped pull can hide matches.
    The cap must be reported, never left to read as 'that's all there is'."""
    from mrfx.nppes import new_enumerations

    _seed(store)
    recent = (dt.date.today() - dt.timedelta(days=10)).strftime("%m/%d/%Y")
    _write_cache(store, [
        {"npi": f"17301532{i:02d}", "org_name": f"New PT {i}", "entity_type": "NPI-2",
         "taxonomy_code": "261QP2000X", "city": "StL", "state": "MO",
         "zip": "63103", "address": "9 New St", "phone": None,
         "enumeration_date": recent} for i in range(12)])
    d = new_enumerations(store, zip_code="63103", radius_miles=25, days=90, limit=2)
    assert d["capped"] is True, "12 rows vs a 2*5 window"
    assert "most recently issued" in d["note"]
    assert len(d["rows"]) == 2
    wide = new_enumerations(store, zip_code="63103", radius_miles=25, days=90, limit=50)
    assert wide["capped"] is False and wide["total"] == 12


def test_new_enumerations_ignores_unparseable_dates(store):
    """A date we cannot read must EXCLUDE the row from a recency filter, never
    pass it through as 'new'."""
    from mrfx.nppes import new_enumerations

    _seed(store)
    recent = (dt.date.today() - dt.timedelta(days=5)).isoformat()   # ISO, not US
    _write_cache(store, [
        {"npi": "1730153216", "org_name": "ISO Dated PT", "entity_type": "NPI-2",
         "taxonomy_code": "261QP2000X", "city": "StL", "state": "MO",
         "zip": "63103", "address": "9 New St", "phone": None,
         "enumeration_date": recent},
        {"npi": "1841281600", "org_name": "Garbage Dated PT", "entity_type": "NPI-2",
         "taxonomy_code": "261QP2000X", "city": "StL", "state": "MO",
         "zip": "63103", "address": "8 Old St", "phone": None,
         "enumeration_date": "not a date"},
    ])
    got = {r["org_name"] for r in new_enumerations(store, days=30)["rows"]}
    assert got == {"ISO Dated PT"}
