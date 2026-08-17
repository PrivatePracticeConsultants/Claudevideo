"""Locality-adjusted MPFS import from the official CMS RVU delivery."""

import io
import zipfile

import pytest

from mrfx.mpfs import (MpfsImportError, compute_locality_rates, import_official,
                       looks_official, parse_gpci, parse_pprrvu)

# headers as CMS ships them (names drift by year — token matching handles it)
PPRRVU = (
    "HCPCS,MOD,DESCRIPTION,STATUS CODE,WORK RVU,NON-FAC PE RVU,"
    "FACILITY PE RVU,MP RVU,NON-FACILITY TOTAL,GLOBAL\n"
    "97110,,Therapeutic exercises,A,0.45,0.42,0.16,0.02,0.89,XXX\n"
    "97110,26,Prof component split,A,0.30,0.10,0.10,0.01,0.41,XXX\n"   # mod: skip
    "97140,,Manual therapy,A,0.43,0.40,0.15,0.02,0.85,XXX\n"
    "0042T,,Investigational thing,I,1.00,1.00,1.00,1.00,3.00,XXX\n"    # not payable
    "92507,,Speech treatment,A,1.30,0.95,0.50,0.05,2.30,XXX\n")
GPCI = (
    "MAC,STATE,LOCALITY NUMBER,LOCALITY NAME,2025 PW GPCI,2025 PE GPCI,2025 MP GPCI\n"
    "05302,MO,01,ST. LOUIS,1.000,0.966,1.104\n"
    "05302,MO,02,KANSAS CITY,0.997,0.951,1.079\n"
    "05302,MO,99,REST OF MISSOURI,1.000,0.877,1.019\n"
    "05102,IA,00,IOWA,1.000,0.892,0.518\n")
CF = 32.35


def _expected(work, pe, mp, g=(1.000, 0.877, 1.019)):
    return round((work * g[0] + pe * g[1] + mp * g[2]) * CF, 2)


def test_pprrvu_parses_payable_unmodified_rows_only():
    rvus = parse_pprrvu(PPRRVU)
    assert set(rvus) == {"97110", "97140", "92507"}      # no 26-split, no status I
    assert rvus["97110"] == {"work": 0.45, "pe_nonfac": 0.42, "mp": 0.02}


def test_gpci_locality_selection_and_disclosure():
    # default: the state's REST OF locality
    g = parse_gpci(GPCI, "MO")
    assert g["name"] == "REST OF MISSOURI" and g["pe"] == 0.877
    # explicit locality by (partial, case-blind) name
    g2 = parse_gpci(GPCI, "MO", "st. louis")
    assert g2["name"] == "ST. LOUIS" and g2["pe"] == 0.966
    # single-locality state needs no default logic
    assert parse_gpci(GPCI, "IA")["name"] == "IOWA"
    with pytest.raises(MpfsImportError, match="available"):
        parse_gpci(GPCI, "MO", "narnia")
    # a real state simply absent from this file
    with pytest.raises(MpfsImportError, match="no GPCI rows"):
        parse_gpci(GPCI, "CA")
    # not a state at all — refused by name, before we go looking for rows
    with pytest.raises(MpfsImportError, match="not a US state code"):
        parse_gpci(GPCI, "ZZ")
    # and the state NAME resolves to its own code, not its first two letters
    assert parse_gpci(GPCI, "Missouri")["name"] == "REST OF MISSOURI"


def test_rates_match_the_published_formula():
    rows = {r["code"]: r for r in compute_locality_rates(
        parse_pprrvu(PPRRVU), parse_gpci(GPCI, "MO"), CF)}
    assert rows["97110"]["non_facility_rate"] == _expected(0.45, 0.42, 0.02)
    assert rows["92507"]["non_facility_rate"] == _expected(1.30, 0.95, 0.05)
    assert rows["97110"]["locality"] == "REST OF MISSOURI"
    # a fat-fingered CF (cents, yearly total, …) is refused, not multiplied
    for bad in (0.3235, 3235.0):
        with pytest.raises(MpfsImportError, match="plausible"):
            compute_locality_rates(parse_pprrvu(PPRRVU), parse_gpci(GPCI, "MO"), bad)


def test_official_zip_loads_into_the_store_and_says_its_basis(store):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("PPRRVU25_JAN.csv", PPRRVU)
        z.writestr("GPCI2025.csv", GPCI)
        z.writestr("25LOCCO.csv", "county crosswalk, unused")
    assert looks_official(buf.getvalue(), "rvu25a.zip")
    r = import_official(store, buf.getvalue(), "rvu25a.zip",
                        conversion_factor=CF, state="MO")
    assert r["rows"] == 3 and r["locality"] == "REST OF MISSOURI"
    src = store.mpfs_loaded()
    # the basis is in the stored source string — every methodology prints it
    assert "REST OF MISSOURI" in src and "32.35" in src and "locality-adjusted" in src
    with store.connect() as con:
        rate = con.execute(
            "SELECT non_facility_rate FROM mpfs WHERE code = '97110'").fetchone()[0]
    assert rate == _expected(0.45, 0.42, 0.02)

    # a bare PPRRVU csv without its GPCI is refused with the fix named
    with pytest.raises(MpfsImportError, match="GPCI"):
        import_official(store, PPRRVU.encode(), "PPRRVU25.csv",
                        conversion_factor=CF, state="MO")
    # and garbage is not an RVU delivery
    with pytest.raises(MpfsImportError):
        import_official(store, b"PK\x03\x04junk", "x.zip",
                        conversion_factor=CF, state="MO")


def test_simple_csv_path_is_unchanged(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    client = TestClient(create_app(cfg, store))
    r = client.post("/api/mpfs/upload",
                    files={"file": ("simple.csv",
                                    b"code,non_facility_rate\n97110,31.40\n")})
    assert r.status_code == 200 and r.json()["rows"] == 1
    # official zip without CF/state: a clear 422 naming the missing box
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("PPRRVU25.csv", PPRRVU)
        z.writestr("GPCI2025.csv", GPCI)
    r = client.post("/api/mpfs/upload", files={"file": ("rvu.zip", buf.getvalue())})
    assert r.status_code == 422 and "conversion factor" in r.json()["detail"]
    r = client.post("/api/mpfs/upload?cf=32.35&state=MO&locality=st.%20louis",
                    files={"file": ("rvu.zip", buf.getvalue())})
    assert r.status_code == 200 and r.json()["locality"] == "ST. LOUIS"
