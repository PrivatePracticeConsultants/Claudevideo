"""The plan/network dimension.

A payer publishes ONE rate file shared by many plans, and the plan name lives
only in its table-of-contents. Without capturing it, a payer's narrow-network
and broad-PPO books blend into a single median that matches neither.
"""

import gzip
import json

import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.fetch import dedup_key, expand_toc, expand_toc_with_plans

TOC = {
    "reporting_entity_name": "Testco",
    "reporting_entity_type": "health insurance issuer",
    "reporting_structure": [
        {   # a NARROW network: two plans, one shared file
            "reporting_plans": [
                {"plan_name": "Testco Narrow HMO", "plan_id": "111",
                 "plan_id_type": "HIOS", "plan_market_type": "individual"},
                {"plan_name": "Testco Narrow HMO Plus", "plan_id": "112",
                 "plan_id_type": "HIOS", "plan_market_type": "individual"},
            ],
            "in_network_files": [
                {"description": "in-network", "location": "https://p.example/narrow.json.gz"}],
        },
        {   # a BROAD PPO: its own file
            "reporting_plans": [
                {"plan_name": "Testco Broad PPO", "plan_id": "222",
                 "plan_id_type": "HIOS", "plan_market_type": "group"}],
            "in_network_files": [
                {"description": "in-network", "location": "https://p.example/broad.json.gz"}],
        },
    ],
}


def _write_toc(tmp_path, doc=TOC):
    p = tmp_path / "toc.json.gz"
    p.write_bytes(gzip.compress(json.dumps(doc).encode()))
    return p


def test_toc_expansion_captures_the_plan_each_file_serves(tmp_path):
    urls, truncated, plans = expand_toc_with_plans(_write_toc(tmp_path), 100)
    assert len(urls) == 2 and not truncated
    by_url = {}
    for p in plans:
        by_url.setdefault(p["url"], set()).add(p["plan_name"])
    assert by_url["https://p.example/narrow.json.gz"] == {
        "Testco Narrow HMO", "Testco Narrow HMO Plus"}, "one file, MANY plans"
    assert by_url["https://p.example/broad.json.gz"] == {"Testco Broad PPO"}
    assert all(p.get("market_type") in ("individual", "group") for p in plans)
    # the plain expander keeps working unchanged for every existing caller
    assert expand_toc(_write_toc(tmp_path), 100)[0] == urls


def test_a_toc_without_plans_still_expands(tmp_path):
    """Plenty of payers publish a bare file list. That must expand exactly as
    before and simply carry no plan tags — never an error."""
    bare = {"reporting_structure": [
        {"in_network_files": [{"location": "https://p.example/only.json.gz"}]}]}
    urls, _t, plans = expand_toc_with_plans(_write_toc(tmp_path, bare), 100)
    assert urls == ["https://p.example/only.json.gz"]
    assert plans == []


def _seed_two_networks(store):
    """Same payer, same code, two files: the narrow book pays 28, the broad 40.
    Pooled they read 34 — a number neither network actually pays."""
    def row(tin, npi, rate, src):
        return dict(payer="Testco", tin_value=tin, tin_type="ein", npi=npi,
                    source_file=src, billing_code="97110", billing_code_type="CPT",
                    discipline="PT", is_timed=True, billing_class="professional",
                    negotiated_rate=rate, negotiated_type="negotiated",
                    is_dollar_rate=True, billing_code_modifier=[], service_code=["11"],
                    file_month="2026-06", last_updated_on="2026-06-01",
                    expiration_date=None, schema_version="2.0.0",
                    tin_is_really_npi=False, state="MO")
    with store.rates_part_writer("narrow.json") as w:
        w.write_batch([row("431234567", "1417594896", 28.0, "narrow.json"),
                       row("437654321", "1999999992", 28.0, "narrow.json")])
    with store.rates_part_writer("broad.json") as w:
        w.write_batch([row("431234567", "1417594896", 40.0, "broad.json"),
                       row("437654321", "1999999992", 40.0, "broad.json")])
    store.save_npis_bulk([
        dict(npi=n, org_name=f"P{i}", entity_type="NPI-2",
             taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X",
             city="StL", state="MO", address="x", zip="63103", phone=None)
        for i, n in enumerate(("1417594896", "1999999992"))])
    store.rebuild_rollups()
    # the queue rows the plan index joins through
    store.enqueue_urls([("https://p.example/narrow.json.gz",
                         dedup_key("https://p.example/narrow.json.gz")),
                        ("https://p.example/broad.json.gz",
                         dedup_key("https://p.example/broad.json.gz"))])
    for url, fname in (("https://p.example/narrow.json.gz", "narrow.json"),
                       ("https://p.example/broad.json.gz", "broad.json")):
        with store.write_lock, store.connect() as con:
            con.execute("UPDATE url_queue SET filename = ?, status = 'done' "
                        "WHERE dedup_key = ?", [fname, dedup_key(url)])
        # the payer name lives on `files`, not on the queue
        store.upsert_file(fname, payer="Testco", status="done", rows_emitted=2,
                          file_type="in_network", last_updated_on="2026-06-01")
    store.save_plan_index([
        {"dedup_key": dedup_key("https://p.example/narrow.json.gz"),
         "plan_name": "Testco Narrow HMO", "plan_id": "111",
         "plan_id_type": "HIOS", "market_type": "individual"},
        {"dedup_key": dedup_key("https://p.example/broad.json.gz"),
         "plan_name": "Testco Broad PPO", "plan_id": "222",
         "plan_id_type": "HIOS", "market_type": "group"},
    ])
    return store


def test_plan_scope_unblends_a_payers_two_networks(cfg, store):
    from mrfx.benchmark import compute_benchmark

    _seed_two_networks(store)
    market = {"month": "2026-06", "therapy_only": False}

    pooled = compute_benchmark(store, "431234567", market)
    p50_pooled = next(r for r in pooled["rows"] if r["billing_code"] == "97110")["p50"]

    narrow = compute_benchmark(store, "431234567", {**market, "plan": "Testco Narrow HMO"})
    broad = compute_benchmark(store, "431234567", {**market, "plan": "Testco Broad PPO"})
    n50 = next(r for r in narrow["rows"] if r["billing_code"] == "97110")["p50"]
    b50 = next(r for r in broad["rows"] if r["billing_code"] == "97110")["p50"]

    assert n50 == 28.0, "the narrow book on its own"
    assert b50 == 40.0, "the broad book on its own"
    assert n50 < p50_pooled < b50, (
        "the pooled median sits between the two networks and matches neither — "
        "the blend this dimension exists to expose")


def test_plan_coverage_flags_a_blended_payer(cfg, store):
    from mrfx.benchmark import plan_coverage

    _seed_two_networks(store)
    cov = plan_coverage(store)
    assert cov["loaded"]
    testco = next(p for p in cov["by_payer"] if p["payer"] == "Testco")
    assert testco["n_plans"] == 2
    assert testco["blended"] is True, "two plans in one payer's book must be flagged"
    assert sorted(testco["market_types"]) == ["group", "individual"]
    assert "blends networks" in cov["note"]

    client = TestClient(create_app(cfg, store))
    assert client.get("/api/plans").json()["loaded"] is True


def test_an_unknown_plan_refuses_instead_of_returning_everything(cfg, store):
    """Silently ignoring an unknown plan would hand back the POOLED market
    under a plan's name — the worst possible failure for this feature."""
    from mrfx.benchmark import BenchmarkError, compute_benchmark

    _seed_two_networks(store)
    with pytest.raises(BenchmarkError, match="no ingested files are tagged"):
        compute_benchmark(store, "431234567",
                          {"month": "2026-06", "plan": "Some Other Plan"})


def test_a_plan_scoped_report_never_claims_the_payers_other_files(cfg, store):
    """The footer is the provenance record. Under a plan scope it must name the
    scope and list ONLY that plan's files — citing the payer's whole catalogue
    under a narrow-network number is exactly the false claim it exists to stop.
    The resolved file list is implementation detail and must not leak into the
    market definition line."""
    from mrfx.benchmark import compute_benchmark, methodology_footer

    _seed_two_networks(store)
    bench = compute_benchmark(store, "431234567",
                              {"month": "2026-06", "therapy_only": False,
                               "plan": "Testco Narrow HMO"})
    assert "_plan_files" not in bench["market"], "internal key leaked to the client"
    assert bench["market"]["plan"] == "Testco Narrow HMO"

    foot = methodology_footer(store, bench)
    assert "Plan scope: Testco Narrow HMO" in foot
    assert "narrow.json" in foot
    assert "broad.json" not in foot, "cited a file this number never read"

    pooled = compute_benchmark(store, "431234567",
                               {"month": "2026-06", "therapy_only": False})
    pooled_foot = methodology_footer(store, pooled)
    assert "Plan scope" not in pooled_foot
    assert "broad.json" in pooled_foot and "narrow.json" in pooled_foot


def test_a_view_that_cannot_scope_refuses_the_plan_filter(cfg, store):
    """Ignoring a plan a view can't honor would return the POOLED market under
    that plan's name. Every view either scopes or refuses — never pretends."""
    from mrfx.benchmark import BenchmarkError
    from mrfx.leads import compute_leads

    _seed_two_networks(store)
    with pytest.raises(BenchmarkError, match="can't be scoped to a plan"):
        compute_leads(store, {"month": "2026-06", "plan": "Testco Narrow HMO"})


def test_the_negotiate_and_ratecard_views_scope_to_a_plan(cfg, store):
    """The two views a consultant actually quotes from must see one network."""
    from mrfx.benchmark import compute_payer_negotiation
    from mrfx.schedule import compute_fee_schedule

    _seed_two_networks(store)
    for plan, expect in (("Testco Narrow HMO", 28.0), ("Testco Broad PPO", 40.0)):
        m = {"month": "2026-06", "therapy_only": False, "plan": plan}
        card = compute_fee_schedule(store, "431234567", m)
        row = next(r for r in card["codes"] if r["billing_code"] == "97110")
        assert row["rates"]["Testco"]["rate"] == expect

        neg = compute_payer_negotiation(store, "431234567", m)
        sec = next(s for s in neg["sections"] if s["payer"] == "Testco")
        assert next(r for r in sec["benchmark"]["rows"]
                    if r["billing_code"] == "97110")["p50"] == expect
