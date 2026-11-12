"""Round 7 (docs/NEXT_STEPS_PROMPT_7.md): each test pins one item, worked by
hand in its docstring."""

from __future__ import annotations

import json

import pytest

from mrfx.ingest import ingest_file
from mrfx.store import Store
from tests.mrfx.test_fastscan_e2e import _make_cfg
from tests.mrfx.test_known_answer_leads import leads_app  # noqa: F401 — fixture


# -- P0.1 non-finite string rates ------------------------------------------------

@pytest.mark.parametrize("fast", [False, True])
def test_a_string_nan_or_infinity_rate_never_reaches_a_median(tmp_path, fast):
    """One practice, 97110, four prices: 50.00, "nan", "Infinity", "1e999".
    float() accepts the three strings; a NaN row made the practice's median
    NaN (DuckDB orders NaN above every number, so `> 0.01` kept it) and
    Infinity made rate_max inf. By hand: the only rate is $50.00, and the
    three strings are counted as unparseable."""
    cfg = _make_cfg(tmp_path / str(fast), fast)
    store = Store(cfg.store_dir)
    prices = [{"negotiated_type": "negotiated", "negotiated_rate": r,
               "billing_class": "professional", "service_code": ["11"],
               "billing_code_modifier": ["GP"]} for r in (50.0, "nan", "Infinity", "1e999")]
    doc = {"reporting_entity_name": "Testco Health", "version": "1.0.0",
           "last_updated_on": "2026-06-01",
           "in_network": [{"billing_code_type": "CPT", "billing_code": "97110",
                           "negotiated_rates": [{"provider_groups": [
                               {"npi": [1234567890], "tin": {"type": "ein", "value": "111111111"}}],
                               "negotiated_prices": prices}]}]}
    p = cfg.inbox_dir / "strings_2026-06.json"
    p.write_text(json.dumps(doc))
    r = ingest_file(cfg, store, p)
    assert r["status"] == "done", r
    with store.connect() as con:
        raw = con.execute("SELECT negotiated_rate FROM rates").fetchall()
        assert raw == [(50.0,)]
        qa = json.loads(con.execute("SELECT qa FROM files").fetchone()[0])
        spine = con.execute("SELECT negotiated_rate, rate_min, rate_max FROM rates_by_tin").fetchall()
    assert qa["unparseable_rates"] == 3
    assert spine == [(50.0, 50.0, 50.0)]


def test_the_rollup_ignores_a_non_finite_rate_already_in_a_part(store):
    """A part written before the fix can still hold NaN: the spine skips it."""
    from tests.mrfx.test_territory import _row
    with store.rates_part_writer("old.json") as w:
        w.write_batch([_row("431000001", "1417594896", 50.0),
                       _row("431000001", "1417594896", float("nan"))])
    store.rebuild_rollups()
    with store.connect() as con:
        assert con.execute("SELECT negotiated_rate, rate_max FROM rates_by_tin").fetchall() == [(50.0, 50.0)]


def test_clean_rate_refuses_glued_or_boolean_values():
    from mrfx.parser import clean_rate
    assert clean_rate("$34.50") == 34.5 and clean_rate("1,234.50") == 1234.5
    assert clean_rate("34.50 USD") == 34.5
    for bad in ("2 x 34.50", "Rate: 34.50 (2 units)", True, "nan", float("inf")):
        assert clean_rate(bad) is None, bad


# -- P0.2 the negotiation one-pager must not add payers' practice-wide values ----

def test_the_negotiation_one_pager_does_not_count_a_unit_once_per_payer(cfg, store):
    """The practice bills 1,000 units of 97110 a year. Payers P and Q each pay
    it $40 while their four other practices get 45, 48, 52, 55 (median $50).
    Each payer is worth (50 - 40) x 1,000 = $10,000 IF every unit were billed
    to it; no payer mix makes the two together worth more than $10,000. The
    one-pager used to print their sum, $20,000/yr."""
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from mrfx.benchmark import compute_payer_negotiation
    from tests.mrfx.conftest import make_fixture
    from tests.mrfx.test_v4 import innetwork, item
    peers = [([f"1{i:09d}"], f"43-00000{i:02d}", "ein", [(r, None)])
             for i, r in enumerate((45.0, 48.0, 52.0, 55.0), start=1)]
    me = [(["1000000000"], "43-0000000", "ein", [(40.0, None)])]
    for name, payer in (("p.json", "P Health"), ("q.json", "Q Health")):
        ingest_file(cfg, store, make_fixture(cfg.inbox_dir, name,
                                             innetwork(payer=payer, items=[item("97110", me + peers)])))
    m = {"month": "2026-06", "allow_national": True}
    neg = compute_payer_negotiation(store, "430000000", m, volumes={"97110": 1000})
    assert [s["opportunity"]["total_at_target"] for s in neg["sections"]] == [10000.0, 10000.0]
    assert neg["total_at_target"] is None and neg["total_at_conservative"] is None
    assert neg["best_single_payer"]["at_target"] == 10000.0
    html = TestClient(create_app(cfg, store)).post("/api/report/negotiation", json={
        "subject": "430000000", "market": m, "volumes": {"97110": 1000}}).text
    assert "$20,000" not in html
    assert "largest single-payer opportunity" in html and "alternatives, not a sum" in html
    # one payer only: the total is that payer's, and the report says so plainly
    one = compute_payer_negotiation(store, "430000000", {**m, "payers": ["P Health"]},
                                    volumes={"97110": 1000})
    assert one["total_at_target"] == 10000.0


# -- P0.3 trend lines follow a fixed panel of contracts ---------------------------

def test_a_payer_entering_the_month_never_moves_the_trend(cfg, store):
    """Payer A pays the practice $40 for 97110 in May and in June; payer B
    first publishes in June, at $50. No rate moved, so the line is flat at
    $40 over May-June (panel = A, the only contract in both months). The old
    series took the median across ALL payers per month: $40 -> $45, a green
    "+12.5%" and "First -> latest $40.00 -> $45.00" in the packet. When B
    then moves $50 -> $55 in July, the panel for June-July is {A, B} and the
    line reads median(40, 50) = 45 -> median(40, 55) = 47.5."""
    from mrfx.spark import series_by_code
    from tests.mrfx.test_territory import _row
    rows = [_row("431234567", "1417594896", 40.0, payer="A Health") | {"file_month": "2026-05"},
            _row("431234567", "1417594896", 40.0, payer="A Health") | {"file_month": "2026-06"},
            _row("431234567", "1417594896", 50.0, payer="B Health") | {"file_month": "2026-06"}]
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    store.rebuild_rollups()
    s = series_by_code(store, ["431234567"], {"therapy_only": False})
    assert s["97110"] == [("2026-05", 40.0), ("2026-06", 40.0)]
    with store.rates_part_writer("b.json") as w:
        w.write_batch([_row("431234567", "1417594896", 40.0, payer="A Health") | {"file_month": "2026-07"},
                       _row("431234567", "1417594896", 55.0, payer="B Health") | {"file_month": "2026-07"}])
    store.rebuild_rollups()
    s = series_by_code(store, ["431234567"], {"therapy_only": False})
    assert s["97110"] == [("2026-06", 45.0), ("2026-07", 47.5)]


# -- P0.4 positions are mid-rank: a flat fee schedule is not a list of leads ------

def _flat_market(cfg, store, a_rates):
    """One payer, five MO practices A..E. B..E are paid 40/35/50 on 97110,
    97140, 97530; A is paid `a_rates` on the same three codes."""
    from tests.mrfx.conftest import make_fixture
    from tests.mrfx.test_v4 import innetwork, item
    groups = {code: [([f"15{i}0000001"], f"5{i}-0000001", "ein", [(r, ["GP"])])
                     for i in range(2, 6)] for code, r in (("97110", 40.0), ("97140", 35.0),
                                                           ("97530", 50.0))}
    items = []
    for code, ar in zip(("97110", "97140", "97530"), a_rates):
        items.append(item(code, [(["1510000001"], "51-0000001", "ein", [(ar, ["GP"])])]
                          + groups[code]))
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "flat.json", innetwork(items=items)))
    for i in range(1, 6):
        store.save_npi(f"15{i}0000001", f"{'ABCDE'[i - 1]} REHAB", "261QP2000X", "x", "Town", "MO",
                       entity_type="NPI-2", zip_code="64105")
    store.rebuild_rollups()


def test_a_flat_fee_schedule_puts_everyone_at_p50_and_yields_no_leads(cfg, store):
    """Every practice paid exactly 40/35/50. By hand, each practice's mid-rank
    among the four others is (0 below + 0.5 x 4 tied) / 4 = 50 on every code.
    percent_rank scored ties 0, so all five were listed as "underpaid p0
    leads" with a $0.00 gap while the Benchmark said p50."""
    from mrfx.leads import compute_leaderboard, compute_leads, compute_payer_roster
    _flat_market(cfg, store, (40.0, 35.0, 50.0))
    m = {"month": "2026-06", "state": "MO", "therapy_only": False}
    assert compute_leads(store, m, threshold_percentile=25, min_codes=1)["leads"] == []
    lb = compute_leaderboard(store, m, min_codes=1)
    assert {p["median_percentile"] for p in lb["practices"]} == {50.0}
    ro = compute_payer_roster(store, "Testco", m, min_codes=1)
    assert {p["median_percentile"] for p in ro["organizations"]} == {50.0}


def test_a_practice_underpaid_where_rates_differ_is_still_a_lead(cfg, store):
    """A is paid 30 and 25 where the others get 40 and 35, and the same 50 on
    97530. By hand: 97110 and 97140 -> 0 below, 0 ties among the 4 others =
    p0; 97530 is a code every practice is paid the same, which says nothing
    about underpayment, so the sweep leaves it out -> A's position is
    median(0, 0) = p0 over 2 codes; B..E sit at median(62.5, 62.5) = p62.5
    -> rounded p63 (1 below + 0.5 x 3 ties = 2.5 / 4)."""
    from mrfx.leads import compute_leads
    _flat_market(cfg, store, (30.0, 25.0, 50.0))
    m = {"month": "2026-06", "state": "MO", "therapy_only": False}
    leads = compute_leads(store, m, threshold_percentile=25, min_codes=2)["leads"]
    assert [(x["display_name"], x["median_percentile"], x["n_codes"]) for x in leads] == [
        ("A REHAB", 0.0, 2)]
    assert leads[0]["avg_gap_to_median"] == 10.0   # median(30,40,40,40,40)=40 -> 10; 35-25=10


# -- P0.5 an index year entered under another name never joins the CPI-U series --

def test_a_mei_year_is_never_divided_by_a_cpi_u_year(store):
    """The user enters 2025 = 160 as "MEI" (the docstring invites it). The
    shipped series is CPI-U (2023 = 304.702). Dividing them gave a deflator of
    0.525 and printed a flat payer as "+90.4% in real terms (MEI)". By hand,
    the two numbers are on different bases: there is no factor to compute."""
    from mrfx.inflation import deflator, load_index, real_change_pct, save_index_values
    save_index_values(store, [(2025, 160.0)], index_name="MEI")
    idx = load_index(store)
    assert idx["names"][2023] == "CPI-U" and idx["names"][2025] == "MEI"
    assert idx["index_name"] != "MEI", "one row must not relabel the whole series"
    d = deflator(idx, "2023-06", "2025-06")
    assert d["factor"] is None and "two different price indexes" in d["reason"]
    assert real_change_pct(0.0, d) is None
    # within one basis it still works: CPI-U 2022 -> 2023 (292.655 -> 304.702)
    ok = deflator(idx, "2022-06", "2023-06")
    assert ok["basis"] == "index" and abs(ok["factor"] - 304.702 / 292.655) < 1e-9


def test_a_non_finite_index_value_is_refused(store):
    from mrfx.inflation import InflationError, save_index_values
    for bad in (float("inf"), float("nan")):
        with pytest.raises(InflationError):
            save_index_values(store, [(2025, bad)])


# -- P0.6 referral leaders: an individual is an NPI, an organization a name in a state

def test_two_therapists_with_one_name_are_two_practices(store, tmp_path):
    """Two individual PTs both named JOHN SMITH — A in St Louis MO 63101, B in
    Wichita KS 67202 — receive 100 and 50 shared patients. Grouped by name they
    were ONE row "JOHN SMITH, KS, 63101, 150 patients" (state and ZIP from
    different people). By hand: two rows, 100 (MO 63101) and 50 (KS 67202).
    An ORGANIZATION with two org NPIs in one state still counts once:
    "Gateway Therapy" (MO) 30 + 20 = 50."""
    from mrfx.medicare import import_shared_patients, referral_leaders
    from tests.mrfx.test_territory import _row
    a, b, g1, g2 = "1111111111", "1222222222", "1333333333", "1444444444"
    d1, d2, d3 = "1901234561", "1811223340", "1722334459"
    with store.rates_part_writer("r.json") as w:
        w.write_batch([_row(f"43100000{i}", n, 50.0) for i, n in enumerate((a, b, g1, g2))])
    store.save_npis_bulk([
        dict(npi=a, org_name="JOHN SMITH", entity_type="NPI-1", taxonomy_code="225100000X",
             taxonomy_codes="225100000X", city="ST LOUIS", state="MO", address="x",
             zip="631011234", phone=None),
        dict(npi=b, org_name="JOHN SMITH", entity_type="NPI-1", taxonomy_code="225100000X",
             taxonomy_codes="225100000X", city="WICHITA", state="KS", address="x",
             zip="67202", phone=None),
        *[dict(npi=n, org_name="Gateway Therapy", entity_type="NPI-2",
               taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X", city="ST LOUIS",
               state="MO", address="x", zip="63103", phone=None) for n in (g1, g2)]])
    store.rebuild_rollups()
    hop = tmp_path / "hop_2022.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,average_day_wait,std_day_wait\n"
                   f"{d1},{a},100,400,12,3\n{d2},{b},50,200,12,3\n"
                   f"{d3},{g1},30,90,12,3\n{d3},{g2},20,60,12,3\n")
    import_shared_patients(store, hop)
    rows = referral_leaders(store, therapy_only=True)["rows"]
    got = sorted((r["practice"], r["state"], r["zip"], r["patients"], r["npis"]) for r in rows)
    assert got == [("Gateway Therapy", "MO", "63103", 50, 2),
                   ("JOHN SMITH", "KS", "67202", 50, 1),
                   ("JOHN SMITH", "MO", "63101", 100, 1)]


# -- P0.7 the ACS 65+ count: every person once, whatever table was downloaded --

_BUCKETS = (("65 to 69 years", 300), ("70 to 74 years", 250), ("75 to 79 years", 200),
            ("80 to 84 years", 150), ("85 years and over", 100))   # = 1,000


def _acs(cols: list[tuple[str, str, float]]) -> str:
    """Two-row data.census.gov layout: variable ids, then labels, then one ZCTA."""
    ids = ["GEO_ID", "NAME", "B01003_001E"] + [i for i, _, _ in cols]
    labels = ["Geography", "Geographic Area Name", "Estimate!!Total"] + [l for _, l, _ in cols]
    vals = ["860Z200US63103", "ZCTA5 63103", "5000"] + [str(v) for _, _, v in cols]
    return "\n".join(",".join(f'"{x}"' for x in r) for r in (ids, labels, vals)) + "\n"


def test_s0101_counts_seniors_once():
    """S0101 carries the five buckets for Total, Male and Female, Percent
    copies, and the "65 years and over" / "75 years and over" summaries. By
    hand the 65+ population is 1,000 — the old importer summed 3,460."""
    from mrfx.demographics import parse_acs_csv
    cols = []
    for k, (lab, v) in enumerate(_BUCKETS):
        cols.append((f"S0101_C01_{k + 1:03d}E", f"Estimate!!Total!!Total population!!AGE!!{lab}", v))
        cols.append((f"S0101_C03_{k + 1:03d}E", f"Estimate!!Male!!Total population!!AGE!!{lab}", v * 0.45))
        cols.append((f"S0101_C05_{k + 1:03d}E", f"Estimate!!Female!!Total population!!AGE!!{lab}", v * 0.55))
        cols.append((f"S0101_C02_{k + 1:03d}E", f"Estimate!!Percent!!Total population!!AGE!!{lab}", 6.0))
    cols += [("S0101_C01_030E", "Estimate!!Total!!Total population!!SELECTED AGE CATEGORIES!!65 years and over", 1000),
             ("S0101_C01_031E", "Estimate!!Total!!Total population!!SELECTED AGE CATEGORIES!!75 years and over", 450),
             ("S0101_C03_030E", "Estimate!!Male!!Total population!!SELECTED AGE CATEGORIES!!65 years and over", 450)]
    rows, _ = parse_acs_csv(_acs(cols))
    assert rows[0]["pop_65_plus"] == 1000
    # the same table with the id row stripped: the label total still wins
    text = _acs(cols).split("\n", 1)[1]
    rows, _ = parse_acs_csv(text)
    assert rows[0]["pop_65_plus"] == 1000


def test_dp05_counts_seniors_once():
    """DP05: 65-74 (550), 75-84 (350), 85+ (100), the 65+ total (1,000) and its
    Male/Female split, plus Percent columns. By hand 1,000 (old: 3,010)."""
    from mrfx.demographics import parse_acs_csv
    cols = [("DP05_0015E", "Estimate!!SEX AND AGE!!Total population!!65 to 74 years", 550),
            ("DP05_0016E", "Estimate!!SEX AND AGE!!Total population!!75 to 84 years", 350),
            ("DP05_0017E", "Estimate!!SEX AND AGE!!Total population!!85 years and over", 100),
            ("DP05_0015PE", "Percent!!SEX AND AGE!!Total population!!65 to 74 years", 11.0),
            ("DP05_0024E", "Estimate!!SEX AND AGE!!Total population!!65 years and over", 1000),
            ("DP05_0025E", "Estimate!!SEX AND AGE!!Total population!!65 years and over!!Male", 450),
            ("DP05_0026E", "Estimate!!SEX AND AGE!!Total population!!65 years and over!!Female", 550)]
    rows, _ = parse_acs_csv(_acs(cols))
    assert rows[0]["pop_65_plus"] == 1000


def test_b01001_sex_by_age_still_sums_both_sexes():
    """B01001 has no sex-total buckets: male + female buckets ARE the count."""
    from mrfx.demographics import parse_acs_csv
    cols = []
    for k, (lab, v) in enumerate(_BUCKETS):
        cols.append((f"B01001_{20 + k:03d}E", f"Estimate!!Total:!!Male:!!{lab}", v * 0.45))
        cols.append((f"B01001_{44 + k:03d}E", f"Estimate!!Total:!!Female:!!{lab}", v * 0.55))
    rows, _ = parse_acs_csv(_acs(cols))
    assert rows[0]["pop_65_plus"] == 1000
    text = _acs(cols).split("\n", 1)[1]          # labels only
    rows, _ = parse_acs_csv(text)
    assert rows[0]["pop_65_plus"] == 1000


# -- P0.8 one volumes reader for every box ------------------------------------------

def _js_function(name: str) -> str:
    """The source of one top-level function in app.js (brace-matched)."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "mrfx" / "web" / "app.js").read_text()
    i = src.index(f"function {name}(")
    if src[max(0, i - 6):i] == "async ":        # keep the keyword: `await` needs it
        i -= 6
    j = src.index("{", src.index(")", i))
    depth = 0
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]
    raise AssertionError(name)


def _browser_page():
    sp = pytest.importorskip("playwright.sync_api")
    pw = sp.sync_playwright().start()
    for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
        try:
            b = pw.chromium.launch(args=["--headless=new", "--no-sandbox"], **kw)
            return pw, b, b.new_page()
        except Exception:  # noqa: BLE001 — no browser on this machine
            continue
    pw.stop()
    pytest.skip("no Chromium available")


def test_every_volumes_box_reads_thousands_and_spaces_the_same():
    """Typed into the Benchmark box: "97110, 4,200" / "97140 1800" /
    "97112,500" / "97530: 1,000.5" / "oops". By hand: 4200, 1800, 500, 1000.5
    and one line not read. The Benchmark box's own reader made 4,200 into 4
    units and dropped the space-separated line; all three boxes now share
    one reader and list what they could not read."""
    import json as _json
    pw, browser, page = _browser_page()
    try:
        page.set_content('<textarea id="v"></textarea><span id="m"></span>')
        page.add_script_tag(content="const $ = (s) => document.querySelector(s);\n"
                                    + _js_function("parseVolumes"))
        text = "97110, 4,200\n97140 1800\n97112,500\n97530: 1,000.5\noops\n"
        page.evaluate("t => { document.querySelector('#v').value = t; }", text)
        got = page.evaluate("JSON.stringify(parseVolumes('#v', '#m'))")
        msg = page.inner_text("#m")
    finally:
        browser.close()
        pw.stop()
    assert _json.loads(got) == {"97110": 4200, "97140": 1800, "97112": 500, "97530": 1000.5}
    assert "oops" in msg
    from pathlib import Path
    app = (Path(__file__).resolve().parents[2] / "mrfx" / "web" / "app.js").read_text()
    assert 'parseVolumes("#b-volumes"' in app and 'parseVolumes("#ng-volumes"' in app
    assert 'parseVolumes("#en-volumes"' in app
    assert 'split(/[,\\t]/)' not in app, "the Benchmark box's own reader is gone"


# -- P1.16 the dashboard only answers its own page on this machine --------------

def test_a_foreign_web_page_cannot_change_or_read_the_store(cfg, store):
    """Reproductions from the audit: a cross-site multipart upload landed in
    the inbox and was ingested; a cross-site POST /api/mpfs/upload replaced
    the Medicare anchor; a body-less POST /api/urls/known queued all 109
    catalog sources; a DNS-rebinding Host read /api/stats. All four are
    refused now; the dashboard's own same-origin requests still work."""
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    c = TestClient(create_app(cfg, store))
    evil = {"Origin": "https://evil.example"}
    up = c.post("/api/upload", headers=evil,
                files={"file": ("x_in-network.json", b'{"in_network": []}', "application/json")})
    assert up.status_code == 403 and not list(cfg.inbox_dir.iterdir())
    assert c.post("/api/mpfs/upload", headers=evil,
                  files={"file": ("m.csv", b"code,locality,non_facility_rate,state\n97110,00,40,MO\n",
                                  "text/csv")}).status_code == 403
    assert c.post("/api/urls/known", headers=evil).status_code == 403
    assert c.post("/api/urls/known", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.get("/api/stats", headers={"Host": "attacker.example.com"}).status_code == 403
    # same origin (the dashboard's own fetch) and no origin (CLI, scripts) pass
    assert c.get("/api/stats").status_code == 200
    ok = c.post("/api/urls", json={"urls": []}, headers={"Origin": "http://testserver"})
    assert ok.status_code != 403
    assert c.get("/api/stats", headers={"Host": "localhost:8377"}).status_code == 200
    assert c.get("/api/stats", headers={"Host": "127.0.0.1:8377"}).status_code == 200


# -- P1.18 the supervisor never restarts what a restart cannot fix ---------------

def test_supervisor_stops_on_port_in_use_and_restarts_only_real_crashes(monkeypatch, tmp_path):
    """Double-clicking Start while the dashboard was up looped "port in use …
    restarting in 5 s, 10 s, 20 s …" for ever; a config typo did the same.
    `serve` now exits EXIT_NO_RESTART for both and the supervisor stops. A
    real crash (exit 1) is still restarted, then a clean exit (0) ends it."""
    import mrfx.cli as cli
    codes = iter([1, 3])
    sleeps = []
    monkeypatch.setattr(cli, "_run_child_teed", lambda child, log: next(codes))
    monkeypatch.setattr(cli, "_serve_log_path", lambda p: None)
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    assert cli._supervise_serve("cfg.yaml") == cli.EXIT_NO_RESTART
    assert sleeps == [5], "one restart for the crash, none for the port"
    codes = iter([1, 0])
    assert cli._supervise_serve("cfg.yaml") == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text("port: eighty\n")
    assert cli.main(["--config", str(bad), "serve"]) == cli.EXIT_NO_RESTART
    assert cli.main(["--config", str(bad), "status"]) == 1


# -- P1.19 the updater: pip first, no silent downgrade, no half-applied tree -----

def test_updater_refuses_a_downgrade_and_rolls_back_a_failed_write(tmp_path, monkeypatch):
    """(1) A 0.1.0 zip over 0.2.0 used to apply silently; it is refused unless
    allow_downgrade. (2) A file that cannot be written (antivirus lock) used
    to escape as a traceback with config/ and docs/ already replaced and mrfx/
    not: every replaced file is now restored and created files removed. (3)
    pip runs on the NEW requirements before any file is written; a failed
    pip changes nothing. (4) Data/ in another letter case is still data/."""
    import subprocess as _sp

    from mrfx.updater import UpdateError, apply_update
    from tests.mrfx.test_updater import NEW, _build_zip, _install
    root = tmp_path / "install"
    before = _install(root)
    old = _build_zip(tmp_path / "old.zip", {**NEW, "mrfx/__init__.py": '__version__ = "0.1.0"\n'})
    with pytest.raises(UpdateError, match="OLDER version"):
        apply_update(old, root, run_pip=False, log=lambda *a: None)
    assert (root / "mrfx/old.py").read_text() == before["mrfx/old.py"]

    # (2) the write of mrfx/old.py fails after config/known_sources.yaml went in
    z = _build_zip(tmp_path / "new.zip", {**NEW, "Data/inbox/evil.json": "x"})
    real = type(root).replace

    def flaky(self, target):
        if str(target).endswith("mrfx/old.py"):
            raise PermissionError("locked by antivirus")
        return real(self, target)
    monkeypatch.setattr(type(root), "replace", flaky)
    with pytest.raises(UpdateError, match="put back"):
        apply_update(z, root, run_pip=False, log=lambda *a: None)
    monkeypatch.undo()
    for rel, text in before.items():
        assert (root / rel).read_text() == text, rel
    assert not (root / "mrfx/brand_new.py").exists()
    assert not (root / "Data").exists(), "Data/ is data/ on NTFS: never written"

    # (3) pip on the new requirements first; when it fails nothing is written
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _sp.CompletedProcess(cmd, 1)
    monkeypatch.setattr("mrfx.updater.subprocess.run", fake_run)
    with pytest.raises(UpdateError, match="NOT changed"):
        apply_update(z, root, run_pip=True, log=lambda *a: None)
    assert calls and calls[0][2:5] == ["pip", "install", "-q"]
    assert (root / "mrfx/old.py").read_text() == before["mrfx/old.py"]


# -- P1.1 exports are built on the store's drive and refused before it fills ----

def test_exports_live_on_the_store_drive_and_a_full_drive_is_a_sentence(cfg, store, monkeypatch):
    """The whole-store CSV was COPY'd to the SYSTEM temp folder, then copied a
    second time just to put the Excel BOM in front: ~40 GB of C: per click on
    the 99M store, and an unhandled 500 ("No space left on device") when it
    filled. Now: one file, in the store's temp folder, streamed with the BOM;
    refused up front with a plain sentence when the drive cannot hold it."""
    import shutil
    import tempfile
    import zipfile
    from collections import namedtuple
    from pathlib import Path

    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from tests.mrfx.conftest import make_fixture
    from tests.mrfx.test_v4 import innetwork, item
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "e.json", innetwork(items=[
        item("97110", [(["1000000000"], "43-0000000", "ein", [(40.0, None)]),
                       (["1000000001"], "43-0000001", "ein", [(45.0, None)])])])))
    c = TestClient(create_app(cfg, store))
    exports = Path(store._tmp_dir) / "exports"
    made = []
    real = tempfile.mkstemp

    def spy(*a, **k):
        made.append(k.get("dir"))
        return real(*a, **k)
    monkeypatch.setattr(tempfile, "mkstemp", spy)
    r = c.get("/api/export.csv?full=true")
    assert r.status_code == 200 and r.content.startswith(b"\xef\xbb\xbf")
    assert r.content.count(b"\xef\xbb\xbf") == 1
    lines = r.content.decode("utf-8-sig").splitlines()
    assert len(lines) == 3 and "43" not in lines[0]        # header + the two practices
    z = c.get("/api/export.zip?full=true")
    assert z.status_code == 200
    with zipfile.ZipFile(__import__("io").BytesIO(z.content)) as zf:
        names = zf.namelist()
        member = zf.read([n for n in names if n.endswith(".csv")][0])
    assert member.startswith(b"\xef\xbb\xbf") and member.decode("utf-8-sig").splitlines() == lines
    assert any(n.endswith("_methodology.txt") for n in names)
    assert made and {Path(d) for d in made} == {exports}, "never the system temp folder"
    assert not list(exports.iterdir()), "every temp file is removed after the download"

    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10**12, 10**12 - 10**6, 10**6))
    for url in ("/api/export.csv?full=true", "/api/export.zip?full=true"):
        r = c.get(url)
        assert r.status_code == 507, (url, r.status_code)
        assert "free" in r.json()["detail"] and "Narrow the export" in r.json()["detail"]
    assert not list(exports.iterdir()), "a refused export leaves nothing behind"
    # a killed server's half-written export — DuckDB's own "tmp_" partial
    # included — is swept at the next start once it is clearly abandoned
    import os
    import time
    stale = [exports / "mrfx-export-old.csv", exports / "tmp_mrfx-export-old.csv"]
    for f in stale:
        f.write_bytes(b"x")
        os.utime(f, (time.time() - 7 * 3600, time.time() - 7 * 3600))
    fresh = exports / "mrfx-export-live.csv"
    fresh.write_bytes(b"x")
    create_app(cfg, store)
    assert not any(f.exists() for f in stale) and fresh.exists()


# -- P1.2 the materialised medians give the live query's numbers, to the bit ------

def test_every_benchmark_reader_is_identical_from_the_materialised_medians(leads_app, store,  # noqa: F811
                                                                           monkeypatch):
    """The known-answer Leads store (A..G, Rho + Sigma, MO/KS, a non-therapy
    practice, A on two payers for 97110 so its all-payer median 32 is NOT a
    median of its payer medians). Every reader is run on the house basis
    twice — from the materialised per-practice medians, and with them
    switched off — under several markets; the answers must be equal."""
    import mrfx.benchmark as bm
    from mrfx.benchmark import (compute_benchmark, compute_payer_comparison,
                                compute_payer_negotiation, contract_gaps_book)
    from mrfx.leads import compute_leaderboard, compute_leads, compute_payer_roster
    from mrfx.quality import size_premium
    from mrfx.schedule import compute_fee_schedule

    markets = [{"month": "latest", "state": "MO", "therapy_only": True},
               {"month": "latest", "therapy_only": False, "allow_national": True},
               {"month": "latest", "payers": ["Rho Health"], "allow_national": True},
               {"month": "latest", "codes": ["97110", "97140"], "state": "MO"},
               {"month": "latest", "city": "Town", "therapy_only": True},
               # review 12: a discipline on the house basis reads the
               # per-discipline table (DISC_PAYER_PRACTICE_MEDIANS)
               {"month": "latest", "state": "MO", "discipline": "PT"}]

    def run_all():
        out = []
        for m in markets:
            out.append(compute_benchmark(store, "A REHAB", m))
            out.append(compute_payer_negotiation(store, "A REHAB", m,
                                                 volumes={"97110": 100, "97140": 50}))
            out.append(compute_payer_comparison(store, "A REHAB", "Rho Health", m))
            out.append({k: (v if isinstance(v, dict) else str(v)) for k, v in
                        contract_gaps_book(store, ["B REHAB", "F REHAB"], m,
                                           min_peers=1).items()})
            out.append(compute_leads.uncached(store, m, threshold_percentile=60, min_codes=1))
            out.append(compute_leaderboard.uncached(store, m, min_codes=1))
            out.append(compute_payer_roster.uncached(store, "Rho Health", m, min_codes=1))
            out.append(size_premium.uncached(store, m, min_practices=1))
            out.append(compute_fee_schedule(store, "A REHAB", m))
        return json.loads(json.dumps(out, sort_keys=True, default=str))

    built = []
    real = store.spine_materialised
    monkeypatch.setattr(store, "spine_materialised",
                        lambda name, *a: built.append(name) or real(name, *a))
    from_tables = run_all()
    # review 12 added the per-discipline table (the discipline market above)
    assert set(built) == {bm.PRACTICE_MEDIANS, bm.PAYER_PRACTICE_MEDIANS,
                          bm.DISC_PAYER_PRACTICE_MEDIANS}
    monkeypatch.setattr(bm, "_house_eligible", lambda m: False)
    live = run_all()
    assert from_tables == live
    # and the hand answer survives: A's 97110 all-payer median is 32
    monkeypatch.undo()
    b = compute_benchmark(store, "A REHAB", {"month": "latest", "state": "MO",
                                             "therapy_only": True})
    assert {r["billing_code"]: r["subject_rate"] for r in b["rows"]}["97110"] == 32.0


def test_the_materialised_medians_follow_the_spine_not_a_name_refresh(leads_app, store):  # noqa: F811
    """Built once per spine generation: a second read reuses the file; a
    rollup makes a new one (the old one is removed); a user edit or a
    directory name refresh does not rebuild (no rate changed)."""
    from mrfx.benchmark import PRACTICE_MEDIANS, compute_benchmark
    m = {"month": "latest", "state": "MO", "therapy_only": True}
    compute_benchmark(store, "A REHAB", m)
    first = sorted(store.dir.glob(f"{PRACTICE_MEDIANS}.*.parquet"))
    assert len(first) == 1
    compute_benchmark(store, "B REHAB", m)
    store.note_user_edit()
    store._bump_generation(spine=False)
    compute_benchmark(store, "C REHAB", m)
    assert sorted(store.dir.glob(f"{PRACTICE_MEDIANS}.*.parquet")) == first
    store.rebuild_rollups()
    compute_benchmark(store, "A REHAB", m)
    now = sorted(store.dir.glob(f"{PRACTICE_MEDIANS}.*.parquet"))
    assert len(now) == 1 and now != first


# -- P1.3 a saved website reaches the cached Leads list at once -----------------

def test_a_saved_website_shows_in_the_cached_leads_at_once(leads_app, store):  # noqa: F811
    """Leads, the leaderboard and the roster are cached per generation and
    print store.org_websites(); saving https://a-rehab.example for A REHAB
    still showed website: null until the next rollup."""
    from tests.mrfx.test_known_answer_leads import TINS, _leads
    assert _leads(leads_app)["leads"][0]["website"] is None
    r = leads_app.post("/api/org-website", json={"tins": [TINS["A"]],
                                                 "url": "https://a-rehab.example"})
    assert r.status_code == 200, r.text
    assert _leads(leads_app)["leads"][0]["website"] == "https://a-rehab.example"
    lb = leads_app.post("/api/leaderboard", json={"market": {"month": "latest", "state": "MO",
                                                             "therapy_only": True}})
    assert lb.status_code == 200, lb.text
    assert {p["display_name"]: p.get("website") for p in lb.json()["practices"]}[
        "A REHAB"] == "https://a-rehab.example"
    gen = store.user_edit_generation
    store.set_entity_map({TINS["B"]: "B GROUP"})
    assert store.user_edit_generation == gen + 1, "the entity map is a user edit too"



# -- P1.5 a worker thread that dies hands its row back at once -----------------

def test_a_dead_downloader_thread_hands_its_row_back(cfg, store, monkeypatch):
    """The first fetch dies with an error the loop re-raises. The row used to
    stay 'downloading' with no owner until the next start, and a CLI drain
    counted it in flight for ever. Now the replacement thread's start puts it
    back in the queue; it is fetched again (port 9: refused -> failed) and the
    drain ends."""
    import threading

    import mrfx.fetch as F
    real = F.fetch_url_record
    calls = {"n": 0}

    def boom(c, s, rec, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt          # anything the loop re-raises
        return real(c, s, rec, **kw)
    monkeypatch.setattr(F, "fetch_url_record", boom)
    cfg.download_retries = 0
    F.add_urls(store, ["http://127.0.0.1:9/never.json.gz"])
    done: dict = {}
    t = threading.Thread(target=lambda: done.setdefault("n", F.run_queue(cfg, store, drain=True)),
                         daemon=True)
    t.start()
    t.join(60)
    assert not t.is_alive(), "the drain ends"
    row = store.list_urls()[0]
    assert calls["n"] >= 2 and row["status"] in ("failed", "paused"), row


# -- P1.6 an index cut at max_toc_files says so -----------------------------------

def test_a_capped_index_says_so_and_counts_what_it_listed(cfg, store, tmp_path):
    """A TOC listing 5 files with max_toc_files = 3: the row said "done",
    error None, and child_count = newly queued (0 after a monthly re-fetch).
    Now child_count = 3 listed, and the amber note names the cap."""
    from mrfx.fetch import cap_note, extract_links_from_text
    trunc: dict = {}
    links = extract_links_from_text(
        " ".join(f'<a href="https://x.example/f{i}_in-network.json.gz">f</a>' for i in range(5)),
        "https://x.example/", 3, trunc=trunc)
    assert len(links) == 3 and trunc == {"truncated": True}
    trunc = {}
    extract_links_from_text('<a href="https://x.example/a_in-network.json">a</a>',
                            "https://x.example/", 3, trunc=trunc)
    assert trunc == {}
    assert "MORE than 2,000" in cap_note(2000) and "max_toc_files" in cap_note(2000)


# -- P1.7 a partly resolved rate group is counted and re-ingested ----------------

def test_a_partly_resolved_reference_group_is_counted_and_refilled(cfg, store):
    """A rate group cites provider references [101, 555]; the file defines
    only 101. It used to emit 101's practice, drop 555's, report
    ref_groups_skipped = 0 and never re-ingest when the companion landed.
    By hand: one missing id; after the companion, 555's two NPIs at $27.50."""
    from pathlib import Path

    from tests.mrfx.conftest import make_fixture
    data = json.loads((Path(__file__).parent / "fixtures" / "innetwork_mixed.json").read_text())
    for it in data["in_network"]:
        for nr in it["negotiated_rates"]:
            if nr.get("provider_references") == [555]:
                nr["provider_references"] = [101, 555]
    p = make_fixture(cfg.inbox_dir, "partial_refs.json", data)
    assert ingest_file(cfg, store, p)["status"] == "done"
    with store.connect() as con:
        qa = json.loads(con.execute("SELECT qa FROM files WHERE filename = ?", [p.name]).fetchone()[0])
        npis = {r[0] for r in con.execute("SELECT DISTINCT npi FROM rates").fetchall()}
    assert qa["missing_ref_ids"] == 1 and store.file_status(p.name)["ref_groups_skipped"] == 0
    assert "5555555555" not in npis
    ref = make_fixture(cfg.inbox_dir, "provider_reference_companion.json", json.loads(
        (Path(__file__).parent / "fixtures" / "provider_reference_companion.json").read_text()))
    res = ingest_file(cfg, store, ref)
    assert p.name in res["requeued"]
    with store.connect() as con:
        got = con.execute("SELECT DISTINCT npi, negotiated_rate FROM rates "
                          "WHERE npi IN ('5555555555', '5555555556') ORDER BY 1").fetchall()
        qa = json.loads(con.execute("SELECT qa FROM files WHERE filename = ?", [p.name]).fetchone()[0])
    assert got == [("5555555555", 27.5), ("5555555556", 27.5)] and qa["missing_ref_ids"] == 0


# -- P1.4 a segmented download may take longer than the wall-clock cap -----------

def test_a_segmented_download_finishes_past_the_time_cap(cfg, monkeypatch):
    """Four segments, each served at ~30 KB/s (2.5 s per segment) with
    download_max_seconds = 1. The cap used to end every segment, delete the
    holey .part and fall back to a single connection judged against the
    first attempt's clock — "nothing received yet", parked, and from byte 0
    on every retry. Segments now run until done (bounded by NO progress)."""
    import hashlib
    import http.server as hs
    import os as _os
    import threading
    import time as _t

    import mrfx.fetch as F
    payload = _os.urandom(300_000)

    class Slow(hs.BaseHTTPRequestHandler):
        def do_GET(self):
            rng = self.headers.get("Range", "")
            a, b = rng[6:].split("-") if rng.startswith("bytes=") else ("0", "")
            start, end = int(a), min(int(b) if b else len(payload) - 1, len(payload) - 1)
            body = payload[start:end + 1]
            self.send_response(206 if rng else 200)
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if len(body) <= 1:                      # the range probe
                self.wfile.write(body)
                return
            for i in range(0, len(body), 7_500):
                self.wfile.write(body[i:i + 7_500])
                self.wfile.flush()
                _t.sleep(0.25)

        def log_message(self, *a):
            pass

    httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(F, "_SEGMENT_MIN_BYTES", 1000)
        monkeypatch.setattr(F, "_SEGMENT_MIN_PIECE", 50_000)
        cfg.download_segments = 4
        cfg.download_max_seconds = 1
        url = f"http://127.0.0.1:{httpd.server_address[1]}/slow.json.gz"
        dest = cfg.downloads_dir / F.filename_for(url)
        t0 = _t.monotonic()
        sha, _final = F.download(cfg, url, dest)
        assert _t.monotonic() - t0 > 1.5, "it really ran past the cap"
        assert sha == hashlib.sha256(payload).hexdigest() and dest.read_bytes() == payload
    finally:
        httpd.shutdown()


# -- P1.8 a failed commit never leaves a failed file's rates live ------------------

def test_a_failed_done_upsert_withdraws_the_part(cfg, store, monkeypatch):
    """The part goes live, then recording the file as done fails (twice). It
    used to leave files = failed while SELECT count(*) FROM rates = 1 and the
    rows reached every number after the rebuild. Now the part is withdrawn."""
    from tests.mrfx.conftest import make_fixture
    from tests.mrfx.test_v4 import innetwork, item
    real = store.upsert_file

    def flaky(name, **kw):
        if kw.get("status") == "done":
            raise RuntimeError("disk hiccup while recording the file")
        return real(name, **kw)
    monkeypatch.setattr(store, "upsert_file", flaky)
    p = make_fixture(cfg.inbox_dir, "f.json", innetwork(items=[
        item("97110", [(["1000000000"], "43-0000000", "ein", [(40.0, None)])])]))
    assert ingest_file(cfg, store, p)["status"] == "failed"
    monkeypatch.undo()
    store.rebuild_rollups()
    with store.connect() as con:
        assert con.execute("SELECT status FROM files").fetchone()[0] == "failed"
        assert con.execute("SELECT count(*) FROM rates").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM rates_by_tin").fetchone()[0] == 0


# -- P1.9 reset and forget never bring a parked part back ------------------------

def test_reset_and_forget_never_bring_a_parked_part_back(cfg, store):
    """A finished parse that could not swap in (Windows lock) is PARKED and
    adopted at the next open. After forget (or reset) the park was adopted
    anyway: rates = 1, files = 0, and forget could not remove it again."""
    import shutil

    from mrfx.store import file_key
    from tests.mrfx.conftest import make_fixture
    from tests.mrfx.test_v4 import innetwork, item

    def ingest_and_park(name):
        p = make_fixture(cfg.inbox_dir, name, innetwork(items=[
            item("97110", [(["1000000000"], "43-0000000", "ein", [(40.0, None)])])]))
        assert ingest_file(cfg, store, p)["status"] == "done"
        part = store.rates_dir / f"{file_key(name)}.parquet"
        park = store.rates_dir / f".{file_key(name)}.parked"
        shutil.copy(part, park)
        with store.write_lock, store.connect() as con:
            parked = json.loads(store._meta_get(con, "parked_parts") or "{}")
            parked[park.name] = part.name
            con.execute("INSERT OR REPLACE INTO meta VALUES ('parked_parts', ?)",
                        [json.dumps(parked)])
        return park

    park = ingest_and_park("a.json")
    store.forget_file("a.json")
    assert not park.exists()
    s2 = Store(cfg.store_dir)
    with s2.connect() as con:
        assert con.execute("SELECT count(*) FROM rates").fetchone()[0] == 0
    park = ingest_and_park("b.json")
    s2.reset()
    assert not park.exists()
    s3 = Store(cfg.store_dir)
    with s3.connect() as con:
        assert con.execute("SELECT count(*) FROM rates").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 0
    # a park whose file has no files row (left by an older version) is never
    # adopted: it is removed at the next open
    p = make_fixture(cfg.inbox_dir, "c.json", innetwork(items=[
        item("97110", [(["1000000000"], "43-0000000", "ein", [(40.0, None)])])]))
    assert ingest_file(cfg, s3, p)["status"] == "done"
    ghost = s3.rates_dir / f".{file_key('ghost.json')}.parked"
    shutil.copy(s3.rates_dir / f"{file_key('c.json')}.parquet", ghost)
    with s3.write_lock, s3.connect() as con:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('parked_parts', ?)",
                    [json.dumps({ghost.name: f"{file_key('ghost.json')}.parquet"})])
    s4 = Store(cfg.store_dir)
    assert not ghost.exists()
    assert not (s4.rates_dir / f"{file_key('ghost.json')}.parquet").exists()


# -- P1.10 a weekly NPPES update reaches the practice directory ------------------

def test_a_weekly_update_renames_an_identified_provider_everywhere(cfg, store, tmp_path):
    """The monthly names NPI 1417594896 OLD CLINIC NAME at 63101; the weekly
    renames it NEW CLINIC NAME at 63099. apply_weekly_update reported 1
    updated, but npi_directory kept OLD / 63101 (the practice stayed at the
    old ZIP while the new-clinics feed showed the new one)."""
    from mrfx.enrich import _nppes_cache_path, _write_nppes_parquet, apply_weekly_update
    from tests.mrfx.test_floors import _nppes_csv, _nppes_row
    old = _nppes_row("1417594896", "OLD CLINIC NAME")
    old[8] = "63101"
    cfg.enrichment.bulk_csv_path = _nppes_csv(tmp_path, [old], name="npidata_pfile_m.csv")
    assert _write_nppes_parquet(cfg, store, _nppes_cache_path(store), None) == 1
    store.save_npi("1417594896", "OLD CLINIC NAME", "261QP2000X", "1 Main St", "StL", "MO",
                   entity_type="NPI-2", zip_code="63101")
    new = _nppes_row("1417594896", "NEW CLINIC NAME")
    new[8] = "63099"
    res = apply_weekly_update(cfg, store, _nppes_csv(tmp_path, [new]))
    assert res["updated_npis"] == 1 and res["directory_refreshed"] == 1
    with store.connect() as con:
        assert con.execute("SELECT org_name, zip FROM npi_directory WHERE npi = '1417594896'"
                           ).fetchone() == ("NEW CLINIC NAME", "63099")


# -- P1.11 hospital files: the CMS wide layout, code|N slots, one modifier rule --

def _hosp_csv(tmp_path, header, rows, name="h.csv"):
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["hospital_name", "last_updated_on", "version"])
    w.writerow(["Test General", "2026-07-01", "2.0.0"])
    w.writerow(header)
    w.writerows(rows)
    p = tmp_path / name
    p.write_text(buf.getvalue())
    return p


def test_a_wide_hospital_file_imports_every_payer(store, tmp_path):
    """standard_charge|Aetna|PPO|negotiated_dollar = 88.50 and
    …|Anthem BCBS|HMO|… = 132.75 on one 97110 line: it imported "rows 2,
    payers 1" — Aetna's column as "Unknown payer". By hand: two rows, two
    payers, 88.50 and 132.75."""
    from mrfx.hospital import import_hospital_file
    p = _hosp_csv(tmp_path, [
        "description", "code|1", "code|1|type", "setting", "modifiers",
        "standard_charge|gross", "standard_charge|Aetna|PPO|negotiated_dollar",
        "standard_charge|Aetna|PPO|methodology",
        "standard_charge|Anthem BCBS|HMO|negotiated_dollar"],
        [["Ther exercise", "97110", "CPT", "outpatient", "", "300",
          "88.50", "fee schedule", "132.75"]])
    res = import_hospital_file(store, p, state="MO")
    assert res["rows"] == 2 and res["payers"] == 2
    with store.connect() as con:
        got = con.execute("SELECT payer, plan_name, billing_code, rate FROM hospital_rates "
                          "ORDER BY payer").fetchall()
    assert got == [("Aetna", "PPO", "97110", 88.5), ("Anthem BCBS", "HMO", "97110", 132.75)]


def test_a_tall_file_with_the_cpt_in_code_2_and_raw_modifiers(store, tmp_path):
    """code|1 = the chargemaster number (type CDM), code|2 = 97110 (CPT): it
    imported nothing. Modifiers "GP|59" / "59,GP" were stored raw and the
    parity pooled them with base rows. By hand: two rows, both stored as the
    sorted set "59|GP"; with a base-only market the parity's hospital side
    keeps only the plain row (100.00)."""
    from mrfx.hospital import import_hospital_file
    p = _hosp_csv(tmp_path, [
        "description", "code|1", "code|1|type", "code|2", "code|2|type", "setting",
        "modifiers", "payer_name", "plan_name", "standard_charge|negotiated_dollar"],
        [["Ther ex", "CDM-4410", "CDM", "97110", "CPT", "outpatient", "", "Aetna", "PPO", "100"],
         ["Ther ex", "CDM-4411", "CDM", "97110", "CPT", "outpatient", "GP|59", "Aetna", "PPO", "150"],
         ["Ther ex", "CDM-4412", "CDM", "97110", "CPT", "outpatient", "59,GP", "Aetna", "PPO", "160"]])
    assert import_hospital_file(store, p, state="MO")["rows"] == 3
    with store.connect() as con:
        mods = sorted(r[0] or "" for r in con.execute(
            "SELECT modifiers FROM hospital_rates").fetchall())
    assert mods == ["", "59|GP", "59|GP"]
    from mrfx.hospital import base_sql
    with store.connect() as con:
        kept = con.execute("SELECT rate FROM hospital_rates h WHERE "
                           + base_sql("coalesce(h.modifiers, '')")).fetchall()
    assert kept == [(100.0,)]


# -- P1.12 the real 2026 CMS RVU files ---------------------------------------------

_PPRRVU_2026_HEAD = (
    ",,2026 National Physician Fee Schedule Relative Value File October Release,,,,,,,,,,,,,,,,,,,,,,,,,,\n"
    ",,CPT codes and descriptions only are copyright 2026 American Medical Association.,,,,,,,,,,,,,,,,,,,,,,,,,,\n"
    ",,,,,,,,,,,,,,,,,,,,,,,,,,,,\n"
    ",,RELEASED 08/26/2026,,,,,,,,,,,,,,,,,,,,,,,,,,\n"
    ",,,,,,,,,,,,,,,,,,,,,,,,,NON-FACILITY,FACILITY,\n"
    ",,,,NOT USED,,,,,,,,,,,,,,,,,,,,,,PHYSICIAN,,DIAGNOSTIC,PE USED,PE USED,MP USED\n"
    ",,,,FOR,,,NON-FAC,,FACILITY,,,,,,,,,,,,,,,,,SUPERVISION OF,,IMAGING,FOR OPPS,FOR OPPS,FOR OPPS\n"
    ",,,STATUS,MEDICARE,WORK,NON-FAC,NA,FACILITY,NA,MP,NON-FACILITY,FACILITY,PCTC,GLOB,PRE,INTRA,"
    "POST,MULT,BILAT,ASST,CO-,TEAM,PRIC,ENDO,CONV,DIAGNOSTIC,CALCULATION,FAMILY,PAYMENT,PAYMENT,PAYMENT\n"
    "HCPCS,MOD,DESCRIPTION,CODE,PAYMENT,RVU,PE RVU,INDICATOR,PE RVU,INDICATOR,RVU,TOTAL,TOTAL,IND,DAYS,"
    "OP,OP,OP,PROC,SURG,SURG,SURG,SURG,IND,BASE,FACTOR,PROCEDURES,FLAG,INDICATOR,AMOUNT,AMOUNT,AMOUNT\n"
    "97110,,Therapeutic exercises,A,,0.45,0.41,,0.41,NA,0.01,0.87,0.87,7,XXX,0.00,0.00,0.00,5,0,0,0,0,9,,"
    "33.4009,09,0,99,0.00,0.00,0.00\n"
    "97110,GP,Therapeutic exercises,A,,0.45,0.41,,0.41,NA,0.01,0.87,0.87,7,XXX,0.00,0.00,0.00,5,0,0,0,0,9,,"
    "33.4009,09,0,99,0.00,0.00,0.00\n")
_GPCI_2026_HEAD = (
    "ADDENDUM E. FINAL CY 2026 GEOGRAPHIC PRACTICE COST INDICES (GPCIs) BY STATE AND MEDICARE LOCALITY,,,,,,\n"
    ",,,,,,\n"
    "Medicare Administrative Contractor (MAC),State,Locality Number,Locality Name,"
    "2026 PW GPCI (with 1.0 Floor)***,2026 PE GPCI,2026 MP GPCI\n"
    "05302,MO,02,METROPOLITAN KANSAS CITY,1.000,0.957,0.913\n"
    "05302,MO,99,REST OF MISSOURI*,1.000,0.857,0.913\n")


def test_the_real_2026_cms_layout_imports(store):
    """The 2026 PPRRVU starts with nine title rows and stacks each column
    name over several rows; the GPCI with two. Both were refused ("could not
    find column(s) HCPCS …"). Rows copied from the CMS RVU26D release.
    By hand, REST OF MISSOURI, 97110: (0.45 x 1.000 + 0.41 x 0.857 + 0.01 x
    0.913) = 0.8105 RVU x 33.4009 = 27.0714 -> $27.07. The file's own CONV FACTOR is
    33.4009: a different typed value is refused."""
    import io
    import zipfile

    from mrfx.mpfs import MpfsImportError, import_official, looks_official, parse_pprrvu
    rv = parse_pprrvu(_PPRRVU_2026_HEAD)
    assert rv == {"97110": {"work": 0.45, "pe_nonfac": 0.41, "mp": 0.01}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("PPRRVU2026_Oct_QPP.csv", _PPRRVU_2026_HEAD.replace("33.4009", "33.5675"))
        z.writestr("PPRRVU2026_Oct_nonQPP.csv", _PPRRVU_2026_HEAD)
        z.writestr("GPCI2026.csv", _GPCI_2026_HEAD)
    data = buf.getvalue()
    res = import_official(store, data, "rvu26d.zip", conversion_factor=33.4009, state="MO")
    assert "PPRRVU2026_Oct_nonQPP.csv" in res["source"] and res["locality"] == "REST OF MISSOURI*"
    with store.connect() as con:
        assert con.execute("SELECT non_facility_rate FROM mpfs WHERE code = '97110'"
                           ).fetchone()[0] == 27.07
    with pytest.raises(MpfsImportError, match="33.4009"):
        import_official(store, data, "rvu26d.zip", conversion_factor=32.3465, state="MO")
    assert looks_official(_PPRRVU_2026_HEAD.encode(), "PPRRVU2026_Oct_nonQPP.csv")


# -- P1.13 one rounding rule --------------------------------------------------------

def test_half_up_is_the_rule_a_person_and_sql_use():
    from mrfx.rounding import half_up
    assert half_up(112.5) == 113 and isinstance(half_up(112.5), int)
    assert half_up(12.5) == 13 and half_up(13.5) == 14          # round() said 12 and 14
    assert half_up(2.675, 2) == 2.68                            # round() said 2.67
    assert half_up(-0.5) == -1 and half_up(0.125, 2) == 0.13
    assert half_up(112.5, 0) == 113.0 and isinstance(half_up(112.5, 0), float)
    assert half_up(7) == 7 and half_up(1e300, 2) == 1e300


def test_a_rate_at_112_5_pct_of_medicare_prints_113_everywhere(cfg, store):
    """The subject is paid $45.00, the MPFS is $40.00: 112.5 %. By hand and
    in SQL that is 113 %; Python's round() printed 112. Two codes put the
    subject at positions 0 and 25: the headline is the median, 12.5 -> p13
    (round() said p12 while the per-code SQL said p13)."""
    from mrfx.benchmark import compute_benchmark, compute_payer_negotiation
    from tests.mrfx.conftest import make_fixture
    from tests.mrfx.test_v4 import innetwork, item

    def group(i, rate):
        return ([f"1{i:09d}"], f"43-00000{i:02d}", "ein", [(rate, None)])
    items = [item("97110", [group(0, 45.0)] + [group(i, r) for i, r in
                                               enumerate((50.0, 52.0, 54.0, 56.0), start=1)]),
             item("97140", [group(0, 45.0)] + [group(i, r) for i, r in
                                               enumerate((40.0, 50.0, 52.0, 54.0), start=1)])]
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "r.json", innetwork(items=items)))
    store.load_mpfs([{"code": "97110", "non_facility_rate": 40.0, "locality": "X"},
                     {"code": "97140", "non_facility_rate": 40.0, "locality": "X"}], "test MPFS")
    m = {"month": "2026-06", "allow_national": True}
    b = compute_benchmark(store, "430000000", m)
    row = {r["billing_code"]: r for r in b["rows"]}
    assert row["97110"]["subject_pct_medicare"] == 113
    assert (row["97110"]["subject_percentile"], row["97140"]["subject_percentile"]) == (0, 25)
    neg = compute_payer_negotiation(store, "430000000", m)
    assert neg["sections"][0]["headline_percentile"] == 13


# -- P1.15 the payer index divides by the market median the page prints ----------

def test_the_payer_index_divides_by_the_practice_market_median(cfg, store):
    """Payer A pays ten practices $50 on three codes; payer B pays one other
    practice $100. The page's market median is $50 (one rate per practice),
    so A is AT market: 1.000. It read 0.667 ("~33 % below market") because
    the denominator was the median of the two PAYER medians, $75. B prices
    one practice — that practice's contract, not B's level: not indexed."""
    from mrfx.overview import market_overview
    from tests.mrfx.conftest import make_fixture
    from tests.mrfx.test_v4 import innetwork, item
    codes = ("97110", "97140", "97530")
    a = [item(c, [([f"1{i:09d}"], f"43-00000{i:02d}", "ein", [(50.0, None)])
                  for i in range(10)]) for c in codes]
    b = [item(c, [(["1999999999"], "43-0000099", "ein", [(100.0, None)])]) for c in codes]
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "a.json", innetwork(payer="A Health", items=a)))
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "b.json", innetwork(payer="B Health", items=b)))
    store.rebuild_rollups()
    ov = market_overview(store)
    idx = {p["payer"]: p["index"] for p in ov["payer_index"]}
    assert idx == {"A Health": 1.0}
    assert {c["billing_code"]: c["median"] for c in ov["top_codes"]}["97110"] == 50.0


# -- P1.20 the dashboard learns that new data landed ------------------------------

def test_stats_carries_the_data_generation_and_it_moves_with_a_rollup(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from tests.mrfx.conftest import make_fixture
    from tests.mrfx.test_v4 import innetwork, item
    c = TestClient(create_app(cfg, store))
    g0 = c.get("/api/stats").json()["data_generation"]
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "n.json", innetwork(items=[
        item("97110", [(["1000000000"], "43-0000000", "ein", [(40.0, None)])])])))
    store.rebuild_rollups()
    assert c.get("/api/stats").json()["data_generation"] != g0
    from pathlib import Path
    app = (Path(__file__).resolve().parents[2] / "mrfx" / "web" / "app.js").read_text()
    assert "onDataUpdated()" in app and "reloadIfStale(view)" in app
    assert "const done = () => { if (cptCtl === ctl) cptCtl = null; };" in app


# -- P1.21 / P1.23 refusals are sentences, never a raw-JSON page -----------------

def test_a_refused_export_and_report_show_their_sentence(tmp_path):
    """Grain Entity -> Export CSV on a big store replaced the dashboard with
    {"detail": …}; "Open market report" with state ZZ threw an unhandled
    rejection and showed nothing. Both now say the sentence."""
    pw, browser, page = _browser_page()
    try:
        def handler(route):
            u = route.request.url
            if u.endswith("/"):
                route.fulfill(status=200, content_type="text/html",
                              body='<span id="msg"></span>')
            elif "/api/export.csv" in u:
                route.fulfill(status=422, content_type="application/json",
                              body='{"detail":"pick a payer and a code first"}')
            elif "/api/report/market" in u:
                route.fulfill(status=422, content_type="application/json",
                              body='{"detail":"\'ZZ\' is not a US state code"}')
            else:
                route.fulfill(status=404, body="")
        page.route("http://mrfx.test/**", handler)
        page.goto("http://mrfx.test/")
        page.add_script_tag(content="const $ = (s) => document.querySelector(s);\n"
                                    + _js_function("refusalText") + "\n"
                                    + _js_function("downloadInFrame") + "\n"
                                    + _js_function("openReportWith"))
        alerts = []
        page.on("dialog", lambda d: (alerts.append(d.message), d.dismiss()))
        page.evaluate("downloadInFrame('/api/export.csv?grain=entity')")
        page.wait_for_timeout(1500)
        assert any("pick a payer and a code first" in a for a in alerts), alerts
        assert page.url == "http://mrfx.test/", "the dashboard stays where it was"
        page.evaluate("openReportWith('/api/report/market', {state: 'ZZ'}, "
                      "(m) => { document.querySelector('#msg').textContent = m; })")
        page.wait_for_timeout(500)
        assert "not a US state code" in page.inner_text("#msg")
    finally:
        browser.close()
        pw.stop()


# -- P1.22 / P1.24 labels -----------------------------------------------------------

def test_the_by_code_header_and_the_validation_type_column(cfg, store):
    """NPI grain: the by-code table said "Practices" for NPI counts. The
    validation cross-check printed a percentage row as "$150.00" with a
    dollar delta: it now has a Type column, "$" only on dollar rows, and no
    delta for a non-dollar row."""
    from pathlib import Path

    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from tests.mrfx.conftest import make_fixture
    from tests.mrfx.test_v4 import innetwork, item
    app = (Path(__file__).resolve().parents[2] / "mrfx" / "web" / "app.js").read_text()
    assert '${state.grain === "npi" ? "NPIs" : "Practices"}</th>' in app
    assert "isDollarType(row.negotiated_type)" in app
    doc = innetwork(items=[item("97110", [(["1000000000"], "43-0000000", "ein", [(40.0, None)])])])
    doc["in_network"][0]["negotiated_rates"][0]["negotiated_prices"].append(
        {"negotiated_type": "percentage", "negotiated_rate": 150.0,
         "billing_class": "professional", "service_code": ["11"], "expiration_date": "9999-12-31"})
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "v.json", doc))
    rows = TestClient(create_app(cfg, store)).post("/api/validate", json={
        "id": "430000000", "code": "97110", "expected_rate": 40}).json()["rows"]
    by_type = {r["negotiated_type"]: r for r in rows}
    assert by_type["negotiated"]["delta_vs_expected"] == 0.0
    assert "delta_vs_expected" not in by_type["percentage"]


# -- P1.14 the pitch report's real-terms block says whose numbers they are -------

def test_the_real_terms_block_is_labelled_as_the_market(monkeypatch):
    """The block is each payer's MEDIAN across every practice in scope. The
    client's own 97110 went $40 -> $50 while three peers went $60 -> $50,
    and the client's report printed "Zed Health: -16.7 % in dollars … the
    cut is deeper than it looks" with no label. It now says it is the
    market's trajectory, not the client's contracts."""
    import html

    import mrfx.monitor as mon
    from mrfx.benchmark import _erosion_block
    monkeypatch.setattr(mon, "compute_payer_trajectory", lambda store, m: {
        "payers": [{"payer": "Zed Health", "first_month": "2025-01", "last_month": "2025-06",
                    "cumulative_pct": -16.7, "real_cumulative_pct": -18.2,
                    "erosion_line": "Zed Health: -16.7% in dollars, -18.2% in real terms"}],
        "inflation": {"caveat": "", "basis": "index", "index_name": "CPI-U", "source": "BLS"}})
    out = _erosion_block(None, {"market": {"month": "latest"}, "subject": "A REHAB"}, html.escape)
    assert "the market, not this practice" in out
    assert "not A REHAB&#x27;s own contracts" in out or "not A REHAB's own contracts" in out
    assert "Market — Zed Health" in out


# -- P2 / P3 --------------------------------------------------------------------------

def test_arrangement_and_type_spellings_are_normalised(cfg, store):
    """"fee for service" sent a whole item to 0 rows as a "bundle", and a
    "fee_schedule" price was stored as NOT dollars (out of every median).
    By hand: the bundle is excluded (1 bundled item), "fee-for-service" and
    "Fee_Schedule" are a dollar row at $50, an odd arrangement is kept and
    counted, a blank type is counted and stays out of the dollar rows."""
    from tests.mrfx.conftest import make_fixture
    from tests.mrfx.test_v4 import innetwork, item

    def it(arr, ntype, rate, npi):
        x = item("97110", [([npi], "43-0000000", "ein", [(rate, None)])])
        x["negotiation_arrangement"] = arr
        x["negotiated_rates"][0]["negotiated_prices"][0]["negotiated_type"] = ntype
        return x
    doc = innetwork(items=[it("bundle", "negotiated", 500.0, "1000000001"),
                           it("Fee-For-Service", "Fee_Schedule", 50.0, "1000000002"),
                           it("per diem plan", "negotiated", 51.0, "1000000003"),
                           it("ffs", "", 52.0, "1000000004")])
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "arr.json", doc))
    with store.connect() as con:
        rows = dict(con.execute("SELECT npi, is_dollar_rate FROM rates").fetchall())
        qa = json.loads(con.execute("SELECT qa FROM files").fetchone()[0])
    assert rows == {"1000000002": True, "1000000003": True, "1000000004": False}
    assert (qa["bundled_items"], qa["unknown_arrangement"], qa["missing_negotiated_type"]) == (1, 1, 1)


def test_paused_rows_block_forget_and_the_caches_and_version(cfg, store, capsys):
    from mrfx import __version__
    from mrfx.cli import main
    from mrfx.fetch import dedup_key
    from mrfx.monitor import compute_rate_changes, payer_months
    a = store.enqueue_url("https://x.example/a.json.gz", dedup_key("https://x.example/a.json.gz"))
    store.update_url(a, filename="deadbeef_a.json.gz")
    store.pause_url(a, 24, "paused: test")
    assert store.url_inflight_for_filename("deadbeef_a.json.gz") is True
    assert hasattr(compute_rate_changes, "uncached") and hasattr(payer_months, "uncached")
    with pytest.raises(SystemExit):
        main(["--version"])
    # bumped with every release (0.8.1: the field store's analytics — large
    # updates payer by payer, the directory in one pass, the header never waits)
    assert capsys.readouterr().out.strip() == f"mrfx {__version__}" == "mrfx 0.8.1"
