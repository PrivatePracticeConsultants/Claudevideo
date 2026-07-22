"""Regression tests for the analytical tab upgrades: market overview, payer
leaderboard (code), per-code summary + below-Medicare, leads worst-payer,
changes by-payer, and the Markets-tab suite (geography, negotiability, % of
Medicare, assistant/POS differential, contract gaps, practice leaderboard,
payer trajectory). The queries must RANK/FLAG correctly, not just run."""

import itertools

from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.benchmark import contract_gaps
from mrfx.leads import compute_leaderboard, compute_leads
from mrfx.market import (assistant_pos_diff, geographic_rates, medicare_index,
                        negotiability)
from mrfx.monitor import compute_payer_trajectory, compute_rate_changes
from mrfx.overview import market_overview


def _write(store, rows, month):
    with store.rates_part_writer(f"{month}.json") as w:
        w.write_batch(rows)


def _row(payer, tin, npi, code, rate, month, *, mods=None, pos=("11",)):
    return dict(
        payer=payer, tin_value=tin, tin_type="ein", npi=npi,
        source_file=f"{month}.json", billing_code=code, billing_code_type="CPT",
        discipline="pt", is_timed=True, billing_class="professional",
        negotiated_rate=float(rate), negotiated_type="negotiated",
        is_dollar_rate=True, billing_code_modifier=list(mods or []), service_code=list(pos),
        file_month=month, last_updated_on=f"{month}-01", expiration_date=None,
        schema_version="2.0.0", tin_is_really_npi=False, state=None)


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
    assert "basis_note" in ov  # honesty: overview states its basis


def test_overview_median_agrees_with_markets_median(store):
    # cross-tab consistency: the landing-page code median and the Markets tab's
    # market median for the SAME code must match (both per-TIN, report basis)
    _seed(store)
    ov = market_overview(store)
    ov_med = {c["billing_code"]: c["median"] for c in ov["top_codes"]}
    mi = medicare_index(store, "97110", {"month": "latest"})
    assert ov_med["97110"] == mi["market_median"]


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


# --------------------------------------------------------------------------
# Markets tab: geography / negotiability / % of Medicare / differential
# --------------------------------------------------------------------------

def test_negotiability_ranks_spread(store):
    _seed(store)  # each payer prices 8 practices at base..base+7 (a real spread)
    d = negotiability(store, "97110", {"month": "2026-06"})
    by = {p["payer"]: p for p in d["payers"]}
    assert set(by) == {"Aetna", "BCBS", "United"}
    for p in d["payers"]:
        assert p["n_practices"] == 8 and p["p90"] > p["p10"]
        assert p["spread_pct"] is not None and p["negotiability"] in ("wide", "moderate", "tight")
    # same absolute spread over a lower median => bigger PERCENT spread
    assert by["United"]["spread_pct"] > by["BCBS"]["spread_pct"]


def test_medicare_index_anchors_to_mpfs(cfg, store):
    _seed(store)
    client = TestClient(create_app(cfg, store))
    # no MPFS: dollars only, pct null
    d = medicare_index(store, "97110", {"month": "2026-06"})
    assert d["mpfs_loaded"] is None and d["market_pct_medicare"] is None
    assert all(p["pct_medicare"] is None for p in d["payers"])
    # load MPFS 97110 = $30 -> percentages appear, ranked like the rates
    client.post("/api/mpfs/upload",
                files={"file": ("m.csv", "code,locality,non_facility_rate\n97110,X,30.00\n", "text/csv")})
    d = medicare_index(store, "97110", {"month": "2026-06"})
    assert d["mpfs_rate"] == 30.0 and d["market_pct_medicare"] > 0
    by = {p["payer"]: p["pct_medicare"] for p in d["payers"]}
    assert by["BCBS"] > by["Aetna"] > by["United"]  # tracks the rate order


def test_geographic_rates_ranks_states(store):
    month = "2026-06"
    rows = []
    # 6 TX practices (higher paid) + 6 CA (lower); state comes from NPPES
    for state, base, js in (("TX", 60, range(0, 6)), ("CA", 40, range(6, 12))):
        for j in js:
            npi = "1%09d" % j
            store.save_npi(npi, f"Practice {j}", "225100000X", "Physical Therapy",
                           "City", state, entity_type="NPI-2")
            rows.append(_row("Aetna", "43%07d" % j, npi, "97110", base + (j % 3), month))
    _write(store, rows, month)
    store.rebuild_rollups()
    d = geographic_rates(store, "97110", {"month": month})
    st = {r["state"]: r for r in d["states"]}
    assert "TX" in st and "CA" in st
    assert st["TX"]["n_practices"] >= 5 and st["CA"]["n_practices"] >= 5
    assert st["TX"]["median_rate"] > st["CA"]["median_rate"]  # ranked high→low
    assert d["states"][0]["state"] == "TX"
    assert d["national_median"] is not None


def test_assistant_and_pos_differential(store):
    month, code = "2026-06", "97110"
    rows = []
    for j in range(6):  # 6 practices: office $100, office+CQ $85, telehealth $90
        tin, npi = "43%07d" % j, "1%09d" % j
        rows.append(_row("Aetna", tin, npi, code, 100, month, pos=("11",)))
        rows.append(_row("Aetna", tin, npi, code, 85, month, mods=("CQ",), pos=("11",)))
        rows.append(_row("Aetna", tin, npi, code, 90, month, pos=("02",)))
    _write(store, rows, month)
    store.rebuild_rollups()
    d = assistant_pos_diff(store, code, {"month": month})
    a = next(p for p in d["payers"] if p["payer"] == "Aetna")
    assert a["asst_pairs"] == 6 and a["asst_pct_of_base"] == 85   # PTA modifier = 85% of base
    assert a["tele_pairs"] == 6 and a["tele_pct_of_office"] == 90  # telehealth = 90% of office


def test_contract_gaps_finds_codes_peers_have(store):
    month = "2026-06"
    codes = ["97110", "97112", "97140", "97530"]
    rows = []
    for j in range(7):  # subject (j==0) prices only 2 codes; 6 peers price all 4
        tin, npi = "43%07d" % j, "1%09d" % j
        my = codes if j != 0 else ["97110", "97112"]
        for c in my:
            rows.append(_row("Aetna", tin, npi, c, 50 + j, month))
    _write(store, rows, month)
    store.rebuild_rollups()
    d = contract_gaps(store, "430000000", {"month": month}, min_peers=5)
    assert {g["billing_code"] for g in d["gaps"]} == {"97140", "97530"}
    for g in d["gaps"]:
        assert g["n_peers"] >= 5 and g["peer_median"] is not None
    # a blank/unknown subject must REFUSE, not fabricate a full gap list
    import pytest
    from mrfx.benchmark import BenchmarkError
    with pytest.raises(BenchmarkError):
        contract_gaps(store, "", {"month": month}, min_peers=5)


def test_practice_leaderboard_orders_by_position(store):
    _seed(store)  # tin j priced at base+j across codes -> higher j = higher %ile
    d = compute_leaderboard(store, {"month": "2026-06"}, min_codes=3, sort="paid")
    assert d["count"] == 8
    # best-paid practice first when sort=paid
    assert d["practices"][0]["median_percentile"] >= d["practices"][-1]["median_percentile"]
    assert all(p["n_codes"] >= 3 for p in d["practices"])


def test_payer_trajectory_ranks_eroding_first(store):
    _seed(store, month="2026-05")
    _seed(store, payer_base={"Aetna": 55, "BCBS": 70, "United": 20}, month="2026-06")
    d = compute_payer_trajectory(store, {})
    by = {p["payer"]: p for p in d["payers"]}
    assert by["United"]["cumulative_pct"] < 0 and by["United"]["n_months"] == 2
    assert by["BCBS"]["cumulative_pct"] > 0
    assert d["payers"][0]["payer"] == "United"  # most-eroding first
    assert by["United"]["direction"] == "down" and by["United"]["cut_streak"] >= 1
