"""Real-terms erosion and the closure feed.

A payer holding a rate flat is cutting pay every year costs rise — the app said
nothing about that until now. The rule that matters most here is what happens
when the index does NOT cover a year: refuse with a reason, never extrapolate.
"""

import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.inflation import (DEFAULT_VALUES, InflationError, deflator,
                            index_status, load_index, real_change_pct,
                            save_index_values)


def test_a_flat_rate_is_a_real_terms_cut():
    """2022->2024 CPI-U rose 7.2%, so a rate unchanged in dollars lost that."""
    idx = {"values": DEFAULT_VALUES, "index_name": "CPI-U"}
    d = deflator(idx, "2022-06", "2024-06")
    assert d["basis"] == "index"
    # 313.689 / 292.655 = 1.0719
    assert round(d["factor"], 4) == 1.0719
    assert real_change_pct(0.0, d) == -6.7      # 1/1.0719 - 1
    # a 3% nominal "raise" over the same span is still a real cut
    assert real_change_pct(3.0, d) == -3.9


def test_real_change_compounds_rather_than_subtracting():
    """nominal - inflation drifts over a multi-year span, and it drifts the way
    that makes a cut look SMALLER. Use the ratio form."""
    idx = {"values": DEFAULT_VALUES, "index_name": "CPI-U"}
    d = deflator(idx, "2020-01", "2024-01")          # 313.689/258.811 = 1.2120
    naive = -5.0 - 21.2                               # what subtraction would say
    real = real_change_pct(-5.0, d)
    assert real == -21.6
    assert real > naive, "subtraction overstates the cut here; the ratio is exact"


def test_a_year_with_no_index_value_refuses_instead_of_estimating(cfg, store):
    """Extrapolating an index to make a slide land is exactly the fabricated
    number this app exists not to produce."""
    idx = load_index(store)
    future = idx["last_year"] + 2
    d = deflator(idx, f"{idx['last_year']}-01", f"{future}-01")
    assert d["factor"] is None
    assert real_change_pct(0.0, d) is None
    assert str(future) in d["reason"]
    assert "mrfx inflation" in d["reason"], "the refusal must say how to fix it"


def test_an_assumption_is_allowed_but_always_labelled(cfg, store):
    idx = load_index(store)
    future = idx["last_year"] + 3
    d = deflator(idx, f"{idx['last_year']}-01", f"{future}-01",
                 assume_pct_per_year=3)
    assert d["basis"] == "assumption"
    assert round(d["factor"], 4) == round(1.03 ** 3, 4)
    assert "ASSUMPTION" in d["assumption_note"]
    assert "3% price growth per year, supplied by you" in d["assumption_note"]
    # nonsense assumptions are refused, not silently clamped
    with pytest.raises(InflationError):
        deflator(idx, "2020-01", "2024-01", assume_pct_per_year=500)
    with pytest.raises(InflationError):
        deflator(idx, "2020-01", "2024-01", assume_pct_per_year="soon")


def test_a_user_supplied_year_extends_the_shipped_table(cfg, store):
    before = load_index(store)
    year = before["last_year"] + 1
    save_index_values(store, [(year, 322.5)], source="BLS release")
    after = load_index(store)
    assert after["values"][year] == 322.5
    assert after["last_year"] == year
    assert year in after["user_years"]
    # correcting a typo is re-entering the year, not a duplicate row
    save_index_values(store, [(year, 323.0)])
    assert load_index(store)["values"][year] == 323.0

    st = index_status(store)
    assert st["last_year"] == year
    assert "CONSUMER basket" in st["caveat"], "the basis caveat travels with it"

    for bad in ((1500, 300.0), (year, -4.0), ("x", 1.0)):
        with pytest.raises(InflationError):
            save_index_values(store, [bad])


def _two_months(store):
    """Same payer, same practices, rate unchanged in dollars across two years."""
    def row(month, rate, src):
        return dict(payer="Testco", tin_value="431234567", tin_type="ein",
                    npi="1417594896", source_file=src, billing_code="97110",
                    billing_code_type="CPT", discipline="PT", is_timed=True,
                    billing_class="professional", negotiated_rate=rate,
                    negotiated_type="negotiated", is_dollar_rate=True,
                    billing_code_modifier=[], service_code=["11"],
                    file_month=month, last_updated_on=f"{month}-01",
                    expiration_date=None, schema_version="2.0.0",
                    tin_is_really_npi=False, state="MO")
    y0, y1 = 2022, 2024
    with store.rates_part_writer("a.json") as w:
        w.write_batch([row(f"{y0}-06", 40.0, "a.json")])
    with store.rates_part_writer("b.json") as w:
        w.write_batch([row(f"{y1}-06", 40.0, "b.json")])
    store.rebuild_rollups()
    return f"{y0}-06", f"{y1}-06"


def test_the_trajectory_reports_the_erosion_a_flat_rate_hides(cfg, store):
    from mrfx.monitor import compute_payer_trajectory

    first, last = _two_months(store)
    t = compute_payer_trajectory(store, {"therapy_only": False})
    p = next(x for x in t["payers"] if x["payer"] == "Testco")
    assert p["cumulative_pct"] == 0.0, "unchanged in dollars"
    assert p["real_cumulative_pct"] == -6.7, "but a real cut"
    assert p["real_basis"] == "index"
    assert "flat pay is a pay cut" in p["erosion_line"]
    assert t["inflation"]["n_eroding_in_real_terms"] == 1
    assert t["inflation"]["index_name"] == "CPI-U"
    assert "CONSUMER basket" in t["inflation"]["caveat"]
    assert first in p["erosion_line"] and last in p["erosion_line"]


def test_the_api_exposes_the_basis_and_accepts_published_values(cfg, store):
    _two_months(store)
    c = TestClient(create_app(cfg, store))

    st = c.get("/api/inflation/status").json()
    assert st["index_name"] == "CPI-U" and st["n_years"] >= 10

    t = c.post("/api/trajectory", json={"market": {"therapy_only": False}}).json()
    assert next(p for p in t["payers"]
                if p["payer"] == "Testco")["real_cumulative_pct"] == -6.7

    year = st["last_year"] + 1
    r = c.post("/api/inflation", json={"values": [[year, 322.5]],
                                       "source": "BLS CPI-U annual average"})
    assert r.status_code == 200 and r.json()["saved"] == 1
    assert c.get("/api/inflation/status").json()["last_year"] == year

    # hostile input is a refusal, never a 500
    for bad in ({}, {"values": "2025"}, {"values": [["x", "y"]]},
                {"values": [[2025]]}):
        assert c.post("/api/inflation", json=bad).status_code == 422
    assert c.post("/api/trajectory", json={"market": {"therapy_only": False},
                                           "assume_inflation_pct": "lots"}
                  ).status_code == 422


def test_the_closure_feed_says_why_it_cannot_run_yet(cfg, store):
    """No NPPES bulk cache is the normal state on a fresh install. That is a
    reason to show, not an empty list that reads as 'nothing closed'."""
    from mrfx.nppes import closure_status, closures

    st = closure_status(store)
    assert st["ready"] is False and "NPPES bulk file" in st["reason"]
    res = closures(store)
    assert res["rows"] == [] and res["reason"] == st["reason"]
    assert "not proof a practice closed" in res["note"]


def test_a_reactivated_npi_is_not_a_closure(cfg, store, tmp_path):
    """NPPES deactivates for paperwork lapses too. An NPI that came back must
    never be reported as closed."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from mrfx.nppes import cache_path, closures

    def r(npi, deact, react):
        return {"npi": npi, "org_name": f"Clinic {npi[-1]}", "entity_type": "NPI-2",
                "taxonomy_code": "261QP2000X", "taxonomy_codes": "261QP2000X",
                "city": "StL", "state": "MO", "address": "x", "zip": "63103",
                "phone": None, "enumeration_date": "01/01/2015",
                "deactivation_date": deact, "reactivation_date": react}

    import datetime as dt
    recent = (dt.date.today() - dt.timedelta(days=30)).strftime("%m/%d/%Y")
    older = (dt.date.today() - dt.timedelta(days=60)).strftime("%m/%d/%Y")
    rows = [r("1417594896", recent, None),            # closed
            r("1999999992", older, recent),           # deactivated then BACK
            r("1215555554", None, None)]              # still open
    tbl = pa.table({k: pa.array([row[k] for row in rows], pa.string())
                    for k in rows[0]})
    pq.write_table(tbl, cache_path(store))

    res = closures(store, days=365)
    got = {x["npi"] for x in res["rows"]}
    assert got == {"1417594896"}, "only the one that stayed deactivated"
    assert res["rows"][0]["deactivated"] == \
        dt.datetime.strptime(recent, "%m/%d/%Y").date().isoformat()
    assert res["rows"][0]["in_store"] is False

    c = TestClient(create_app(cfg, store))
    assert c.post("/api/closures", json={"days": 365}).json()["total"] == 1
    assert c.post("/api/closures", json={"zip": "nope"}).status_code == 422


def test_a_closed_referral_source_reaches_the_client_packet(cfg, store, tmp_path):
    """The whole point of the closure feed for an existing client: the practice
    that fed them patients is gone, and they should hear it from you."""
    import datetime as dt

    import pyarrow as pa
    import pyarrow.parquet as pq

    from mrfx.medicare import import_shared_patients
    from mrfx.nppes import cache_path
    from mrfx.packets import build_client_packet

    mine, doc_closed, doc_open = "1417594896", "1901234561", "1811223340"
    rows = [dict(
        payer="Aetna", tin_value="431234567", tin_type="ein", npi=mine,
        source_file="m.json", billing_code=c, billing_code_type="CPT",
        discipline="PT", is_timed=True, billing_class="professional",
        negotiated_rate=55.0, negotiated_type="negotiated", is_dollar_rate=True,
        billing_code_modifier=[], service_code=["11"], file_month="2026-06",
        last_updated_on="2026-06-01", expiration_date=None,
        schema_version="2.0.0", tin_is_really_npi=False, state="MO")
        for c in ("97110", "97140")]
    with store.rates_part_writer("m.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk([
        dict(npi=n, org_name=nm, entity_type="NPI-2", taxonomy_code="261QP2000X",
             taxonomy_codes="261QP2000X", city="StL", state="MO", address="x",
             zip="63103", phone=None)
        for n, nm in ((mine, "Gateway Therapy"), (doc_closed, "Hilltop Ortho"),
                      (doc_open, "Riverside Ortho"))])
    store.rebuild_rollups()

    pairs = tmp_path / "pspp_2015_days30.txt"
    pairs.write_text(f"{doc_closed},{mine},400,44,3\n{doc_open},{mine},300,33,3\n")
    import_shared_patients(store, pairs)

    recent = (dt.date.today() - dt.timedelta(days=45)).strftime("%m/%d/%Y")

    def n(npi, deact):
        return {"npi": npi, "org_name": "Hilltop Ortho", "entity_type": "NPI-2",
                "taxonomy_code": "207X00000X", "taxonomy_codes": "207X00000X",
                "city": "StL", "state": "MO", "address": "x", "zip": "63103",
                "phone": "3145551212", "enumeration_date": "01/01/2010",
                "deactivation_date": deact, "reactivation_date": None}
    cache_rows = [n(doc_closed, recent), n(doc_open, None)]
    pq.write_table(pa.table({k: pa.array([r[k] for r in cache_rows], pa.string())
                             for k in cache_rows[0]}), cache_path(store))

    res = build_client_packet(cfg, store, "431234567", out_dir=tmp_path / "pk",
                              market={"month": "2026-06", "therapy_only": False})
    doc = next((w for w in res["written"] if "closed_referral" in w), None)
    assert doc, f"section missing; skipped={res['skipped']}"
    html = (tmp_path / "pk" / "431234567" / doc).read_text()
    assert "Hilltop Ortho" in html, "the source that closed"
    assert "Riverside" not in html, "the one still open must not be listed"
    assert "44" in html, "its shared-patient count, from the referral release"
    assert "not proof a practice closed" in html, "the caveat travels with it"
