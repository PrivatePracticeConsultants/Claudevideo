"""Audit 8, outputs: every number below was worked by hand and was wrong
(or two pages disagreed) before this round. One tiny store per test."""

import csv
import io
import json
import re
import zipfile

import duckdb
import pytest

from mrfx.ingest import scan_inbox

PT, CLINIC = "225100000X", "261QP2000X"
M = {"month": "latest", "state": "MO", "therapy_only": False}


def price(rate, mods=("GP",), pos=("11",), cls="professional"):
    return {"negotiated_type": "negotiated", "negotiated_rate": float(rate),
            "billing_class": cls, "service_code": list(pos),
            "billing_code_modifier": list(mods), "expiration_date": "9999-12-31"}


def doc(payer, day, prices, npis):
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
    for name, d in docs:
        (cfg.inbox_dir / name).write_text(json.dumps(d))
    assert all(r["status"] == "done" for r in scan_inbox(cfg, store))


def name(store, npi, label, state, tax=CLINIC):
    store.save_npi(npi, label, tax, "x", "Town", state, entity_type="NPI-2", zip_code="64105")


def practices(store, letters, prefix):
    T = {k: f"{prefix}{i}000001" for i, k in enumerate(letters, start=1)}
    return T, {t: [f"1{t}"] for t in T.values()}


def label_all(store, T, npis, state="MO", tax=CLINIC):
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", state, tax)


def test_trajectory_adding_a_code_is_not_a_cut(cfg, store):
    # P0: same $50 on 97110 both years; 97140 at $30 appears in year 2.
    # One pooled median read $50 -> $40, -20 %. Nothing moved: 0 %.
    from mrfx.monitor import compute_payer_trajectory
    T, npis = practices(store, "ABCDE", "71")
    y1 = {"97110": {t: 50 for t in T.values()}}
    y2 = {"97110": {t: 50 for t in T.values()}, "97140": {t: 30 for t in T.values()}}
    load(cfg, store, [("p_2023-06.json", doc("P Health", "2023-06-01", y1, npis)),
                      ("p_2024-06.json", doc("P Health", "2024-06-01", y2, npis))])
    label_all(store, T, npis)
    store.rebuild_rollups()
    p = compute_payer_trajectory(store, {"state": "MO", "therapy_only": False})["payers"][0]
    assert p["cumulative_pct"] == 0.0
    assert p["panel_codes"] == 1 and p["panel_practices"] == 5
    assert p["real_cumulative_pct"] == -2.9        # CPI-U 304.702 -> 313.689


def test_trajectory_measures_a_real_move_on_the_matched_panel(cfg, store):
    from mrfx.monitor import compute_payer_trajectory
    T, npis = practices(store, "ABCD", "72")
    y1 = {"97110": {t: 40 for t in T.values()}, "97140": {t: 30 for t in T.values()}}
    # 97110 +10 %, 97140 unchanged, and a new practice E at $90 must not count
    y2 = {"97110": {**{t: 44 for t in T.values()}, "729000001": 90},
          "97140": {t: 30 for t in T.values()}}
    npis["729000001"] = ["1729000001"]
    load(cfg, store, [("p_2023-06.json", doc("P Health", "2023-06-01", y1, npis)),
                      ("p_2024-06.json", doc("P Health", "2024-06-01", y2, npis))])
    p = compute_payer_trajectory(store, {"therapy_only": False})["payers"][0]
    # per code: 97110 ratio 1.10, 97140 ratio 1.00 -> median across codes 1.05
    assert p["cumulative_pct"] == 5.0
    assert p["panel_practices"] == 4


@pytest.mark.parametrize("mapped", [False, True])
def test_multi_state_subject_rate_is_one_number(cfg, store, mapped):
    # P1-2: ACME = a MO TIN paid $40 and a KS TIN paid $60; market MO. It
    # used to read $40 on Benchmark and $50 on the rate card. One rule now:
    # a bare NPPES name shared by practices in two states is two practices
    # (the Explorer's "ACME REHAB · MO" / "· KS") -> the MO one, $40, on every
    # page; a MAPPED entity is one practice -> median(40, 60) = $50 everywhere.
    from mrfx.benchmark import compute_benchmark, compute_payer_negotiation
    from mrfx.schedule import compute_fee_schedule
    tins = {"S1": "610000001", "S2": "610000002", "B": "620000001", "C": "630000001",
            "D": "640000001", "E": "650000001"}
    npis = {t: [f"1{t}"] for t in tins.values()}
    pr = {"97110": dict(zip(tins.values(), (40, 60, 45, 50, 55, 60)))}
    load(cfg, store, [("rho_2026-06.json", doc("Rho Health", "2026-06-01", pr, npis))])
    name(store, "1610000001", "ACME REHAB", "MO")
    name(store, "1610000002", "ACME REHAB", "KS")
    for k in "BCDE":
        name(store, npis[tins[k]][0], f"{k} REHAB", "MO")
    store.rebuild_rollups()
    subj, want = "ACME REHAB", 40.0
    if mapped:
        store.set_entity_map({tins["S1"]: "ACME GROUP", tins["S2"]: "ACME GROUP"})
        subj, want = "ACME GROUP", 50.0
    b = compute_benchmark(store, subj, M)
    assert [r["subject_rate"] for r in b["rows"]] == [want]
    n = compute_payer_negotiation(store, subj, M)
    assert [r["subject_rate"] for s in n["sections"] for r in s["benchmark"]["rows"]] == [want]
    fs = compute_fee_schedule(store, subj, M)
    assert fs["codes"][0]["rates"]["Rho Health"]["rate"] == want


def test_worst_payer_counts_only_ranked_codes(cfg, store):
    # P1-3: Rho pays A $30 on 97110 (others 40-70) and everyone a flat $50 on
    # 97530; Sigma pays A $34 on 97110. A's market median on 97110 is the
    # median of the OTHER practices, 40/50/60/70 -> $55 (review 10 item 3.9).
    # Rho is $25 under on the only ranked code; Sigma $21. B: 55 - 40 = 15.
    from mrfx.leads import compute_leaderboard, compute_leads
    T, npis = practices(store, "ABCDE", "80")
    rho = {"97110": dict(zip(T.values(), (30, 40, 50, 60, 70))),
           "97530": {t: 50 for t in T.values()}}
    load(cfg, store, [("rho_2026-06.json", doc("Rho Health", "2026-06-01", rho, npis)),
                      ("sig_2026-06.json", doc("Sigma Health", "2026-06-01",
                                               {"97110": {T["A"]: 34}}, npis))])
    label_all(store, T, npis)
    store.rebuild_rollups()
    leads = {x["display_name"]: x for x in compute_leads(
        store, M, threshold_percentile=25, min_codes=1)["leads"]}
    assert (leads["A REHAB"]["worst_payer"], leads["A REHAB"]["worst_payer_gap"]) == ("Rho Health", 25.0)
    assert leads["B REHAB"]["worst_payer_gap"] == 15.0
    lb = {p["display_name"]: p for p in compute_leaderboard(store, M, min_codes=1)["practices"]}
    assert lb["A REHAB"]["median_percentile"] == leads["A REHAB"]["median_percentile"] == 0


def test_visit_value_compares_payers_on_one_basket(cfg, store, tmp_path):
    # P1-4: mix 97110 x1000, 97530 x100. A prices only 97530 at $50; B prices
    # 97110 $40 and 97530 $60. On the shared code B pays more.
    from mrfx.economics import visit_economics
    from mrfx.utilization import import_utilization
    S = "430000001"
    npis = {S: ["1417594896"]}
    load(cfg, store, [("a_2026-06.json", doc("A Health", "2026-06-01", {"97530": {S: 50}}, npis)),
                      ("b_2026-06.json", doc("B Health", "2026-06-01",
                                             {"97110": {S: 40}, "97530": {S: 60}}, npis))])
    name(store, "1417594896", "SUBJ REHAB", "MO")
    store.rebuild_rollups()
    p = tmp_path / "puf_2024.csv"
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Rndrng_NPI", "HCPCS_Cd", "Tot_Srvcs", "Tot_Benes", "Avg_Mdcr_Alowd_Amt",
                    "Place_Of_Srvc"])
        w.writerow(["1417594896", "97110", 1000, 40, 30.0, "O"])
        w.writerow(["1417594896", "97530", 100, 20, 35.0, "O"])
    import_utilization(store, p, year="2024")
    ve = visit_economics(store, "SUBJ REHAB", {"month": "latest", "state": "MO"}, units_per_visit=3)
    assert (ve["best_payer"], ve["worst_payer"]) == ("B Health", "A Health")
    assert {r["payer"]: r["value_per_visit"] for r in ve["rows"]} == {"B Health": 180.0,
                                                                      "A Health": 150.0}
    assert ve["common_codes"] == ["97530"]


def test_size_premium_uses_mid_rank(cfg, store):
    # P1-5: one payer's flat schedule across five practices -> p50, not p0
    from mrfx.quality import size_premium
    T = {k: f"6{i}4000001" for i, k in enumerate("ABCDE", start=1)}
    npis = {t: [f"16{i}400000{j}" for j in range(1, 2 + i)] for i, t in enumerate(T.values(), start=1)}
    pr = {c: {t: r for t in T.values()} for c, r in (("97110", 40), ("97140", 35), ("97530", 50))}
    load(cfg, store, [("r_2026-06.json", doc("Rho Health", "2026-06-01", pr, npis))])
    for k, t in T.items():
        for n in npis[t]:
            name(store, n, f"{k} REHAB", "MO", tax=PT)
    store.rebuild_rollups()
    sp = size_premium(store, M, subject="A REHAB", min_practices=1)
    assert sp["subject"]["avg_percentile"] == 50.0
    assert all(b["avg_percentile"] == 50.0 for b in sp["bands"] if not b["thin"])


def _outreach(cfg, store, **params):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    r = TestClient(create_app(cfg, store)).get("/api/export/outreach.zip",
                                               params={"grain": "entity", **params})
    z = zipfile.ZipFile(io.BytesIO(r.content))
    rows = list(csv.DictReader(io.StringIO(
        z.read(next(n for n in z.namelist() if n.endswith(".csv"))).decode("utf-8-sig"))))
    meth = z.read(next(n for n in z.namelist() if n.endswith(".txt"))).decode()
    return {x["ORG_NAME"]: x for x in rows}, meth


def test_outreach_merge_fields_are_on_the_house_basis(cfg, store):
    # P1-6: A has May $40, June $42 + a CQ $34 line + an institutional $100.
    # Benchmark: $42.00, p0. Outreach pooled them all: $41, PCTL 20.
    from mrfx.benchmark import compute_benchmark
    T, npis = practices(store, "ABCDE", "62")
    A = T["A"]
    may = {"97110": dict(zip(T.values(), (40, 45, 50, 55, 60)))}
    jun = {"97110": {A: [price(42), price(34, mods=("GP", "CQ")), price(100, cls="institutional")],
                     **dict(zip(list(T.values())[1:], (45, 50, 55, 60)))}}
    load(cfg, store, [("r_2026-05.json", doc("Rho Health", "2026-05-01", may, npis)),
                      ("r_2026-06.json", doc("Rho Health", "2026-06-01", jun, npis))])
    label_all(store, T, npis)
    store.rebuild_rollups()
    b = compute_benchmark(store, "A REHAB", {"month": "latest", "state": "MO"})
    assert (b["rows"][0]["subject_rate"], b["rows"][0]["subject_percentile"]) == (42.0, 0)
    rows, meth = _outreach(cfg, store, dollar_only="1", state="MO")
    assert (rows["A REHAB"]["C97110_RATE"], rows["A REHAB"]["C97110_PCTL"]) == ("42.0", "0")
    assert rows["C REHAB"]["C97110_PCTL"] == "50" and rows["E REHAB"]["C97110_PCTL"] == "100"
    assert "C<code>_PCTL" in meth and "mid-rank" in meth


def test_outreach_flat_code_is_p50_and_median_matches_sql(cfg, store):
    # P2-9 + P1-6: the 97110 median of 40.25, 40.25, 50, 50 is 45.125 —
    # DuckDB prints 45.13 (Benchmark, Markets); Python's round() said 45.12.
    # A code every practice is paid the same on is p50, not 100.
    T, npis = practices(store, "ABCD", "63")
    pr = {"97110": dict(zip(T.values(), (40.25, 40.25, 50, 50))),
          "97140": {t: 35 for t in T.values()}}
    load(cfg, store, [("r_2026-06.json", doc("Rho Health", "2026-06-01", pr, npis))])
    label_all(store, T, npis)
    store.rebuild_rollups()
    rows, _ = _outreach(cfg, store, dollar_only="1", state="MO")
    # each row's market is the median of the OTHER practices (review 10 3.9):
    # A (40.25) -> median(40.25, 50, 50) = 50; D (50) -> median(40.25, 40.25, 50)
    assert rows["A REHAB"]["C97110_MKT_MEDIAN"] == "50.0"
    assert rows["D REHAB"]["C97110_MKT_MEDIAN"] == "40.25"
    assert rows["A REHAB"]["C97140_PCTL"] == "50"
    from mrfx.overview import market_overview
    top = {c["billing_code"]: c["median"] for c in market_overview(store, "MO")["top_codes"]}
    assert top["97110"] == 45.13


def test_half_up_is_duckdbs_round():
    from mrfx.rounding import half_up
    vals = [20.025, 2.675, 45.125, 112.5, 12.5, 33.325, -2.5, 0.5] + [
        (i + 0.5) / 100 for i in range(1000, 4000, 7)] + [i / 1000 for i in range(10000, 13000, 3)]
    con = duckdb.connect()
    for d in (0, 1, 2):
        got = con.execute("SELECT x, round(x, ?) FROM (SELECT unnest(?::DOUBLE[]) x)",
                          [d, vals]).fetchall()
        assert [x for x, r in got if half_up(x, d) != r] == []
    assert half_up(20.025, 2) == 20.02 and half_up(112.5) == 113 and half_up(12.5) == 13


def test_sparkline_stops_at_the_pinned_month(cfg, store):
    # P1-7: a report pinned to 2026-05 drew a June point
    from mrfx.spark import series_by_code
    T, npis = practices(store, "SB", "51")
    docs = [(f"a_{d}.json", doc("A Health", d, {"97110": {T["S"]: r, T["B"]: 45}}, npis))
            for d, r in (("2026-04-01", 40), ("2026-05-01", 40), ("2026-06-01", 30))]
    load(cfg, store, docs)
    label_all(store, T, npis)
    store.rebuild_rollups()
    pinned = series_by_code(store, [T["S"]], {"month": "2026-05", "state": "MO"})
    assert [m for m, _v in pinned["97110"]] == ["2026-04", "2026-05"]
    latest = series_by_code(store, [T["S"]], {"month": "latest", "state": "MO"})
    assert [m for m, _v in latest["97110"]][-1] == "2026-06"


def test_index_year_keeps_its_own_source(store):
    # P1-8: saving MEI 2025 relabelled the CPI-U 2023->2024 deflator
    from mrfx.inflation import deflator, load_index, save_index_values
    save_index_values(store, [(2025, 120.0)], index_name="MEI",
                      source="CMS Medicare Economic Index 2025")
    d = deflator(load_index(store), "2023-06", "2024-06")
    assert d["index_name"] == "CPI-U" and d["source"].startswith("BLS CPI-U")


def test_digest_follows_an_entity_map_edit(cfg, store):
    # P2-10: B joins ACME GROUP and prices 97140 -> no longer a contract gap
    from mrfx.clients import add_client, client_digest
    T = {k: f"6{i}3000001" for i, k in enumerate("ABCDEFG", start=1)}
    npis = {t: [f"16{i}3000001"] for i, t in enumerate(T.values(), start=1)}
    pr = {"97110": {T["A"]: 40, **{T[k]: 50 for k in "CDEFG"}},
          "97140": {T["B"]: 35, **{T[k]: 45 for k in "CDEFG"}}}
    load(cfg, store, [("r_2026-06.json", doc("Rho Health", "2026-06-01", pr, npis))])
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", "MO", tax=PT)
    store.rebuild_rollups()
    store.set_entity_map({T["A"]: "ACME GROUP"})
    add_client(store, "ACME GROUP")
    assert client_digest(store)["clients"][0]["gaps"]["n"] == 1
    store.set_entity_map({T["A"]: "ACME GROUP", T["B"]: "ACME GROUP"})
    assert client_digest(store)["clients"][0]["gaps"]["n"] == 0


def test_hospital_median_is_per_hospital(cfg, store, tmp_path):
    # P2-11: Hospital One lists 3 Aetna plans at $100, Two lists one at $200.
    # Per hospital: median(100, 200) = $150 -> 3.0x a $50 practice median.
    from mrfx.hospital import hospital_parity, import_hospital_file
    T, npis = practices(store, "ABC", "65")
    load(cfg, store, [("a_2026-06.json", doc("Aetna", "2026-06-01",
                                             {"97110": {t: 50 for t in T.values()}}, npis))])
    label_all(store, T, npis, tax=PT)
    store.rebuild_rollups()

    def hosp(path, hname, plans):
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["hospital_name", "last_updated_on", "version"])
            w.writerow([hname, "2026-07-01", "2.0.0"])
            w.writerow(["description", "code|1", "code|1|type", "setting", "modifiers",
                        "standard_charge|gross"]
                       + [f"standard_charge|Aetna|{p}|negotiated_dollar" for p, _ in plans])
            w.writerow(["Ther exercise", "97110", "CPT", "outpatient", "", "300"]
                       + [str(r) for _, r in plans])
    hosp(tmp_path / "h1.csv", "Hospital One", [("PPO", 100), ("HMO", 100), ("EPO", 100)])
    hosp(tmp_path / "h2.csv", "Hospital Two", [("PPO", 200)])
    import_hospital_file(store, tmp_path / "h1.csv", state="MO")
    import_hospital_file(store, tmp_path / "h2.csv", state="MO")
    r = hospital_parity(store, {"month": "latest"}, payer="Aetna", state="MO")["rows"][0]
    assert (r["hospital_median"], r["hospital_multiple"]) == (150.0, 3.0)


def test_market_report_states_as_of_basis_and_sources(cfg, store):
    # P2-12
    from mrfx.dossier import build_market_report
    T, npis = practices(store, "ABCDEFGH", "66")
    load(cfg, store, [("rho_2026-06.json", doc("Rho Health", "2026-06-01",
                                               {"97110": {t: 53.5 for t in T.values()}}, npis))])
    label_all(store, T, npis)
    store.rebuild_rollups()
    html = build_market_report(cfg, store, state="MO")["html"]
    text = re.sub(r"<[^>]+>", " ", html)
    assert "As of: latest available" in text and "newest month loaded 2026-06" in text
    assert "rho_2026-06.json" in text
