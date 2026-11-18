"""Requests that returned HTTP 500 or silently answered a different question
(round 6, P0.3). Each test posts the exact body that used to fail."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.mrfx.test_known_answer_leads import TINS, leads_app  # noqa: F401  (fixture)


def test_underpayment_check_with_a_state_is_answered(leads_app):  # noqa: F811
    """A state in the market referenced the practice directory this query never
    joined: DuckDB binder error, HTTP 500. The practice's OWN rate ignores peer
    filters (the rate card's rule): A's Rho 97110 is $30, a $20 payment is $10
    short — with or without a state."""
    for market in ({"month": "latest"}, {"month": "latest", "state": "MO"},
                   {"month": "latest", "state": "Missouri", "therapy_only": True}):
        r = leads_app.post("/api/remits/check", json={
            "subject": "A REHAB", "text": "payer,cpt,units,paid\nRho Health,97110,1,20.00",
            "market": market})
        assert r.status_code == 200, (market, r.text)
        flagged = r.json()["flagged"]
        assert [(f["billing_code"], f["published_rate"], f["shortfall"]) for f in flagged] == [
            ("97110", 30.0, 10.0)], market


def test_a_state_name_finds_the_same_rate_changes_as_its_code(leads_app):  # noqa: F811
    body = lambda st: {"market": {"month": "2026-06", "prev_month": "2026-05", "state": st}}  # noqa: E731
    mo = leads_app.post("/api/changes", json=body("MO")).json()
    name = leads_app.post("/api/changes", json=body("Missouri")).json()
    assert mo["count"] == name["count"] == 1          # B 97110 $20 -> $40
    assert leads_app.post("/api/changes", json=body("Nowhere")).status_code == 422


def test_hospital_parity_with_a_state_name(cfg, store, tmp_path):
    from mrfx.hospital import hospital_parity, import_hospital_file
    from tests.mrfx.test_hospital import _csv, _seed_practice
    _seed_practice(store)
    import_hospital_file(store, _csv(tmp_path), state="MO", cfg=cfg)
    m = {"month": "2026-06", "therapy_only": False}
    by_code = lambda st: {r["billing_code"]: r["practice_median"]  # noqa: E731
                          for r in hospital_parity(store, m, payer="Aetna", state=st)["rows"]}
    assert by_code("Missouri") == by_code("MO") == by_code(None)
    assert by_code("MO")["97110"] == 44.0


@pytest.mark.parametrize("comparables", ["B REHAB", 5, {"a": 1}])
def test_proposal_comparables_must_be_a_list(leads_app, comparables):  # noqa: F811
    for route in ("/api/negotiate/proposal", "/api/report/proposal",
                  "/api/negotiate/compare", "/api/report/payer-compare"):
        r = leads_app.post(route, json={
            "subject": "A REHAB", "payer": "Rho Health",
            "market": {"month": "latest", "state": "MO"}, "target": {"kind": "p50"},
            "comparables": comparables})
        assert r.status_code == 422 and "list of practice names" in r.json()["detail"], (route, r.text)
    ok = leads_app.post("/api/negotiate/compare", json={
        "subject": "A REHAB", "payer": "Rho Health", "market": {"month": "latest", "state": "MO"},
        "comparables": ["B REHAB"]}).json()
    assert [c["name"] for c in ok["comparables"]] == ["B REHAB"]


def test_proposal_bad_percentile_is_a_refusal_not_a_500(leads_app):  # noqa: F811
    r = leads_app.post("/api/negotiate/proposal", json={
        "subject": "A REHAB", "payer": "Rho Health", "target": {"kind": "p50"},
        "market": {"month": "latest", "state": "MO", "target_percentile": "lots"}})
    assert r.status_code == 422, r.text


def test_proposal_pct_of_medicare_with_a_state_name(leads_app, store):  # noqa: F811
    """The compare normalized "Missouri" to MO and found the MO fee schedule;
    the proposal read the raw "MISSOURI" and refused it."""
    store.load_mpfs([{"code": "97110", "non_facility_rate": 31.0},
                     {"code": "97140", "non_facility_rate": 28.0},
                     {"code": "97530", "non_facility_rate": 35.0}],
                    source="test MPFS", state="MO")
    for st in ("MO", "Missouri"):
        r = leads_app.post("/api/negotiate/proposal", json={
            "subject": "A REHAB", "payer": "Rho Health",
            "market": {"month": "latest", "state": st},
            "target": {"kind": "pct_medicare", "value": 110}})
        assert r.status_code == 200, (st, r.text)
        rows = {x["billing_code"]: x["proposed"] for x in r.json()["rows"]}
        assert rows["97110"] == 34.1                  # 110% x $31.00 > current $30


def test_negotiate_compare_and_proposal_under_a_plan(cfg, store):
    """The 'other payers' market was built with public_market(), which drops
    the resolved plan's file list — every plan-scoped compare refused, telling
    the user to use … Negotiate."""
    from mrfx.api import create_app
    from tests.mrfx.test_plans import _seed_two_networks
    _seed_two_networks(store)
    c = TestClient(create_app(cfg, store))
    for plan, expect in (("Testco Narrow HMO", 28.0), ("Testco Broad PPO", 40.0)):
        m = {"month": "2026-06", "therapy_only": False, "plan": plan, "allow_national": True}
        r = c.post("/api/negotiate/compare", json={"subject": "431234567", "payer": "Testco", "market": m})
        assert r.status_code == 200, r.text
        row = next(x for x in r.json()["rows"] if x["billing_code"] == "97110")
        assert row["p50"] == expect
        p = c.post("/api/negotiate/proposal", json={"subject": "431234567", "payer": "Testco",
                                                     "market": m, "target": {"kind": "p50"}})
        assert p.status_code == 200, p.text
