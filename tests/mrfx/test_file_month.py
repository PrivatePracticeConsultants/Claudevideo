"""file_month = the payer's statement of vintage, never a date in the future
(round 3, P0.6). Measured in the real store: a UHC HEALTHSCOPE file read on
2026-11-05 carries last_updated_on 2026-12-01 — the Months list offered
December in November, "latest" preferred it, and the real December file would
later have pooled with it as ONE month. BCBS Mississippi's October files carry
2026-08-25 (header month kept; the published month is shown beside it)."""

from __future__ import annotations

import json

import pytest

from mrfx.parser import file_month_for, published_month_of
from mrfx.store import ROW_RULES_VERSION


@pytest.mark.parametrize("hdr, name, im, want", [
    ("2026-12-01", "uhc_2026-11-01_HEALTHSCOPE.json.gz", "2026-11", ("2026-11", True)),
    ("2026-08-25", "bcbsms_2026-10_320_33B0.json.gz", "2026-11", ("2026-08", False)),
    ("2026-11-01", "tufts_2026-11-01.zip", "2026-11", ("2026-11", False)),
    (None, "x_2026-10_rates.json", "2026-11", ("2026-10", False)),
    (None, "nodate.json", "2026-11", ("2026-11", False)),
    ("2027-01-01", "nodate.json", "2026-11", ("2026-11", True)),      # no name date -> month read
    ("2026-12-01", "x_2026-12.json", "2026-11", ("2026-11", True)),   # name also future -> month read
    ("2024-13-01", "plan_2024_2025-03_rates.json", "2026-11", ("2025-03", False)),
])
def test_month_rule(hdr, name, im, want):
    assert file_month_for(hdr, name, im) == want


def test_published_month():
    assert published_month_of("bcbsms_2026-10_320.json.gz", "2026-11") == "2026-10"
    assert published_month_of("nodate.json", "2026-11") == "2026-11"


def _mrf(day, rate):
    return {"reporting_entity_name": "Echo Health", "reporting_entity_type": "issuer",
            "last_updated_on": day, "version": "2.0.0", "provider_references": [],
            "in_network": [{"negotiation_arrangement": "ffs", "name": "x",
                            "billing_code_type": "CPT", "billing_code_type_version": "2026",
                            "billing_code": "97110", "description": "x",
                            "negotiated_rates": [{
                                "provider_groups": [{"npi": ["1999000001"],
                                                     "tin": {"type": "ein", "value": "999000001"}}],
                                "negotiated_prices": [{"negotiated_type": "negotiated",
                                                       "negotiated_rate": rate,
                                                       "billing_class": "professional",
                                                       "service_code": ["11"],
                                                       "expiration_date": "9999-12-31"}]}]}]}


FUTURE = "echo_2026-05_in-network.json"     # header 2099-01-01: from the future
LAGGED = "echo_2026-06_in-network.json"     # header 2026-04-20: the payer's clock lags


@pytest.fixture
def km(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from mrfx.ingest import scan_inbox
    (cfg.inbox_dir / FUTURE).write_text(json.dumps(_mrf("2099-01-01", 40.0)))
    (cfg.inbox_dir / LAGGED).write_text(json.dumps(_mrf("2026-04-20", 38.0)))
    list(scan_inbox(cfg, store))
    return TestClient(create_app(cfg, store))


def test_future_header_is_filed_under_its_name_month(km):
    assert km.get("/api/months").json()["months"] == ["2026-05", "2026-04"]
    files = {f["filename"]: f for f in km.get("/api/files").json()["files"]}
    f = files[FUTURE]
    assert (f["file_month"], f["published_month"], f["qa"]["future_header_date"]) == (
        "2026-05", "2026-05", "2099-01-01")
    g = files[LAGGED]
    assert (g["file_month"], g["published_month"], g["qa"]["future_header_date"]) == (
        "2026-04", "2026-06", "")
    # latest per contract = the file the payer dated last that is not from the future
    rows = km.get("/api/rates?grain=tin&cpt=97110&latest=1&limit=10").json()["rows"]
    assert [(r["file_month"], r["negotiated_rate"]) for r in rows] == [("2026-05", 40.0)]


def test_an_older_store_with_a_future_month_is_fixed_once(cfg, store, monkeypatch):
    from mrfx.ingest import scan_inbox
    from mrfx.store import Store
    old = lambda h, n, im: ((h or "")[:7] or im, False)   # noqa: E731 — the pre-round-3 rule
    monkeypatch.setattr("mrfx.parser.file_month_for", old)
    (cfg.inbox_dir / FUTURE).write_text(json.dumps(_mrf("2099-01-01", 40.0)))
    list(scan_inbox(cfg, store))
    with store.connect() as con:
        assert con.execute("SELECT DISTINCT file_month FROM rates").fetchall() == [("2099-01",)]
        con.execute("UPDATE files SET row_rules = 1, file_month = NULL, published_month = NULL")
    store.update_rollups_incremental()
    store.close()
    monkeypatch.undo()
    st = Store(cfg.store_dir)
    with st.connect() as con:
        assert con.execute("SELECT DISTINCT file_month FROM rates").fetchall() == [("2026-05",)]
        fm, pm, rules, qa = con.execute(
            "SELECT file_month, published_month, row_rules, qa FROM files").fetchone()
    assert (fm, pm, rules, json.loads(qa)["future_header_date"]) == ("2026-05", "2026-05", ROW_RULES_VERSION,
                                                                      "2099-01-01")
    assert st.rollups_stale()          # its payer is queued for the analytics
    st.update_rollups_incremental()
    with st.connect() as con:
        assert con.execute("SELECT DISTINCT file_month FROM rates_by_tin").fetchall() == [
            ("2026-05",)]
    st.close()
