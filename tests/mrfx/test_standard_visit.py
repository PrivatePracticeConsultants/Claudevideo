"""One standard visit, priced per payer (mrfx/visit.py).

Units must be exactly what CMS's 8-minute rule bills (Medicare Claims
Processing Manual, Pub. 100-04, ch. 5, sec. 20.2): its five worked examples
are reproduced below verbatim. Prices are worked by hand on the Leads
known-answer fixture, where practice A's latest rates are
  Rho Health:   97110 $30, 97140 $25, 97530 $40, 97112 $99
  Sigma Health: 97110 $34 (nothing else)
"""

from __future__ import annotations

import pytest

from mrfx.visit import (PRESETS, VisitError, define_visit, parse_lines,
                        units_by_8_minute_rule, units_per_code)
from tests.mrfx.test_known_answer_leads import leads_app  # noqa: F401  (fixture)

MKT = {"month": "latest"}


# --- the 8-minute rule, from the CMS manual's own examples -------------------

@pytest.mark.parametrize("lines,billed", [
    # ex. 1: 24 min 97112 + 23 min 97110 = 47 min -> 3 units, more to the longer
    ([("97112", 24), ("97110", 23)], {"97112": 2, "97110": 1}),
    # ex. 2: 20 + 20 = 40 min -> 3 units, "choose either code for 2 units"
    ([("97112", 20), ("97110", 20)], {"97112": 2, "97110": 1}),
    # ex. 3: 33 min 97110 + 7 min 97140 = 40 -> 2 units 97110, 1 unit 97140
    ([("97110", 33), ("97140", 7)], {"97110": 2, "97140": 1}),
    # ex. 4: 18 + 13 + 10 + 8 = 49 -> 3 units; the ultrasound is not billable
    ([("97110", 18), ("97140", 13), ("97116", 10), ("97035", 8)],
     {"97110": 1, "97140": 1, "97116": 1, "97035": 0}),
    # ex. 5: 7 + 7 + 7 = 21 -> ONE unit for one of the codes
    ([("97112", 7), ("97110", 7), ("97140", 7)], {"97112": 1, "97110": 0, "97140": 0}),
])
def test_units_are_the_cms_manual_examples(lines, billed):
    assert units_by_8_minute_rule(lines) == billed


@pytest.mark.parametrize("minutes,units", [
    (7, 0), (8, 1), (22, 1), (23, 2), (37, 2), (38, 3), (52, 3), (53, 4),
    (67, 4), (68, 5), (82, 5), (113, 8), (127, 8)])
def test_the_unit_table(minutes, units):
    assert units_by_8_minute_rule([("97110", minutes)]) == {"97110": units}


def test_counting_code_by_code_is_reported_when_it_differs():
    # ex. 4 counted per code (>= 8 min each) is 4 units, the CMS count 3
    assert sum(units_per_code([("97110", 18), ("97140", 13), ("97116", 10), ("97035", 8)]).values()) == 4
    v = define_visit(parse_lines([("97110", 18), ("97140", 13), ("97116", 10), ("97035", 8)]))
    assert (v["units"], v["units_per_code_rule"]) == (3, 4)
    assert any("4 units, not 3" in f for f in v["flags"])
    assert any(f.startswith("97035: performed but not billable") for f in v["flags"])


# --- the visits offered --------------------------------------------------------

def test_every_preset_is_four_units_at_fifteen_minutes_each_and_agrees_both_ways():
    for key, p in PRESETS.items():
        v = define_visit(parse_lines(p["lines"]))
        assert (v["units"], v["units_per_code_rule"], v["total_timed_minutes"]) == (4, 4, 60), key


def test_the_97140_97530_pair_is_flagged_and_the_alternative_is_not():
    flagged = define_visit(parse_lines(PRESETS["ortho"]["lines"]))["flags"]
    assert any("97140 with 97530" in f and "59 or XU" in f for f in flagged)
    assert define_visit(parse_lines(PRESETS["ortho_no_pair"]["lines"]))["flags"] == []
    # a pair only counts when BOTH are billed: 97530 left with no unit is no pair
    v = define_visit(parse_lines([("97110", 30), ("97140", 15), ("97530", 4)]))  # 49 min = 3
    assert [r["units"] for r in v["lines"]] == [2, 1, 0]
    assert not any("97140 with 97530" in f for f in v["flags"])


def test_definitions_that_cannot_be_billed_are_refused():
    assert parse_lines("97110, 15\n97110, 15\n97140 10") == [("97110", 30), ("97140", 10)]
    for bad, msg in (("97111, 15", "not a therapy code"), ("97110, abc", "must be a number"),
                     ("97110, 0", "between 1 and"), ("97110", "one code and its minutes"),
                     ("", "no codes")):
        with pytest.raises(VisitError, match=msg):
            parse_lines(bad)


# --- priced, by hand --------------------------------------------------------------

def _visit(c, **body):
    r = c.post("/api/schedule/visit", json={"subject": "A REHAB", "market": MKT, **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_default_visit_is_the_hand_answer(leads_app):  # noqa: F811
    d = _visit(leads_app)
    assert d["units"] == 4 and d["preset"] == "ortho"
    rho, sigma = d["rows"]
    assert (rho["payer"], rho["visit_value"], rho["complete"]) == ("Rho Health", 194.0, True)
    assert [(p["code"], p["units"], p["rate"]) for p in rho["parts"]] == [
        ("97110", 1, 30.0), ("97530", 1, 40.0), ("97140", 1, 25.0), ("97112", 1, 99.0)]
    # Sigma prices one code of four: no total, never $34 as "a visit"
    assert (sigma["payer"], sigma["visit_value"], sigma["complete"]) == ("Sigma Health", None, False)
    assert (sigma["missing_codes"], sigma["priced_value"], sigma["priced_units"]) == (
        ["97530", "97140", "97112"], 34.0, 1)
    assert d["headline"].startswith("One visit (97110×1 + 97530×1 + 97140×1 + 97112×1, 4 units) "
                                    "is worth $194.00 from Rho Health")


def test_other_visits_are_the_hand_answer(leads_app):  # noqa: F811
    # 97110 x2 + 97140 + 97112 = 2*30 + 25 + 99
    assert _visit(leads_app, preset="ortho_no_pair")["rows"][0]["visit_value"] == 184.0
    # CMS example 3 as a custom visit: 97110 x2 + 97140 x1 = 60 + 25
    d = _visit(leads_app, lines="97110, 33\n97140, 7")
    assert (d["preset"], d["units"], d["rows"][0]["visit_value"]) == (None, 3, 85.0)
    # one code both payers price: both complete, best first, the gap stated
    d = _visit(leads_app, lines=[{"code": "97110", "minutes": 15}])
    assert [(r["payer"], r["visit_value"]) for r in d["rows"]] == [
        ("Sigma Health", 34.0), ("Rho Health", 30.0)]
    assert "$4.00 a visit apart" in d["headline"]
    # B has no 97112 rate with anyone: nobody is complete, and it says so
    b = leads_app.post("/api/schedule/visit", json={"subject": "B REHAB", "market": MKT}).json()
    assert b["complete_count"] == 0 and "No payer publishes a rate for every code" in b["headline"]


def test_the_endpoints_refuse_plainly_and_list_the_presets(leads_app):  # noqa: F811
    r = leads_app.post("/api/schedule/visit", json={"subject": "A REHAB", "lines": "97111, 15"})
    assert r.status_code == 422 and "not a therapy code" in r.json()["detail"]
    r = leads_app.post("/api/schedule/visit", json={"subject": "A REHAB", "preset": "nope"})
    assert r.status_code == 422
    p = leads_app.get("/api/schedule/visit/presets").json()
    assert p["default"] == "ortho" and [x["key"] for x in p["presets"]] == list(PRESETS)
    assert "8-minute rule" in p["units_rule"]


def test_the_same_visit_twice_is_the_same_answer(leads_app):  # noqa: F811
    assert _visit(leads_app) == _visit(leads_app)


def test_an_npi_is_priced_as_its_practice(leads_app):  # noqa: F811
    """Searching by a clinician's NPI prices the practice (TIN) it bills under
    — the rate card's own subject rule."""
    by_npi = leads_app.post("/api/schedule/visit", json={"subject": "1510000002", "market": MKT})
    assert by_npi.status_code == 200, by_npi.text
    assert [(r["payer"], r["visit_value"]) for r in by_npi.json()["rows"]] == [
        ("Rho Health", 194.0), ("Sigma Health", None)]
