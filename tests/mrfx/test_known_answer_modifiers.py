"""Known answers for the three modifier-field patterns measured in real payer
files (round 3, see mrfx/modifiers.py), pushed through the real pipeline
(inbox -> parser -> store -> rollups -> API) and checked against arithmetic
done by hand below.

Payer "Beta Health", one file, 2026-05, all professional, all 97110:

  T4 [X]  $30  modifiers ["21","31","22","24"]  service_code ["01"]   (BCBS AL:
          $32  modifiers ["32","12","11","10"]  service_code ["01"]    POS codes
                                                                       in the
                                                                       modifier field)
  T5 [Y]  $45  no modifier                                             (First Choice:
          $60  modifiers ["26","TC","RR","NU","UE"]                    a different
                                                                       number for a
                                                                       modifier list)
  T6 [Z]  $38  no modifier, service_code ["11"]                        (BCBS ND:
          $40  ["22"]       service_code ["11"]                        two modifiers
          $50  ["52,53"]    service_code ["11"]                        in one element)
  T7 [X]  $36  ["GP","GO"]                                             (discipline only)

By hand:
  T4's lists hold 10/11/12/21/31 — numbers no CPT modifier uses — so they are
  places of service: modifier '' and POS 01|21|22|24|31 ($30) and
  01|10|11|12|32 ($32). Under "base only" T4 counts, median(30, 32) = 31.
  T5 base = $45 only; its $60 keeps modifier set 26|NU|RR|TC|UE (not base).
  T6 base = $38; 22 stays a modifier (a valid one); "52,53" -> 52|53.
  T7 base = $36 (GO|GP is discipline-only).
  Benchmark, subject T7, peers T4 31 / T5 45 / T6 38 (sorted 31, 38, 45):
    n_peers 3, p50 = 38, p25 = 31 + .5*(38-31) = 34.5, p75 = 38 + .5*(45-38)
    = 41.5; subject 36 has 1 peer below, 0 ties -> percentile 100*1/3 = 33.
  QA: pos_in_modifier_rows = 2 (T4's two prices x 1 NPI), modifier_split_rows = 1.
"""

from __future__ import annotations

import json

import pytest

X, Y, Z = "1444444444", "1555555555", "1666666666"
T4, T5, T6, T7 = "440000004", "550000005", "660000006", "770000007"


def _price(rate, mods=None, svc=None):
    p = {"negotiated_type": "negotiated", "negotiated_rate": rate,
         "billing_class": "professional", "expiration_date": "9999-12-31"}
    if mods is not None:
        p["billing_code_modifier"] = list(mods)
    if svc is not None:
        p["service_code"] = list(svc)
    return p


def _rate(npis, tin, prices):
    return {"provider_groups": [{"npi": npis, "tin": {"type": "ein", "value": tin}}],
            "negotiated_prices": prices}


FILE = {
    "reporting_entity_name": "Beta Health", "reporting_entity_type": "issuer",
    "last_updated_on": "2026-05-01", "version": "2.0.0", "provider_references": [],
    "in_network": [{
        "negotiation_arrangement": "ffs", "name": "97110", "billing_code_type": "CPT",
        "billing_code_type_version": "2026", "billing_code": "97110", "description": "x",
        "negotiated_rates": [
            _rate([X], T4, [_price(30.0, ["21", "31", "22", "24"], ["01"]),
                            _price(32.0, ["32", "12", "11", "10"], ["01"])]),
            _rate([Y], T5, [_price(45.0), _price(60.0, ["26", "TC", "RR", "NU", "UE"])]),
            _rate([Z], T6, [_price(38.0, None, ["11"]), _price(40.0, ["22"], ["11"]),
                            _price(50.0, ["52,53"], ["11"])]),
            _rate([X], T7, [_price(36.0, ["GP", "GO"])]),
        ]}],
}
NAME = "beta_2026-05_in-network.json"


@pytest.fixture
def kb(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from mrfx.ingest import scan_inbox

    (cfg.inbox_dir / NAME).write_text(json.dumps(FILE))
    results = {r["file"]: r for r in scan_inbox(cfg, store)}
    return results, TestClient(create_app(cfg, store)), store


def _ranked(c, qs=""):
    d = c.get(f"/api/code/97110?grain=tin{qs}").json()
    return sorted((r["unit_id"], r["modifier_set"], r["median_rate"], r["n"]) for r in d["ranked"])


def test_rows_and_qa_counters_by_hand(kb):
    results, c, _ = kb
    assert results[NAME]["rows"] == 8
    qa = next(f for f in c.get("/api/files").json()["files"] if f["filename"] == NAME)["qa"]
    assert qa["pos_in_modifier_rows"] == 2
    assert qa["modifier_split_rows"] == 1


def test_stored_fields_by_hand(kb):
    _, c, _ = kb
    rows = c.get("/api/rates?grain=tin&cpt=97110&limit=100").json()["rows"]
    got = sorted((r["tin_value"], r["modifier_set"], r["service_code_set"], r["negotiated_rate"])
                 for r in rows)
    assert got == sorted([
        (T4, "", "01|21|22|24|31", 30.0),
        (T4, "", "01|10|11|12|32", 32.0),
        (T5, "", "", 45.0),
        (T5, "26|NU|RR|TC|UE", "", 60.0),
        (T6, "", "11", 38.0),
        (T6, "22", "11", 40.0),
        (T6, "52|53", "11", 50.0),
        (T7, "GO|GP", "", 36.0),
    ])


def test_code_page_base_only_by_hand(kb):
    _, c, _ = kb
    assert _ranked(c, "&modifier=base") == sorted([
        (T4, "", 31.0, 2), (T5, "", 45.0, 1), (T6, "", 38.0, 1), (T7, "GO|GP", 36.0, 1)])
    # every modifier set, unfiltered: the non-base lines appear separately
    assert _ranked(c) == sorted([
        (T4, "", 31.0, 2), (T5, "", 45.0, 1), (T5, "26|NU|RR|TC|UE", 60.0, 1),
        (T6, "", 38.0, 1), (T6, "22", 40.0, 1), (T6, "52|53", 50.0, 1),
        (T7, "GO|GP", 36.0, 1)])


def test_split_modifier_is_findable_and_pos_filter_reads_moved_codes(kb):
    _, c, _ = kb
    has53 = c.get("/api/rates?grain=tin&cpt=97110&mod_has=53&limit=100").json()["rows"]
    assert [(r["tin_value"], r["negotiated_rate"]) for r in has53] == [(T6, 50.0)]
    office = c.get("/api/rates?grain=tin&cpt=97110&pos=11&modifier=base&limit=100").json()["rows"]
    assert sorted((r["tin_value"], r["negotiated_rate"]) for r in office) == [(T4, 32.0), (T6, 38.0)]
    inpatient = c.get("/api/rates?grain=tin&cpt=97110&pos=21&limit=100").json()["rows"]
    assert [(r["tin_value"], r["negotiated_rate"]) for r in inpatient] == [(T4, 30.0)]


def test_benchmark_uses_the_same_base_rule_by_hand(kb):
    _, c, _ = kb
    r = c.post("/api/benchmark/market", json={
        "subject": T7, "market": {"payers": ["Beta Health"], "codes": ["97110"]}}).json()
    row = next(x for x in r["rows"] if x["billing_code"] == "97110")
    assert row["n_peers"] == 3
    assert (row["p25"], row["p50"], row["p75"]) == (34.5, 38.0, 41.5)
    assert row["subject_rate"] == 36.0
    assert row["subject_percentile"] == 33
    assert "GP/GO/GN" in r["basis_note"]


def test_export_methodology_states_the_modifier_rules(kb):
    import io
    import zipfile
    _, c, _ = kb
    z = zipfile.ZipFile(io.BytesIO(c.get("/api/export.zip?grain=tin&modifier=base").content))
    meth = z.read(next(n for n in z.namelist() if "methodology" in n.lower())).decode()
    assert "place of service" in meth and "52,53" in meth
    assert "GP/GO/GN" in meth
