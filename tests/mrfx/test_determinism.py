"""The same question, asked twice, must get the same answer.

A rate card, a pitch report and a market report are documents the user SELLS.
Generate one on Monday and again on Tuesday from an unchanged store and it has
to be the same document — otherwise two copies in a client's inbox disagree
about who the best-paying payer is.

DuckDB scans in parallel, so an ORDER BY whose key has ties hands those tied
rows back in whichever thread finished first. Every ranked list therefore needs
a UNIQUE tiebreaker. These tests seed deliberate ties and pin that.

Also here: `_market_where` emits `td.`-qualified predicates for a state filter,
so any relation it is pasted into must join `tin_directory` under exactly that
alias. Two contracts queries did not, and 500'd on a state-filtered market.
"""
import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.store import Store

REPEATS = 4


@pytest.fixture
def tied_store(cfg, store):
    """Three payers paying IDENTICAL rates and two cities sharing a median —
    every ranking below therefore has a tie that must be broken the same way
    every time."""
    rows, dirs = [], []
    cities = [("St Louis", "63103"), ("St Louis", "63103"),
              ("Springfield", "65801"), ("Springfield", "65801"),
              ("Columbia", "65201")]
    for i, (city, zipc) in enumerate(cities):
        tin, npi = str(431000000 + i), str(1417594896 + i)
        for code, rate in (("97110", 40.0), ("97140", 30.0)):
            for payer in ("Aetna", "BCBS", "Cigna"):
                rows.append(dict(
                    payer=payer, tin_value=tin, tin_type="ein", npi=npi,
                    source_file="a.json", billing_code=code,
                    billing_code_type="CPT", discipline="PT", is_timed=True,
                    billing_class="professional", negotiated_rate=rate,
                    negotiated_type="negotiated", is_dollar_rate=True,
                    billing_code_modifier=[], service_code=["11"],
                    file_month="2026-06", last_updated_on="2026-06-01",
                    expiration_date=None, schema_version="2.0.0",
                    tin_is_really_npi=False, state="MO"))
        dirs.append(dict(npi=npi, org_name=f"Clinic {i}", entity_type="NPI-2",
                         taxonomy_code="261QP2000X",
                         taxonomy_codes="261QP2000X", city=city, state="MO",
                         address="x", zip=zipc, phone=None))
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk(dirs)
    store.rebuild_rollups()
    for p in ("Aetna", "BCBS", "Cigna"):
        store.upsert_file("a.json", payer=p, status="done",
                          file_type="in_network", last_updated_on="2026-06-01")
    return cfg, store


M = {"month": "2026-06"}
MS = {"month": "2026-06", "state": "MO"}

RANKED = [
    ("payer concentration", "/api/territory/concentration",
     {"state": "MO", "market": M}),
    ("city rate map", "/api/territory/local",
     {"state": "MO", "code": "97110", "market": M}),
    ("rate-type mix", "/api/quality/types", {"market": MS}),
    ("payer posture", "/api/quality/posture", {"market": MS}),
    ("market report", "/api/report/market",
     {"state": "MO", "code": "97110", "market": M}),
    ("rate card", "/api/schedule/fee", {"subject": "431000000", "market": MS}),
    ("pitch report", "/api/report/pitch",
     {"subject": "431000000", "market": MS}),
]


@pytest.mark.parametrize("label,route,body", RANKED, ids=[r[0] for r in RANKED])
def test_a_ranked_list_with_ties_comes_back_in_one_stable_order(
        tied_store, label, route, body):
    cfg, store = tied_store
    c = TestClient(create_app(cfg, store))
    answers = set()
    for _ in range(REPEATS):
        r = c.post(route, json=body)
        assert r.status_code < 500, f"{label}: {r.text[:200]}"
        # the methodology footer stamps a wall-clock minute; that is allowed to
        # move, the ORDER of the rows is not
        import re
        answers.add(re.sub(r"Generated \d{4}-\d\d-\d\d \d\d:\d\d UTC", "", r.text))
    assert len(answers) == 1, (
        f"{label} returned {len(answers)} different answers to the same "
        f"question — a tied ORDER BY is being resolved by scan order")


@pytest.mark.parametrize("route,body", [
    ("/api/contracts/renewals", {"market": MS}),
    ("/api/contracts/renewals", {"market": M}),
])
def test_a_state_filtered_contract_query_binds(tied_store, route, body):
    """_market_where emits `list_contains(td.states, ?)`, so the relation has to
    join tin_directory AS td. expiration_coverage joined nothing and
    renewal_radar joined it as `d`, so both raised a DuckDB BinderException —
    a 500, not a refusal."""
    cfg, store = tied_store
    c = TestClient(create_app(cfg, store))
    r = c.post(route, json=body)
    assert r.status_code == 200, r.text[:300]
    assert "coverage" in r.json()


def test_expiration_coverage_accepts_a_state(tied_store):
    from mrfx.contracts import expiration_coverage
    _cfg, store = tied_store
    cov = expiration_coverage(store, {"month": "2026-06", "state": "MO"})
    assert cov["rows"] > 0
    # and the state actually filters rather than being ignored
    none_there = expiration_coverage(store, {"month": "2026-06", "state": "MI"})
    assert none_there["rows"] == 0


def test_out_of_order_months_are_refused_in_the_readers_words(tied_store):
    """The Changes tab offers every loaded month in BOTH dropdowns, so the pair
    can be picked out of order. The refusal used to read "prev_month must be
    earlier than month" — the function's parameter names, not words the person
    reading the tab chose."""
    import pytest as _pytest

    from mrfx.benchmark import BenchmarkError
    from mrfx.monitor import compute_rate_changes
    _cfg, store = tied_store
    with _pytest.raises(BenchmarkError) as e:
        compute_rate_changes(store, {"month": "2026-06", "prev_month": "2026-06"})
    msg = str(e.value)
    assert "prev_month" not in msg and "month being examined" in msg
    # both months are named, so the reader can see which way round it goes
    assert msg.count("2026-06") >= 2
