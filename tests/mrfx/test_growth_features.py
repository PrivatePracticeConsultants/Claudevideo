"""Leverage, per-visit economics, weighted position, growth, service lines,
cash anchors, and the two assembled documents.

These all RECOMBINE existing numbers, so the risk is not arithmetic — it is a
caveat lost in transit, or a signal stated more strongly than the data allows.
The tests weight accordingly.
"""

import csv
import io

import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.benchmark import BenchmarkError

MARKET = {"month": "2026-06", "therapy_only": False}


def _row(tin, npi, rate, *, code="97110", payer="Aetna", month="2026-06"):
    return dict(payer=payer, tin_value=tin, tin_type="ein", npi=npi,
                source_file="a.json", billing_code=code, billing_code_type="CPT",
                discipline="PT", is_timed=True, billing_class="professional",
                negotiated_rate=rate, negotiated_type="negotiated",
                is_dollar_rate=True, billing_code_modifier=[], service_code=["11"],
                file_month=month, last_updated_on=f"{month}-01",
                expiration_date=None, schema_version="2.0.0",
                tin_is_really_npi=False, state="MO")


def _world(store, *, n_practices=8, zip_code="63103", city="St Louis"):
    """n practices in one ZIP, two payers, three codes, ascending rates."""
    rows, dirs = [], []
    for i in range(n_practices):
        tin, npi = str(431000000 + i), str(1417594896 + i)
        for code, base in (("97110", 40.0), ("97140", 36.0), ("97112", 33.0)):
            rows.append(_row(tin, npi, base + i, code=code))
            rows.append(_row(tin, npi, base + i + 6, code=code, payer="BCBS"))
        dirs.append(dict(npi=npi, org_name=f"Clinic {i}", entity_type="NPI-2",
                         taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X",
                         city=city, state="MO", address="x", zip=zip_code,
                         phone=None))
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk(dirs)
    store.rebuild_rollups()
    store.upsert_file("a.json", payer="Aetna", status="done",
                      file_type="in_network", last_updated_on="2026-06-01",
                      rows_emitted=len(rows))


def _puf(store, tmp_path, year, mult=1.0, n=8, extra_code=None):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Rndrng_NPI", "HCPCS_Cd", "Tot_Srvcs", "Tot_Benes",
                "Avg_Mdcr_Alowd_Amt", "Place_Of_Srvc"])
    for i in range(n):
        npi = str(1417594896 + i)
        for code, units in (("97110", 3000), ("97140", 900), ("97112", 400)):
            w.writerow([npi, code, int(units * mult), 40, 30.0, "O"])
        if extra_code and i > 0:
            w.writerow([npi, extra_code, int(500 * mult), 20, 32.0, "O"])
    p = tmp_path / f"puf_{year}.csv"
    p.write_text(buf.getvalue())
    from mrfx.utilization import import_utilization
    return import_utilization(store, p, year=year)


def _acs(store, tmp_path, zip_code="63103", pop=50000, seniors_a=4000,
         seniors_b=4200):
    p = tmp_path / "acs.csv"
    p.write_text("GEO_ID,NAME,B01001_001E,B01001_020E,B01001_044E\n"
                 f"86000US{zip_code},ZCTA5 {zip_code},{pop},{seniors_a},{seniors_b}\n")
    from mrfx.demographics import import_demographics
    return import_demographics(store, p)


# ------------------------------------------------------------------ leverage

def test_leverage_counts_the_payers_local_alternatives(cfg, store, tmp_path):
    from mrfx.leverage import network_leverage

    _world(store)
    _acs(store, tmp_path)
    lv = network_leverage(store, "431000000", "Aetna", MARKET, radius_miles=25)
    assert lv["n_alternatives"] == 7, "the other seven practices, not itself"
    assert lv["seniors"] == 8200                     # 4000 + 4200
    assert lv["seniors_per_alternative"] == 1171     # 8200 / 7
    assert lv["band"] == "moderate"
    assert lv["home_zip"] == "63103"
    assert all(a["miles"] is not None for a in lv["alternatives"])
    # the caveat that keeps this from being an adequacy claim
    assert "never a network-adequacy finding" in lv["note"]
    assert "leverage is GREATER than shown" in lv["note"]


def test_a_thin_network_is_called_thin_and_an_empty_one_says_so(cfg, store, tmp_path):
    """The whole point: a payer with no local fallback cannot afford to lose
    this practice, and the sentence has to say that plainly."""
    from mrfx.leverage import network_leverage

    _world(store, n_practices=3)      # subject + 2 others
    lv = network_leverage(store, "431000000", "Aetna", MARKET, radius_miles=25)
    assert lv["n_alternatives"] == 2 and lv["band"] == "thin"
    assert "thin fallback" in lv["headline"]

    # every practice in _world shares one ZIP, so they sit 0 miles apart and no
    # radius excludes them. Put the alternatives far away instead.
    far = [dict(npi="1999999992", org_name="Far Clinic", entity_type="NPI-2",
                taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X",
                city="Miami", state="FL", address="x", zip="33125", phone=None)]
    store.save_npis_bulk(far)
    with store.rates_part_writer("far.json") as w:
        w.write_batch([_row("439999999", "1999999992", 50.0)])
    store.rebuild_rollups()
    solo = network_leverage(store, "439999999", "Aetna", MARKET, radius_miles=25)
    assert solo["home_zip"] == "33125"
    assert solo["n_alternatives"] == 0, "nobody within 25 miles of Miami here"
    assert "NO other therapy practice" in solo["headline"]


def test_leverage_refuses_what_it_cannot_place(cfg, store):
    from mrfx.leverage import network_leverage

    _world(store)
    with pytest.raises(BenchmarkError, match="pick the payer"):
        network_leverage(store, "431000000", "", MARKET)
    # an unknown subject resolves to its own raw string, so the refusal comes
    # from the location lookup — it still has to name the practice
    with pytest.raises(BenchmarkError, match="nobody at all"):
        network_leverage(store, "nobody at all", "Aetna", MARKET)


def test_the_leverage_summary_opens_with_the_weakest_payer(cfg, store, tmp_path):
    from mrfx.leverage import leverage_summary

    _world(store, n_practices=3)
    s = leverage_summary(store, "431000000", MARKET, radius_miles=25)
    assert s["count"] == 2, "both payers ranked"
    assert s["payers"][0]["n_alternatives"] <= s["payers"][-1]["n_alternatives"]
    assert "Open with" in s["headline"]


# ----------------------------------------------------------------- economics

def test_a_visit_is_priced_on_the_practices_own_medicare_mix(cfg, store, tmp_path):
    """Hand arithmetic: 2023 units are 3450/1034/459 for 97110/97140/97112.
    Aetna pays the subject 40/36/33, so the weighted unit value is
    (40*3450 + 36*1034 + 33*459) / 4943 = 190371/4943 = $38.51."""
    from mrfx.economics import visit_economics

    _world(store)
    _puf(store, tmp_path, "2023", mult=1.15)
    ve = visit_economics(store, "431000000", MARKET, units_per_visit=3)
    assert ve["loaded"] is True
    by = {r["payer"]: r for r in ve["rows"]}
    assert by["Aetna"]["value_per_billed_unit"] == 38.51
    assert by["BCBS"]["value_per_billed_unit"] == 44.51
    # the per-visit figure multiplies BEFORE rounding, so it is 115.54 rather
    # than the 115.53 you get from rounding the unit value first
    assert by["Aetna"]["value_per_visit"] == 115.54
    assert by["Aetna"]["mix_coverage_pct"] == 100.0
    assert ve["best_payer"] == "BCBS" and ve["worst_payer"] == "Aetna"
    assert "$18.00 apart" in ve["headline"]
    # the divisor is an assumption and must be labelled wherever it is used
    assert "ASSUMPTION" in ve["assumption_note"]
    assert "Medicare counts SERVICES" in ve["note"]


def test_without_a_units_divisor_no_per_visit_figure_is_invented(cfg, store, tmp_path):
    from mrfx.economics import visit_economics

    _world(store)
    _puf(store, tmp_path, "2023")
    ve = visit_economics(store, "431000000", MARKET)
    assert all(r["value_per_visit"] is None for r in ve["rows"])
    assert ve["assumption_note"] is None
    assert "billed unit" in ve["headline"], "stated per unit, not per visit"
    for bad in (0.1, 40, "three"):
        with pytest.raises(BenchmarkError):
            visit_economics(store, "431000000", MARKET, units_per_visit=bad)


def test_a_payer_is_never_made_to_look_cheap_by_a_code_it_doesnt_publish(cfg, store, tmp_path):
    """Pricing a missing code at zero would do exactly that. The mix is
    re-normalized over the codes the payer actually prices, and the coverage
    is reported so a thin overlap is visible."""
    from mrfx.economics import visit_economics

    _world(store)
    _puf(store, tmp_path, "2023")
    # BCBS drops 97112 entirely
    with store.connect(), store.write_lock:
        pass
    rows = [_row("431000000", "1417594896", 40.0, code="97110", payer="Thin"),
            _row("431000000", "1417594896", 36.0, code="97140", payer="Thin")]
    with store.rates_part_writer("thin.json") as w:
        w.write_batch(rows)
    store.rebuild_rollups()

    ve = visit_economics(store, "431000000", MARKET)
    thin = next(r for r in ve["rows"] if r["payer"] == "Thin")
    # (40*3000 + 36*900) / 3900 = 39.08 — NOT dragged toward 0 by 97112
    assert thin["value_per_billed_unit"] == 39.08
    assert thin["mix_coverage_pct"] < 100.0
    assert thin["codes_priced"] == 2 and thin["codes_in_mix"] == 3


def test_weighted_position_needs_volumes_that_cover_the_book(cfg, store, tmp_path):
    """Weighting a fragment would misrepresent the whole, so below the
    coverage floor it declines and says why."""
    from mrfx.economics import weighted_position

    _world(store)
    wp = weighted_position(store, "431000000", MARKET,
                           volumes={"97110": 4000})   # 1 of 3 codes = 33%
    assert wp["loaded"] is False
    assert wp["coverage_pct"] < wp["min_coverage_pct"]
    assert "would misrepresent the whole" in wp["reason"]
    assert wp["unweighted_percentile"] is not None, "the plain number still shown"


def test_weighted_position_moves_when_the_heavy_code_is_the_weak_one(cfg, store, tmp_path):
    """The finding this exists for: a practice can look mid-market on a simple
    average while the code carrying its volume is its worst."""
    from mrfx.economics import weighted_position

    # subject priced HIGH on two low-volume codes and LOW on the heavy one
    rows, dirs = [], []
    for i in range(6):
        tin, npi = str(431000000 + i), str(1417594896 + i)
        rows.append(_row(tin, npi, 30.0 if i == 0 else 40.0 + i, code="97110"))
        rows.append(_row(tin, npi, 60.0 if i == 0 else 36.0 + i, code="97140"))
        rows.append(_row(tin, npi, 60.0 if i == 0 else 33.0 + i, code="97112"))
        dirs.append(dict(npi=npi, org_name=f"C{i}", entity_type="NPI-2",
                         taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X",
                         city="StL", state="MO", address="x", zip="63103",
                         phone=None))
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk(dirs)
    store.rebuild_rollups()

    wp = weighted_position(store, "431000000", MARKET,
                           volumes={"97110": 9000, "97140": 500, "97112": 500})
    assert wp["loaded"] is True
    assert wp["heaviest_code"] == "97110"
    assert wp["weighted_percentile"] < wp["unweighted_percentile"], (
        "the heavy code is the weak one, so weighting must pull the number DOWN")
    assert "carries" in wp["headline"]


# -------------------------------------------------------------------- growth

def test_growth_compares_only_codes_present_in_both_years(cfg, store, tmp_path):
    """CMS suppression can make a code appear or vanish with nothing changing
    in the practice, so those are listed apart and never counted as movement."""
    from mrfx.utilization import practice_growth

    _world(store)
    _puf(store, tmp_path, "2022", mult=1.0)
    _puf(store, tmp_path, "2023", mult=1.15, extra_code="97530")
    g = practice_growth(store, "431000000")
    assert g["loaded"] is True
    assert g["from_year"] == "2022" and g["to_year"] == "2023"
    assert g["total_units_from"] == 4300 and g["total_units_to"] == 4943
    assert g["pct_change"] == 15.0 and g["direction"] == "grew"
    assert {r["billing_code"] for r in g["rows"]} == {"97110", "97140", "97112"}
    assert "suppression" in g["note"]


def test_growth_says_why_with_only_one_year(cfg, store, tmp_path):
    from mrfx.utilization import practice_growth

    _world(store)
    _puf(store, tmp_path, "2023")
    g = practice_growth(store, "431000000")
    assert g["loaded"] is False and "needs two Medicare utilization years" in g["reason"]


def test_service_line_gaps_find_what_peers_bill_and_this_one_doesnt(cfg, store, tmp_path):
    from mrfx.utilization import service_line_gaps

    _world(store)
    _puf(store, tmp_path, "2023", extra_code="97530")   # peers bill it, subject doesn't
    sl = service_line_gaps(store, "431000000", radius_miles=25)
    assert sl["loaded"] is True
    assert [r["billing_code"] for r in sl["rows"]] == ["97530"]
    assert sl["rows"][0]["n_peers"] == 7
    assert "question to ask the owner" in sl["note"]
    assert "BILLING-BEHAVIOUR comparison, not a contract one" in sl["note"]


# --------------------------------------------------------------- cash anchors

def test_cash_anchors_surface_what_hospitals_charge_self_pay(cfg, store, tmp_path):
    from mrfx.hospital import cash_anchors, import_hospital_file

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["hospital_name", "last_updated_on", "version"])
    w.writerow(["St Mary's", "2026-04-01", "2.0.0"])
    w.writerow(["description", "code|1", "code|1|type", "setting", "payer_name",
                "plan_name", "standard_charge|gross",
                "standard_charge|discounted_cash",
                "standard_charge|negotiated_dollar", "standard_charge|methodology",
                "estimated_amount", "modifiers"])
    w.writerow(["x", "97110", "CPT", "outpatient", "Aetna", "PPO", "310", "180",
                "128.40", "fee schedule", "", ""])
    p = tmp_path / "h.csv"
    p.write_text(buf.getvalue())
    import_hospital_file(store, p, state="MO", cfg=cfg)

    ca = cash_anchors(store, state="MO")
    assert ca["loaded"] is True
    r = ca["rows"][0]
    assert r["cash_median"] == 180.0 and r["gross_median"] == 310.0
    assert r["negotiated_pct_of_cash"] == 71.3          # 128.40 / 180
    assert "list price almost nobody pays" in ca["note"]
    assert "should sit below them" in ca["note"]


# ------------------------------------------------------------------ documents

def test_the_dossier_assembles_and_lists_what_it_could_not_build(cfg, store, tmp_path):
    """A branded page with a silently missing section reads as a complete
    picture when it is not."""
    from mrfx.dossier import build_prospect_dossier

    _world(store)
    _puf(store, tmp_path, "2023")
    _acs(store, tmp_path)
    d = build_prospect_dossier(cfg, store, "431000000", MARKET, radius_miles=25)
    html = d["html"]
    assert "Practice profile" in html
    assert "How much therapy they bill" in html
    assert "Where their rates sit" in html
    assert "Which payer is weakest for them" in html
    assert "Their leverage with that payer" in html
    assert "Their market" in html
    # referral data is absent in this store: it must be LISTED, not dropped
    assert d["skipped"] and any("Referral" in s for s in d["skipped"])
    assert "Not included, and why" in html
    assert "nothing came from the practice" in html
    assert "Published rates are not proof of collection" in html


def test_the_dossier_refuses_a_practice_that_does_not_exist(cfg, store):
    from mrfx.dossier import build_prospect_dossier

    _world(store)
    with pytest.raises(BenchmarkError, match="no practice matches"):
        build_prospect_dossier(cfg, store, "not a practice", MARKET)


def test_the_market_report_builds_from_sections_that_exist(cfg, store, tmp_path):
    from mrfx.dossier import build_market_report

    _world(store)
    _acs(store, tmp_path)
    r = build_market_report(cfg, store, state="MO", code="97110",
                            zip_code="63103", radius_miles=25, market=MARKET)
    assert "Therapy market report — MO" in r["html"]
    assert "Who holds the contracts" in r["html"]
    assert "Which payers negotiate" in r["html"]
    assert "Demand around" in r["html"]
    assert r["state"] == "MO"


def test_a_market_report_without_a_state_refuses(cfg, store):
    from mrfx.dossier import build_market_report

    _world(store)
    with pytest.raises(BenchmarkError, match="two-letter state"):
        build_market_report(cfg, store, state="")


# ----------------------------------------------------------------- endpoints

def test_every_new_endpoint_answers_and_refuses_cleanly(cfg, store, tmp_path):
    _world(store)
    _puf(store, tmp_path, "2023")
    _acs(store, tmp_path)
    c = TestClient(create_app(cfg, store), raise_server_exceptions=False)
    S = "431000000"

    ok = [
        ("/api/leverage", {"subject": S, "payer": "Aetna", "market": MARKET}),
        ("/api/leverage/summary", {"subject": S, "market": MARKET}),
        ("/api/economics/visit", {"subject": S, "market": MARKET, "units_per_visit": 3}),
        ("/api/economics/weighted-position", {"subject": S, "market": MARKET}),
        ("/api/utilization/growth", {"subject": S}),
        ("/api/utilization/service-lines", {"subject": S}),
        ("/api/hospital/cash", {"state": "MO"}),
        ("/api/report/dossier", {"subject": S, "market": MARKET}),
        ("/api/report/market", {"state": "MO", "zip": "63103", "market": MARKET}),
    ]
    for route, body in ok:
        r = c.post(route, json=body)
        assert r.status_code == 200, f"{route}: {r.status_code} {r.text[:150]}"
    assert c.get("/api/utilization/years").json()["years"] == ["2023"]

    # hostile input: refusals, never 500s
    bad = [
        ("/api/leverage", {"subject": S, "payer": ""}),
        ("/api/leverage", {"subject": "nobody", "payer": "Aetna"}),
        ("/api/leverage", {"subject": S, "payer": "Aetna", "radius_miles": "far"}),
        ("/api/economics/visit", {"subject": S, "units_per_visit": 999}),
        ("/api/economics/weighted-position", {"subject": S, "volumes": "lots"}),
        ("/api/utilization/growth", {"subject": "nobody"}),
        ("/api/utilization/service-lines", {"subject": S, "radius_miles": "near"}),
        ("/api/report/dossier", {"subject": ""}),
        ("/api/report/market", {"state": ""}),
        ("/api/report/market", {"state": "ZZ"}),
    ]
    for route, body in bad:
        r = c.post(route, json=body)
        assert r.status_code < 500, f"{route} {body}: {r.status_code} {r.text[:150]}"


def test_a_negative_volume_can_never_become_a_negative_opportunity(cfg, store):
    """Found by the growth-feature audit: nothing rejected negative annual
    units, so -4,000 units produced a -$12,000 'opportunity' that flowed into
    the pitch report, the proposal and the win report — and weighted a position
    calculation backwards. A unit count is a quantity; it cannot be negative."""
    from mrfx.benchmark import (clean_volumes, compute_benchmark,
                                compute_opportunity)
    from mrfx.economics import weighted_position

    _world(store)
    assert clean_volumes({"97110": 4000}) == {"97110": 4000.0}
    assert clean_volumes({"97110": 0}) == {"97110": 0.0}, "zero is a real answer"
    for bad in ({"97110": -5}, {"97110": -0.5, "97140": 10}):
        with pytest.raises(BenchmarkError, match="cannot be negative"):
            clean_volumes(bad)
    for bad in ({"97110": float("nan")}, {"97110": float("inf")}):
        with pytest.raises(BenchmarkError, match="real numbers"):
            clean_volumes(bad)

    b = compute_benchmark(store, "431000000", MARKET)
    with pytest.raises(BenchmarkError, match="cannot be negative"):
        compute_opportunity(b, {"97110": -4000})
    with pytest.raises(BenchmarkError, match="cannot be negative"):
        weighted_position(store, "431000000", MARKET, volumes={"97110": -5,
                                                               "97140": 10})
