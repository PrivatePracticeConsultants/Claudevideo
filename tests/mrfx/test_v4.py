"""V4 acceptance tests (§9): TIN grain, entity map, benchmarks, pitch report."""

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.benchmark import BenchmarkError, compute_benchmark, compute_opportunity
from mrfx.ingest import ingest_file
from mrfx.store import looks_like_ssn, mask_tin
from tests.mrfx.conftest import make_fixture


def innetwork(payer="Testco Health Plans Inc", month="2026-06-01", items=None):
    return {
        "reporting_entity_name": payer,
        "reporting_entity_type": "health insurance issuer",
        "last_updated_on": month,
        "version": "2.0.0",
        "provider_references": [],
        "in_network": items or [],
    }


def item(code, groups_prices, code_type="CPT"):
    return {
        "negotiation_arrangement": "ffs",
        "billing_code_type": code_type,
        "billing_code_type_version": "2026",
        "billing_code": code,
        "negotiated_rates": [
            {
                "provider_groups": [
                    {"npi": [int(n) for n in npis], "tin": {"type": tin_type, "value": tin}}
                ],
                "negotiated_prices": [
                    {"negotiated_type": "negotiated", "negotiated_rate": rate,
                     "service_code": ["11"], "billing_class": "professional",
                     **({"billing_code_modifier": mods} if mods else {})}
                    for rate, mods in prices
                ],
            }
            for (npis, tin, tin_type, prices) in groups_prices
        ],
    }


# ---------------------------------------------------------------------------
# TIN-grain tests
# ---------------------------------------------------------------------------


def test_two_npis_one_tin_roll_up(cfg, store):
    data = innetwork(items=[
        item("97110", [ (["1111111111", "1222222222"], "43-1111111", "ein", [(40.0, None)]) ]),
    ])
    p = make_fixture(cfg.inbox_dir, "rollup.json", data)
    ingest_file(cfg, store, p)
    client = TestClient(create_app(cfg, store))
    r = client.get("/api/rates").json()
    assert r["total"] == 1
    assert r["rows"][0]["npi_count"] == 2
    assert r["rows"][0]["tin_value"] == "431111111"


def test_tin_type_npi_flagged_and_hideable(cfg, store):
    data = innetwork(items=[
        item("97110", [
            (["1111111111"], "1111111111", "npi", [(40.0, None)]),   # tin is really an NPI
            (["2222222222"], "43-2222222", "ein", [(35.0, None)]),
        ]),
    ])
    p = make_fixture(cfg.inbox_dir, "tinnpi.json", data)
    ingest_file(cfg, store, p)
    client = TestClient(create_app(cfg, store))
    all_rows = client.get("/api/rates").json()
    flagged = [x for x in all_rows["rows"] if x["tin_is_really_npi"]]
    assert len(flagged) == 1
    hidden = client.get("/api/rates?hide_tin_npi=1").json()
    assert hidden["total"] == all_rows["total"] - 1
    assert all(not x["tin_is_really_npi"] for x in hidden["rows"])


def test_rate_variants_flagged(cfg, store):
    """Same (code, modifier, class) tuple, two different rates under one TIN."""
    data = innetwork(items=[
        item("97110", [
            (["1111111111"], "43-3333333", "ein", [(40.0, None)]),
            (["1222222222"], "43-3333333", "ein", [(45.0, None)]),
        ]),
    ])
    p = make_fixture(cfg.inbox_dir, "variants.json", data)
    ingest_file(cfg, store, p)
    client = TestClient(create_app(cfg, store))
    rows = client.get("/api/rates").json()["rows"]
    assert len(rows) == 1
    assert rows[0]["rate_variants"] == 2
    assert rows[0]["rate_min"] == 40.0 and rows[0]["rate_max"] == 45.0


def test_tin_display_name_from_nppes(cfg, store):
    data = innetwork(items=[
        item("97110", [ (["1111111111", "1222222222"], "43-4444444", "ein", [(40.0, None)]) ]),
    ])
    p = make_fixture(cfg.inbox_dir, "names.json", data)
    ingest_file(cfg, store, p)
    store.save_npi("1111111111", "Sunrise Physical Therapy LLC", "261QP2000X",
                   "Clinic/Center, Physical Therapy", "Saint Louis", "MO", entity_type="NPI-2")
    store.save_npi("1222222222", "Sunrise Physical Therapy LLC", "261QP2000X",
                   "Clinic/Center, Physical Therapy", "Fenton", "MO", entity_type="NPI-2")
    store.rebuild_rollups()
    client = TestClient(create_app(cfg, store))
    row = client.get("/api/rates").json()["rows"][0]
    assert row["display_name"] == "Sunrise Physical Therapy LLC"
    detail = client.get("/api/entity/tin/434444444").json()
    assert detail["tins"][0]["entity_kind"] == "org"
    assert set(detail["tins"][0]["states"]) == {"MO"}


def test_partitioned_rollup_identical_to_single_shot(cfg, store, monkeypatch):
    # big stores rebuild rollups in hash-partitioned passes; the union of the
    # slices must be row-identical to the one-shot build
    import mrfx.store as st

    data = innetwork(items=[
        item("97110", [ (["1111111111", "1222222222"], "43-1111111", "ein", [(40.0, None), (45.0, None)]) ]),
        item("97112", [ (["1333333333"], "43-2222222", "ein", [(50.0, ["GP"])]) ]),
        item("97161", [ (["1444444444"], "43-3333333", "ein", [(80.0, None)]) ]),
        item("92507", [ (["1555555555"], "43-4444444", "ein", [(60.0, None), (61.0, None)]) ]),
    ])
    p = make_fixture(cfg.inbox_dir, "parts.json", data)
    ingest_file(cfg, store, p)
    store.save_npi("1111111111", "Sunrise PT LLC", "261QP2000X", "Clinic", "Saint Louis", "MO",
                   entity_type="NPI-2")

    def snapshot():
        with store.connect() as con:
            tin = con.execute(
                "SELECT * FROM rates_by_tin_tbl ORDER BY billing_code, tin_value, "
                "modifier_set, negotiated_rate").fetchall()
            dirs = con.execute("SELECT * FROM tin_directory_tbl ORDER BY tin_value").fetchall()
        return tin, dirs

    store.rebuild_rollups()          # single shot (4 items << threshold)
    single = snapshot()
    monkeypatch.setattr(st, "ROLLUP_PARTITION_ROWS", 2)  # force multiple passes
    store.rebuild_rollups()
    parted = snapshot()
    assert parted == single
    assert len(single[0]) > 0 and len(single[1]) > 0


def test_ssn_pattern_tin_masked():
    assert looks_like_ssn("078051120")       # classic SSN-pattern (invalid EIN prefix 07)
    assert not looks_like_ssn("431111111")   # 43 is a valid EIN prefix
    assert mask_tin("078051120") == "MASKED-SSN"
    assert mask_tin("431111111") == "431111111"


def test_discipline_attribution(cfg, store):
    data = innetwork(items=[
        item("97110", [ (["1111111111"], "43-5555555", "ein",
                         [(40.0, ["GP"]), (38.0, ["GO"]), (36.0, None)]) ]),
        item("97161", [ (["1111111111"], "43-5555555", "ein", [(80.0, None)]) ]),  # PT-only code
    ])
    p = make_fixture(cfg.inbox_dir, "disc.json", data)
    ingest_file(cfg, store, p)
    client = TestClient(create_app(cfg, store))
    rows = client.get("/api/rates").json()["rows"]
    by = {(r["billing_code"], r["modifier_set"]): r["discipline"] for r in rows}
    assert by[("97110", "GP")] == "PT"
    assert by[("97110", "GO")] == "OT"
    assert by[("97110", "")] == "unspecified"   # shared code, no modifier: never guessed
    assert by[("97161", "")] == "PT"            # single-discipline code attributes by code
    pt_only = client.get("/api/rates?discipline=PT").json()
    assert {r["billing_code"] for r in pt_only["rows"]} == {"97110", "97161"}
    assert all(r["discipline"] == "PT" for r in pt_only["rows"])


# ---------------------------------------------------------------------------
# entity-map tests
# ---------------------------------------------------------------------------


def seed_two_tins(cfg, store):
    data = innetwork(items=[
        item("97110", [
            (["1111111111"], "43-1000001", "ein", [(40.0, None)]),
            (["1222222222"], "43-1000002", "ein", [(46.0, None)]),
            (["1333333333"], "43-2000009", "ein", [(30.0, None)]),
        ]),
    ])
    p = make_fixture(cfg.inbox_dir, "entities.json", data)
    ingest_file(cfg, store, p)


def test_entity_map_aggregates_and_lists_tins(cfg, store):
    seed_two_tins(cfg, store)
    cfg.entity_map_path.write_text(yaml.safe_dump(
        {"entities": [{"name": "ATR Hand Therapy", "tins": ["431000001", "431000002"]}]}
    ))
    client = TestClient(create_app(cfg, store))
    r = client.get("/api/rates?grain=entity").json()
    assert r["grain"] == "entity"
    atr = next(x for x in r["rows"] if x["display_name"] == "ATR Hand Therapy")
    assert atr["tin_count"] == 2
    assert atr["negotiated_rate"] == 43.0  # median of 40 / 46
    solo = next(x for x in r["rows"] if x["tin_value"] == "432000009")
    assert solo["tin_count"] == 1  # unmapped TIN stands alone
    # detail never hides the constituent TINs
    detail = client.get("/api/entity/entity/ATR Hand Therapy").json()
    assert {t["tin_value"] for t in detail["tins"]} == {"431000001", "431000002"}


def test_entity_grain_auto_groups_by_nppes_name_without_a_map(cfg, store):
    # The core "roll up automatically" behavior: two DIFFERENT tax IDs whose
    # providers resolve to the same NPPES organization name must merge into one
    # entity at entity grain even with NO manual entity_map.yaml. Regression:
    # grain_of used to silently degrade entity->tin whenever no map was loaded,
    # so an explicit Entity-grain request showed per-TIN rows and the automatic
    # org rollup was invisible.
    seed_two_tins(cfg, store)
    # enrich both org TINs' NPIs to the same chain name (as NPPES would)
    for npi in store.unenriched_npis():
        name = "Regional Rehab Group" if npi in ("1111111111", "1222222222") else "Solo PT"
        store.save_npi(npi, name, "225100000X", "PT", "KC", "MO", entity_type="NPI-2")
    store.rebuild_rollups()
    assert not cfg.entity_map_path.exists()  # no manual map at all
    client = TestClient(create_app(cfg, store))
    r = client.get("/api/rates?grain=entity").json()
    assert r["grain"] == "entity"  # request honored, not degraded to tin
    chain = next(x for x in r["rows"] if x["display_name"] == "Regional Rehab Group")
    assert chain["tin_count"] == 2                         # two tax IDs rolled into one org
    assert chain["negotiated_rate"] == 43.0               # median of 40 / 46
    assert set(chain["tin_value"].split("; ")) == {"431000001", "431000002"}
    # drilling in must list BOTH constituent tax IDs + their providers (an
    # auto-grouped org used to open an empty drawer — the detail resolved
    # members only through the manual map)
    detail = client.get("/api/entity/entity/Regional Rehab Group").json()
    assert {t["tin_value"] for t in detail["tins"]} == {"431000001", "431000002"}
    assert {n["npi"] for n in detail["npis"]} == {"1111111111", "1222222222"}


def test_verified_website_saves_and_flows_to_export(cfg, store):
    # NPPES has no URL field, so the only trustworthy website is one the user
    # hand-verifies. It must save against the org's tax ids, show back on the
    # entity, reject non-http junk, and land in the outreach export; a lookup
    # SEARCH link is always offered (never a claimed official site).
    seed_two_tins(cfg, store)
    for npi in store.unenriched_npis():
        name = "Regional Rehab Group" if npi in ("1111111111", "1222222222") else "Solo PT"
        store.save_npi(npi, name, "225100000X", "PT", "KC", "MO", entity_type="NPI-2")
    store.rebuild_rollups()
    client = TestClient(create_app(cfg, store))
    det = client.get("/api/entity/entity/Regional Rehab Group").json()
    assert det["website"] is None and det["website_lookup"].startswith("https://www.google.com/search")
    assert set(det["website_tins"]) == {"431000001", "431000002"}

    # non-http is rejected, not stored as a bad link
    assert client.post("/api/org-website", json={"tins": det["website_tins"], "url": "ftp://x"}).status_code == 422
    # a real URL saves against every constituent tax id and shows back
    assert client.post("/api/org-website", json={"tins": det["website_tins"],
                                                 "url": "https://regionalrehab.example"}).status_code == 200
    assert client.get("/api/entity/entity/Regional Rehab Group").json()["website"] == "https://regionalrehab.example"

    # and it flows to the outreach CSV (WEBSITE = verified, WEBSITE_LOOKUP = search link)
    csv = client.get("/api/export/outreach.csv?grain=entity&cpt=97110").text
    header = csv.splitlines()[0]
    assert "WEBSITE" in header and "WEBSITE_LOOKUP" in header
    line = next(l for l in csv.splitlines() if "Regional Rehab Group" in l)
    assert "https://regionalrehab.example" in line

    # clearing removes it
    client.post("/api/org-website", json={"tins": det["website_tins"], "url": ""})
    assert client.get("/api/entity/entity/Regional Rehab Group").json()["website"] is None


def test_entity_map_ui_edit_persists_to_yaml(cfg, store):
    seed_two_tins(cfg, store)
    client = TestClient(create_app(cfg, store))
    r = client.post("/api/entities/update", json={
        "name": "Competitor PT Group", "add_tins": ["43-2000009"],
    })
    assert r.status_code == 200
    saved = yaml.safe_load(cfg.entity_map_path.read_text())
    assert saved["entities"][0]["name"] == "Competitor PT Group"
    assert saved["entities"][0]["tins"] == ["432000009"]
    rows = client.get("/api/rates?grain=entity").json()["rows"]
    assert any(x["display_name"] == "Competitor PT Group" for x in rows)


# ---------------------------------------------------------------------------
# benchmark tests
# ---------------------------------------------------------------------------


@pytest.fixture
def market_store(cfg, store):
    """Synthetic market: subject TIN at $30; 9 peers at $31..$39 (known distribution)."""
    groups = [([f"1{i:09d}"], f"43-00000{i:02d}", "ein", [(30.0 + i, None)]) for i in range(10)]
    data = innetwork(items=[item("97110", groups)])
    p = make_fixture(cfg.inbox_dir, "market.json", data)
    ingest_file(cfg, store, p)
    return store


def test_benchmark_percentiles_and_position(cfg, market_store):
    bench = compute_benchmark(market_store, "430000000", {
        "month": "2026-06", "billing_class": "professional",
    })
    row = next(r for r in bench["rows"] if r["billing_code"] == "97110")
    # peers are 31..39 -> median 35, p25 33, p75 37 (linear interpolation)
    assert row["subject_rate"] == 30.0
    assert row["n_peers"] == 9
    assert row["p50"] == 35.0
    assert row["p25"] == 33.0 and row["p75"] == 37.0
    assert row["subject_percentile"] == 0.0  # below every peer
    assert row["gap_to_target"] == 5.0       # median - subject
    # subject at the top
    top = compute_benchmark(market_store, "430000009", {"month": "2026-06"})
    assert top["rows"][0]["subject_percentile"] == 100.0


def test_benchmark_requires_month(market_store):
    with pytest.raises(BenchmarkError, match="as-of month"):
        compute_benchmark(market_store, "430000000", {})


def test_placeholder_dollar_rates_excluded_from_benchmark(cfg, store):
    # $0.01/$0 dollar "rates" are payer placeholders — they must not drag the
    # subject median or appear as peers
    groups = [
        (["1000000000"], "43-0000000", "ein", [(0.01, None), (85.0, None)]),  # subject
        (["1000000001"], "43-0000001", "ein", [(0.01, None)]),                # placeholder-only peer
        (["1000000002"], "43-0000002", "ein", [(80.0, None)]),
        (["1000000003"], "43-0000003", "ein", [(90.0, None)]),
    ]
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "ph.json",
                                         innetwork(items=[item("97110", groups)])))
    row = next(r for r in compute_benchmark(store, "430000000", {"month": "2026-06"})["rows"]
               if r["billing_code"] == "97110")
    assert row["subject_rate"] == 85.0   # not median(0.01, 85) == 42.5
    assert row["n_peers"] == 2           # the 0.01-only peer is excluded
    assert row["p50"] == 85.0            # median of {80, 90}


def test_negotiation_report_surfaces_bad_percentile_not_fabricated_zero(cfg, two_payer_store):
    # a conservative percentile above the target is a genuine config error for
    # every payer; the report must 422, not silently print "$0/yr"
    client = TestClient(create_app(cfg, two_payer_store))
    r = client.post("/api/report/negotiation", json={
        "subject": "430000000",
        "market": {"month": "2026-06", "allow_national": True, "target_percentile": 25},
        "volumes": {"97110": 1000},
        "conservative_percentile": 40,
    })
    assert r.status_code == 422


def test_opportunity_model_math(market_store):
    bench = compute_benchmark(market_store, "430000000", {"month": "2026-06"})
    opp = compute_opportunity(bench, {"97110": 1000}, conservative_percentile=40)
    row = opp["rows"][0]
    assert row["opportunity_at_target"] == 5000.0        # (35-30) x 1000
    assert row["opportunity_at_conservative"] == 4200.0  # p40 = 34.2 -> 4.2 x 1000
    assert opp["total_at_target"] == 5000.0
    with pytest.raises(BenchmarkError, match="units"):
        compute_opportunity(bench, {})


def test_mpfs_percent_of_medicare(cfg, market_store):
    client = TestClient(create_app(cfg, market_store))
    # no MPFS loaded -> no % of Medicare column
    bench = client.post("/api/benchmark/market", json={
        "subject": "430000000", "market": {"month": "2026-06"}}).json()
    assert bench["mpfs_loaded"] is False
    assert "subject_pct_medicare" not in bench["rows"][0]
    # load MPFS: 97110 non-facility $31.00 -> subject 30/31 ≈ 97%
    csv_data = "code,locality,non_facility_rate\n97110,MO-STL,31.00\n"
    r = client.post("/api/mpfs/upload", files={"file": ("mpfs.csv", csv_data, "text/csv")})
    assert r.status_code == 200
    bench = client.post("/api/benchmark/market", json={
        "subject": "430000000", "market": {"month": "2026-06"}}).json()
    row = bench["rows"][0]
    assert bench["mpfs_loaded"] is True
    assert row["subject_pct_medicare"] == 97.0
    assert row["median_pct_medicare"] == 113.0  # 35/31


def test_peer_sets_curated(cfg, market_store):
    client = TestClient(create_app(cfg, market_store))
    client.post("/api/peersets", json={"name": "stl-competitors",
                                       "tins": ["430000008", "430000009"]})
    bench = client.post("/api/benchmark/market", json={
        "subject": "430000000",
        "market": {"month": "2026-06", "peer_set": "stl-competitors"}}).json()
    row = bench["rows"][0]
    assert row["n_peers"] == 2            # only the curated TINs
    assert row["p50"] == 38.5             # median of 38, 39
    assert "stl-competitors" in bench["peer_set"]


def test_pitch_report_renders_with_methodology_and_refuses_without_month(cfg, market_store):
    client = TestClient(create_app(cfg, market_store))
    ok = client.post("/api/report/pitch", json={
        "subject": "430000000", "market": {"month": "2026-06", "allow_national": True},
        "volumes": {"97110": 1000},
    })
    assert ok.status_code == 200
    html = ok.text
    assert "METHODOLOGY" in html
    assert "As-of month: 2026-06" in html
    assert "Ghost rates" in html
    assert "$5,000.00" in html  # opportunity at target
    assert "NATIONAL COMPARISON" in html  # explicit-national warning banner
    refused = client.post("/api/report/pitch", json={"subject": "430000000", "market": {}})
    assert refused.status_code == 422


def test_report_requires_state_unless_national_explicitly_allowed(cfg, market_store):
    client = TestClient(create_app(cfg, market_store))
    # no state, no opt-in -> refused with a state-scope message
    refused = client.post("/api/report/pitch", json={
        "subject": "430000000", "market": {"month": "2026-06"}})
    assert refused.status_code == 422
    assert "state" in refused.text.lower()
    # a state-scoped report renders without the national banner (fixture TINs
    # have no NPPES state, so peers are empty, but the report still renders)
    scoped = client.post("/api/report/pitch", json={
        "subject": "430000000", "market": {"month": "2026-06", "state": "MO"}})
    assert scoped.status_code == 200
    assert "NATIONAL COMPARISON" not in scoped.text


@pytest.fixture
def two_payer_store(cfg, store):
    """Subject 43-0000000 contracts with TWO payers: underpaid by Alpha
    (subject $30 vs peers 31..39) and near-top with Beta (subject $38)."""
    peers = [([f"1{i:09d}"], f"43-00000{i:02d}", "ein", [(30.0 + i, None)]) for i in range(1, 10)]
    alpha = innetwork(payer="Alpha Health Plan",
                      items=[item("97110", [(["1000000000"], "43-0000000", "ein", [(30.0, None)])] + peers)])
    beta = innetwork(payer="Beta Health Plan",
                     items=[item("97110", [(["1000000000"], "43-0000000", "ein", [(38.0, None)])] + peers)])
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "alpha.json", alpha))
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "beta.json", beta))
    return store


def test_payer_negotiation_report_orders_weakest_payer_first(cfg, two_payer_store):
    from mrfx.benchmark import compute_payer_negotiation
    neg = compute_payer_negotiation(two_payer_store, "430000000", {"month": "2026-06"})
    # both payers discovered; weakest position (Alpha, p0) leads
    assert neg["payers"] == ["Alpha Health Plan", "Beta Health Plan"]
    alpha = neg["sections"][0]
    assert alpha["payer"] == "Alpha Health Plan"
    assert alpha["headline_percentile"] == 0.0   # $30 below every Alpha peer
    beta = neg["sections"][1]
    assert beta["headline_percentile"] == 89.0   # $38 above 8 of 9 Beta peers
    # each section benchmarks the subject only against THAT payer's peers
    assert alpha["benchmark"]["market"]["payers"] == ["Alpha Health Plan"]
    assert alpha["benchmark"]["rows"][0]["n_peers"] == 9


def test_payer_negotiation_endpoint_renders_and_totals_opportunity(cfg, two_payer_store):
    client = TestClient(create_app(cfg, two_payer_store))
    r = client.post("/api/report/negotiation", json={
        "subject": "430000000", "market": {"month": "2026-06", "allow_national": True},
        "volumes": {"97110": 1000},
    })
    assert r.status_code == 200
    html = r.text
    assert "Alpha Health Plan" in html and "Beta Health Plan" in html
    assert "METHODOLOGY" in html
    # Alpha gap (35-30)x1000 = 5000; Beta subject above median -> $0 opportunity
    assert "$5,000.00" in html
    # refuses without a pinned month, like the pitch report
    refused = client.post("/api/report/negotiation", json={"subject": "430000000", "market": {}})
    assert refused.status_code == 422
    # and refuses a stateless national comparison unless explicitly allowed
    no_state = client.post("/api/report/negotiation", json={
        "subject": "430000000", "market": {"month": "2026-06"}})
    assert no_state.status_code == 422
    assert "state" in no_state.text.lower()


# ---------------------------------------------------------------------------
# fee schedule + payer scorecard (§7C)
# ---------------------------------------------------------------------------


@pytest.fixture
def ratecard_store(cfg, store):
    """Subject 43-0000000 with two payers over two codes (no MPFS):
    Payer A 97110=$40 97140=$50; Payer B 97110=$30 97140=$60."""
    a = innetwork(payer="Payer A", items=[
        item("97110", [(["1000000000"], "43-0000000", "ein", [(40.0, None)])]),
        item("97140", [(["1000000000"], "43-0000000", "ein", [(50.0, None)])]),
    ])
    b = innetwork(payer="Payer B", items=[
        item("97110", [(["1000000000"], "43-0000000", "ein", [(30.0, None)])]),
        item("97140", [(["1000000000"], "43-0000000", "ein", [(60.0, None)])]),
    ])
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "rc_a.json", a))
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "rc_b.json", b))
    return store


def test_fee_schedule_lays_out_rates_by_payer(cfg, ratecard_store):
    from mrfx.schedule import compute_fee_schedule
    fs = compute_fee_schedule(ratecard_store, "430000000", {"month": "2026-06"})
    assert fs["payers"] == ["Payer A", "Payer B"]
    codes = {c["billing_code"]: c for c in fs["codes"]}
    assert codes["97110"]["rates"]["Payer A"]["rate"] == 40.0
    assert codes["97110"]["rates"]["Payer B"]["rate"] == 30.0
    assert codes["97140"]["rates"]["Payer B"]["rate"] == 60.0
    assert fs["mpfs_loaded"] is False


def test_payer_scorecard_ranks_by_pct_of_best_without_medicare(cfg, ratecard_store):
    from mrfx.schedule import compute_fee_schedule, payer_scorecard
    sc = payer_scorecard(compute_fee_schedule(ratecard_store, "430000000", {"month": "2026-06"}))
    assert sc["metric"] == "pct_of_best"
    # 97110 best=40 (A): A=100, B=75; 97140 best=60 (B): A=83, B=100
    # median % of best: A=median(100,83.3)=92, B=median(75,100)=88 -> A ranks first
    assert sc["best_payer"] == "Payer A"
    ranks = {r["payer"]: r["rank"] for r in sc["rows"]}
    assert ranks == {"Payer A": 1, "Payer B": 2}
    a = next(r for r in sc["rows"] if r["payer"] == "Payer A")
    assert a["n_codes"] == 2 and a["n_comparable"] == 2


def test_ratecard_report_and_csv_endpoints(cfg, ratecard_store):
    client = TestClient(create_app(cfg, ratecard_store))
    html = client.post("/api/report/ratecard", json={
        "subject": "430000000", "market": {"month": "2026-06"}})
    assert html.status_code == 200
    assert "Payer A" in html.text and "Payer B" in html.text
    assert "who pays best" in html.text and "METHODOLOGY" in html.text
    csv = client.post("/api/schedule/fee.csv", json={
        "subject": "430000000", "market": {"month": "2026-06"}})
    assert csv.status_code == 200
    assert "# " in csv.text and "97110" in csv.text  # methodology header + data
    # month is required, like the other reports
    refused = client.post("/api/report/ratecard", json={"subject": "430000000", "market": {}})
    assert refused.status_code == 422


def test_bulk_enrichment_reads_nppes_zip(cfg, store, tmp_path):
    import csv as _csv
    import io as _io
    import zipfile as _zip

    from mrfx.enrich import _BULK_COLS, enrich_via_bulk
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "b.json", innetwork(items=[
        item("97110", [(["1000000001"], "43-9000000", "ein", [(40.0, None)])])])))
    hdr = [_BULK_COLS[k] for k in
           ("npi", "org", "first", "last", "city", "state", "tax1", "entity", "address", "zip", "phone")]
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(hdr)
    w.writerow(["1000000001", "Cornerstone PT", "", "", "Columbus", "OH", "225100000X", "2",
                "1 Main St", "43004", "6145551212"])
    zpath = tmp_path / "NPPES_Data_Dissemination.zip"
    with _zip.ZipFile(zpath, "w") as zf:
        zf.writestr("npidata_pfile_20260601-20260630_fileheader.csv", b"small header\n")
        zf.writestr("npidata_pfile_20260601-20260630.csv", buf.getvalue().encode())  # the big one
    cfg.enrichment.mode = "bulk"
    cfg.enrichment.bulk_csv_path = zpath
    assert enrich_via_bulk(cfg, store) == 1  # streamed straight from the .zip
    with store.connect() as con:
        row = con.execute(
            "SELECT org_name, city, state, entity_type FROM npi_directory WHERE npi = '1000000001'"
        ).fetchone()
    assert row == ("Cornerstone PT", "Columbus", "OH", "NPI-2")


def test_code_comparison_filters_by_state(cfg, store):
    # comparing a MO rate against a CA rate is misleading; the Code-comparison
    # endpoint must honor a state filter (backend already supports it via
    # FilterSet — this locks in that the code view passes it through)
    data = innetwork(items=[item("97110", [
        (["1000000001"], "43-1111111", "ein", [(40.0, None)]),
        (["1000000002"], "43-2222222", "ein", [(50.0, None)]),
    ])])
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "cc.json", data))
    store.save_npi("1000000001", "MO Rehab", None, None, "Columbia", "MO", entity_type="NPI-2")
    store.save_npi("1000000002", "CA Rehab", None, None, "LA", "CA", entity_type="NPI-2")
    store.rebuild_rollups()
    client = TestClient(create_app(cfg, store))
    assert len(client.get("/api/code/97110").json()["ranked"]) == 2
    mo = client.get("/api/code/97110?state=MO").json()["ranked"]
    assert len(mo) == 1 and mo[0]["unit_id"] == "431111111"


def test_directory_refresh_is_globally_throttled(cfg, store, monkeypatch):
    import mrfx.enrich as E
    calls = []
    monkeypatch.setattr(store, "rebuild_rollups", lambda: calls.append(1))
    monkeypatch.setattr(E, "_last_dir_refresh", 0.0)
    assert E._maybe_refresh_directory(store) is True       # first call runs
    assert E._maybe_refresh_directory(store) is False      # within the window -> throttled
    assert E._maybe_refresh_directory(store, force=True) is True  # force overrides (CLI path)
    assert len(calls) == 2


def test_defuse_csv_quotes_formula_cells():
    from mrfx.store import defuse_csv
    assert defuse_csv("=SUM(A1)") == "'=SUM(A1)"
    assert defuse_csv("+1") == "'+1" and defuse_csv("@x") == "'@x" and defuse_csv("-3") == "'-3"
    assert defuse_csv("Regional PT") == "Regional PT"
    assert defuse_csv(None) is None and defuse_csv(42) == 42


def test_changes_csv_defuses_malicious_payer_name(cfg, store):
    # payer comes from the MRF's reporting_entity_name — a hostile "=..." name
    # must not execute when the exported CSV opens in Excel
    for name, month, rate in [("m.json", "2026-05-01", 50.0), ("j.json", "2026-06-01", 45.0)]:
        data = innetwork(payer="=EvilPayer", month=month, items=[
            item("97110", [(["1000000000"], "43-2000000", "ein", [(rate, None)])])])
        ingest_file(cfg, store, make_fixture(cfg.inbox_dir, name, data))
    client = TestClient(create_app(cfg, store))
    csv = client.post("/api/changes.csv", json={"market": {"month": "2026-06"}}).text
    assert "'=EvilPayer" in csv  # leading '=' defused with a quote


def test_scorecard_does_not_cross_scale_rank_when_medicare_loaded(cfg, store):
    # PayerC prices only a non-MPFS code; with the anchor loaded it must be
    # UNRANKED (no % of Medicare), not ranked on its %-of-best (a different scale)
    for payer, rows in [("PayerA", [("97110", 40.0), ("97140", 50.0)]),
                        ("PayerB", [("97110", 48.0), ("97140", 55.0)]),
                        ("PayerC", [("97140", 60.0)])]:
        data = innetwork(payer=payer, items=[
            item(c, [(["1000000000"], "43-3000000", "ein", [(r, None)])]) for c, r in rows])
        ingest_file(cfg, store, make_fixture(cfg.inbox_dir, f"{payer}.json", data))
    client = TestClient(create_app(cfg, store))
    client.post("/api/mpfs/upload", files={"file": (
        "m.csv", "code,locality,non_facility_rate\n97110,X,40\n", "text/csv")})
    sc = {r["payer"]: r for r in client.post("/api/schedule/fee", json={
        "subject": "433000000", "market": {"month": "2026-06"}}).json()["scorecard"]["rows"]}
    assert sc["PayerA"]["rank"] is not None and sc["PayerB"]["rank"] is not None
    assert sc["PayerC"]["rank"] is None                    # no MPFS code -> unranked
    assert sc["PayerC"]["median_pct_medicare"] is None
    assert sc["PayerC"]["median_pct_of_best"] == 100.0     # still computed, just not ranked on


def test_rate_changes_biggest_cut_none_when_only_increases(cfg, store):
    for name, month, r1, r2 in [("m.json", "2026-05-01", 50.0, 60.0),
                                ("j.json", "2026-06-01", 55.0, 66.0)]:  # both go UP
        data = innetwork(payer="Acme", month=month, items=[
            item("97110", [(["1000000000"], "43-2000000", "ein", [(r1, None)])]),
            item("97140", [(["1000000000"], "43-2000000", "ein", [(r2, None)])])])
        ingest_file(cfg, store, make_fixture(cfg.inbox_dir, name, data))
    from mrfx.monitor import compute_rate_changes
    res = compute_rate_changes(store, {"month": "2026-06"})
    assert res["n_cuts"] == 0 and res["n_increases"] == 2
    assert res["biggest_cut_pct"] is None            # not the smallest increase
    assert res["biggest_increase_pct"] is not None and res["biggest_increase_pct"] > 0


# ---------------------------------------------------------------------------
# underpaid-practice leads (§7D)
# ---------------------------------------------------------------------------


@pytest.fixture
def market5_store(cfg, store):
    """5 TINs over 3 codes; TIN ...001 is lowest on every code (percent_rank p0)."""
    tins = ["43-1000001", "43-1000002", "43-1000003", "43-1000004", "43-1000005"]
    code_rates = {"97110": [30, 35, 40, 45, 50],
                  "97140": [40, 45, 50, 55, 60],
                  "97112": [25, 30, 35, 40, 45]}
    items = [item(code, [([f"1{i:09d}"], tins[i], "ein", [(float(rates[i]), None)])
                         for i in range(5)]) for code, rates in code_rates.items()]
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "market5.json", innetwork(items=items)))
    return store


def test_leads_finds_underpaid_entities_ranked(cfg, market5_store):
    from mrfx.leads import compute_leads
    res = compute_leads(market5_store, {"month": "2026-06"}, threshold_percentile=25, min_codes=3)
    assert res["count"] == 2                    # p0 and p25 are both at/below p25
    lead = res["leads"][0]                       # most underpaid first
    assert lead["tin_value"] == "431000001"
    assert lead["median_percentile"] == 0.0    # percent_rank: cheapest = p0 (was 20 under cume_dist)
    assert lead["avg_gap_to_median"] == 10.0   # 10 below each code's median
    assert lead["n_codes"] == 3


def test_leads_percentile_agrees_with_benchmark_in_thin_market(cfg, store):
    # 2 providers on a code: the cheapest must read p0 in BOTH the lead sweep
    # and the pitch benchmark (cume_dist wrongly put it at p50, so a p25 sweep
    # could never surface the very practice the finder exists to find)
    from mrfx.benchmark import compute_benchmark
    from mrfx.leads import compute_leads
    data = innetwork(items=[
        item("97110", [(["1000000001"], "43-1111111", "ein", [(50.0, None)]),
                       (["1000000002"], "43-2222222", "ein", [(100.0, None)])]),
        item("97140", [(["1000000001"], "43-1111111", "ein", [(50.0, None)]),
                       (["1000000002"], "43-2222222", "ein", [(100.0, None)])]),
    ])
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "thin.json", data))
    leads = compute_leads(store, {"month": "2026-06"}, threshold_percentile=25, min_codes=1)
    cheap = next(l for l in leads["leads"] if l["tin_value"] == "431111111")
    assert cheap["median_percentile"] == 0.0                    # surfaced at p0
    row = next(r for r in compute_benchmark(store, "431111111", {"month": "2026-06"})["rows"]
               if r["billing_code"] == "97110")
    assert row["subject_percentile"] == 0.0                      # benchmark agrees


def test_leads_threshold_exclude_and_csv(cfg, market5_store):
    client = TestClient(create_app(cfg, market5_store))
    data = client.post("/api/leads", json={
        "market": {"month": "2026-06"}, "threshold_percentile": 40, "min_codes": 3}).json()
    tins = {x["tin_value"] for x in data["leads"]}
    assert "431000001" in tins and "431000002" in tins  # p20 and p40
    # excluding a known client drops it from the OUTPUT (distribution unchanged)
    excl = client.post("/api/leads", json={
        "market": {"month": "2026-06"}, "threshold_percentile": 40,
        "exclude_subject": "431000001"}).json()
    et = {x["tin_value"] for x in excl["leads"]}
    assert "431000001" not in et and "431000002" in et
    csv = client.post("/api/leads.csv", json={
        "market": {"month": "2026-06"}, "threshold_percentile": 40})
    assert csv.status_code == 200 and "# " in csv.text and "median_percentile" in csv.text


# ---------------------------------------------------------------------------
# rate-change monitoring (§7E)
# ---------------------------------------------------------------------------


@pytest.fixture
def two_month_store(cfg, store):
    """Same payer/TIN, May → June: 97110 cut 10%, 97140 up 10%."""
    may = innetwork(payer="Acme", month="2026-05-01", items=[
        item("97110", [(["1000000000"], "43-2000000", "ein", [(50.0, None)])]),
        item("97140", [(["1000000000"], "43-2000000", "ein", [(60.0, None)])]),
    ])
    jun = innetwork(payer="Acme", month="2026-06-01", items=[
        item("97110", [(["1000000000"], "43-2000000", "ein", [(45.0, None)])]),
        item("97140", [(["1000000000"], "43-2000000", "ein", [(66.0, None)])]),
    ])
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "may.json", may))
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "jun.json", jun))
    return store


def test_rate_changes_detects_cut_and_increase(cfg, two_month_store):
    from mrfx.monitor import compute_rate_changes
    res = compute_rate_changes(two_month_store, {"month": "2026-06"})
    assert res["prev_month"] == "2026-05" and res["new_month"] == "2026-06"
    assert res["n_cuts"] == 1 and res["n_increases"] == 1
    by_code = {c["billing_code"]: c for c in res["changes"]}
    assert by_code["97110"]["old_rate"] == 50.0 and by_code["97110"]["new_rate"] == 45.0
    assert by_code["97110"]["pct_change"] == -10.0 and by_code["97110"]["direction"] == "cut"
    assert by_code["97140"]["pct_change"] == 10.0 and by_code["97140"]["direction"] == "increase"


def test_rate_changes_requires_two_months(cfg, store):
    from mrfx.monitor import compute_rate_changes
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "one.json", innetwork(items=[
        item("97110", [(["1000000000"], "43-2000000", "ein", [(50.0, None)])])])))
    with pytest.raises(BenchmarkError, match="two months"):
        compute_rate_changes(store, {"month": "2026-06"})


def test_rate_changes_endpoint_csv_and_min_pct(cfg, two_month_store):
    client = TestClient(create_app(cfg, two_month_store))
    r = client.post("/api/changes", json={"market": {"month": "2026-06"}})
    assert r.status_code == 200 and r.json()["count"] == 2
    csv = client.post("/api/changes.csv", json={"market": {"month": "2026-06"}})
    assert csv.status_code == 200 and "old_rate" in csv.text and "# " in csv.text
    # both moves are 10% -> a 15% floor returns nothing
    filtered = client.post("/api/changes", json={
        "market": {"month": "2026-06"}, "min_pct": 15}).json()
    assert filtered["count"] == 0


def test_qa_flags_outliers_and_zero_rates(cfg, store):
    groups = [(["1000000001"], "43-7777777", "ein", [(30.0, None)]),
              (["1000000002"], "43-7777778", "ein", [(31.0, None)]),
              (["1000000003"], "43-7777779", "ein", [(29.0, None)]),
              (["1000000004"], "43-7777780", "ein", [(500.0, None)]),  # >5x median
              (["1000000005"], "43-7777781", "ein", [(0.01, None)])]   # placeholder
    data = innetwork(items=[item("97110", groups)])
    p = make_fixture(cfg.inbox_dir, "qa.json", data)
    ingest_file(cfg, store, p)
    qa = json.loads(store.file_status("qa.json")["qa"])
    assert qa["zero_rates"] == 1
    assert qa["outlier_rates"] >= 2  # the 500 and the 0.01
    # $0.01 placeholders are excluded from the rate views by default (they'd
    # drag medians below the deliverables); the per-file zero_rates QA count
    # above still reports them. hide-outliers then also hides the $500 outlier.
    client = TestClient(create_app(cfg, store))
    shown = client.get("/api/rates").json()["total"]
    hidden = client.get("/api/rates?hide_outliers=1").json()["total"]
    assert shown == 4 and hidden == 3


def test_npi_grain_export_does_not_mask_npis(cfg, store):
    # the SQL mask must be 9-digit-block scoped: 10-digit NPIs beginning
    # 17/18/19/28/29 were exported as 'MASKED-SSN', destroying identifiers
    data = innetwork(items=[
        item("97110", [ (["1712345678"], "43-1234567", "ein", [(40.0, None)]) ]),
    ])
    p = make_fixture(cfg.inbox_dir, "npi17.json", data)
    ingest_file(cfg, store, p)
    store.rebuild_rollups()
    client = TestClient(create_app(cfg, store))
    csv_text = client.get("/api/export.csv?grain=npi").text
    assert "1712345678" in csv_text
    assert "MASKED-SSN" not in csv_text
