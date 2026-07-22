"""Regression tests for the analytical tab upgrades: market overview, payer
leaderboard (code), per-code summary + below-Medicare, leads worst-payer, and
changes by-payer. The queries must RANK/FLAG correctly, not just run."""

import itertools

from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.leads import compute_leads
from mrfx.monitor import compute_rate_changes
from mrfx.overview import market_overview


def _seed(store, payer_base=None, codes=("97110", "97112", "97140", "97530"),
          month="2026-06", tins=8):
    """Write rates: each payer prices every code across `tins` practices, at a
    payer-specific base + a per-practice bump (so a real spread exists)."""
    payer_base = payer_base or {"Aetna": 55, "BCBS": 60, "United": 30}
    rows = []
    for p, c in itertools.product(payer_base, codes):
        for j in range(tins):
            rows.append(dict(
                payer=p, tin_value="43%07d" % j, tin_type="ein", npi="1%09d" % j,
                source_file=f"{month}.json", billing_code=c, billing_code_type="CPT",
                discipline="pt", is_timed=True, billing_class="professional",
                negotiated_rate=float(payer_base[p] + j), negotiated_type="negotiated",
                is_dollar_rate=True, modifier_set=[], service_code=["11"],
                file_month=month, last_updated_on=f"{month}-01", expiration_date=None,
                schema_version="2.0.0", tin_is_really_npi=False, state=None))
    with store.rates_part_writer(f"{month}.json") as w:
        w.write_batch(rows)
    store.rebuild_rollups()


def test_market_overview_ranks_payers_above_and_below_market(store):
    _seed(store)  # BCBS pays most, United least
    ov = market_overview(store)
    assert ov["payers"] == 3 and ov["codes"] == 4
    idx = {x["payer"]: x["index"] for x in ov["payer_index"]}
    # index = payer median / market median, per code, medianed
    assert idx["BCBS"] > 1.0 > idx["United"]           # above vs below market
    assert idx["United"] < 0.7                          # United ~half of market
    # ranked best-paying first
    assert [x["payer"] for x in ov["payer_index"]][0] == "BCBS"
    # thin-sample guard: a payer pricing < 3 codes is omitted
    assert all(x["codes"] >= 3 for x in ov["payer_index"])
    # practices is a DISTINCT count (8 seeded), not sum-over-codes (would be 32)
    assert all(x["practices"] == 8 for x in ov["payer_index"])


def test_code_payer_rank_orders_by_median(cfg, store):
    _seed(store)
    client = TestClient(create_app(cfg, store))
    d = client.get("/api/code/97110").json()
    pr = d["payer_rank"]
    assert [p["payer"] for p in pr] == ["BCBS", "Aetna", "United"]  # high → low
    assert pr[0]["median_rate"] > pr[-1]["median_rate"]
    assert all(p["n_entities"] == 8 for p in pr)                    # practice count


def test_summary_by_code_breaks_out_each_code(cfg, store):
    _seed(store)
    client = TestClient(create_app(cfg, store))
    s = client.get("/api/summary").json()
    codes = {c["billing_code"] for c in s["by_code"]}
    assert codes == {"97110", "97112", "97140", "97530"}
    for c in s["by_code"]:
        assert c["median"] is not None and c["p25"] <= c["median"] <= c["p75"]
        assert c["entities"] == 8


def test_leads_worst_payer_names_the_underpayer(store):
    _seed(store)  # United underpays every practice
    res = compute_leads(store, {}, threshold_percentile=40, min_codes=2, limit=10)
    assert res["count"] >= 1
    for lead in res["leads"]:
        assert lead["worst_payer"] == "United"          # the outreach hook
        assert lead["worst_payer_gap"] > 0


def test_changes_by_payer_summarizes_direction(store):
    # two months: United cuts, BCBS raises
    _seed(store, month="2026-05")
    _seed(store, payer_base={"Aetna": 55, "BCBS": 70, "United": 20}, month="2026-06")
    res = compute_rate_changes(store, {"month": "2026-06", "prev_month": "2026-05"})
    by = {p["payer"]: p for p in res["by_payer"]}
    assert by["United"]["median_pct_change"] < 0        # systematic cut
    assert by["BCBS"]["median_pct_change"] > 0          # systematic increase
    # most-cutting payer sorts first
    assert res["by_payer"][0]["payer"] == "United"
