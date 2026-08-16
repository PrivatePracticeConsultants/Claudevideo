"""Hospital price transparency import + parity.

The risk this feature carries is not a crash — it is a MISLEADING comparison.
A hospital's rate for 97110 is a facility payment; a practice's is a
professional fee. So the tests care most about what is kept, what is refused,
and that nothing ever presents the gap as money owed.
"""

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.hospital import (HospitalImportError, hospital_parity,
                           hospital_status, import_hospital_file)

HEADER = ["description", "code|1", "code|1|type", "setting", "payer_name",
          "plan_name", "standard_charge|gross", "standard_charge|discounted_cash",
          "standard_charge|negotiated_dollar", "standard_charge|methodology",
          "estimated_amount", "modifiers"]

ROWS = [
    ["Therapeutic exercise", "97110", "CPT", "outpatient", "Aetna", "PPO",
     "310", "180", "128.40", "fee schedule", "", ""],
    ["Manual therapy", "97140", "CPT", "outpatient", "Aetna", "PPO",
     "290", "165", "119.00", "fee schedule", "", ""],
    # percent-of-charges is not a price
    ["Therapeutic exercise", "97110", "CPT", "outpatient", "Cigna", "PPO",
     "310", "180", "", "percent of billed charges", "", ""],
    # inpatient therapy line: a different product
    ["Therapeutic exercise", "97110", "CPT", "inpatient", "Aetna", "PPO",
     "310", "180", "500.00", "fee schedule", "", ""],
    # $0 placeholder, exactly as in insurer MRFs
    ["Gait training", "97116", "CPT", "outpatient", "Aetna", "PPO",
     "300", "170", "0", "fee schedule", "", ""],
    # outside the therapy catalog
    ["Echocardiogram", "93306", "CPT", "outpatient", "Aetna", "PPO",
     "1200", "900", "640.00", "fee schedule", "", ""],
]


def _csv(tmp_path, rows=ROWS, name="st_marys.csv", preamble=True):
    buf = io.StringIO()
    w = csv.writer(buf)
    if preamble:
        w.writerow(["hospital_name", "last_updated_on", "version"])
        w.writerow(["St Mary's Regional", "2026-04-01", "2.0.0"])
    w.writerow(HEADER)
    for r in rows:
        w.writerow(r)
    p = tmp_path / name
    p.write_text(buf.getvalue())
    return p


def test_only_real_outpatient_dollar_prices_survive_the_import(cfg, store, tmp_path):
    res = import_hospital_file(store, _csv(tmp_path), state="MO", cfg=cfg)
    assert res["hospital"] == "St Mary's Regional", "identity read from the file"
    assert res["last_updated_on"] == "2026-04-01"
    with store.connect() as con:
        kept = con.execute(
            "SELECT billing_code, payer, setting, rate FROM hospital_rates "
            "ORDER BY billing_code").fetchall()
    assert kept == [("97110", "Aetna", "outpatient", 128.40),
                    ("97140", "Aetna", "outpatient", 119.00)], (
        "percent-of-charges, the inpatient line, the $0 placeholder and the "
        "cardiology code all have to be gone")


def test_an_estimate_is_kept_but_never_counted_as_a_negotiated_rate(cfg, store, tmp_path):
    """A hospital may publish only its own ESTIMATE behind an algorithm. That
    is worth keeping and worth labelling — it is not a negotiated price."""
    rows = ROWS + [["Speech therapy", "92507", "CPT", "outpatient", "Aetna",
                    "PPO", "280", "160", "", "algorithm", "102.50", ""]]
    import_hospital_file(store, _csv(tmp_path, rows), state="MO", cfg=cfg)
    with store.connect() as con:
        row = con.execute(
            "SELECT rate, estimated_amount, methodology FROM hospital_rates "
            "WHERE billing_code = '92507'").fetchone()
    assert row == (None, 102.50, "algorithm"), "an estimate, stored as one"


def test_the_json_shape_reads_the_same_and_skips_non_dollar_terms(cfg, store, tmp_path):
    doc = {
        "hospital_name": "Riverside Medical Center",
        "last_updated_on": "2026-03-15", "version": "2.0.0",
        "standard_charge_information": [
            {"description": "Therapeutic exercise",
             "code_information": [{"code": "97110", "type": "CPT"}],
             "standard_charges": [{
                 "setting": "outpatient", "gross_charge": 275,
                 "discounted_cash": 150,
                 "payers_information": [
                     {"payer_name": "Aetna", "plan_name": "Choice POS",
                      "standard_charge_dollar": 104.75},
                     {"payer_name": "Aetna", "plan_name": "HMO",
                      "standard_charge_percentage": 45},
                 ]}]},
            {"description": "Knee replacement",
             "code_information": [{"code": "27447", "type": "CPT"}],
             "standard_charges": [{
                 "setting": "inpatient",
                 "payers_information": [{"payer_name": "Aetna",
                                         "standard_charge_dollar": 30000}]}]},
        ],
    }
    p = tmp_path / "riverside.json"
    p.write_text(json.dumps(doc))
    res = import_hospital_file(store, p, state="MO", cfg=cfg)
    assert res["rows"] == 1 and res["hospital"] == "Riverside Medical Center"
    with store.connect() as con:
        assert con.execute(
            "SELECT billing_code, rate FROM hospital_rates").fetchall() == [
            ("97110", 104.75)]


def test_a_file_that_is_not_a_standard_charges_file_is_refused(cfg, store, tmp_path):
    """A silent partial import of the wrong file would put invented numbers in
    front of a client."""
    bad = tmp_path / "sales.csv"
    bad.write_text("a,b,c\n1,2,3\n")
    with pytest.raises(HospitalImportError, match="does not look like"):
        import_hospital_file(store, bad, cfg=cfg)
    with pytest.raises(HospitalImportError, match="no such file"):
        import_hospital_file(store, tmp_path / "nope.csv", cfg=cfg)


def test_a_valid_file_with_no_therapy_line_is_reported_not_failed(cfg, store, tmp_path):
    """Plenty of hospitals publish no therapy codes at all. That is an answer,
    and it must not write an empty part that later reads as data."""
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"hospital_name": "Tiny Clinic Hospital",
                             "standard_charge_information": []}))
    res = import_hospital_file(store, p, cfg=cfg)
    assert res["rows"] == 0 and "no therapy codes" in res["note"]
    assert hospital_status(store)["loaded"] is False
    d = store.dir / "hospital"
    assert not list(d.glob("*.parquet.tmp")) if d.exists() else True, \
        "no partial part left behind"


def _seed_practice(store):
    def row(tin, npi, code, rate):
        return dict(payer="Aetna", tin_value=tin, tin_type="ein", npi=npi,
                    source_file="p.json", billing_code=code,
                    billing_code_type="CPT", discipline="PT", is_timed=True,
                    billing_class="professional", negotiated_rate=rate,
                    negotiated_type="negotiated", is_dollar_rate=True,
                    billing_code_modifier=[], service_code=["11"],
                    file_month="2026-06", last_updated_on="2026-06-01",
                    expiration_date=None, schema_version="2.0.0",
                    tin_is_really_npi=False, state="MO")
    with store.rates_part_writer("p.json") as w:
        w.write_batch([row("431234567", "1417594896", "97110", 42.0),
                       row("437654321", "1999999992", "97110", 46.0),
                       row("431234567", "1417594896", "97140", 38.0)])
    store.save_npis_bulk([
        dict(npi=n, org_name=f"P{i}", entity_type="NPI-2",
             taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X",
             city="Springfield", state="MO", address="x", zip="65801", phone=None)
        for i, n in enumerate(("1417594896", "1999999992"))])
    store.rebuild_rollups()


def test_parity_reports_a_ratio_and_never_an_amount_owed(cfg, store, tmp_path):
    _seed_practice(store)
    import_hospital_file(store, _csv(tmp_path), state="MO", cfg=cfg)

    par = hospital_parity(store, {"month": "2026-06", "therapy_only": False},
                          payer="Aetna", subject="431234567")
    by_code = {r["billing_code"]: r for r in par["rows"]}
    a = by_code["97110"]
    assert a["practice_median"] == 44.0        # median(42, 46)
    assert a["hospital_median"] == 128.40
    assert a["hospital_multiple"] == 2.92      # 128.40 / 44.00
    assert a["subject_rate"] == 42.0
    assert a["subject_multiple"] == 3.06       # 128.40 / 42.00
    assert a["n_practices"] == 2 and a["n_hospitals"] == 1
    # the honesty rail: a ratio, and no field that reads as money owed
    assert not any(k for k in a if "opportunity" in k or "owed" in k)
    assert "FACILITY" in par["caveat"] and "never as an amount" in par["caveat"]
    assert "never fuzzy-matches payer names" in par["note"]


def test_a_payer_named_differently_in_the_two_sources_does_not_match(cfg, store, tmp_path):
    """Hospitals name payers freely ('Aetna Better Health of MO'). Guessing
    that it is the same payer would invent a comparison."""
    _seed_practice(store)
    rows = [["Therapeutic exercise", "97110", "CPT", "outpatient",
             "Aetna Better Health of Missouri", "PPO", "310", "180", "128.40",
             "fee schedule", "", ""]]
    import_hospital_file(store, _csv(tmp_path, rows), state="MO", cfg=cfg)
    par = hospital_parity(store, {"month": "2026-06", "therapy_only": False})
    assert par["rows"] == [], "no fuzzy payer matching"
    assert "payer name matches" in par["reason"]


def test_the_api_imports_compares_and_forgets(cfg, store, tmp_path):
    _seed_practice(store)
    c = TestClient(create_app(cfg, store))

    assert c.get("/api/hospital/status").json()["loaded"] is False
    p = _csv(tmp_path)
    with open(p, "rb") as fh:
        r = c.post("/api/hospital/import?state=MO&city=Springfield",
                   files={"file": ("st_marys.csv", fh, "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == 2 and r.json()["status"]["loaded"] is True

    par = c.post("/api/hospital/parity",
                 json={"payer": "Aetna", "subject": "431234567",
                       "market": {"month": "2026-06", "therapy_only": False}}).json()
    assert par["median_hospital_multiple"] == 3.02   # median(2.92, 3.13)

    # a junk upload is a 422 with a reason, never a 500
    with open(tmp_path / "junk.csv", "w") as fh:
        fh.write("a,b\n1,2\n")
    with open(tmp_path / "junk.csv", "rb") as fh:
        assert c.post("/api/hospital/import",
                      files={"file": ("junk.csv", fh, "text/csv")}).status_code == 422

    d = c.delete("/api/hospital/St Mary's Regional").json()
    assert d["removed"] is True and d["status"]["loaded"] is False


def test_hospital_rates_never_enter_the_practice_rate_spine(cfg, store, tmp_path):
    """The single most damaging thing this importer could do is let a facility
    rate into `rates` — every practice median in the app would move."""
    _seed_practice(store)
    with store.connect() as con:
        before = con.execute("SELECT count(*), sum(negotiated_rate) FROM rates").fetchone()
    import_hospital_file(store, _csv(tmp_path), state="MO", cfg=cfg)
    store.rebuild_rollups()
    with store.connect() as con:
        after = con.execute("SELECT count(*), sum(negotiated_rate) FROM rates").fetchone()
        med = con.execute(
            "SELECT median(negotiated_rate) FROM rates_by_tin "
            "WHERE billing_code = '97110'").fetchone()[0]
    assert before == after, "the rate spine must be byte-for-byte untouched"
    assert med == 44.0, "still the practice median, not blended with 128.40"
