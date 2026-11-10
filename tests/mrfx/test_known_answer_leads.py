"""Known answers for the LEADS tab (Growth -> Leads), worked out by hand and
checked through ingest -> rollups -> HTTP API -> CSV.

Practices (professional, GP, POS 11; NPPES names/taxonomies loaded):

  TIN  name       state  NPIs  kind
  A    A REHAB    MO     3     PT clinic
  B    B REHAB    MO     2     PT clinic
  C    C REHAB    MO+KS  4     PT clinic (one clinician in KS)
  D    D REHAB    MO     1     PT clinic
  E    E REHAB    MO     5     PT clinic, NEW in June (absent from May)
  F    F REHAB    KS     1     PT clinic
  G    G MEDICAL  MO     1     internal medicine (not a therapy practice)

  Rho Health, June file:   97110  A 30  B 40  C 50  D 60  E 70  F 10  G 5
                           97140  A 25  B 35  C 45  D 55  E 65  F 10  G 5
                           97530  A 40  B 50  C 60  D 70  E 80  F 10  G 5
                           97112  A 99            (nobody else prices it)
  Rho Health, May file:    97110  A 30  B 20  C 50  D 60  F 10  G 5   (no E;
                           B's $20 is superseded by June's $40 under "latest")
  Sigma Health, June file: 97110  A 34

By hand — market {latest, MO, therapy practices only} = A..E:
  97110 per-practice rate: A median(30, 34) = 32, B 40, C 50, D 60, E 70
        percent_rank: A 0, B 25, C 50, D 75, E 100
  97140: A 25 .. E 65 -> same ranks.  97530: A 40 .. E 80
  97112: priced by A alone -> no market, not counted (n_codes stays 3)
  The market median is the median of the OTHER practices (Benchmark's rule,
  review 10 item 3.9): for A, 97110 median(40,50,60,70) = 55, 97140 50,
  97530 65; for B, 97110 median(32,50,60,70) = 55, 97140 50, 97530 65.
  A: median percentile 0,  avg gap to median = (23 + 25 + 25) / 3 = 24.33
  B: median percentile 25, avg gap = 15.00          C: 50 (not a lead)
  -> leads at/below p25: A, then B.
  Worst payer for A, over the codes that HAVE a market: Rho 30/25/40 vs
  55/50/65 -> 25.00 under on each = 25.00; Sigma 34 vs 55 -> 21.00.
  -> Rho, $25.00. (Averaging in 97112 — A alone, gap 0 — made Rho 18.75 and
  named Sigma: the wrong payer on the outreach line.)
  With therapy practices only OFF, G (5 on every code) joins: 6 practices,
  G at 0, A at 20 (second of six), B at 40 -> leads G, A.
  With no state: F (10, KS) joins instead: F 0, A 20 -> leads F, A.
  Excluding A as "your client": A leaves the LIST, not the market -> B, 25.
Leaderboard (MO, latest, therapy): size E5 C4 A3 B2 D1; paid E D C B A;
  footprint C (MO+KS) first, then by size E A B D. A's median rate =
  median(32, 25, 40) = 32.00.
New to network (Rho, June vs Rho's previous month May): E only — 3 codes,
  median rate median(70, 65, 80) = 70.00. Sigma has no earlier month: not compared.
"""

from __future__ import annotations

import csv
import io
import json

import pytest

TINS = {k: f"5{i}0000001" for i, k in enumerate("ABCDEFG", start=1)}
PT, CLINIC, MD = "225100000X", "261QP2000X", "207R00000X"
# practice -> [(npi, entity_type, taxonomy, state)]
NPIS = {
    "A": [("1510000001", "NPI-2", CLINIC, "MO"), ("1510000002", "NPI-1", PT, "MO"),
          ("1510000003", "NPI-1", PT, "MO")],
    "B": [("1520000001", "NPI-2", CLINIC, "MO"), ("1520000002", "NPI-1", PT, "MO")],
    "C": [("1530000001", "NPI-2", CLINIC, "MO"), ("1530000002", "NPI-1", PT, "MO"),
          ("1530000003", "NPI-1", PT, "MO"), ("1530000004", "NPI-1", PT, "KS")],
    "D": [("1540000001", "NPI-2", CLINIC, "MO")],
    "E": [("1550000001", "NPI-2", CLINIC, "MO")] + [(f"155000000{i}", "NPI-1", PT, "MO")
                                                    for i in range(2, 6)],
    "F": [("1560000001", "NPI-2", CLINIC, "KS")],
    "G": [("1570000001", "NPI-1", MD, "MO")],
}
JUNE_RHO = {"97110": dict(A=30, B=40, C=50, D=60, E=70, F=10, G=5),
            "97140": dict(A=25, B=35, C=45, D=55, E=65, F=10, G=5),
            "97530": dict(A=40, B=50, C=60, D=70, E=80, F=10, G=5),
            "97112": dict(A=99)}
MAY_RHO = {"97110": dict(A=30, B=20, C=50, D=60, F=10, G=5)}
JUNE_SIGMA = {"97110": dict(A=34)}
MKT = {"month": "latest", "state": "MO", "therapy_only": True}


def _file(payer, day, prices):
    items = []
    for code, by_tin in prices.items():
        items.append({"negotiation_arrangement": "ffs", "name": code, "billing_code_type": "CPT",
                      "billing_code_type_version": "2026", "billing_code": code,
                      "description": code, "negotiated_rates": [{
                          "provider_groups": [{"npi": [n for n, *_ in NPIS[k]],
                                               "tin": {"type": "ein", "value": TINS[k]}}],
                          "negotiated_prices": [{
                              "negotiated_type": "negotiated", "negotiated_rate": float(rate),
                              "billing_class": "professional", "service_code": ["11"],
                              "billing_code_modifier": ["GP"],
                              "expiration_date": "9999-12-31"}]}
                          for k, rate in by_tin.items()]})
    return {"reporting_entity_name": payer, "reporting_entity_type": "issuer",
            "last_updated_on": day, "version": "2.0.0", "provider_references": [],
            "in_network": items}


@pytest.fixture
def leads_app(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from mrfx.ingest import scan_inbox
    for name, doc in (("rho_2026-05_in-network.json", _file("Rho Health", "2026-05-01", MAY_RHO)),
                      ("rho_2026-06_in-network.json", _file("Rho Health", "2026-06-01", JUNE_RHO)),
                      ("sigma_2026-06_in-network.json",
                       _file("Sigma Health", "2026-06-01", JUNE_SIGMA))):
        (cfg.inbox_dir / name).write_text(json.dumps(doc))
    assert all(r["status"] == "done" for r in scan_inbox(cfg, store))
    for k, npis in NPIS.items():
        label = f"{k} MEDICAL" if k == "G" else f"{k} REHAB"
        for npi, etype, tax, st in npis:
            store.save_npi(npi, label if etype == "NPI-2" or k == "G" else f"CLINICIAN {npi}",
                           tax, "x", "Town", st, entity_type=etype, zip_code="64105")
    store.rebuild_rollups()
    return TestClient(create_app(cfg, store))


def _leads(c, **kw):
    body = {"market": MKT, "threshold_percentile": 25, "min_codes": 3, **kw}
    r = c.post("/api/leads", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_underpaid_list_is_the_hand_answer(leads_app):
    d = _leads(leads_app)
    got = [(x["display_name"], x["median_percentile"], x["n_codes"], x["avg_gap_to_median"])
           for x in d["leads"]]
    assert got == [("A REHAB", 0.0, 3, 24.33), ("B REHAB", 25.0, 3, 15.0)]
    assert d["leads"][0]["state"] == "MO" and d["leads"][0]["npi_count"] == 3


def test_the_worst_payer_is_judged_on_codes_with_a_market(leads_app):
    a, b = _leads(leads_app)["leads"]
    assert (a["worst_payer"], a["worst_payer_gap"]) == ("Rho Health", 25.0)
    assert (b["worst_payer"], b["worst_payer_gap"]) == ("Rho Health", 15.0)


def test_market_scope_changes_who_is_a_lead(leads_app):
    no_filter = _leads(leads_app, market={**MKT, "therapy_only": False})["leads"]
    assert [(x["display_name"], x["median_percentile"]) for x in no_filter] == [
        ("G MEDICAL", 0.0), ("A REHAB", 20.0)]
    no_state = _leads(leads_app, market={"month": "latest", "therapy_only": True})["leads"]
    assert [(x["display_name"], x["median_percentile"]) for x in no_state] == [
        ("F REHAB", 0.0), ("A REHAB", 20.0)]


def test_excluding_your_client_keeps_it_in_the_market(leads_app):
    d = _leads(leads_app, exclude_subject=TINS["A"])
    assert [(x["display_name"], x["median_percentile"]) for x in d["leads"]] == [("B REHAB", 25.0)]
    assert _leads(leads_app, min_codes=4)["leads"] == []          # nobody prices 4 market codes


def test_a_superseded_month_never_counts(leads_app):
    """B's May $20 for 97110 would make B the cheapest there (p0); June's $40
    is its current rate. A pinned May shows the May picture instead."""
    assert _leads(leads_app)["leads"][1]["median_percentile"] == 25.0
    may = _leads(leads_app, market={**MKT, "month": "2026-05"}, min_codes=1)["leads"]
    assert [(x["display_name"], x["median_percentile"]) for x in may] == [("B REHAB", 0.0)]


def test_the_csv_is_the_screen(leads_app):
    body = {"market": MKT, "threshold_percentile": 25, "min_codes": 3}
    screen = _leads(leads_app)["leads"]
    r = leads_app.post("/api/leads.csv", json=body)
    assert r.status_code == 200
    text = r.text.lstrip("﻿")
    notes = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert any("at or below p25" in n for n in notes)          # the methodology travels
    rows = list(csv.DictReader(io.StringIO("\n".join(
        ln for ln in text.splitlines() if not ln.startswith("#")))))
    assert len(rows) == len(screen) == 2
    for row, s in zip(rows, screen):
        assert (row["display_name"], row["tin"], row["state"], row["npi_count"]) == (
            s["display_name"], s["tin_value"], s["state"], str(s["npi_count"]))
        assert (float(row["median_percentile"]), float(row["avg_gap_to_median"])) == (
            s["median_percentile"], s["avg_gap_to_median"])
        assert (row["worst_payer"], float(row["worst_payer_gap"])) == (
            s["worst_payer"], s["worst_payer_gap"])


def test_the_leaderboard_orders(leads_app):
    def names(sort):
        r = leads_app.post("/api/leaderboard", json={"market": MKT, "min_codes": 3,
                                                     "sort": sort, "limit": 50})
        assert r.status_code == 200, r.text
        return [p["display_name"][0] for p in r.json()["practices"]], r.json()["practices"]
    assert names("size")[0] == list("ECABD")
    assert names("paid")[0] == list("EDCBA")
    assert names("footprint")[0] == list("CEABD")
    a = next(p for p in names("size")[1] if p["display_name"] == "A REHAB")
    assert (a["median_rate"], a["median_percentile"], a["npi_count"]) == (32.0, 0.0, 3)


def test_new_to_network_is_the_one_practice_rho_added(leads_app):
    r = leads_app.post("/api/contracts/new-to-network",
                       json={"market": {"month": "latest"}, "zip": None, "radius_miles": 25})
    assert r.status_code == 200, r.text
    d = r.json()
    got = [(x["practice"], x["payer"], x["prev_month"], x["codes"], x["median_rate"])
           for x in d["rows"]]
    assert got == [("E REHAB", "Rho Health", "2026-05", 3, 70.0)]
    assert [tuple(p) for p in d["compared"]] == [("Rho Health", "2026-05")]


def test_every_leads_answer_is_the_same_twice(leads_app):
    for _ in range(3):
        assert _leads(leads_app) == _leads(leads_app)


# --- the two NPPES-fed panels: brand-new clinics and closures ---------------
# Days ago are relative to today, so the windows below never drift. ZIP
# centroids are the shipped Census file; distances are checked against an
# independent haversine here, not the app's.
#
#   new clinics (enumerated N days ago)        closures (deactivated / reactivated)
#   N1 66101 KS PT     10d   ~2 mi             X1 64105 MO PT    20d / -        closed
#   N2 64151 MO OT     40d   ~8 mi             X2 64105 MO PT    50d / 10d      came back: NOT closed
#   N3 64086 MO SLP   100d  ~22 mi             X3 64105 MO PT   400d / -        outside a year
#   N4 63103 MO PT      5d  ~226 mi            X4 63103 MO PT    30d / -        outside 25 mi
#   N5 64105 MO MD      5d  (not therapy)      X5 64105 MO PT    10d / 3 yrs    re-closed: closed
#   N6 64105 MO PT    400d  (too old)          D  64105 MO clinic 60d / -       closed, HAD RATES
#   N7 99999 MO PT     15d  (no centroid)
#   E  64105 MO clinic 30d  (the new-to-network practice: has rates)
#
# By hand, ZIP 64105 / 25 mi / 180 days: N1, E, N2, N3 (newest first); N7 is
# "unplaced", N4 too far, N5 not therapy, N6 too old. Closures, 64105 / 25 mi /
# a year: X5, X1, D (newest deactivation first); D is flagged in_store.
SLP, OT = "235Z00000X", "225X00000X"
FEED_ORIGIN = "64105"


def _ago(days):
    import datetime as dt
    return (dt.date.today() - dt.timedelta(days=days)).strftime("%m/%d/%Y")


def _iso_ago(days):
    import datetime as dt
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def _miles(z1, z2):
    import math
    from pathlib import Path
    cents = {}
    for ln in (Path(__file__).resolve().parents[2] / "config" / "zcta-centroids.csv").read_text().splitlines()[1:]:
        z, la, lo = ln.split(",")
        if z in (z1, z2):
            cents[z] = (float(la), float(lo))
    (a1, o1), (a2, o2) = cents[z1], cents[z2]
    p1, p2, dp, dl = map(math.radians, (a1, a2, a2 - a1, o2 - o1))
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * 3958.8 * math.asin(math.sqrt(h)), 1)


def _write_nppes_cache(store):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from mrfx.enrich import _NPPES_CACHE_COLS
    from mrfx.nppes import cache_path

    def row(npi, name, tax, st, z, enum=None, deact=None, react=None, etype="NPI-2"):
        return {"npi": npi, "org_name": name, "entity_type": etype, "taxonomy_code": tax,
                "taxonomy_codes": tax, "city": "Town", "state": st, "address": "1 Main",
                "zip": z, "phone": "8165550100", "enumeration_date": enum or "01/01/2015",
                "deactivation_date": deact, "reactivation_date": react}
    rows = [row("1600000001", "N1 PT", PT, "KS", "66101", _ago(10)),
            row("1600000002", "N2 OT", OT, "MO", "64151", _ago(40)),
            row("1600000003", "N3 SLP", SLP, "MO", "64086", _ago(100)),
            row("1600000004", "N4 PT", PT, "MO", "63103", _ago(5)),
            row("1600000005", "N5 CARDIOLOGY", "207RC0000X", "MO", "64105", _ago(5)),
            row("1600000006", "N6 PT", PT, "MO", "64105", _ago(400)),
            row("1600000007", "N7 PT", PT, "MO", "99999", _ago(15)),
            row("1550000001", "E REHAB", CLINIC, "MO", "64105", _ago(30)),
            row("1700000001", "X1 PT", PT, "MO", "64105", deact=_ago(20)),
            row("1700000002", "X2 PT", PT, "MO", "64105", deact=_ago(50), react=_ago(10)),
            row("1700000003", "X3 PT", PT, "MO", "64105", deact=_ago(400)),
            row("1700000004", "X4 PT", PT, "MO", "63103", deact=_ago(30)),
            row("1700000005", "X5 PT", PT, "MO", "64105", deact=_ago(10), react=_ago(1100)),
            row("1540000001", "D REHAB", CLINIC, "MO", "64105", deact=_ago(60))]
    pq.write_table(pa.table({c: pa.array([r[c] for r in rows], pa.string())
                             for c in _NPPES_CACHE_COLS}), cache_path(store))


@pytest.fixture
def feeds_app(leads_app, store):
    _write_nppes_cache(store)
    return leads_app


def test_new_clinics_is_the_hand_answer(feeds_app):
    r = feeds_app.post("/api/newclinics", json={"zip": FEED_ORIGIN, "radius_miles": 25,
                                                "state": None, "days": 180})
    assert r.status_code == 200, r.text
    d = r.json()
    got = [(x["org_name"], x["enumerated"], x["zip"], x["miles"], x["in_store"]) for x in d["rows"]]
    assert got == [("N1 PT", _iso_ago(10), "66101", _miles(FEED_ORIGIN, "66101"), False),
                   ("E REHAB", _iso_ago(30), "64105", 0.0, True),
                   ("N2 OT", _iso_ago(40), "64151", _miles(FEED_ORIGIN, "64151"), False),
                   ("N3 SLP", _iso_ago(100), "64086", _miles(FEED_ORIGIN, "64086"), False)]
    assert (d["total"], d["unplaced"], d["capped"]) == (4, 1, False)
    assert _miles(FEED_ORIGIN, "63103") > 25 > _miles(FEED_ORIGIN, "64086")   # the fixture is what it says
    # narrower radius drops the 22-mile one; no ZIP = the whole state list
    near = feeds_app.post("/api/newclinics", json={"zip": FEED_ORIGIN, "radius_miles": 15,
                                                   "days": 180}).json()
    assert [x["org_name"] for x in near["rows"]] == ["N1 PT", "E REHAB", "N2 OT"]
    mo = feeds_app.post("/api/newclinics", json={"state": "mo", "days": 180}).json()
    assert [x["org_name"] for x in mo["rows"]] == ["N4 PT", "N7 PT", "E REHAB", "N2 OT", "N3 SLP"]
    assert mo["unplaced"] == 0 and "miles" not in mo["rows"][0]
    month = feeds_app.post("/api/newclinics", json={"days": 31}).json()   # 31 days: N4, N1, N7, E
    assert [x["org_name"] for x in month["rows"]] == ["N4 PT", "N1 PT", "N7 PT", "E REHAB"]


def test_closures_is_the_hand_answer(feeds_app):
    r = feeds_app.post("/api/closures", json={"zip": FEED_ORIGIN, "radius_miles": 25,
                                              "state": None, "days": 365})
    assert r.status_code == 200, r.text
    d = r.json()
    got = [(x["org_name"], x["deactivated"], x["miles"], x["in_store"]) for x in d["rows"]]
    assert got == [("X5 PT", _iso_ago(10), 0.0, False),
                   ("X1 PT", _iso_ago(20), 0.0, False),
                   ("D REHAB", _iso_ago(60), 0.0, True)]
    assert (d["total"], d["unplaced"]) == (3, 0)
    anywhere = feeds_app.post("/api/closures", json={"days": 365}).json()
    assert [x["org_name"] for x in anywhere["rows"]] == ["X5 PT", "X1 PT", "X4 PT", "D REHAB"]
    assert feeds_app.post("/api/closures", json={"days": 2000}).json()["total"] == 5   # + X3


def test_a_bad_zip_is_a_plain_refusal_not_a_crash(feeds_app):
    for route in ("/api/newclinics", "/api/closures"):
        r = feeds_app.post(route, json={"zip": "6410", "radius_miles": 25})
        assert r.status_code == 422 and "5-digit ZIP" in r.json()["detail"]
        r = feeds_app.post(route, json={"zip": "99999", "radius_miles": 25})
        assert r.status_code == 422 and "centroid" in r.json()["detail"]


# --- the prospect dossier (Leads tab -> "Open dossier"), as the tab asks ----
# Market {latest, every practice, no state}; everyone's NPIs sit in 64105.
# A against the six OTHER practices, pooled payers:
#   97110 A 32 vs 5 10 40 50 60 70 -> median 45, two below A -> p33
#   97140 A 25 vs 5 10 35 45 55 65 -> median 40, p33;  97530 A 40 vs 5 10 50 60 70 80 -> 55, p33
# Per payer: Rho, A 30/25/40 against Rho's other six -> p33 on each, below the
# median on 3 of 3. Sigma prices nobody else -> no peer set -> not listed.
# Leverage with Rho inside 25 miles: B C D E F G = 6 alternatives, and G is an
# internal-medicine group -> "6 other practices (1 of them not a therapy
# practice)"; with therapy practices only, 5 therapy practices.
DOSSIER = {"subject": "A REHAB", "radius_miles": 25,
           "market": {"month": "latest", "therapy_only": False}}


def _text(html_doc):
    import html
    import re
    t = re.sub(r"<style.*?</style>", "", html_doc, flags=re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", t)).split())


def test_the_dossier_is_the_hand_answer(leads_app):
    """No state chosen -> the dossier is scoped to A's own state, MO (round 6
    P3: it used to pool every loaded state). By hand, MO peers of A with
    therapy-only OFF are B C D E G (F is KS-only): on 97110 only G ($5) sits
    below A's $32 -> 1 of 5 = p20, peer median median(5,40,50,60,70) = $50;
    97140 $25 vs 5,35,45,55,65 -> p20, $45; 97530 $40 vs 5,50,60,70,80 ->
    p20, $60. Rho alone: A's 30/25/40 vs the same peers -> p20, 3 of 3 below."""
    meta = leads_app.post("/api/report/dossier.json", json=DOSSIER)
    assert meta.status_code == 200, meta.text
    facts = meta.json()["facts"]
    assert (facts["display_name"], facts["median_percentile"], facts["weakest_payer"],
            facts["state"]) == ("A REHAB", 20.0, "Rho Health", "MO")
    page = leads_app.post("/api/report/dossier", json=DOSSIER)
    assert page.status_code == 200
    t = _text(page.text)
    assert "compared within MO, this practice's main state" in t
    # every position carries its peer count
    assert ("97110 $32.00 $50.00 p20 5 97140 $25.00 $45.00 p20 5 "
            "97530 $40.00 $60.00 p20 5") in t
    # the weakest-payer table carries numbers, not dashes, and no payer without peers
    assert "Codes below median Rho Health p20 3 of 3 " in t
    assert "Sigma Health" not in t
    assert ("Rho Health publishes rates for 6 other practices (1 of them not a therapy "
            "practice) within 25 miles — a moderate fallback.") in t


def test_a_national_dossier_is_a_deliberate_labelled_choice(leads_app):
    """allow_national keeps the pooled comparison, under the national banner.
    By hand, all states: A's peers B C D E F G; on 97110 F ($10) and G ($5)
    sit below $32 -> 2 of 6 = p33, peer median median(5,10,40,50,60,70) = $45."""
    body = {**DOSSIER, "market": {**DOSSIER["market"], "allow_national": True}}
    meta = leads_app.post("/api/report/dossier.json", json=body).json()
    assert (meta["facts"]["median_percentile"], meta["facts"]["state"]) == (33.0, None)
    t = _text(leads_app.post("/api/report/dossier", json=body).text)
    assert "97110 $32.00 $45.00 p33 6 " in t
    assert "NATIONAL COMPARISON" in t


def test_the_dossier_position_is_a_median_not_a_mean(leads_app, monkeypatch):
    """The fixture puts A at the same position on every code, which cannot
    tell a median from a mean. Skew one code: positions 10, 10, 90 -> the
    headline is the median 10 (the rule the Leads list and per-payer view
    use), not the mean 36.7 the dossier used to print as "average"."""
    import mrfx.benchmark as bm
    real = bm.compute_benchmark

    def skewed(store, subject, market):
        b = real(store, subject, market)
        for r in b["rows"]:
            if r.get("subject_percentile") is not None:
                r["subject_percentile"] = {"97530": 90.0}.get(r["billing_code"], 10.0)
                r["subject_percentile_exact"] = r["subject_percentile"]
        return b
    monkeypatch.setattr(bm, "compute_benchmark", skewed)
    meta = leads_app.post("/api/report/dossier.json", json=DOSSIER).json()
    assert meta["facts"]["median_percentile"] == 10.0


def test_leverage_names_what_it_counted(leads_app):
    def headline(therapy_only):
        r = leads_app.post("/api/leverage", json={
            "subject": "A REHAB", "payer": "Rho Health", "radius_miles": 25,
            "market": {"month": "latest", "therapy_only": therapy_only}})
        assert r.status_code == 200, r.text
        return r.json()
    every = headline(False)
    assert every["n_alternatives"] == 6
    assert "6 other practices (1 of them not a therapy practice)" in every["headline"]
    assert [a["display_name"] for a in every["alternatives"]
            if not a["is_therapy_practice"]] == ["G MEDICAL"]
    ther = headline(True)
    assert ther["n_alternatives"] == 5
    assert "5 other therapy practices within 25 miles" in ther["headline"]


# --- the market report (Leads tab -> "Market report"), MO / 97110 ---------
# MO practices pricing 97110: A 32 (median of Rho 30, Sigma 34), B 40, C 50,
# D 60, E 70, G 5 -> one city, median (40 + 50) / 2 = $45.00, 6 practices.
# Who holds the contracts: Rho 6 of 7 relationships = 85.7%, Sigma 1 = 14.3%;
# Rho's 97110 median over A30 B40 C50 D60 E70 G5 = $45.00 (its median over
# EVERY code is $50.00 — the number the table used to print), Sigma $34.00.
# Openings / closures in MO, last year: N2 N3 N4 N7 E = 5; X1 X4 X5 D = 4.
def test_the_market_report_is_the_hand_answer(feeds_app):
    r = feeds_app.post("/api/report/market", json={
        "state": "MO", "code": "97110", "zip": None,
        "market": {"month": "latest", "therapy_only": False}})
    assert r.status_code == 200, r.text
    t = _text(r.text)
    assert "City Median vs state Practices Town $45.00 +0.0% 6" in t
    assert ("Payer Share Practices Median 97110 rate Rho Health 85.7% 6 $45.00 "
            "Sigma Health 14.3% 1 $34.00") in t
    assert "In the last year in MO: 5 therapy NPI(s) newly issued, 4 deactivated" in t


def test_a_payer_without_the_code_has_no_median_not_zero(leads_app):
    """97112 is priced by Rho alone: Sigma still holds a relationship (share)
    but has no 97112 rate — its median is absent, never $0.00."""
    d = leads_app.post("/api/territory/concentration", json={
        "market": {"month": "latest", "state": "MO"}, "code": "97112"}).json()
    got = {p["payer"]: (p["n_practices"], p["median_rate"]) for p in d["payers"]}
    assert got == {"Rho Health": (6, 99.0), "Sigma Health": (1, None)}
    assert d["median_code"] == "97112"
    pooled = leads_app.post("/api/territory/concentration", json={
        "market": {"month": "latest", "state": "MO"}}).json()
    assert {p["payer"]: p["median_rate"] for p in pooled["payers"]} == {
        "Rho Health": 50.0, "Sigma Health": 34.0}
    assert pooled["median_code"] is None
