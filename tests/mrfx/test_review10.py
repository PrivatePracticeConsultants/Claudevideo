"""Review 10: every finding below was reproduced on 2bb81b8 (v0.7.3) and is
pinned here with a tiny hand-worked store. The value each test failed with
on 2bb81b8 is in its comment ("was: …")."""

from __future__ import annotations

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.ingest import scan_inbox

CLINIC = "261QP2000X"
M = {"month": "latest", "state": "MO", "therapy_only": False}


def price(rate, cls="professional", pos=("11",), mods=("GP",)):
    return {"negotiated_type": "negotiated", "negotiated_rate": float(rate),
            "billing_class": cls, "service_code": list(pos),
            "billing_code_modifier": list(mods), "expiration_date": "9999-12-31"}


def doc(payer, day, prices, npis):
    """prices: {code: {tin: rate | [price dicts]}}; npis: {tin: [npi, …]}."""
    items = []
    for code, by_tin in prices.items():
        nr = [{"provider_groups": [{"npi": npis[tin], "tin": {"type": "ein", "value": tin}}],
               "negotiated_prices": r if isinstance(r, list) else [price(r)]}
              for tin, r in by_tin.items()]
        items.append({"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                      "billing_code": code, "negotiated_rates": nr})
    return {"reporting_entity_name": payer, "last_updated_on": day, "version": "2.0.0",
            "in_network": items}


def load(cfg, store, docs):
    for fname, d in docs:
        (cfg.inbox_dir / fname).write_text(json.dumps(d))
    assert all(r["status"] == "done" for r in scan_inbox(cfg, store))


def org(store, npi, label, state, city="Town"):
    store.save_npi(npi, label, CLINIC, "x", city, state, entity_type="NPI-2",
                   zip_code="64105")


def tins(prefix, n):
    return [f"{prefix}{i:04d}" for i in range(1, n + 1)]


def simple_market(cfg, store, rates, *, payer="Rho Health", day="2026-06-01",
                  code="97110", prefix="81000", state="MO"):
    """One payer, one code, one practice per rate, all in `state`."""
    T = tins(prefix, len(rates))
    npis = {t: [f"1{t}"] for t in T}
    load(cfg, store, [(f"{prefix}_{day[:7]}.json",
                       doc(payer, day, {code: dict(zip(T, rates))}, npis))])
    for i, t in enumerate(T):
        org(store, npis[t][0], f"P{i + 1} REHAB", state)
    store.rebuild_rollups()
    return T, npis


# ------------------------------------------------------------ group 1: search


def test_boot_answers_the_first_screen_in_one_request(cfg, store):
    # 1.4: the page waited on /api/stats (the header counts) before its first
    # search. was: 404
    simple_market(cfg, store, [40, 50])
    r = TestClient(create_app(cfg, store)).get("/api/boot")
    assert r.status_code == 200 and r.json()["default_grain"] in ("tin", "entity", "npi")


def test_percent_and_underscore_are_literal_in_searches(cfg, store):
    # 1.9: `q=__` and `modifier=%` were SQL wildcards. was: 2 rows each
    simple_market(cfg, store, [40, 50])
    c = TestClient(create_app(cfg, store))
    assert c.get("/api/rates?grain=tin&q=__").json()["total"] == 0
    assert c.get("/api/rates?grain=tin&modifier=%25").json()["total"] == 0
    assert c.get("/api/rates?grain=tin&q=P1").json()["total"] == 1


def test_a_ten_digit_npi_suggests_its_practice_and_explains_the_tin_view(cfg, store):
    # 1.9 / 1.7: an NPI in the subject box suggested nothing, and in the
    # Explorer at the TIN grain read "No rates match". was: [] and no hint
    T, npis = simple_market(cfg, store, [40, 50])
    c = TestClient(create_app(cfg, store))
    subj = c.get(f"/api/benchmark/subjects?q={npis[T[0]][0]}").json()
    assert T[0] in json.dumps(subj)
    r = c.get(f"/api/rates?grain=tin&q={npis[T[0]][0]}").json()
    assert r["total"] == 0 and T[0] in r["hint"]


def test_entity_two_pass_page_is_the_full_aggregate(cfg, store, monkeypatch):
    # 1.7: the entity page now picks its 100 groups on a light aggregate and
    # builds the list columns for those only. Same rows, every sort and page.
    # was: AttributeError (no two-pass path)
    import mrfx.api as api
    T = tins("82000", 12)
    npis = {t: [f"1{t}"] for t in T}
    pr = {"97110": {t: 30 + i for i, t in enumerate(T)},
          "97140": {t: 25 + (i % 4) for i, t in enumerate(T)}}
    load(cfg, store, [("a_2026-05.json", doc("Rho Health", "2026-05-01", pr, npis)),
                      ("b_2026-06.json", doc("Sigma Health", "2026-06-01", pr, npis))])
    for i, t in enumerate(T):
        org(store, npis[t][0], f"E{i % 5} REHAB", "MO" if i % 2 else "KS")
    store.rebuild_rollups()
    c = TestClient(create_app(cfg, store))
    urls = [f"/api/rates?grain=entity&latest=1&page_size=5&page={pg}&sort={s}&dir={d}"
            for s in ("negotiated_rate", "display_name", "npi_count", "file_month")
            for d in ("asc", "desc") for pg in (1, 2)]
    urls.append("/api/rates?grain=entity&latest=1&state=MO&page_size=5")
    fast = [c.get(u).json() for u in urls]
    monkeypatch.setattr(api, "entity_two_pass_ok", lambda fs, sort="negotiated_rate": False)
    slow = [TestClient(create_app(cfg, store)).get(u).json() for u in urls]
    assert fast == slow
    assert all(f["rows"] for f in fast)


# ------------------------------------------------- group 2: wrong numbers


def test_market_report_cities_are_the_states_own(cfg, store):
    # 2.1: a practice with clinicians in MO (Springfield) and KS (Wichita)
    # put WICHITA into the MO report. was: ['SPRINGFIELD', 'WICHITA']
    from mrfx.territory import local_rate_map
    T = tins("83000", 5)
    npis = {t: [f"1{t}", f"2{t}"] for t in T}
    load(cfg, store, [("r_2026-06.json",
                       doc("Rho Health", "2026-06-01", {"97110": {t: 40 for t in T}}, npis))])
    for i, t in enumerate(T):
        org(store, npis[t][0], f"P{i} REHAB", "MO", "Springfield")
        org(store, npis[t][1], f"P{i} REHAB", "KS", "Wichita")
    store.rebuild_rollups()
    got = local_rate_map(store, "97110", {"month": "latest"}, state="MO")
    assert [c["city"].upper() for c in got["cities"]] == ["SPRINGFIELD"]


def test_markets_with_a_state_ranks_every_state_on_its_own_practices(cfg, store):
    # 2.3: State = MO kept MO's practices and printed KS from them alone.
    # 5 MO-only $40, 5 MO+KS $50, 5 KS-only $70. was: KS 50.0 (should be 60)
    from mrfx.market import geographic_rates
    mo, both, ks = tins("84000", 5), tins("84100", 5), tins("84200", 5)
    npis = {t: [f"1{t}"] for t in mo + ks}
    npis.update({t: [f"1{t}", f"2{t}"] for t in both})
    pr = {"97110": {**{t: 40 for t in mo}, **{t: 50 for t in both}, **{t: 70 for t in ks}}}
    load(cfg, store, [("r_2026-06.json", doc("Rho Health", "2026-06-01", pr, npis))])
    for t in mo:
        org(store, npis[t][0], f"M{t} REHAB", "MO")
    for t in ks:
        org(store, npis[t][0], f"K{t} REHAB", "KS")
    for t in both:
        org(store, npis[t][0], f"B{t} REHAB", "MO")
        org(store, npis[t][1], f"B{t} REHAB", "KS")
    store.rebuild_rollups()
    g = geographic_rates(store, "97110", {"month": "latest", "state": "MO"})
    by = {r["state"]: r["median_rate"] for r in g["states"]}
    assert by == {"KS": 60.0, "MO": 45.0}
    assert g["selected_state"] == "MO"


def test_rate_changes_for_a_picked_month_compare_each_payer_with_its_own_previous_file(cfg, store):
    # 2.4: Beta published 04 and 06 (not 05); picking 06 compared the store's
    # 05 and dropped Beta's cut. was: Beta absent ({'Alpha'})
    from mrfx.monitor import compute_rate_changes
    T = tins("85000", 2)
    npis = {t: [f"1{t}"] for t in T}
    load(cfg, store, [
        ("a5.json", doc("Alpha", "2026-05-01", {"97110": {T[0]: 40}}, npis)),
        ("a6.json", doc("Alpha", "2026-06-01", {"97110": {T[0]: 44}}, npis)),
        ("b4.json", doc("Beta", "2026-04-01", {"97110": {T[1]: 47}}, npis)),
        ("b6.json", doc("Beta", "2026-06-01", {"97110": {T[1]: 40}}, npis))])
    store.rebuild_rollups()
    d = compute_rate_changes(store, {"month": "2026-06"})
    assert {c["payer"] for c in d["changes"]} == {"Alpha", "Beta"}
    assert {(c["payer"], c["prev_month"]) for c in d["compared"]} == {
        ("Alpha", "2026-05"), ("Beta", "2026-04")}
    beta = next(c for c in d["changes"] if c["payer"] == "Beta")
    assert (beta["old_rate"], beta["new_rate"]) == (47.0, 40.0)


def test_best_paying_payer_is_judged_on_shared_codes():
    # 2.6: with an MPFS anchor the scorecard ranked % of Medicare over each
    # payer's OWN codes: Beta ($42 on 97110 only, 140 %) over Alpha ($45 on the
    # same code). was: best_payer 'Beta' (with the anchor loaded)
    from mrfx.schedule import payer_scorecard
    fs = {"payers": ["Alpha", "Beta"], "mpfs_loaded": True, "codes": [
        {"billing_code": "97110", "rates": {"Alpha": {"rate": 45.0, "pct_medicare": 150},
                                            "Beta": {"rate": 42.0, "pct_medicare": 140}}},
        {"billing_code": "97140", "rates": {"Alpha": {"rate": 30.0, "pct_medicare": 100}}},
        {"billing_code": "97530", "rates": {"Alpha": {"rate": 44.0, "pct_medicare": 110}}}]}
    sc = payer_scorecard(fs)
    assert sc["best_payer"] == "Alpha" and sc["shared_codes"] == ["97110"]


def test_monthly_packet_contains_market_position(cfg, store, tmp_path):
    # 2.7: the packet's market had no state, so "Market position" was always
    # refused. was: '2_market_position.html' not written
    from mrfx.clients import add_client
    from mrfx.packets import build_all_packets
    T, _ = simple_market(cfg, store, [40, 45, 50, 55, 60, 65])
    add_client(store, T[0])
    res = build_all_packets(cfg, store, tmp_path / "pk")
    assert "2_market_position.html" in res["packets"][0]["written"]


def test_size_panel_prints_the_one_overall_position(cfg, store):
    # 2.8: the size panel printed the band statistic as "is at p37.5" while
    # every other screen said p13. was: KeyError 'position'
    from mrfx.benchmark import compute_benchmark, overall_position
    from mrfx.quality import size_premium
    T, _ = simple_market(cfg, store, [40, 45, 50, 55, 60, 65])
    sp = size_premium(store, M, subject=T[0], min_practices=1)
    want = overall_position(compute_benchmark(store, T[0], M)["rows"])
    assert sp["subject"]["position"] == want == 0


def test_printed_reports_are_landscape(cfg, store):
    # 2.10: a rate card printed portrait and lost its right-hand columns.
    # was: no "size: landscape" in the document
    from mrfx.schedule import compute_fee_schedule, payer_scorecard, render_rate_card
    T, _ = simple_market(cfg, store, [40, 45, 50, 55, 60, 65])
    fs = compute_fee_schedule(store, T[0], M)
    html = render_rate_card(cfg, store, fs, payer_scorecard(fs))
    assert "size: landscape" in html


# ------------------------------------------------ group 3: consistency


def test_code_page_month_table_is_on_the_payer_tables_basis(cfg, store):
    # 3.1: /api/trend took a raw line median incl. an institutional $900 line.
    # was: 2026-06 median 470.0 (median of 40 and 900); one-month payers listed
    T = tins("86000", 1)
    npis = {t: [f"1{t}"] for t in T}
    may = {"97110": {T[0]: 40}}
    june = {"97110": {T[0]: [price(40), price(900, cls="institutional")]}}
    load(cfg, store, [("r5.json", doc("Rho Health", "2026-05-01", may, npis)),
                      ("r6.json", doc("Rho Health", "2026-06-01", june, npis)),
                      ("s6.json", doc("Sigma Health", "2026-06-01", may, npis))])
    store.rebuild_rollups()
    rows = TestClient(create_app(cfg, store)).get("/api/trend?cpt=97110&grain=tin").json()["rows"]
    assert [(r["payer"], r["file_month"], r["median_rate"]) for r in rows] == [
        ("Rho Health", "2026-05", 40.0), ("Rho Health", "2026-06", 40.0)]


def test_by_code_medians_arrive_rounded_half_up(cfg, store):
    # 3.2: the browser rounded raw doubles its own way ($70.70 vs $70.69).
    # 40.125 / 40.135 -> 40.13 (DuckDB / half_up). was: 40.13 printed from 40.13
    # only by luck; raw 40.130000000000003 arrived
    simple_market(cfg, store, [40.125, 40.135])
    s = TestClient(create_app(cfg, store)).get("/api/summary?grain=tin&latest=1").json()
    meds = [r["median"] for r in s["by_code"]]
    assert meds == [round(m, 2) for m in meds] and meds == [40.13]


def test_rate_changes_rows_say_which_claim_line(cfg, store):
    # 3.3: the POS 02 and POS 11 lines read as two identical rows.
    # was: KeyError 'service_code_set'
    from mrfx.monitor import compute_rate_changes
    T = tins("87000", 1)
    npis = {t: [f"1{t}"] for t in T}
    load(cfg, store, [
        ("r5.json", doc("Rho", "2026-05-01",
                        {"97110": {T[0]: [price(40, pos=("11",)), price(30, pos=("02",))]}}, npis)),
        ("r6.json", doc("Rho", "2026-06-01",
                        {"97110": {T[0]: [price(44, pos=("11",)), price(33, pos=("02",))]}}, npis))])
    store.rebuild_rollups()
    d = compute_rate_changes(store, {"month": "latest"})
    assert sorted((c["billing_class"], str(c["service_code_set"])) for c in d["changes"]) == [
        ("professional", "02"), ("professional", "11")]


def test_state_names_are_codes_and_foreign_places_are_not_states(cfg, store):
    # 3.4: "69 states with data" counted MASSACHUSETTS, RIYADH, AE …
    # was: 'MASSACHUSETTS' stored, states == 3
    T, npis = simple_market(cfg, store, [40, 50, 60])
    store.save_npi(npis[T[1]][0], "P2 REHAB", CLINIC, "x", "Boston", "Massachusetts",
                   entity_type="NPI-2")
    store.save_npi(npis[T[2]][0], "P3 REHAB", CLINIC, "x", "Riyadh", "RIYADH",
                   entity_type="NPI-2")
    store.rebuild_rollups()
    from mrfx.overview import market_overview
    with store.connect() as con:
        st = con.execute("SELECT state FROM npi_directory WHERE npi = ?",
                         [npis[T[1]][0]]).fetchone()[0]
    assert st == "MA"
    assert sorted(store.available_states()) == ["MA", "MO"]
    assert market_overview(store, "MO")["states"] == 1


def test_freshness_ignores_zero_row_files_and_says_future(cfg, store):
    # 3.6: a 0-row file counted as the payer's publication; a future header
    # printed "-21 days". was: published '2026-10-14'
    from mrfx.quality import payer_freshness
    T = tins("88000", 1)
    npis = {t: [f"1{t}"] for t in T}
    load(cfg, store, [
        ("r8.json", doc("Rho", "2026-08-25", {"97110": {T[0]: 40}}, npis)),
        ("r10.json", doc("Rho", "2026-10-14", {"99999": {T[0]: 40}}, npis)),   # 0 rows
        ("s.json", doc("Sigma", "2099-01-01", {"97110": {T[0]: 41}}, npis))])
    store.rebuild_rollups()
    f = {p["payer"]: p for p in payer_freshness.__wrapped__(store)["payers"]}
    assert f["Rho"]["published"] == "2026-08-25"
    assert f["Sigma"]["future_dated"] is True


def test_sources_tab_carries_the_tested_catalog():
    # 3.7: Sources said "unverified — search for current URL" for links Files
    # auto-queues; KS pairs Kansas with Kansas, not Kansas City.
    # was: ImportError (no merge)
    from mrfx.known_sources import merge_registry_cards
    cards = [{"key": "a", "name": "Blue Cross and Blue Shield of Kansas", "mrf_url": None},
             {"key": "b", "name": "Blue Cross and Blue Shield of Kansas City", "mrf_url": None}]
    known = [{"name": "BCBS Kansas City", "url": "https://kc", "states": ["KS", "MO"],
              "queueable": True, "verified": "2026-11-05"},
             {"name": "Blue Cross and Blue Shield of Kansas", "url": "https://ks",
              "states": ["KS"], "queueable": True, "verified": "2026-11-05"},
             {"name": "Medica", "url": "https://medica", "states": ["KS"],
              "queueable": True, "verified": "2026-11-05"}]
    got = merge_registry_cards(cards, known, "KS")
    assert [(c["name"], c["tested"]["url"]) for c in got] == [
        ("Blue Cross and Blue Shield of Kansas", "https://ks"),
        ("Blue Cross and Blue Shield of Kansas City", "https://kc"),
        ("Medica", "https://medica")]


def test_new_to_network_means_absent_from_every_earlier_book(cfg, store):
    # 3.8: B was in Rho's April book, missing from a partly loaded May, back
    # in June — "newly contracted". was: ['B', 'C']
    from mrfx.contracts import new_to_network
    A, B, C = tins("89000", 3)
    npis = {t: [f"1{t}"] for t in (A, B, C)}
    load(cfg, store, [
        ("r4.json", doc("Rho", "2026-04-01", {"97110": {A: 40, B: 41}}, npis)),
        ("r5.json", doc("Rho", "2026-05-01", {"97110": {A: 40}}, npis)),
        ("r6.json", doc("Rho", "2026-06-01", {"97110": {A: 40, B: 41, C: 42}}, npis))])
    for k, t in zip("ABC", (A, B, C)):
        org(store, npis[t][0], f"{k} REHAB", "MO")
    store.rebuild_rollups()
    got = new_to_network(store, {"month": "latest"}, therapy_only=False)
    assert [r["practice"] for r in got["rows"]] == ["C REHAB"]


def test_leads_market_median_is_benchmarks(cfg, store):
    # 3.9: Leads included the practice in its own market median: $47.50 on
    # Leads vs $48.00 on Benchmark for a 6-practice market. was: gap 7.5
    from mrfx.benchmark import compute_benchmark
    from mrfx.leads import compute_leads
    T, _ = simple_market(cfg, store, [40, 46, 48, 49, 50, 60])
    lead = next(x for x in compute_leads(store, M, threshold_percentile=25, min_codes=1)["leads"]
                if x["display_name"] == "P1 REHAB")
    p50 = compute_benchmark(store, T[0], M)["rows"][0]["p50"]
    assert p50 == 49.0 and lead["avg_gap_to_median"] == p50 - 40 == 9.0


def test_a_practice_missing_from_the_payers_newest_file_is_flagged(cfg, store):
    # 3.12: H REHAB, absent from the payer's newer file, still topped Leads
    # unflagged. was: KeyError 'not_in_newest'
    from mrfx.leads import compute_leads
    T = tins("90000", 6)
    npis = {t: [f"1{t}"] for t in T}
    may = {"97110": dict(zip(T, (20, 45, 50, 55, 60, 65)))}
    june = {"97110": dict(zip(T[1:], (45, 50, 55, 60, 65)))}
    load(cfg, store, [("r5.json", doc("Rho", "2026-05-01", may, npis)),
                      ("r6.json", doc("Rho", "2026-06-01", june, npis))])
    for i, t in enumerate(T):
        org(store, npis[t][0], f"P{i + 1} REHAB", "MO")
    store.rebuild_rollups()
    leads = compute_leads(store, M, threshold_percentile=25, min_codes=1)["leads"]
    top = leads[0]
    assert top["display_name"] == "P1 REHAB"
    assert top["not_in_newest"] == [{"payer": "Rho", "last_seen": "2026-05",
                                     "payer_newest": "2026-06"}]
    assert all(not x["not_in_newest"] for x in leads[1:])


def test_pinned_documents_stop_at_their_month(cfg, store):
    # 3.13: a trajectory pinned to 2023-06 listed 2024-06 in `months`, and the
    # rate card listed later months' files. was: ['2023-06', '2024-06']
    from mrfx.monitor import compute_payer_trajectory
    from mrfx.schedule import _methodology, compute_fee_schedule
    T = tins("91000", 3)
    npis = {t: [f"1{t}"] for t in T}
    load(cfg, store, [
        ("p_2023-06.json", doc("P", "2023-06-01", {"97110": {t: 40 for t in T}}, npis)),
        ("p_2024-06.json", doc("P", "2024-06-01", {"97110": {t: 44 for t in T}}, npis))])
    for i, t in enumerate(T):
        org(store, npis[t][0], f"P{i} REHAB", "MO")
    store.rebuild_rollups()
    tr = compute_payer_trajectory(store, {"month": "2023-06", "therapy_only": False})
    assert tr["months"] == ["2023-06"]
    pinned = {"month": "2023-06", "state": "MO", "therapy_only": False}
    text = _methodology(store, compute_fee_schedule(store, T[0], pinned))
    assert "p_2023-06.json" in text and "p_2024-06.json" not in text


def test_export_sidecar_states_filters_in_words_and_file_months(cfg, store):
    # 3.13: `Filters: {"dollar_rates_only": true, …}` and the header's month.
    # was: 'Filters: {"'
    import zipfile
    simple_market(cfg, store, [40, 50])
    r = TestClient(create_app(cfg, store)).get("/api/export.zip?grain=tin&state=MO&latest=1")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    meth = next(z.read(n).decode() for n in z.namelist() if "methodology" in n)
    assert "Filters: state: MO" in meth and 'Filters: {"' not in meth
    assert "month 2026-06" in meth


# ------------------------------------------------- group 4: small, reproduced


@pytest.mark.parametrize("path,body", [
    ("/api/contract-gaps", {"subject": "x", "min_peers": 1e308}),
    ("/api/contracts/new-to-network", {"radius_miles": "NaN", "zip": "64105"}),
    ("/api/trajectory", {"market": "MO"}),
    ("/api/engagements/compare", {"subject": "x", "market": [1]}),
])
def test_bad_input_is_a_sentence_not_a_500(cfg, store, path, body):
    # was: 500 on each (min_peers 1e308 is now bounded and simply answered)
    simple_market(cfg, store, [40, 50])
    r = TestClient(create_app(cfg, store), raise_server_exceptions=False).post(path, json=body)
    assert r.status_code in (200, 404, 422), (r.status_code, r.text[:200])


def test_saving_an_index_year_refreshes_cached_answers(store, monkeypatch):
    # group 4: the cached trajectory kept "No real-terms figures" after a CPI
    # year was added. was: note_user_edit never called
    from mrfx.inflation import save_index_values
    calls = []
    monkeypatch.setattr(store, "note_user_edit", lambda: calls.append(1))
    save_index_values(store, [(2031, 340.0)])
    assert calls == [1]


def test_a_client_name_that_matches_nothing_is_refused(cfg, store):
    # group 4: accepted, then "no longer matches". was: accepted
    from mrfx.benchmark import BenchmarkError
    from mrfx.clients import add_client
    simple_market(cfg, store, [40, 50])
    with pytest.raises(BenchmarkError, match="no practice in the store matches"):
        add_client(store, "Nobody Rehab LLC")
    assert add_client(store, "P1 REHAB") == ["P1 REHAB"]


def test_leads_csv_keeps_its_column_order_and_fee_csv_prints_cents(cfg, store):
    # group 4: 0.7.3 put worst_payer_codes before website (a mail merge mapped
    # by position shifted); fee.csv printed 40.0. was: [..., 'worst_payer_codes',
    # 'website'] and '40.0'
    from mrfx.leads import compute_leads, leads_csv
    from mrfx.schedule import compute_fee_schedule, fee_schedule_csv
    T, _ = simple_market(cfg, store, [40, 45, 50, 55, 60])
    text = leads_csv(store, compute_leads(store, M, threshold_percentile=25, min_codes=1))
    header = next(r for r in csv.reader(io.StringIO(text)) if r and not r[0].startswith("#"))
    assert header[11:] == ["worst_payer_gap", "website", "worst_payer_codes",
                           "not_in_payers_newest_file"]
    from mrfx.schedule import payer_scorecard
    fs = compute_fee_schedule(store, T[0], M)
    fee = fee_schedule_csv(fs, payer_scorecard(fs), store)
    row = next(r for r in csv.reader(io.StringIO(fee)) if r and r[0] == "Rho Health")
    assert row[4] == "40.00"


def test_wording_posture_and_floors(cfg, store):
    # group 4: "0 of 2 payer(s) demonstrably price practices differently —
    # those are the negotiations with room." was: that sentence
    from mrfx.quality import payer_posture
    simple_market(cfg, store, [40, 40, 40, 40, 40])
    p = payer_posture.__wrapped__(store, M) if hasattr(payer_posture, "__wrapped__") \
        else payer_posture(store, M)
    assert not p["headline"].startswith("0 of")
