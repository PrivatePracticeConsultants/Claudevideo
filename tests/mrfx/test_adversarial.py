"""Adversarial regressions: hostile inputs that once produced a 500 or blinded
the store must now degrade to a clean refusal / fault-isolated skip.

Every case here was found by fuzzing the live API (adv_fuzz_api.py) or the
crash-resilience harness (adv_concurrency.py)."""

import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.benchmark import BenchmarkError, clean_volumes, normalize_market

GW, NPI = "431234567", "1417594896"


def _row(**kw):
    base = dict(payer="Aetna", tin_value=GW, tin_type="ein", npi=NPI,
                source_file="s.json", billing_code="97110", billing_code_type="CPT",
                discipline="PT", is_timed=True, billing_class="professional",
                negotiated_rate=30.0, negotiated_type="negotiated",
                is_dollar_rate=True, billing_code_modifier=[], service_code=["11"],
                file_month="2026-06", last_updated_on="2026-06-01",
                expiration_date=None, schema_version="2.0.0",
                tin_is_really_npi=False, state="MO")
    base.update(kw)
    return base


@pytest.fixture
def client(cfg, store):
    rows = [_row(payer=p, billing_code=c) for p in ("Aetna", "BCBS")
            for c in ("97110", "97140")]
    with store.rates_part_writer("s.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk([dict(npi=NPI, org_name="Gateway PT", entity_type="NPI-2",
                               taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X",
                               city="StL", state="MO", address="1 Main", zip="63103",
                               phone=None)])
    store.rebuild_rollups()
    return TestClient(create_app(cfg, store), raise_server_exceptions=False)


# ---- normalize_market: the shapes that 500'd DuckDB ----------------------

def test_normalize_market_rejects_bad_shapes():
    with pytest.raises(BenchmarkError, match="object of filters"):
        normalize_market("latest")            # a string, not a dict
    with pytest.raises(BenchmarkError, match="payers must be a list"):
        normalize_market({"month": "latest", "payers": {"p": 1}})
    # a NUMERIC month must not reach SQL as an int (DuckDB then tried to cast
    # the whole VARCHAR file_month column to INT32 and 500'd) — it is stringified
    assert normalize_market({"month": 202606})["month"] == "202606"
    # a single payer string is a convenience, coerced to a list
    assert normalize_market({"payers": "Aetna"})["payers"] == ["Aetna"]
    # null/blank payers are dropped, everything stringified
    assert normalize_market({"payers": [None, "", "Aetna", 5]})["payers"] == ["Aetna", "5"]


def test_clean_volumes_never_raises_typeerror():
    assert clean_volumes(None) == {}
    assert clean_volumes({}) == {}
    with pytest.raises(BenchmarkError, match="billing code"):
        clean_volumes("not a dict")           # .items() used to AttributeError
    with pytest.raises(BenchmarkError, match="numbers"):
        clean_volumes({"97110": "lots"})
    assert clean_volumes({"97110": "4200", "97140": 1800}) == {"97110": 4200.0,
                                                               "97140": 1800.0}


@pytest.mark.parametrize("path", [
    "/api/benchmark/market", "/api/benchmark/opportunity", "/api/leads",
    "/api/leaderboard", "/api/contract-gaps", "/api/trajectory",
    "/api/contracts/renewals", "/api/contracts/new-to-network",
    "/api/engagements/baseline",
])
def test_hostile_market_bodies_never_500(client, path):
    """A non-dict market, a numeric month, and non-string payers all produced
    a 500 from DuckDB before the boundary guards. Now every one is a clean
    2xx/4xx — never a 5xx."""
    for body in ({"subject": GW, "market": "not-a-dict"},
                 {"subject": GW, "market": {"month": 202606}},
                 {"subject": GW, "market": {"payers": "Aetna"}},
                 {"subject": GW, "market": {"payers": [None, 5, {"p": 1}]}},
                 {"subject": GW, "market": {"month": "latest"}, "volumes": "abc"}):
        r = client.post(path, json=body)
        assert r.status_code < 500, f"{path} 500'd on {body}: {r.text[:200]}"


def test_changes_hostile_market_never_500(client):
    for body in ({"market": "not-a-dict"},
                 {"market": {"month": 202606, "prev_month": 202605}}):
        r = client.post("/api/changes", json=body)
        assert r.status_code < 500, r.text[:200]


# ---- numeric accuracy: the percentile a client is SOLD on ----------------

def test_subject_percentile_is_mid_rank_and_agrees_with_p50(cfg, store):
    """A practice whose rate equals the peer median must report p50.

    The position used to be at-or-below ('<='), which counted the subject's own
    ties in full: a practice sitting exactly at the market median was reported
    at p60 of 5 peers, and the SAME row displayed 'subject 34.00 · p50 34.00'
    — self-contradictory. Ties are the norm (payers publish identical fee
    schedules to many practices), and the bias always ran upward, making
    clients look better paid than they are and shrinking the negotiating gap.
    Mid-rank (strictly below + half the ties) is the standard definition and
    the only one consistent with the quantile_cont columns beside it."""
    subj_tin, subj_npi = "431234567", "1417594896"
    peers = [("437654321", "1999999992", 30.0), ("434440001", "1876543219", 32.0),
             ("434440002", "1765432196", 34.0), ("434440003", "1654321987", 36.0),
             ("434440004", "1543219876", 38.0)]
    rows = [_row(tin_value=subj_tin, npi=subj_npi, negotiated_rate=34.0)]
    rows += [_row(tin_value=t, npi=n, negotiated_rate=r) for t, n, r in peers]
    with store.rates_part_writer("s.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk(
        [dict(npi=n, org_name=f"P{i}", entity_type="NPI-2",
              taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X",
              city="StL", state="MO", address="x", zip="63103", phone=None)
         for i, n in enumerate([subj_npi] + [p[1] for p in peers])])
    store.rebuild_rollups()

    client = TestClient(create_app(cfg, store))
    r = client.post("/api/benchmark/market", json={
        "subject": subj_tin,
        "market": {"month": "2026-06", "therapy_only": False}}).json()
    row = next(x for x in r["rows"] if x["billing_code"] == "97110")
    assert row["subject_rate"] == 34.0
    assert row["p50"] == 34.0
    assert row["subject_percentile"] == 50, (
        "a subject AT the peer median must be p50, not p60 — the row would "
        "otherwise contradict its own p50 column")

    # and a subject BELOW everything is still near the bottom, ABOVE is near
    # the top: the fix must not flatten the scale
    for rate, lo, hi in ((20.0, 0, 10), (99.0, 90, 100)):
        store.forget_file("s.json")
        rows2 = [_row(tin_value=subj_tin, npi=subj_npi, negotiated_rate=rate)]
        rows2 += [_row(tin_value=t, npi=n, negotiated_rate=x) for t, n, x in peers]
        with store.rates_part_writer("s.json") as w:
            w.write_batch(rows2)
        store.rebuild_rollups()
        got = next(x for x in client.post("/api/benchmark/market", json={
            "subject": subj_tin,
            "market": {"month": "2026-06", "therapy_only": False}}).json()["rows"]
            if x["billing_code"] == "97110")["subject_percentile"]
        assert lo <= got <= hi, f"rate {rate} -> p{got}, expected p{lo}-p{hi}"


# ---- corrupt-part quarantine: one bad file must not blind the store -------

def test_corrupt_parquet_is_quarantined_not_fatal(cfg, store, tmp_path):
    """The store's own writes are atomic, but external corruption (bit-rot, a
    half-copied restore, an AV truncation) can leave a damaged .parquet in the
    glob. read_parquet fails ENTIRELY on one bad file, so it must be moved
    aside on open — the rest of the store keeps serving (invariant 3)."""
    from pathlib import Path

    from mrfx.store import Store

    with store.rates_part_writer("good.json") as w:
        w.write_batch([_row(billing_code="97110"), _row(billing_code="97140")])
    store.rebuild_rollups()
    with store.connect() as con:
        before = con.execute("SELECT count(*) FROM rates").fetchone()[0]
    assert before == 2
    rates_dir = Path(store.rates_dir)
    store.close()

    # drop two kinds of damage a real disk fault leaves behind
    (rates_dir / "truncated.parquet").write_bytes(b"not a parquet")
    (rates_dir / "headerless.parquet").write_bytes(b"\x00" * 2000)

    store2 = Store(cfg.store_dir)
    with store2.connect() as con:
        after = con.execute("SELECT count(*) FROM rates").fetchone()[0]
    assert after == before, "the good data must still be readable"
    # the corrupt parts were moved out of the glob, never deleted
    assert not (rates_dir / "truncated.parquet").exists()
    assert (rates_dir / "corrupt" / "truncated.parquet").exists()
    assert (rates_dir / "corrupt" / "headerless.parquet").exists()
    # a real part is untouched
    assert list(rates_dir.glob("*.parquet")), "the good part stays in the glob"
