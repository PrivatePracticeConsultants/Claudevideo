"""Round 6, P3: basis and honesty consistency. Each test pins one rule the
audit found stated one way and applied another, on the hand-worked Leads
fixture (tests/mrfx/test_known_answer_leads.py)."""

from __future__ import annotations

import re

from tests.mrfx.test_known_answer_leads import leads_app  # noqa: F401  (fixture)

MO = {"month": "latest", "state": "MO", "therapy_only": True}


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_weighted_position_names_its_mean_and_carries_the_median(leads_app):  # noqa: F811
    """The weighted figure is a volume-weighted MEAN, so it is compared with
    the plain mean; the house MEDIAN travels alongside under its own name.
    A sits at p0 on all three codes in MO (B..E above it): mean = median = 0."""
    r = leads_app.post("/api/economics/weighted-position", json={
        "subject": "A REHAB", "market": MO,
        "volumes": {"97110": 100, "97140": 50, "97530": 10}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["loaded"] and d["unweighted_percentile"] == 0.0
    assert d["median_percentile"] == 0.0
    assert "AVERAGES" in d["basis"] and "MEDIAN" in d["basis"]


def test_basis_note_names_the_toggles_the_query_used(leads_app):  # noqa: F811
    """The fee schedule and contract gaps printed the bare house basis
    ("base-modifier, professional") even when the request turned those off."""
    m = {**MO, "base_only": False, "billing_class": ""}
    fee = leads_app.post("/api/schedule/fee", json={
        "subject": "A REHAB", "market": m}).json()["fee_schedule"]
    gaps = leads_app.post("/api/contract-gaps", json={
        "subject": "A REHAB", "market": m, "min_peers": 1}).json()
    assert "basis_note" in gaps, gaps
    for note in (fee["basis_note"], gaps["basis_note"]):
        assert "[ALL modifier rows included" in note
        assert "[all billing classes included" in note
    plain = leads_app.post("/api/schedule/fee", json={
        "subject": "A REHAB", "market": MO}).json()["fee_schedule"]
    assert "[" not in plain["basis_note"], "the house basis stays bare"


def test_engagement_value_refuses_negative_units(leads_app):  # noqa: F811
    """-4,000 units turned a rate win into a NEGATIVE annual value."""
    assert leads_app.post("/api/engagements/baseline", json={
        "subject": "A REHAB", "market": MO}).status_code == 200
    for bad in ({"97110": -4000}, {"97110": "nan"}, {"97110": "inf"}):
        r = leads_app.post("/api/engagements/compare", json={
            "subject": "A REHAB", "volumes": bad})
        assert r.status_code == 422, (bad, r.text)
    ok = leads_app.post("/api/engagements/compare", json={
        "subject": "A REHAB", "volumes": {"97110": 100}})
    assert ok.status_code == 200, ok.text


def test_leverage_summary_echoes_the_radius_it_measured(leads_app):  # noqa: F811
    r = leads_app.post("/api/leverage/summary", json={
        "subject": "A REHAB", "radius_miles": 900, "market": {"month": "latest"}})
    assert r.status_code == 200, r.text
    assert r.json()["radius_miles"] == 250.0


def test_a_cut_lead_list_says_so(leads_app):  # noqa: F811
    """Two MO leads (A at p0, B at p25). Asking for one returns one and says
    the list was cut; the CSV says it too. Asking for 5000 is held to the
    1,000 the finder actually returns, and the response says which."""
    body = {"market": MO, "threshold_percentile": 25, "min_codes": 3}
    one = leads_app.post("/api/leads", json={**body, "limit": 1}).json()
    assert one["count"] == 1 and one["truncated"] is True and one["limit"] == 1
    assert "rounded to a whole percentile" in one["note"]
    full = leads_app.post("/api/leads", json={**body, "limit": 5000}).json()
    assert full["count"] == 2 and full["truncated"] is False and full["limit"] == 1000
    csv_cut = leads_app.post("/api/leads.csv", json={**body, "limit": 1}).text
    assert "List CUT at 1 leads" in csv_cut
    assert "List CUT" not in leads_app.post("/api/leads.csv", json=body).text


def test_the_pitch_report_prints_its_peer_counts(leads_app):  # noqa: F811
    """A's MO peers are B C D E on every code: 4 peers, under the 5 every
    deliverable flags as thin."""
    r = leads_app.post("/api/report/pitch", json={"subject": "A REHAB", "market": MO})
    assert r.status_code == 200, r.text
    t = _text(r.text)
    assert "Peers" in t and " 4 thin " in t
    assert "3 code(s) marked thin rest on fewer than 5 peer practices" in t
