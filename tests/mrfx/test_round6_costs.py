"""Round 6, P1: the measured costs. Each fix must leave the answer identical
and stop paying for the work it removed; these pin both halves."""

from __future__ import annotations

import csv
import io

from fastapi.testclient import TestClient

from tests.mrfx.test_known_answer_leads import TINS, leads_app  # noqa: F401  (fixture)

MO = {"month": "latest", "state": "MO", "therapy_only": True}


# -- P1.1 / P1.2: the client digest is built once per book and kept -----------------

def test_the_digest_is_computed_once_per_data_generation(leads_app, store, monkeypatch):  # noqa: F811
    """The Clients tab re-ran the whole digest (rate changes, a national
    contract-gaps scan, referrals) on every visit: 23 s at 99M rates. It is
    now kept per (data generation, watchlist, Medicare imports)."""
    import mrfx.clients as cl
    calls = []
    real = cl._build_digest

    def counted(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(cl, "_build_digest", counted)
    leads_app.post("/api/clients", json={"subject": "A REHAB"})
    first = leads_app.get("/api/clients/digest").json()
    again = leads_app.get("/api/clients/digest").json()
    assert first == again and len(calls) == 1, "a second visit recomputed the digest"
    leads_app.post("/api/clients", json={"subject": "B REHAB"})        # watchlist changed
    leads_app.get("/api/clients/digest")
    assert len(calls) == 2
    store.rebuild_rollups()                                           # new data generation
    leads_app.get("/api/clients/digest")
    assert len(calls) == 3


def test_packets_build_the_book_once_not_once_per_client(cfg, leads_app, store, monkeypatch):  # noqa: F811
    """Each packet called client_digest(store) - the WHOLE book - and its own
    contract-gaps scan: O(clients^2), 175 s for 5 clients at 99M. Now one
    digest and one market scan serve every packet."""
    import mrfx.benchmark as bm
    import mrfx.clients as cl
    from mrfx.packets import build_all_packets
    digests, books = [], []
    real_d, real_b = cl._build_digest, bm.contract_gaps_book

    def d(*a, **k):
        digests.append(1)
        return real_d(*a, **k)

    def b(*a, **k):
        books.append(1)
        return real_b(*a, **k)
    monkeypatch.setattr(cl, "_build_digest", d)
    monkeypatch.setattr(bm, "contract_gaps_book", b)
    for s in ("A REHAB", "B REHAB", "C REHAB"):
        leads_app.post("/api/clients", json={"subject": s})
    res = build_all_packets(cfg, store)
    assert res["clients"] == 3, res
    assert len(digests) == 1, f"the digest ran {len(digests)} times for 3 packets"
    assert len(books) == 1, f"the market gaps scan ran {len(books)} times for 3 packets"


# -- P1.3: the visit card does not pay for peer medians it never shows --------------

def test_the_fee_schedule_without_peers_has_the_same_rates(leads_app, store):  # noqa: F811
    from mrfx.schedule import compute_fee_schedule
    full = compute_fee_schedule(store, "A REHAB", MO)
    own = compute_fee_schedule(store, "A REHAB", MO, peers=False)
    rates = lambda fs: {(c["billing_code"], p): v["rate"]  # noqa: E731
                        for c in fs["codes"] for p, v in c["rates"].items()}
    assert rates(full) == rates(own) and rates(own)[("97110", "Rho Health")] == 30.0
    assert all(v["market_median"] is None and v["n_peers"] == 0
               for c in own["codes"] for v in c["rates"].values())
    assert any(v["market_median"] is not None
               for c in full["codes"] for v in c["rates"].values())


# -- P1.4: outreach percentiles by binary search, ties included --------------------

def test_outreach_percentile_counts_ties_at_or_below(cfg, store):
    """By hand, 97110 rates 30, 32, 32, 32, 40, as the house MID-RANK among
    the 4 other organizations (audit 8 P1-6; it was at-or-below, which put a
    flat code at 100 while Benchmark said p50): $30 -> 0 below / 4 = 0;
    $32 -> (1 below + 0.5 x 2 tied) / 4 = 50; $40 -> 4 / 4 = 100. Still two
    binary searches per row, not a count per practice."""
    from mrfx.ingest import ingest_file
    from tests.mrfx.conftest import make_fixture
    from tests.mrfx.test_v4 import innetwork, item
    rates = [30.0, 32.0, 32.0, 32.0, 40.0]
    groups = [([f"1{i:09d}"], f"43-00000{i:02d}", "ein", [(r, ["GP"])]) for i, r in enumerate(rates)]
    ingest_file(cfg, store, make_fixture(cfg.inbox_dir, "ties.json", innetwork(items=[item("97110", groups)])))
    store.rebuild_rollups()
    body = TestClient(create_app_for(cfg, store)).get("/api/export/outreach.csv?cpt=97110").content
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    got = sorted((float(r["C97110_RATE"]), int(r["C97110_PCTL"])) for r in rows)
    assert got == [(30.0, 0), (32.0, 50), (32.0, 50), (32.0, 50), (40.0, 100)]


def create_app_for(cfg, store):
    from mrfx.api import create_app
    return create_app(cfg, store)


# -- P1.5: whole-store screens are cached per data generation -------------------------

def test_generation_cache_returns_copies_and_clears_on_new_data(store):
    """The Overview's quality panels and payer freshness (inside every
    benchmark) re-scanned the whole store per request: 0.45-14 s each at 99M.
    Store.cached keeps one answer per data generation, hands out copies, and
    forgets everything when a rollup lands."""
    calls = []

    def compute():
        calls.append(1)
        return {"rows": [1, 2]}
    a = store.cached("probe", ("k", 1), compute)
    a["rows"].append("poison")                 # a caller decorating its copy...
    b = store.cached("probe", ("k", 1), compute)
    assert b == {"rows": [1, 2]} and len(calls) == 1, "...must not poison the cache"
    store.cached("probe", ("k", 2), compute)    # another key is another answer
    assert len(calls) == 2
    store.rebuild_rollups()                     # new data generation
    store.cached("probe", ("k", 1), compute)
    assert len(calls) == 3


def test_the_quality_panels_are_cached(leads_app, monkeypatch):  # noqa: F811
    import mrfx.benchmark as bm
    calls = []
    real = bm.normalize_market

    def counted(m):
        calls.append(1)
        return real(m)
    monkeypatch.setattr(bm, "normalize_market", counted)
    for _ in range(3):
        assert leads_app.post("/api/quality/types", json={"market": {"month": "latest"}}).status_code == 200
    assert len(calls) == 1, f"the whole-store scan ran {len(calls)} times for 3 visits"


# -- P1.6: ZIP placement from one stamped file, identical to the raw rows --------------

def test_the_placement_file_matches_the_raw_rows_and_follows_new_data(leads_app, store):  # noqa: F811
    from mrfx.sqlfn import zip_counts_by_tin
    place = store.placement_relation()
    tins = sorted(TINS.values())
    q = "SELECT unnest(?::VARCHAR[])"
    with store.connect() as con:
        raw = sorted(con.execute(zip_counts_by_tin(q, ", count(DISTINCT n.npi) AS nn"), [tins]).fetchall())
        got = sorted(con.execute(f"SELECT tin_value, zip, c, nn FROM {place} "
                                 "WHERE tin_value IN (SELECT unnest(?::VARCHAR[]))", [tins]).fetchall())
    assert got == raw and len(got) == 7
    # A: 3 NPIs at 64105 -> (A, 64105, rows, 3)
    assert [r for r in got if r[0] == TINS["A"]][0][1:] [::2] == ("64105", 3)
    assert store.placement_relation() == place, "same inputs, same file"
    store.save_npi("1510000002", "CLINICIAN 1510000002", "225100000X", "x", "Town", "MO",
                   entity_type="NPI-1", zip_code="64106")        # enrichment moved an NPI
    moved = store.placement_relation()
    assert moved != place, "a directory change must yield a new placement file"
    with store.connect() as con:
        a = con.execute(f"SELECT zip, nn FROM {moved} WHERE tin_value = ? ORDER BY zip",
                        [TINS["A"]]).fetchall()
    assert a == [("64105", 2), ("64106", 1)]
    # round 7 P2: the newest PREVIOUS stamp is kept (a request may still hold
    # its read_parquet string); anything older is removed
    assert len(list(store.dir.glob("tin_zip_place.*.parquet"))) == 2
    store.save_npi("1510000003", "CLINICIAN 1510000003", "225100000X", "x", "Town", "MO",
                   entity_type="NPI-1", zip_code="64107")
    third = store.placement_relation()
    left = sorted(p.name for p in store.dir.glob("tin_zip_place.*.parquet"))
    assert len(left) == 2 and place.split("'")[1].rsplit("/", 1)[-1] not in left
    assert third != moved


# -- P1.7: leads are cached per generation and the second call is free ----------------

def test_leads_answer_once_per_generation(leads_app, monkeypatch):  # noqa: F811
    import mrfx.leads as ld
    calls = []
    real = ld.normalize_market

    def counted(m):
        calls.append(1)
        return real(m)
    monkeypatch.setattr(ld, "normalize_market", counted)
    body = {"market": MO, "threshold_percentile": 25, "min_codes": 3}
    a = leads_app.post("/api/leads", json=body).json()
    b = leads_app.post("/api/leads", json=body).json()
    assert a == b and [x["display_name"] for x in a["leads"]] == ["A REHAB", "B REHAB"]
    assert len(calls) == 1, "the second identical request recomputed the market"
