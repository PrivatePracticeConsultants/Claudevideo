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


def test_names_only_rebuild_updates_directory_not_rate_spine(cfg, store, monkeypatch):
    # enrichment resolves NAMES, which only feed tin_directory. rebuild_rollups
    # (names_only=True) must materialize those names AND leave the rate spine
    # (rates_by_tin) byte-identical — it's built purely from rates and is what's
    # expensive to rebuild, so skipping it is what keeps search responsive during
    # a long identification.
    import mrfx.store as st
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "no.json", innetwork(items=[
        item("97110", [(["1111111111", "1222222222"], "43-4444444", "ein",
                        [(40.0, None), (44.0, None)])])])))
    store.rebuild_rollups()   # full build establishes both tables
    with store.connect() as con:
        spine_before = con.execute(
            "SELECT payer, tin_value, billing_code, negotiated_rate, npi_count "
            "FROM rates_by_tin ORDER BY billing_code").fetchall()
        name_before = con.execute(
            "SELECT display_name FROM tin_directory WHERE tin_value='434444444'").fetchone()[0]

    # names arrive AFTER the full build; a names-only refresh must surface them
    store.save_npi("1111111111", "Sunrise PT LLC", "261QP2300X", "Clinic/Center - Physical Therapy",
                   "STL", "MO", entity_type="NPI-2")
    store.save_npi("1222222222", "Sunrise PT LLC", "261QP2300X", "Clinic/Center - Physical Therapy",
                   "Fenton", "MO", entity_type="NPI-2")
    # force partitioning too, to exercise the sliced names-only path
    monkeypatch.setattr(st, "ROLLUP_PARTITION_ROWS", 1)
    store.rebuild_rollups(names_only=True)

    with store.connect() as con:
        spine_after = con.execute(
            "SELECT payer, tin_value, billing_code, negotiated_rate, npi_count "
            "FROM rates_by_tin ORDER BY billing_code").fetchall()
        name_after = con.execute(
            "SELECT display_name FROM tin_directory WHERE tin_value='434444444'").fetchone()[0]
    assert spine_after == spine_before          # rate spine untouched
    assert name_before is None or name_before != "Sunrise PT LLC"
    assert name_after == "Sunrise PT LLC"       # names materialized by the light rebuild


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


def test_malformed_tin_object_does_not_crash_file(cfg, store):
    # a payer that writes "tin" as a bare string or a list (not the {type,value}
    # object) must not take down the WHOLE file — the bad group is tolerated and
    # the valid sibling group in the same item still lands.
    from mrfx.parser import parse_provider_group
    # unit: non-dict tin slots return an empty-TIN group instead of raising
    assert parse_provider_group({"npi": ["1111111111"], "tin": "123456789"}) == (None, None, ("1111111111",))
    assert parse_provider_group({"npi": ["2222222222"], "tin": ["43-1"]}) == (None, None, ("2222222222",))

    # end-to-end: a file whose first group has a string tin still ingests; the
    # good group's rate is captured, and the file status is 'done' not 'failed'.
    data = innetwork(items=[{
        "negotiation_arrangement": "ffs", "billing_code_type": "CPT",
        "billing_code_type_version": "2026", "billing_code": "97110",
        "negotiated_rates": [{
            "provider_groups": [
                {"npi": ["1111111111"], "tin": "123456789"},               # malformed
                {"npi": ["2222222222"], "tin": {"type": "ein", "value": "43-7777777"}},
            ],
            "negotiated_prices": [{"negotiated_type": "negotiated",
                                   "negotiated_rate": 42.0, "service_code": ["11"],
                                   "billing_class": "professional"}],
        }],
    }])
    rec = ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "badtin.json", data))
    assert rec["status"] == "done"
    with store.connect() as con:
        tins = {r[0] for r in con.execute(
            "SELECT tin_value FROM rates WHERE billing_code='97110'").fetchall()}
    assert "437777777" in tins       # the well-formed sibling group survived


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


def test_benchmark_defaults_to_latest_month(market_store):
    # the user never has to pick a month: an empty market means "latest
    # available" and the result records the explicit vintage it defaulted to
    auto = compute_benchmark(market_store, "430000000", {})
    explicit = compute_benchmark(market_store, "430000000", {"month": "latest"})
    assert auto["market"]["month"] == "latest"
    assert auto["rows"] == explicit["rows"]


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


def test_fee_schedule_peer_comparison(market_store):
    # each rate-card cell compares the subject's rate to what THAT payer pays
    # the subject's PEERS for the same code (subject excluded)
    from mrfx.schedule import compute_fee_schedule
    fs = compute_fee_schedule(market_store, "430000000", {"month": "2026-06"})
    e = next(x for x in fs["codes"] if x["billing_code"] == "97110")
    payer = fs["payers"][0]
    v = e["rates"][payer]
    assert v["rate"] == 30.0
    assert v["market_median"] == 35.0    # median of the 9 peers 31..39
    assert v["n_peers"] == 9             # subject's own TIN excluded
    assert v["vs_market_pct"] == -14     # 30 is ~14% below the peer median


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
    # no month = latest available (the user never has to pick one) — and the
    # report must still STATE its vintage instead of refusing
    auto = client.post("/api/report/ratecard", json={"subject": "430000000", "market": {}})
    assert auto.status_code == 200
    assert "latest available" in auto.text


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


def test_bulk_enrichment_change_aware_skips_rescan(cfg, store, tmp_path, monkeypatch):
    # after a full scan, an NPI the file DOESN'T contain must not force a fresh
    # multi-GB re-read every cycle — the persistent serve loop's efficiency fix
    import csv as _csv
    import io as _io
    import zipfile as _zip

    import mrfx.enrich as E
    from mrfx.enrich import _BULK_COLS, enrich_via_bulk
    monkeypatch.setattr(E, "_bulk_sig", None)   # isolate module change-awareness
    monkeypatch.setattr(E, "_BULK_MIN_FULL_ROWS", 1)  # tiny fixture counts as a "full" file
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "cc.json", innetwork(items=[
        item("97110", [(["1000000001"], "43-9000000", "ein", [(40.0, None)]),
                       (["1000000002"], "43-9000001", "ein", [(41.0, None)])])])))
    hdr = [_BULK_COLS[k] for k in
           ("npi", "org", "first", "last", "city", "state", "tax1", "entity", "address", "zip", "phone")]
    buf = _io.StringIO()
    _csv.writer(buf).writerows([hdr,
        ["1000000001", "In-File PT", "", "", "Columbus", "OH", "225100000X", "2", "1 St", "43004", "6140000000"]])
    zpath = tmp_path / "npidata.zip"
    with _zip.ZipFile(zpath, "w") as zf:
        zf.writestr("npidata_pfile_2026.csv", buf.getvalue().encode())
    cfg.enrichment.mode = "bulk"
    cfg.enrichment.bulk_csv_path = zpath
    assert enrich_via_bulk(cfg, store) == 1        # resolves 001; 002 recorded absent
    opens = []
    real = E._open_bulk_text
    monkeypatch.setattr(E, "_open_bulk_text", lambda p: opens.append(1) or real(p))
    assert enrich_via_bulk(cfg, store) == 0        # 002 still un-enriched but known-absent
    assert opens == []                             # the file was NOT re-read


def test_bulk_enrichment_marks_absent_npis_processed(cfg, store, tmp_path, monkeypatch):
    # An NPI the NPPES file doesn't contain (deactivated / junk from a messy MRF)
    # must be recorded as a PROCESSED no-name row, exactly like the API path's
    # dead row for an empty result — otherwise the dashboard's "identifying N
    # more…" banner can never reach zero even though enrichment is finished.
    import csv as _csv
    import io as _io
    import zipfile as _zip

    import mrfx.enrich as E
    from mrfx.enrich import _BULK_COLS, enrich_via_bulk
    monkeypatch.setattr(E, "_bulk_sig", None)
    monkeypatch.setattr(E, "_bulk_read_failures", 0)
    monkeypatch.setattr(E, "_BULK_MIN_FULL_ROWS", 1)  # tiny fixture counts as a "full" file
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "ab.json", innetwork(items=[
        item("97110", [(["1000000001"], "43-9000000", "ein", [(40.0, None)]),
                       (["1000000002"], "43-9000001", "ein", [(41.0, None)])])])))
    hdr = [_BULK_COLS[k] for k in
           ("npi", "org", "first", "last", "city", "state", "tax1", "entity", "address", "zip", "phone")]
    buf = _io.StringIO()
    _csv.writer(buf).writerows([hdr,
        ["1000000001", "In-File PT", "", "", "Columbus", "OH", "225100000X", "2", "1 St", "43004", "6140000000"]])
    zpath = tmp_path / "npidata.zip"
    with _zip.ZipFile(zpath, "w") as zf:
        zf.writestr("npidata_pfile_2026.csv", buf.getvalue().encode())
    cfg.enrichment.mode = "bulk"
    cfg.enrichment.bulk_csv_path = zpath
    assert enrich_via_bulk(cfg, store) == 1
    # the absent NPI 002 is now a processed dead row (present, but no name)...
    with store.connect() as con:
        row = con.execute("SELECT org_name FROM npi_directory WHERE npi = '1000000002'").fetchone()
    assert row is not None and row[0] is None
    # ...so nothing is left "remaining", though only 001 actually got a name
    prog = store.enrichment_progress(max_age_seconds=0)
    assert prog["total"] == 2 and prog["remaining"] == 0 and prog["named"] == 1


def test_api_mode_with_present_bulk_file_uses_bulk(cfg, store, tmp_path, monkeypatch):
    # THE "names stuck at a few thousand" footgun: a user who sets bulk_csv_path
    # but leaves mode:api (the default) used to have the downloaded file silently
    # ignored and crawl the rate-limited API for days. Now a PRESENT bulk file is
    # preferred regardless of mode, so run_enrichment resolves from the file.
    import csv as _csv
    import io as _io
    import zipfile as _zip

    import mrfx.enrich as E
    from mrfx.enrich import _BULK_COLS, run_enrichment, use_bulk_enrichment
    monkeypatch.setattr(E, "_bulk_sig", None)
    monkeypatch.setattr(E, "_bulk_read_failures", 0)
    monkeypatch.setattr(E, "_BULK_MIN_FULL_ROWS", 1)
    # fail loudly if the API path is ever taken (it must NOT be)
    monkeypatch.setattr(E, "enrich_via_api",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("used API, not bulk")))
    hdr = [_BULK_COLS[k] for k in
           ("npi", "org", "first", "last", "city", "state", "tax1", "entity", "address", "zip", "phone")]
    buf = _io.StringIO()
    _csv.writer(buf).writerows([hdr,
        ["1000000001", "In-File PT", "", "", "KC", "MO", "225100000X", "2", "1 St", "64000", "0"]])
    zpath = tmp_path / "npidata.zip"
    with _zip.ZipFile(zpath, "w") as zf:
        zf.writestr("npidata_pfile_2026.csv", buf.getvalue().encode())

    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "a.json", innetwork(items=[
        item("97110", [(["1000000001"], "43-1000001", "ein", [(40.0, None)])])])))

    cfg.enrichment.mode = "api"                     # NOT bulk
    cfg.enrichment.bulk_csv_path = zpath            # but a real file is present
    assert use_bulk_enrichment(cfg) is True         # present file wins over api
    assert run_enrichment(cfg, store) == 1          # resolved from the file, no API
    with store.connect() as con:
        name = con.execute(
            "SELECT org_name FROM npi_directory WHERE npi='1000000001'").fetchone()[0]
    assert name == "In-File PT"

    # a MISSING bulk path must NOT switch to bulk — the API remains the fallback
    cfg.enrichment.bulk_csv_path = tmp_path / "does_not_exist.zip"
    assert use_bulk_enrichment(cfg) is False
    # and with no bulk path at all, plain api
    cfg.enrichment.bulk_csv_path = None
    assert use_bulk_enrichment(cfg) is False


def test_auto_bulk_falls_back_to_api_when_file_is_bad(cfg, store, tmp_path, monkeypatch):
    # regression: an auto-promoted bulk (mode:api + a PRESENT but corrupt/partial
    # NPPES file) must NOT strand the user with no names — after bulk resolves
    # nothing, run_enrichment falls back to the API for the remainder. (An
    # explicit mode:bulk is left alone; that user opted out of the API.)
    import mrfx.enrich as E
    from mrfx.enrich import run_enrichment
    monkeypatch.setattr(E, "_bulk_sig", None)
    monkeypatch.setattr(E, "_bulk_read_failures", 0)

    # a stub API that resolves whatever bulk left un-enriched (no real network)
    api_calls = []
    def fake_api(cfg_, store_, stop=None):
        npis = store_.unenriched_npis(limit=1000)
        for n in npis:
            store_.save_npi(n, "API Name", "225100000X", "Physical Therapist", "KC", "MO")
        api_calls.append(len(npis))
        return len(npis)
    monkeypatch.setattr(E, "enrich_via_api", fake_api)

    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "a.json", innetwork(items=[
        item("97110", [(["1000000001", "1000000002"], "43-1000001", "ein", [(40.0, None)])])])))

    corrupt = tmp_path / "npidata.zip"
    corrupt.write_bytes(b"this is not a valid zip file")   # bulk read fails
    cfg.enrichment.mode = "api"                             # auto-promote path
    cfg.enrichment.bulk_csv_path = corrupt

    run_enrichment(cfg, store)
    assert api_calls and api_calls[0] == 2                  # API finished both NPIs
    with store.connect() as con:
        named = {r[0] for r in con.execute(
            "SELECT npi FROM npi_directory WHERE org_name='API Name'").fetchall()}
    assert named == {"1000000001", "1000000002"}           # not stranded

    # explicit mode:bulk must NOT fall back to the API (user opted out)
    api_calls.clear()
    monkeypatch.setattr(E, "_bulk_sig", None)
    store.reset()
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "b.json", innetwork(items=[
        item("97110", [(["1000000003"], "43-1000002", "ein", [(41.0, None)])])])))
    cfg.enrichment.mode = "bulk"
    run_enrichment(cfg, store)
    assert api_calls == []                                  # no API fallback


def test_bulk_enrichment_uses_cache_for_new_npis(cfg, store, tmp_path, monkeypatch):
    # THE speed fix: after the NPPES file is converted to a local parquet once,
    # a later batch of new NPIs must resolve WITHOUT re-reading the bulk file.
    import csv as _csv
    import io as _io
    import zipfile as _zip

    import mrfx.enrich as E
    from mrfx.enrich import _BULK_COLS, _nppes_cache_path, enrich_via_bulk
    monkeypatch.setattr(E, "_bulk_sig", None)
    monkeypatch.setattr(E, "_bulk_read_failures", 0)
    monkeypatch.setattr(E, "_BULK_MIN_FULL_ROWS", 1)
    hdr = [_BULK_COLS[k] for k in
           ("npi", "org", "first", "last", "city", "state", "tax1", "entity", "address", "zip", "phone")]
    buf = _io.StringIO()
    _csv.writer(buf).writerows([hdr,
        ["1000000001", "Alpha PT", "", "", "KC", "MO", "225100000X", "2", "1 St", "64000", "0"],
        ["1000000002", "Beta Rehab", "", "", "STL", "MO", "225100000X", "2", "2 Rd", "63000", "0"]])
    zpath = tmp_path / "npidata.zip"
    with _zip.ZipFile(zpath, "w") as zf:
        zf.writestr("npidata_pfile_2026.csv", buf.getvalue().encode())
    cfg.enrichment.mode = "bulk"
    cfg.enrichment.bulk_csv_path = zpath

    # first NPI arrives -> cache built (file read once), name resolved
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "a.json", innetwork(items=[
        item("97110", [(["1000000001"], "43-1000001", "ein", [(40.0, None)])])])))
    assert enrich_via_bulk(cfg, store) == 1
    assert _nppes_cache_path(store).exists()
    with store.connect() as con:
        assert con.execute("SELECT org_name FROM npi_directory WHERE npi='1000000001'").fetchone()[0] == "Alpha PT"

    # a NEW NPI arrives later — it must resolve from the cache, NOT by re-reading
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "b.json", innetwork(items=[
        item("97112", [(["1000000002"], "43-1000002", "ein", [(55.0, None)])])])))
    opens = []
    real = E._open_bulk_text
    monkeypatch.setattr(E, "_open_bulk_text", lambda p: opens.append(1) or real(p))
    assert enrich_via_bulk(cfg, store) == 1        # resolved 002
    assert opens == []                             # the bulk file was NOT re-read
    with store.connect() as con:
        assert con.execute("SELECT org_name FROM npi_directory WHERE npi='1000000002'").fetchone()[0] == "Beta Rehab"


def test_bulk_enrichment_partial_file_does_not_poison(cfg, store, tmp_path, monkeypatch):
    # A readable but PARTIAL NPPES file (e.g. a weekly incremental grabbed by
    # mistake) must NOT mark the store's NPIs unresolvable. Absences from a file
    # too small to be the full monthly are distrusted, so the NPIs stay retryable
    # and the true monthly file (a new signature) later resolves them.
    import csv as _csv
    import io as _io
    import zipfile as _zip

    import mrfx.enrich as E
    from mrfx.enrich import _BULK_COLS, enrich_via_bulk
    monkeypatch.setattr(E, "_bulk_sig", None)
    # leave _BULK_MIN_FULL_ROWS at its real default (2M) — our fixtures are tiny,
    # so they read as "partial" exactly like a weekly file would
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "pp.json", innetwork(items=[
        item("97110", [(["1000000001"], "43-9000000", "ein", [(40.0, None)])])])))
    hdr = [_BULK_COLS[k] for k in
           ("npi", "org", "first", "last", "city", "state", "tax1", "entity", "address", "zip", "phone")]

    def zip_with(rows, name):
        buf = _io.StringIO()
        _csv.writer(buf).writerows([hdr, *rows])
        zp = tmp_path / name
        with _zip.ZipFile(zp, "w") as zf:
            zf.writestr("npidata_pfile_2026.csv", buf.getvalue().encode())
        return zp

    # a partial file that does NOT contain our NPI at all
    cfg.enrichment.mode = "bulk"
    cfg.enrichment.bulk_csv_path = zip_with(
        [["9999999999", "Someone Else", "", "", "X", "TX", "2", "2", "1 St", "70000", "0"]],
        "weekly.zip")
    assert enrich_via_bulk(cfg, store) == 0            # nothing resolved
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM npi_directory WHERE npi='1000000001'").fetchone()[0] == 0
    # the NPI was NOT poisoned: still un-enriched and still pending
    assert "1000000001" in store.unenriched_npis()
    assert store.enrichment_progress(max_age_seconds=0)["remaining"] == 1

    # now the true (still tiny in-test, so lower the gate) full file resolves it
    monkeypatch.setattr(E, "_BULK_MIN_FULL_ROWS", 1)
    cfg.enrichment.bulk_csv_path = zip_with(
        [["1000000001", "Cornerstone PT", "", "", "Columbus", "OH", "225100000X", "2", "1 St", "43004", "0"]],
        "monthly.zip")
    assert enrich_via_bulk(cfg, store) == 1
    with store.connect() as con:
        assert con.execute(
            "SELECT org_name FROM npi_directory WHERE npi='1000000001'").fetchone()[0] == "Cornerstone PT"


def test_duckdb_memory_override_and_partition_scaling(tmp_path):
    # the config knob overrides the auto memory cap, and the rollup slice size
    # scales to that budget (capped by the module ceiling) so a small-RAM box
    # slices finer instead of OOM-ing; the retry escalator subdivides further.
    from mrfx.store import ROLLUP_PARTITION_ROWS, ROLLUP_ROWS_PER_GB, Store
    s = Store(tmp_path / "st", memory_limit_gb=3)
    assert s._memory_limit_gb == 3
    assert s._rollup_partition_rows(1) == min(ROLLUP_PARTITION_ROWS, 3 * ROLLUP_ROWS_PER_GB)
    assert s._rollup_partition_rows(4) == s._rollup_partition_rows(1) // 4
    assert s._rollup_partition_rows(10 ** 9) >= 1          # floors at 1, never 0
    # a parallel build divides the per-slice budget by the thread count (one
    # un-spillable hash table per thread), so it fits without OOM-ing
    assert s._rollup_partition_rows(1, threads=4) == s._rollup_partition_rows(1) // 4
    assert s._rollup_partition_rows(1, threads=1) == s._rollup_partition_rows(1)
    assert s._rollup_partition_rows(1, threads=10 ** 9) >= 1  # still floors at 1
    # thread division happens BEFORE the module ceiling, so when the ceiling
    # binds (big memory) the two are NOT simply divisor-related — pin the actual
    # divide-then-cap semantics: min(ceiling, gb*rows_per_gb // threads). This is
    # the memory-safe ordering (threads × slice ≤ memory budget).
    big = Store(tmp_path / "st_big", memory_limit_gb=8)        # 8*2M=16M > 15M ceiling
    assert big._rollup_partition_rows(1) == ROLLUP_PARTITION_ROWS            # capped at 15M
    assert big._rollup_partition_rows(1, threads=4) == min(
        ROLLUP_PARTITION_ROWS, (8 * ROLLUP_ROWS_PER_GB) // 4)               # 4M, not 15M//4
    assert big._rollup_partition_rows(1, threads=4) != big._rollup_partition_rows(1) // 4
    assert Store(tmp_path / "st2", memory_limit_gb=0)._memory_limit_gb == 1  # never below 1
    s3 = Store(tmp_path / "st3")                            # auto mode
    assert 2 <= s3._memory_limit_gb <= 12


def test_tin_late_join_matches_full_join(cfg, store):
    # The table's fast path sorts+limits the base rows, THEN attaches names/geo to
    # just the page. It must return byte-identical rows to the original full-join
    # query for every eligible filter/sort — this guards that equivalence.
    from mrfx.api import FilterSet, _TIN_JOINS, _TIN_PROJECTION, order_sql, rel_sql
    data = innetwork(items=[
        item("97110", [(["1000000001"], "43-1000001", "ein", [(40.0, None)]),
                       (["1000000002"], "43-1000002", "ein", [(0.01, None)]),   # placeholder
                       (["1000000003"], "43-1000003", "ein", [(90.0, None)])]),
        item("97112", [(["1000000001"], "43-1000001", "ein", [(55.0, None)]),
                       (["1900000009"], "1900000009", "npi", [(75.0, None)])]),  # tin_type=npi
    ])
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "lj.json", data))
    store.save_npi("1000000001", "Alpha PT", None, None, "KC", "MO", entity_type="NPI-2")
    store.save_npi("1000000002", "Beta Rehab", None, None, "LA", "CA", entity_type="NPI-2")
    store.save_npi("1900000009", "Solo Provider", None, None, "STL", "MO", entity_type="NPI-1")
    store.rebuild_rollups()

    def old(fs, s, dr):
        return f"{rel_sql('tin', fs)} {order_sql(s, dr)} LIMIT ? OFFSET ?"

    def new(fs, s, dr):
        o = order_sql(s, dr)
        return (f"WITH page AS (SELECT *, tin_value AS unit_id FROM rates_by_tin "
                f"WHERE {fs.where} {o} LIMIT ? OFFSET ?) "
                f"SELECT * FROM (SELECT {_TIN_PROJECTION} FROM page t {_TIN_JOINS}) {o}")

    combos = [({}, "negotiated_rate", "desc"), ({}, "negotiated_rate", "asc"),
              ({}, "payer", "asc"), ({}, "npi_count", "desc"), ({}, "billing_code", "asc"),
              ({"cpt": "97110"}, "negotiated_rate", "desc"),
              ({"rate_min": "50"}, "negotiated_rate", "desc"),
              ({"dollar_only": "1"}, "negotiated_rate", "asc")]
    with store.connect() as con:
        for qp, s, dr in combos:
            fs = FilterSet(qp)
            assert not fs.uses_dim_cols
            pr = [*fs.params, 100, 0]
            assert con.execute(old(fs, s, dr), pr).fetchall() == \
                   con.execute(new(fs, s, dr), pr).fetchall(), (qp, s, dr)
    # dim-col filters (state) route through the full-join path and still work
    mo = TestClient(create_app(cfg, store)).get("/api/rates?state=MO").json()
    assert mo["rows"] and all(r["display_name"] for r in mo["rows"])


def test_row_count_cache_invalidates_on_new_data(cfg, store):
    # the /api/rates pagination total is cached per filter for speed; it must
    # never serve a STALE count after more rows land. A rebuild (which every
    # ingest triggers) bumps store.data_generation, part of the cache key.
    g0 = store.data_generation
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "a.json", innetwork(items=[
        item("97110", [(["1000000001"], "43-1000001", "ein", [(40.0, None)])])])))
    assert store.data_generation > g0  # rebuild bumped the generation
    client = TestClient(create_app(cfg, store))
    t1 = client.get("/api/rates?cpt=97110").json()["total"]
    assert t1 == 1
    # a second TIN for the same code arrives — the cached total must update
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "b.json", innetwork(items=[
        item("97110", [(["1000000002"], "43-1000002", "ein", [(55.0, None)])])])))
    t2 = client.get("/api/rates?cpt=97110").json()["total"]
    assert t2 == 2  # not the stale 1


def test_keep_warm_pins_settings_and_stays_correct(tmp_path):
    # serve pins one connection so per-request connections skip the four SET
    # pragmas; they must still SEE those global settings, and a non-warm store
    # (the CLI default) must keep working with no pin.
    from mrfx.store import Store
    s = Store(tmp_path / "w", memory_limit_gb=3, keep_warm=True)
    assert s._pin is not None
    with s.connect() as con:  # fresh connection inherits the pinned globals
        assert con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
        assert str(con.execute(
            "SELECT current_setting('preserve_insertion_order')").fetchone()[0]).lower() == "false"
        assert con.execute("SELECT 1").fetchone()[0] == 1
    s.close(); s.close()          # idempotent
    assert s._pin is None
    s2 = Store(tmp_path / "c")     # CLI default: no pin, still functional
    assert s2._pin is None
    with s2.connect() as con:
        assert con.execute("SELECT 1").fetchone()[0] == 1


def test_therapy_only_ingest_drops_non_therapists(cfg, store, tmp_path, monkeypatch):
    # opt-in: at extraction, keep only PT/OT/SLP-provider rows; drop MDs/DOs/NPs;
    # keep TIN-only rows; ingest all (no drop) when the NPPES cache isn't built.
    import csv as _csv
    import io as _io
    import zipfile as _zip
    from pathlib import Path

    import mrfx.catalog as C
    import mrfx.enrich as E
    from mrfx.enrich import _BULK_COLS, enrich_via_bulk
    monkeypatch.setattr(E, "_bulk_sig", None)
    monkeypatch.setattr(E, "_BULK_MIN_FULL_ROWS", 1)
    # the real NPPES monthly has ~8-9M rows; trust this tiny test cache
    monkeypatch.setattr(C, "_THERAPY_CACHE_MIN_ROWS", 1)
    monkeypatch.setattr(C, "_therapy_npi_cache", {})
    hdr = [_BULK_COLS[k] for k in
           ("npi", "org", "first", "last", "city", "state", "tax1", "entity", "address", "zip", "phone")]
    buf = _io.StringIO()
    _csv.writer(buf).writerows([hdr,
        ["1000000001", "Alpha PT", "", "", "KC", "MO", "225100000X", "2", "1 St", "64000", "0"],
        ["1000000002", "Beta MD", "", "", "KC", "MO", "207R00000X", "1", "2 St", "64000", "0"],
        ["1000000003", "Gamma SLP", "", "", "KC", "MO", "235Z00000X", "2", "3 St", "64000", "0"]])
    zp = tmp_path / "npidata.zip"
    with _zip.ZipFile(zp, "w") as zf:
        zf.writestr("npidata_pfile_2026.csv", buf.getvalue().encode())
    cfg.enrichment.mode = "bulk"
    cfg.enrichment.bulk_csv_path = zp

    # seed a file (before the cache exists) + build the NPPES cache
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "seed.json", innetwork(items=[
        item("97110", [(["1000000001", "1000000002", "1000000003"], "43-1111111", "ein", [(40.0, None)])])])))
    enrich_via_bulk(cfg, store)
    assert (Path(cfg.store_dir) / "nppes_cache.parquet").exists()

    # now ingest a NEW file with therapy_only_ingest ON — MD is dropped
    cfg.therapy_only_ingest = True
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "filt.json", innetwork(items=[
        item("97112", [(["1000000001", "1000000002", "1000000003"], "43-2222222", "ein", [(50.0, None)])])])))
    with store.connect() as con:
        npis = {r[0] for r in con.execute("SELECT npi FROM rates WHERE billing_code='97112'").fetchall()}
        qa = json.loads(con.execute(
            "SELECT qa FROM files WHERE filename='filt.json'").fetchone()[0])
    assert npis == {"1000000001", "1000000003"}      # PT + SLP kept, MD dropped
    assert qa["non_therapy_dropped"] == 1            # the drop is counted for transparency

    # NPI-in-TIN-slot: an NPI sitting in the tin slot (empty npi array) IS
    # classifiable — the MD there is dropped, the PT there is kept (a genuine
    # EIN TIN-only rate, below, is always kept since there's no NPI to judge).
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "slot.json", innetwork(items=[
        item("97116", [([], "1000000002", "npi", [(60.0, None)])])])))     # MD in tin slot
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "slot2.json", innetwork(items=[
        item("97116", [([], "1000000001", "npi", [(61.0, None)]),           # PT in tin slot
                       ([], "43-3333333", "ein", [(62.0, None)])])])))      # genuine EIN TIN-only
    with store.connect() as con:
        tins = {r[0] for r in con.execute(
            "SELECT tin_value FROM rates WHERE billing_code='97116'").fetchall()}
    assert "1000000002" not in tins                  # MD-in-tin-slot dropped
    assert {"1000000001", "433333333"} <= tins       # PT-in-slot + EIN TIN-only kept


def test_therapy_npi_set_distrusts_full_cache_with_no_therapists(tmp_path, monkeypatch):
    # a full-SIZE cache whose taxonomy column is unusable (all-NULL / wrong
    # layout) matches zero therapists -> must return None (keep all), NOT an
    # empty set that would silently drop every real therapist row at ingest.
    import duckdb

    import mrfx.catalog as C
    monkeypatch.setattr(C, "_THERAPY_CACHE_MIN_ROWS", 3)
    monkeypatch.setattr(C, "_therapy_npi_cache", {})
    pq = tmp_path / "nppes_cache.parquet"
    con = duckdb.connect()
    # 4 rows (over the min), every taxonomy_code NULL -> no therapy match
    con.execute("CREATE TABLE t(npi VARCHAR, taxonomy_code VARCHAR)")
    con.execute("INSERT INTO t VALUES ('1','x'),('2',NULL),('3',NULL),('4',NULL)")
    con.execute("UPDATE t SET taxonomy_code=NULL")
    con.execute(f"COPY t TO '{pq}' (FORMAT PARQUET)")
    con.close()
    assert C.therapy_npi_set(pq) is None      # fail safe: keep everything

    # sanity: a cache WITH a real therapist taxonomy returns that npi
    con = duckdb.connect()
    con.execute("CREATE TABLE t(npi VARCHAR, taxonomy_code VARCHAR)")
    con.execute("INSERT INTO t VALUES ('9','2251X00000X'),('8','207R00000X'),"
                "('7','225100000X'),('6',NULL)")
    pq2 = tmp_path / "nppes_cache2.parquet"
    con.execute(f"COPY t TO '{pq2}' (FORMAT PARQUET)")
    con.close()
    got = C.therapy_npi_set(pq2)
    assert got is not None and "9" in got and "7" in got and "8" not in got


def test_apply_nppes_result_tolerates_null_fields(cfg, store):
    # NPPES can emit "basic": null / "taxonomies": null / "addresses": null — a
    # present-but-null container must not throw an uncaught TypeError that aborts
    # the enrichment cycle; the NPI is saved with whatever is available.
    from mrfx.enrich import _apply_nppes_result
    _apply_nppes_result(store, "1234567893", {"results": [{
        "enumeration_type": "NPI-1", "number": "1234567893",
        "basic": None, "taxonomies": None, "addresses": None}]})
    with store.connect() as con:
        row = con.execute("SELECT npi FROM npi_directory WHERE npi='1234567893'").fetchone()
    assert row is not None       # saved, no crash


def test_store_migrates_stale_rollup_schema(cfg, store):
    # a store materialized by an OLDER version lacks the is_therapy column;
    # reopening must self-heal (rebuild) so the dashboard doesn't 500 on every
    # query until an ingest happens to trigger a rebuild.
    from mrfx.store import Store
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "m.json", innetwork(items=[
        item("97110", [(["1000000001"], "43-1000001", "ein", [(40.0, None)])])])))
    with store.connect() as con:
        con.execute("ALTER TABLE tin_directory_tbl DROP COLUMN is_therapy")
        assert "is_therapy" not in {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='tin_directory_tbl'").fetchall()}
    s2 = Store(cfg.store_dir)  # reopen triggers the schema migration
    with s2.connect() as con:
        assert "is_therapy" in {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='tin_directory_tbl'").fetchall()}
    assert TestClient(create_app(cfg, s2)).get("/api/rates?grain=tin").status_code == 200


def test_therapy_only_filter(cfg, store):
    # keep only PT/OT/SLP providers & therapy practices (by NPPES taxonomy);
    # drop the MDs/DOs/NPs who merely billed a 97xxx code.
    data = innetwork(items=[item("97110", [
        (["1000000001"], "43-1000001", "ein", [(40.0, None)]),   # PT  2251
        (["1000000002"], "43-1000002", "ein", [(120.0, None)]),  # MD  207R
        (["1000000003"], "43-1000003", "ein", [(55.0, None)]),   # SLP 235Z
    ])])
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "t.json", data))
    store.save_npi("1000000001", "Alpha PT",  "225100000X", None, "KC", "MO", entity_type="NPI-2")
    store.save_npi("1000000002", "Beta MD",   "207R00000X", None, "KC", "MO", entity_type="NPI-1")
    store.save_npi("1000000003", "Gamma SLP", "235Z00000X", None, "KC", "MO", entity_type="NPI-2")
    store.rebuild_rollups()
    with store.connect() as con:
        flags = dict(con.execute("SELECT display_name, is_therapy FROM tin_directory").fetchall())
    assert flags["Alpha PT"] and flags["Gamma SLP"] and not flags["Beta MD"]
    c = TestClient(create_app(cfg, store))
    assert c.get("/api/rates?grain=tin").json()["total"] == 3
    ther = c.get("/api/rates?grain=tin&therapy_only=1").json()
    assert {r["display_name"] for r in ther["rows"]} == {"Alpha PT", "Gamma SLP"}
    npith = c.get("/api/rates?grain=npi&therapy_only=1").json()   # NPI grain too
    assert {r["display_name"] for r in npith["rows"]} == {"Alpha PT", "Gamma SLP"}


def test_therapy_filter_excludes_hospitals_and_md_majority_groups(cfg, store):
    # STRICT practice rule: majority of identified NPIs are therapy (or a
    # therapy-clinic org NPI), AND no hospital-class NPI. The old any-member
    # rule let hospital systems and physician groups with a single employed PT
    # into leads/benchmarks.
    data = innetwork(items=[item("97110", [
        # hospital system: 1 PT among 3 MDs + a hospital org NPI
        (["1000000001", "1000000002", "1000000003", "1000000004", "1000000005"],
         "43-2000001", "ein", [(90.0, None)]),
        # physician group: 1 PT + 2 MDs (therapy minority)
        (["1000000006", "1000000007", "1000000008"], "43-2000002", "ein", [(80.0, None)]),
        # true private practice: 2 PTs + 1 front-office NP (therapy majority)
        (["1000000009", "1000000010", "1000000011"], "43-2000003", "ein", [(60.0, None)]),
        # all-PT staff but under a REHAB HOSPITAL's NPI umbrella -> veto
        (["1000000012", "1000000013", "1000000014"], "43-2000004", "ein", [(70.0, None)]),
        # tiny clinic: 1 PT + 1 NP (tie) but carries a PT-clinic org NPI
        (["1000000015", "1000000016", "1000000017"], "43-2000005", "ein", [(50.0, None)]),
    ])])
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "strict.json", data))
    seed = [
        # hospital system
        ("1000000001", "Hosp PT", "225100000X"), ("1000000002", "Hosp MD1", "207R00000X"),
        ("1000000003", "Hosp MD2", "207R00000X"), ("1000000004", "Hosp MD3", "207R00000X"),
        ("1000000005", "General Hospital", "282N00000X"),
        # physician group
        ("1000000006", "Grp PT", "225100000X"), ("1000000007", "Grp MD1", "207R00000X"),
        ("1000000008", "Grp MD2", "207R00000X"),
        # private practice
        ("1000000009", "Priv PT1", "225100000X"), ("1000000010", "Priv PT2", "225100000X"),
        ("1000000011", "Priv NP", "363L00000X"),
        # all-PT under a rehab-hospital NPI
        ("1000000012", "RH PT1", "225100000X"), ("1000000013", "RH PT2", "225100000X"),
        ("1000000014", "Rehab Hospital", "283X00000X"),
        # tiny clinic w/ clinic org code
        ("1000000015", "Tiny PT", "225100000X"), ("1000000016", "Tiny NP", "363L00000X"),
        ("1000000017", "Tiny PT Clinic", "261QP2300X"),
    ]
    for npi, name, tax in seed:
        store.save_npi(npi, name, tax, None, "KC", "MO", entity_type="NPI-2")
    store.rebuild_rollups()
    with store.connect() as con:
        flags = dict(con.execute("SELECT tin_value, is_therapy FROM tin_directory").fetchall())
        hosp = dict(con.execute("SELECT tin_value, has_hospital FROM tin_directory").fetchall())
    assert not flags["432000001"]        # hospital system: minority + hospital veto
    assert hosp["432000001"] is True
    assert not flags["432000002"]        # physician group: therapy minority
    assert flags["432000003"]            # private practice: 2 of 3 therapy
    assert not flags["432000004"]        # all-PT staff but hospital-class NPI -> veto
    assert hosp["432000004"] is True
    assert flags["432000005"]            # tie, but therapy-clinic org NPI qualifies

    # the filter flows through to the API therapy toggle at tin grain
    c = TestClient(create_app(cfg, store))
    ther = c.get("/api/rates?grain=tin&therapy_only=1&cpt=97110").json()
    kept = {r["tin_value"] for r in ther["rows"]}
    assert kept & {"432000003", "432000005"}
    assert not kept & {"432000001", "432000002", "432000004"}


def test_benchmark_subjects_search_and_cap(cfg, store):
    # the subject picker is a server-side typeahead: capped so it stays fast on a
    # big store, and filterable by org name or TIN.
    data = innetwork(items=[item("97110", [
        ([f"1{i:09d}"], f"43{i:07d}"[:9], "ein", [(40.0 + i, None)]) for i in range(120)
    ])])
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "s.json", data))
    for i in range(120):
        store.save_npi(f"1{i:09d}", ("Acme PT" if i % 2 else "Beacon Rehab") + f" {i}",
                       None, None, "KC", "MO", entity_type="NPI-2")
    store.rebuild_rollups()
    c = TestClient(create_app(cfg, store))
    default = c.get("/api/benchmark/subjects?limit=50").json()
    assert 0 < len(default["tins"]) <= 50            # capped, not all 120
    acme = c.get("/api/benchmark/subjects?q=acme").json()
    assert acme["tins"] and all("acme" in t["display_name"].lower() for t in acme["tins"])
    tinq = c.get("/api/benchmark/subjects?q=430000005").json()
    assert any(t["tin_value"].startswith("430000005") for t in tinq["tins"])


def test_latest_month_uses_newest_file_per_contract(cfg, store):
    # "latest" as-of: each contract uses its NEWEST file, so every payer appears
    # at its freshest vintage regardless of which calendar month is newest —
    # without the double-count of pooling all months.
    from mrfx.benchmark import _entity_rates, subject_payers

    def one(payer, month, rate):
        d = innetwork(payer=payer, month=f"{month}-01", items=[
            item("97110", [(["1000000001"], "43-5000001", "ein", [(rate, None)])])])
        return make_fixture(cfg.inbox_dir, f"{payer.replace(' ','')}_{month}.json", d)

    # Payer A: subject priced in May ($50) AND June ($60, a raise). Payer B:
    # ONLY May ($40) -- its newest file is May.
    ingest_file(cfg, store, one("Alpha Health", "2026-05", 50.0))
    ingest_file(cfg, store, one("Alpha Health", "2026-06", 60.0))
    ingest_file(cfg, store, one("Beta Plans", "2026-05", 40.0))
    store.rebuild_rollups()
    tins = ["435000001"]

    # PINNED June: only Alpha has a June file -> just its June rate 60.
    assert _entity_rates(store, tins, {"month": "2026-06"}) == {"97110": 60.0}
    assert subject_payers(store, "435000001", {"month": "2026-06"}) == ["Alpha Health"]
    # PINNED May: Alpha's May 50 + Beta's May 40 -> entity median 45.
    assert _entity_rates(store, tins, {"month": "2026-05"}) == {"97110": 45.0}

    # LATEST: Alpha at its NEWEST (June 60, NOT pooled with May's 50) AND Beta at
    # its newest (May 40) -> median(60, 40) = 50. Both payers present, each at
    # its freshest vintage regardless of which calendar month is newest.
    assert _entity_rates(store, tins, {"month": "latest"}) == {"97110": 50.0}
    assert subject_payers(store, "435000001", {"month": "latest"}) == ["Alpha Health", "Beta Plans"]

    # API accepts "latest"; the report labels it, not a fake date
    c = TestClient(create_app(cfg, store))
    r = c.post("/api/report/pitch", json={"subject": "435000001",
               "market": {"month": "latest", "allow_national": True}})
    assert r.status_code == 200 and "latest available" in r.text

    # rate CHANGES compare two real months: "latest" is lexically > every
    # YYYY-MM, so it silently fabricated a comparison — must 422 instead
    bad = c.post("/api/changes", json={"market": {"month": "latest"}})
    assert bad.status_code == 422 and "specific months" in bad.json()["detail"]
    ok = c.post("/api/changes", json={"market": {"month": "2026-06"}})
    assert ok.status_code == 200 and ok.json()["new_month"] == "2026-06"


def test_latest_supersedes_restructured_variants(cfg, store):
    # A payer's newer file that RESTRUCTURES a line — drops a modifier variant,
    # widens the POS set — fully supersedes the older publication. Regression:
    # supersession used to be keyed on the full variant (modifier/POS) set, so
    # a June variant with no July twin lingered forever and dragged the median
    # (June ''+GP @34.50 pooled with July '' @37.95 -> 36.22, not 37.95).
    from mrfx.benchmark import _entity_rates

    def _pos_item(code, tin, rate, service_codes):
        return {"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                "billing_code_type_version": "2026", "billing_code": code,
                "negotiated_rates": [{
                    "provider_groups": [{"npi": [1000000002],
                                         "tin": {"type": "ein", "value": tin}}],
                    "negotiated_prices": [{"negotiated_type": "negotiated",
                                           "negotiated_rate": rate,
                                           "service_code": service_codes,
                                           "billing_class": "professional"}]}]}

    june = innetwork(month="2026-06-01", items=[
        # modifier drift: base + GP variants in June…
        item("97110", [(["1000000001"], "43-6000001", "ein",
                        [(34.5, None), (34.5, ["GP"])])]),
        # POS drift: '11' in June…
        _pos_item("97112", "43-6000002", 40.0, ["11"]),
    ])
    july = innetwork(month="2026-07-01", items=[
        # …July publishes ONLY the base variant, at the raise
        item("97110", [(["1000000001"], "43-6000001", "ein", [(37.95, None)])]),
        # …July widens to '11|12', at the raise
        _pos_item("97112", "43-6000002", 45.0, ["11", "12"]),
    ])
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "drift_june.json", june))
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "drift_july.json", july))
    store.rebuild_rollups()

    assert _entity_rates(store, ["436000001"], {"month": "latest"}) == {"97110": 37.95}
    assert _entity_rates(store, ["436000002"], {"month": "latest"}) == {"97112": 45.0}
    # pinned months still see each vintage exactly as published
    assert _entity_rates(store, ["436000001"], {"month": "2026-06"}) == {"97110": 34.5}
    assert _entity_rates(store, ["436000002"], {"month": "2026-06"}) == {"97112": 40.0}


def test_payer_comparison_math_and_report(cfg, store):
    # Negotiate view: subject vs ONE payer's market, named comparables as
    # columns, plus what the subject's OTHER payers pay — all hand-checked.
    from mrfx.benchmark import compute_payer_comparison, render_payer_compare_report

    # the negotiating payer: subject @50, comparable @75, peers @60/70/80
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "nego.json", innetwork(items=[
        item("97110", [
            (["1000000001"], "43-9111111", "ein", [(50.0, None)]),   # subject
            (["1000000002"], "43-9222222", "ein", [(75.0, None)]),   # comparable
            (["1000000003"], "43-9333333", "ein", [(60.0, None)]),
            (["1000000004"], "43-9444444", "ein", [(70.0, None)]),
            (["1000000005"], "43-9555555", "ein", [(80.0, None)]),
        ])])))
    # a different payer paying the SUBJECT 66 for the same code (leverage line)
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "other.json", innetwork(
        payer="Other Insurance Co",
        items=[item("97110", [(["1000000001"], "43-9111111", "ein", [(66.0, None)])])])))

    market = {"month": "2026-06"}
    comp = compute_payer_comparison(store, "439111111", "Testco",
                                    market, comparables=["43-9222222"])
    (r,) = comp["rows"]
    assert r["subject_rate"] == 50.0
    # peers exclude the subject: [60, 70, 75, 80] -> p50 72.5, gap 22.5.
    # UPLIFT semantics, subject denominator: (72.5-50)/50 = +45.0%
    assert r["p50"] == 72.5 and r["gap_to_median"] == 22.5
    assert r["gap_to_median_pct"] == 45.0
    assert r["subject_percentile"] == 0          # below every peer
    assert r["comp_rates"] == [75.0] and r["best_comparable"] == 75.0
    assert r["other_payers_rate"] == 66.0 and r["n_other_payers"] == 1
    s = comp["summary"]
    assert s["n_codes"] == 1 and s["n_below_median"] == 1
    assert s["headline_percentile"] == 0
    # other payers pay (66-50)/50 = +32.0% more (subject denominator everywhere)
    assert s["avg_other_payer_diff_pct"] == 32.0
    (cs,) = comp["comparables"]
    assert cs["n_shared_codes"] == 1 and cs["n_paid_more"] == 1
    assert cs["median_premium_pct"] == 50.0      # (75-50)/50

    # printable report: needs a state (or explicit national), carries the asks
    html_doc = render_payer_compare_report(
        cfg, store, compute_payer_comparison(
            store, "439111111", "Testco",
            {**market, "allow_national": True}, comparables=["43-9222222"]))
    # comparable label: dash-stripped for the mask check -> "439222222" (a
    # valid-EIN-prefix TIN stays visible; an SSN-pattern one would be masked)
    for needle in ("Ask scenarios", "439222222", "Testco",
                   "METHODOLOGY", "$72.50", "$75.00"):
        assert needle in html_doc, needle

    # API surface: no month = latest available; the response records the
    # explicit vintage it defaulted to (never a silent blank)
    c = TestClient(create_app(cfg, store))
    auto = c.post("/api/negotiate/compare",
                  json={"subject": "439111111", "payer": "Testco",
                        "market": {}})
    assert auto.status_code == 200
    assert auto.json()["market"]["month"] == "latest"
    ok = c.post("/api/negotiate/compare",
                json={"subject": "439111111", "payer": "Testco",
                      "market": market, "comparables": ["43-9222222"]})
    assert ok.status_code == 200 and ok.json()["rows"][0]["p50"] == 72.5
    # a string comparables body must not char-split into garbage columns
    guard = c.post("/api/negotiate/compare",
                   json={"subject": "439111111", "payer": "Testco",
                         "market": market, "comparables": "Acme"})
    assert guard.status_code == 200 and guard.json()["comparables"] == []

    # OTHER-payers column follows the two-level entity rule: per-TIN median
    # first, then across TINs/payers. Subject = two TINs grouped by NPPES name;
    # other payer pays TIN A [90,100,110] (3 base-modifier variants -> median
    # 100) and TIN B [60] -> entity rate median(100,60)=80. Pooling raw rows
    # would say 95.
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "twotin.json", innetwork(
        payer="Other Insurance Co",
        items=[item("97140", [
            (["1000000021"], "43-9777771", "ein", [(90.0, None), (100.0, ["GP"]), (110.0, ["GO"])]),
            (["1000000022"], "43-9777772", "ein", [(60.0, None)]),
        ]),
        item("97110", [(["1000000021"], "43-9777771", "ein", [(55.0, None)])])])))
    # the NEGOTIATING payer prices the same org on 97110 only — 97140 has no
    # subject rate with Testco, so only 97110 shows; other_payers uses 97110=55?
    # No: subject must be priced on the code with the negotiating payer for the
    # row to exist. Price 97140 with Testco for TIN A too:
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "twotin_t.json", innetwork(
        items=[item("97140", [(["1000000021"], "43-9777771", "ein", [(70.0, None)]),
                              (["1000000023"], "43-9888888", "ein", [(75.0, None)])])])))
    store.save_npi("1000000021", "TwoTin Rehab", "225100000X", None, "KC", "MO", entity_type="NPI-2")
    store.save_npi("1000000022", "TwoTin Rehab", "225100000X", None, "KC", "MO", entity_type="NPI-2")
    store.rebuild_rollups()
    comp2 = compute_payer_comparison(store, "TwoTin Rehab", "Testco", market)
    r97140 = next(x for x in comp2["rows"] if x["billing_code"] == "97140")
    assert r97140["other_payers_rate"] == 80.0   # two-level, NOT the pooled 95.0


def test_config_duckdb_memory_knob(tmp_path):
    from mrfx.config import load_mrfx_config
    p = tmp_path / "mrfx.yaml"
    p.write_text("duckdb_memory_gb: 8\n")
    assert load_mrfx_config(p).duckdb_memory_gb == 8
    assert load_mrfx_config(tmp_path / "missing.yaml").duckdb_memory_gb is None  # default


def test_entity_grain_median_excludes_placeholder_only_tin(cfg, store):
    # A multi-TIN entity where one member published only a $0.01 placeholder and
    # another a real $80 must report the REAL rate as its entity median, not the
    # midpoint (40). Guards api._ENTITY_REL's cross-TIN re-median against folding
    # placeholder-only member TINs back in — which made the Explorer disagree
    # with the benchmark (the invariant: one store never shows two medians).
    data = innetwork(items=[item("97110", [
        (["1111111111"], "43-1000001", "ein", [(80.0, None)]),   # real rate
        (["1222222222"], "43-1000002", "ein", [(0.01, None)]),   # placeholder only
    ])])
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "ph.json", data))
    for npi in store.unenriched_npis():
        store.save_npi(npi, "Regional Rehab Group", "225100000X", "PT", "KC", "MO", entity_type="NPI-2")
    store.rebuild_rollups()
    client = TestClient(create_app(cfg, store))
    rows = client.get("/api/rates?grain=entity").json()["rows"]
    chain = next(x for x in rows if x["display_name"] == "Regional Rehab Group")
    assert chain["tin_count"] == 2
    assert chain["negotiated_rate"] == 80.0   # NOT median(80, 0.01) == 40


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
    monkeypatch.setattr(store, "rebuild_rollups", lambda *a, **k: calls.append(1))
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


def test_leads_state_filter_shows_matched_state_for_multistate_tin(cfg, store):
    # A billing TIN spanning AR+MO matches a MO filter (it HAS a MO location);
    # it must DISPLAY MO — not its alphabetically-first state (AR) — and expose
    # the full list + multi_state flag so it's never silently mislabeled. A
    # purely-AR TIN must not appear at all.
    from mrfx.leads import compute_leads, leads_csv

    def r(t, n, code, rate):
        return dict(payer="P", tin_value=t, tin_type="ein", npi=n, source_file="f.json",
            billing_code=code, billing_code_type="CPT", discipline="pt", is_timed=True,
            billing_class="professional", negotiated_rate=rate, negotiated_type="negotiated",
            is_dollar_rate=True, modifier_set=[], service_code=["11"], file_month="2026-06",
            last_updated_on="2026-06-01", expiration_date=None, schema_version="2.0.0",
            tin_is_really_npi=False, state="MO")
    recs = []
    for code, rate in [("97110", 40), ("97112", 50), ("97140", 60)]:
        recs += [r("450000001", "1400000001", code, rate),        # multi: AR npi
                 r("450000001", "1400000002", code, rate),        # multi: MO npi
                 r("450000002", "1400000003", code, rate + 30),   # pure MO (peer)
                 r("450000003", "1400000004", code, rate)]        # pure AR
    with store.rates_part_writer("f.json") as w:
        w.write_batch(recs)
    store.upsert_file("f.json", payer="P", file_type="in_network", status="done",
                      rows_emitted=len(recs), finished_at="2026-07-01 10:00:00")
    store.save_npi("1400000001", "Multi Group", "2251C2600X", None, "BENTONVILLE", "AR", entity_type="NPI-2")
    store.save_npi("1400000002", "Multi Group", "2251C2600X", None, "KANSAS CITY", "MO", entity_type="NPI-2")
    store.save_npi("1400000003", "KC PT", "2251C2600X", None, "KANSAS CITY", "MO", entity_type="NPI-2")
    store.save_npi("1400000004", "Arkansas PT", "2251C2600X", None, "LITTLE ROCK", "AR", entity_type="NPI-2")
    store.rebuild_rollups()

    res = compute_leads(store, {"month": "latest", "state": "MO"},
                        threshold_percentile=99, min_codes=1)
    tins = {L["tin_value"] for L in res["leads"]}
    assert "450000003" not in tins  # purely-AR org correctly excluded
    multi = next(L for L in res["leads"] if L["tin_value"] == "450000001")
    assert multi["state"] == "MO"               # shows the matched state, not "AR"
    assert multi["multi_state"] is True
    assert set(multi["states"]) == {"AR", "MO"}  # full list preserved
    assert "all_states" in leads_csv(store, res)  # export is transparent too


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


def test_npi_typed_tin_never_blends_with_real_ein(cfg, store):
    # a 9-digit value typed 'npi' colliding with a real EIN string is TWO
    # identities: bool_or-folding them made one hybrid row whose median
    # (92.0) matched neither, and NOT tin_is_really_npi then hid BOTH — the
    # real EIN practice vanished from every benchmark surface
    from mrfx.benchmark import _entity_rates

    a = innetwork(month="2026-06-01", items=[
        item("97110", [(["1111111111"], "43-1111111", "ein", [(85.0, None)])])])
    b = innetwork(month="2026-06-01", items=[
        item("97110", [(["3333333333"], "431111111", "npi", [(99.0, None)])])])
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "ein.json", a))
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "npityped.json", b))
    store.rebuild_rollups()
    with store.connect() as con:
        rows = con.execute(
            "SELECT tin_is_really_npi, negotiated_rate FROM rates_by_tin "
            "WHERE tin_value = '431111111' ORDER BY negotiated_rate").fetchall()
    assert rows == [(False, 85.0), (True, 99.0)]  # two rows, no blend
    # the EIN practice is present in market math at its real rate
    assert _entity_rates(store, ["431111111"], {"month": "latest"}) == {"97110": 85.0}
