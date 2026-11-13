"""0.8.3 — the rate card, Price a visit and Visit value with an NPI, on a big store.

Field report (1.18 B rates, an analytics build running): the rate card with an
NPI as the practice "loaded nothing for an hour". Two costs, both per request:

* the NPI -> practice lookup scanned EVERY raw rate
  (`SELECT DISTINCT tin_value FROM rates_dedup WHERE npi = ?`): 22 s on a
  99M-rate store, run several times per rate card;
* the rate card's peer medians and the visit value read whole-store
  practice-median tables that were (re)built INSIDE the request whenever the
  spine's key had changed — every loaded file and every analytics part
  changes it — 6.8 s cold on the 99M store, where the scoped query for the
  practice's own payers / codes takes 0.35 s, with the same rows.

Each test fails on 0.8.2 (4b1f307) with the value in its docstring.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from mrfx import benchmark as bm
from mrfx.api import create_app
from mrfx.ingest import scan_inbox
from mrfx.store import file_key

from .test_review12 import VV_A, VV_B, VV_BODY, VV_C, VV_STATE, VV_TINS, _vv_doc


@pytest.fixture
def vv_app(cfg, store):
    """test_review12's Visit-value store (worked answers there): practices
    T1..T8, payers A / B / C and an OT book, NPIs named in NPPES."""
    docs = {"a_2026-06.json": _vv_doc("Payer A", VV_A),
            "a_ot_2026-06.json": _vv_doc("Payer A OT book", {"T7": 50}, mods=("GO",)),
            "b_2026-06.json": _vv_doc("Payer B", {**VV_B, "T6": 45},
                                      partial={"T6": ("97140",)}),
            "c_2026-06.json": _vv_doc("Payer C", VV_C)}
    for name, doc in docs.items():
        (cfg.inbox_dir / name).write_text(json.dumps(doc))
    assert all(r["status"] == "done" for r in scan_inbox(cfg, store))
    for k, tin in VV_TINS.items():
        store.save_npi(f"18{tin[1:]}", f"{k} REHAB", "225100000X", "x", "Town",
                       VV_STATE[k], entity_type="NPI-2", zip_code="64105")
    store.rebuild_rollups()
    return TestClient(create_app(cfg, store), base_url="http://localhost")

T3_TIN = VV_TINS["T3"]
T3_NPI = f"18{T3_TIN[1:]}"              # the fixture's NPI for practice T3


class _Spy:
    """store.connect() whose SQL is recorded."""

    def __init__(self, store, monkeypatch):
        self.sql: list[str] = []
        real = store.connect
        spy = self

        class Con:
            def __init__(self, con):
                self._con = con

            def execute(self, q, *a, **k):
                spy.sql.append(q)
                return self._con.execute(q, *a, **k)

            def __enter__(self):
                self._con.__enter__()
                return self

            def __exit__(self, *exc):
                return self._con.__exit__(*exc)

            def __getattr__(self, name):
                return getattr(self._con, name)
        monkeypatch.setattr(store, "connect", lambda: Con(real()))


def _parts_with(store, npi):
    with store.connect() as con:
        return sorted(p.name for p in store.rates_dir.glob("*.parquet")
                      if con.execute(f"SELECT count(*) FROM read_parquet('{p}') WHERE npi = ?",
                                     [npi]).fetchone()[0])


def _index(store):
    with store.connect() as con:
        store._rate_npis_relation(con)


def test_an_npi_subject_reads_only_the_files_that_hold_it(vv_app, store, monkeypatch):
    """(P0) The NPI -> practice lookup reads only the rate parts whose kept
    NPI set lists it — never every raw rate — and answers exactly what the
    full scan answers.
    was: `SELECT DISTINCT tin_value FROM rates_dedup WHERE npi = ?` — a scan
    of every rate row, several times per rate card."""
    _index(store)
    holding = _parts_with(store, T3_NPI)
    every = sorted(p.name for p in store.rates_dir.glob("*.parquet"))
    assert 0 < len(holding) < len(every)            # the OT book does not hold T3
    with store.connect() as con:
        full = [r[0] for r in con.execute(
            "SELECT DISTINCT tin_value FROM rates_dedup WHERE npi = ? AND tin_value IS NOT NULL "
            "ORDER BY 1", [T3_NPI]).fetchall()]
    spy = _Spy(store, monkeypatch)
    got = bm.resolve_subject_tins(store, T3_NPI)
    assert got == full == [T3_TIN]
    read = "\n".join(spy.sql)
    assert "rates_dedup" not in read and "FROM rates " not in read
    assert all(p in read for p in holding)
    assert not any(p in read for p in set(every) - set(holding))


def test_a_file_the_npi_index_has_not_reached_is_still_read(cfg, vv_app, store):
    """(P0 — exactness) A file that landed after the NPI index was last
    brought up to date is read in full, so an NPI that bills under a second
    practice there is found: the same TINs as the full scan.
    was: n/a (0.8.2 scanned everything); pinned so the fast path can never
    miss a practice."""
    _index(store)
    doc = _vv_doc("Payer D", {"T3": 70})
    doc["in_network"][0]["negotiated_rates"][0]["provider_groups"][0]["tin"]["value"] = "730000099"
    (cfg.inbox_dir / "d_2026-06.json").write_text(json.dumps(doc))
    assert all(r["status"] == "done" for r in scan_inbox(cfg, store))
    new_part = f"{file_key('d_2026-06.json')}.parquet"
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM rate_npi_parts WHERE part = ?",
                           [new_part]).fetchone()[0] == 0        # not indexed yet
        full = [r[0] for r in con.execute(
            "SELECT DISTINCT tin_value FROM rates_dedup WHERE npi = ? AND tin_value IS NOT NULL "
            "ORDER BY 1", [T3_NPI]).fetchall()]
    assert store.tins_billing_npi(T3_NPI) == full == [T3_TIN, "730000099"]


def _numbers(client, subject):
    mk = {"month": "latest", "state": "MO", "discipline": "PT"}
    fee = client.post("/api/schedule/fee", json={"subject": subject, "market": mk})
    visit = client.post("/api/schedule/visit", json={"subject": subject, "market": mk,
                                                     "preset": "ortho"})
    vm = client.post("/api/schedule/visit/market", json={**VV_BODY, "subject": subject})
    card = client.post("/api/report/ratecard", json={"subject": subject, "market": mk,
                                                      "preset": "ortho", "visit_market": True})
    for r in (fee, visit, vm, card):
        assert r.status_code == 200, r.text
    f = fee.json()
    return {"codes": f["fee_schedule"]["codes"], "scorecard": f["scorecard"],
            "visit": visit.json()["rows"], "market": vm.json()["rows"],
            "all": vm.json()["all_payers"]}


def test_rate_card_visit_and_visit_value_by_npi_equal_by_tax_id(vv_app):
    """(P0 — the user's question) The rate card, Price a visit and Visit
    value in this market give the same numbers for practice T3 typed as its
    NPI as typed as its tax ID, and the printable card builds.
    was: the same numbers, after a full raw scan per lookup (minutes each at
    1.18 B rates) — pinned so the fast lookup cannot change them."""
    by_tin = _numbers(vv_app, T3_TIN)
    by_npi = _numbers(vv_app, T3_NPI)
    assert by_tin["codes"] and by_tin["visit"] and by_tin["market"]
    assert json.dumps(by_npi, sort_keys=True) == json.dumps(by_tin, sort_keys=True)


def _medians_files(store):
    return sorted(p.name for p in store.dir.glob("practice_medians*.parquet"))


def _cold(store):
    for p in store.dir.glob("practice_medians*.parquet*"):
        p.unlink()
    store._spine_mat.clear()


def _wait_warm(store, timeout=30):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if not any(n.startswith("mrfx-warm-") for n in
                   (t.name for t in __import__("threading").enumerate())):
            return
        time.sleep(0.05)


def test_a_cold_rate_card_and_visit_value_never_build_the_store_wide_medians(vv_app, store):
    """(P0 — the other half of the hour) With the practice-median tables not
    built for the spine as it stands (every loaded file and every analytics
    part changes its key), the rate card and Visit value answer from the
    scoped query and build nothing in the request — and while an analytics
    build runs, nothing in the background either. The numbers equal the
    ones read from the built tables.
    was: practice_medians_disc_payer_v1 built inside the request (6.8 s on a
    99M-rate store, ~80 s at 1.18 B, on nearly every request)."""
    store.rebuild_rollups()
    _cold(store)
    store._rollup_active = True                    # a build is running
    try:
        cold = _numbers(vv_app, T3_TIN)
        assert _medians_files(store) == []
    finally:
        store._rollup_active = False
    # no build running: the first cold request starts the table in the
    # background; once built, the same answers come from it
    _numbers(vv_app, T3_TIN)
    _wait_warm(store)
    assert any(n.startswith(bm.DISC_PAYER_PRACTICE_MEDIANS) for n in _medians_files(store))
    assert store.spine_materialised_current(bm.DISC_PAYER_PRACTICE_MEDIANS) is not None
    warm = _numbers(vv_app, T3_TIN)
    assert json.dumps(warm, sort_keys=True) == json.dumps(cold, sort_keys=True)


def test_the_rate_card_without_a_discipline_is_the_same_cold_and_warm(vv_app, store):
    """(P1) The all-disciplines card (the per-payer medians table): cold
    (scoped query) and warm (the table) print the same peer medians.
    was: the table was built inside the request."""
    store.rebuild_rollups()
    body = {"subject": T3_TIN, "market": {"month": "latest", "state": "MO"}}
    _cold(store)
    store._rollup_active = True
    try:
        cold = vv_app.post("/api/schedule/fee", json=body).json()
        assert _medians_files(store) == []
    finally:
        store._rollup_active = False
    store.spine_materialised(bm.PAYER_PRACTICE_MEDIANS, *bm._house_table_sql(True))
    warm = vv_app.post("/api/schedule/fee", json=body).json()
    assert cold["fee_schedule"]["codes"] and \
        json.dumps(cold, sort_keys=True) == json.dumps(warm, sort_keys=True)


def test_a_loaded_file_leaves_the_practice_medians_current(cfg, vv_app, store, monkeypatch):
    """(P1) A file that finishes loading changes no spine row until the
    analytics add it, so the materialised practice medians stay current;
    the analytics adding it (a spine write) makes them stale.
    was: the spine's key included every rate part's signature — each loaded
    file made the medians stale and the next report rebuilt them over the
    whole spine."""
    store.spine_materialised(bm.DISC_PAYER_PRACTICE_MEDIANS, *bm._house_disc_table_sql())
    key = store.spine_key()
    monkeypatch.setattr(store, "rebuild_rollups", lambda *a, **k: 0.0)
    monkeypatch.setattr(store, "update_rollups_incremental", lambda *a, **k: 0.0)
    (cfg.inbox_dir / "d_2026-06.json").write_text(json.dumps(_vv_doc("Payer D", {"T3": 70})))
    assert all(r["status"] == "done" for r in scan_inbox(cfg, store))
    assert store.spine_key() == key
    store._spine_mat.clear()                       # as after a restart
    assert store.spine_materialised_current(bm.DISC_PAYER_PRACTICE_MEDIANS) is not None
    monkeypatch.undo()
    store.rebuild_rollups()
    assert store.spine_key() != key
    assert store.spine_materialised_current(bm.DISC_PAYER_PRACTICE_MEDIANS) is None


def _raw_pairs(store):
    with store.connect() as con:
        return con.execute(
            "SELECT tin_value, npi, tin_is_really_npi, count(*) FROM rates "
            "GROUP BY ALL ORDER BY ALL").fetchall()


def _kept_pairs(store):
    with store.connect() as con:
        return con.execute(f"SELECT * FROM {store.pairs_relation()} ORDER BY ALL").fetchall()


def test_the_pairs_file_is_merged_from_per_file_pairs(cfg, vv_app, store, monkeypatch):
    """(P0 — the practice drawer at 1.19 B rates) The (TIN, NPI) pairs file
    behind the practice drawer, the city map, sizing, the digest, outreach,
    new-to-network and steal-share is merged from pairs kept per rate part:
    a file that lands is aggregated ALONE — the rest come from the kept
    sets — and the result equals the raw aggregate exactly (a part not
    covered yet is read in the same statement).
    was: every rate of the store aggregated whenever any part changed (on a
    1.19 B-rate store the drawer ran out of temp disk after 16 s)."""
    assert _kept_pairs(store) == _raw_pairs(store)
    (cfg.inbox_dir / "d_2026-06.json").write_text(json.dumps(_vv_doc("Payer D", {"T3": 70, "T8": 61})))
    monkeypatch.setattr(store, "rebuild_rollups", lambda *a, **k: 0.0)
    monkeypatch.setattr(store, "update_rollups_incremental", lambda *a, **k: 0.0)
    assert all(r["status"] == "done" for r in scan_inbox(cfg, store))
    new_part = f"{file_key('d_2026-06.json')}.parquet"
    spy = _Spy(store, monkeypatch)
    assert _kept_pairs(store) == _raw_pairs(store)
    built = "\n".join(q for q in spy.sql if "tin_npi_pairs" in q or "part_pairs" in q)
    assert "FROM rates" not in built
    old_parts = [p.name for p in store.rates_dir.glob("*.parquet") if p.name != new_part]
    # each old part came from the kept set, never read again
    assert not any(f"{p}'" in built for p in old_parts)
    # a part not covered yet (the set lagging behind) is read in the merge
    monkeypatch.undo()
    for p in (store.dir / "part_pairs").glob(f"{new_part.removesuffix('.parquet')}.*"):
        p.unlink()
    for p in store.dir.glob("tin_npi_pairs.*.parquet"):
        p.unlink()
    monkeypatch.setattr(store, "_pair_sets_current", lambda con, budget_s=None: False)
    assert _kept_pairs(store) == _raw_pairs(store)


def test_the_practice_drawer_lists_the_same_npis(vv_app, store):
    """(no step backwards) The practice drawer's NPI list off the merged
    pairs equals the raw (TIN, NPI) pairs."""
    r = vv_app.get(f"/api/entity/tin/{T3_TIN}")
    assert r.status_code == 200, r.text
    listed = sorted(n["npi"] for n in r.json()["npis"])
    with store.connect() as con:
        raw = [x[0] for x in con.execute(
            "SELECT DISTINCT npi FROM rates WHERE tin_value = ? AND npi IS NOT NULL ORDER BY 1",
            [T3_TIN]).fetchall()]
    assert listed == raw == [T3_NPI]


def test_the_practice_placement_is_built_from_the_kept_pairs(cfg, vv_app, store, monkeypatch):
    """(P0 — Leverage, the radius map, the dossier, new-to-network at 1.19 B)
    The per-practice ZIP placement reads the kept (TIN, NPI) pairs, never
    every raw rate, and holds the same rows as the raw computation — also
    after a file lands.
    was: an aggregate of every rate whenever any part had changed (Leverage
    answered 503 after 16 s on a 1.19 B-rate store: temp disk exhausted)."""
    from mrfx.catalog import therapy_taxonomy_sql
    from mrfx.sqlfn import _HAS_ZIP, ZIP5

    def raw():
        therapy = therapy_taxonomy_sql("n.taxonomy_code", all_col="n.taxonomy_codes")
        with store.connect() as con:
            return con.execute(f"""
                WITH tn AS (SELECT tin_value, npi, count(*) AS c FROM rates
                            WHERE tin_value IS NOT NULL AND npi IS NOT NULL GROUP BY 1, 2),
                     nz AS (SELECT n.npi, {ZIP5} AS zip, {therapy} AS is_t
                            FROM npi_directory n WHERE {_HAS_ZIP})
                SELECT tn.tin_value, nz.zip, sum(tn.c)::BIGINT, count(DISTINCT tn.npi),
                       count(DISTINCT tn.npi) FILTER (nz.is_t)
                FROM tn JOIN nz USING (npi) GROUP BY 1, 2 ORDER BY ALL""").fetchall()

    def kept():
        rel = store.placement_relation()
        with store.connect() as con:
            return con.execute(f"SELECT * FROM {rel} ORDER BY ALL").fetchall()
    spy = _Spy(store, monkeypatch)
    assert kept() == raw() and kept()
    assert not any("tin_zip_place" in q and "FROM rates" in q for q in spy.sql)
    monkeypatch.undo()
    monkeypatch.setattr(store, "rebuild_rollups", lambda *a, **k: 0.0)
    monkeypatch.setattr(store, "update_rollups_incremental", lambda *a, **k: 0.0)
    (cfg.inbox_dir / "d_2026-06.json").write_text(json.dumps(_vv_doc("Payer D", {"T3": 70, "T8": 61})))
    assert all(r["status"] == "done" for r in scan_inbox(cfg, store))
    assert kept() == raw()


def test_the_npi_drawer_reads_only_the_files_that_hold_the_npi(vv_app, store, monkeypatch):
    """(P1) The practice drawer at the NPI grain reads only the rate parts
    that can hold the NPI and shows the same rows as the full view.
    was: rates_dedup over every part (7 s on a 1.19 B-rate store held in
    memory; minutes from disk)."""
    _index(store)
    full = vv_app.get(f"/api/entity/npi/{T3_NPI}").json()
    assert full["rates"]
    holding = _parts_with(store, T3_NPI)
    every = sorted(p.name for p in store.rates_dir.glob("*.parquet"))
    store._npi_tins_cache = {}
    spy = _Spy(store, monkeypatch)
    again = vv_app.get(f"/api/entity/npi/{T3_NPI}").json()
    read = "\n".join(q for q in spy.sql if "unit_id = ?" in q)
    assert read and "rates_dedup" not in read
    assert all(p in read for p in holding)
    assert not any(p in read for p in set(every) - set(holding))
    assert json.dumps(again["rates"], sort_keys=True) == json.dumps(full["rates"], sort_keys=True)
    monkeypatch.undo()
    monkeypatch.setattr(store, "npi_parts", lambda npi: None)       # the old, full read
    old = vv_app.get(f"/api/entity/npi/{T3_NPI}").json()
    assert json.dumps(old["rates"], sort_keys=True) == json.dumps(full["rates"], sort_keys=True)


# ---- Visit value in this market: the practices behind a row, ranked -------
#
# Hand-worked on test_review12's market (MO, PT, the ortho visit = 4 codes x
# 1 unit, every code at the practice's one rate, so visit = 4 x rate):
#   Payer A  T6 180, T5 160, T4 140, T3 120, T2 100, T1 80   (T8 is in KS)
#            median 130; T6 percentile 5/5 = 100, T3 2/5 = 40
#   Payer B  T5 200, T4 160, T3 140, T1 120, T2 120          (T6 partial)
#            T1 and T2 tie at rank 4, listed by tax ID; T1 percentile
#            (0 + 0.5 x 1) / 4 = 12.5 -> 13
#   All      median of each practice's payers (C counts: T1 80,120,40 -> 80,
#            T2 100,120,40 -> 100): T5 180, T6 180 (rank 1, by tax ID),
#            T4 150, T3 130, T2 100, T1 80
#   Payer C  2 practices: under the minimum of 5 -> refused, like its cell

def _plist(client, payer, **kw):
    body = {**VV_BODY, "payer": payer, **kw}
    return client.post("/api/schedule/visit/market/practices", json=body)


def _ids(d):
    return [(VV_NAMES[x["name"]], x["value"], x["rank"]) for x in d["rows"]]


VV_NAMES = {f"T{i} REHAB": f"T{i}" for i in range(1, 9)}


def test_visit_value_practices_are_ranked_highest_first_with_hand_worked_numbers(vv_app):
    """(feature) Each row of Visit value in this market opens the practices
    behind it, highest visit value first: rank (ties share it, by tax ID),
    percentile, vs median, the rates that make up the visit, name, city,
    tax ID and NPIs.
    was: POST /api/schedule/visit/market/practices answered 404."""
    a = _plist(vv_app, "Payer A")
    assert a.status_code == 200, a.text
    a = a.json()
    assert _ids(a) == [("T6", 180.0, 1), ("T5", 160.0, 2), ("T4", 140.0, 3),
                       ("T3", 120.0, 4), ("T2", 100.0, 5), ("T1", 80.0, 6)]
    assert (a["total"], a["median"], a["payer"]) == (6, 130.0, "Payer A")
    top, t3 = a["rows"][0], a["rows"][3]
    assert (top["percentile"], top["vs_median"], top["vs_median_pct"]) == (100, 50.0, 38.5)
    assert (t3["percentile"], t3["vs_median"]) == (40, -10.0)
    assert t3["rates"] == [{"code": c, "units": 1, "rate": 30.0}
                           for c in ("97110", "97530", "97140", "97112")]
    assert t3["tin"] == T3_TIN and t3["cities"] == ["Town"] and t3["state"] == "MO"
    assert [n["npi"] for n in t3["npis"]] == [T3_NPI] and t3["npis"][0]["name"] == "T3 REHAB"
    b = _plist(vv_app, "Payer B").json()
    assert _ids(b) == [("T5", 200.0, 1), ("T4", 160.0, 2), ("T3", 140.0, 3),
                       ("T1", 120.0, 4), ("T2", 120.0, 4)]
    assert b["rows"][3]["percentile"] == 13
    al = _plist(vv_app, "All payers").json()
    assert _ids(al) == [("T5", 180.0, 1), ("T6", 180.0, 1), ("T4", 150.0, 3),
                        ("T3", 130.0, 4), ("T2", 100.0, 5), ("T1", 80.0, 6)]
    t1 = al["rows"][-1]
    assert (t1["payers"], t1["low"], t1["high"]) == (3, 40.0, 120.0)
    c = _plist(vv_app, "Payer C")
    assert c.status_code == 422 and "fewer than 5" in c.json()["detail"]


def test_visit_value_practices_agree_with_the_row_and_page_and_sort(vv_app):
    """(feature) The list is the row's own population: its count and median
    are the row's, the subject is marked and carries the block's value;
    pages and lowest-first are the same rows in order; repeated calls are
    byte-identical (invariant 7)."""
    block = vv_app.post("/api/schedule/visit/market",
                        json={**VV_BODY, "subject": T3_TIN}).json()
    rows = {r["payer"]: r for r in block["rows"] + [block["all_payers"]]}
    for payer in ("Payer A", "Payer B", "All payers"):
        d = _plist(vv_app, payer, subject=T3_TIN).json()
        assert (d["total"], d["median"], d["mean"]) == (
            rows[payer]["practices"], rows[payer]["median"], rows[payer]["mean"])
        mine = [x for x in d["rows"] if x["is_subject"]]
        assert [x["value"] for x in mine] == [rows[payer]["subject"]["value"]]
    full = _plist(vv_app, "Payer A").json()["rows"]
    p1 = _plist(vv_app, "Payer A", offset=0, limit=4).json()
    p2 = _plist(vv_app, "Payer A", offset=4, limit=4).json()
    assert p1["rows"] + p2["rows"] == full and p2["total"] == 6
    asc = _plist(vv_app, "Payer B", order="asc").json()["rows"]
    assert [(VV_NAMES[x["name"]], x["value"]) for x in asc] == [
        ("T1", 120.0), ("T2", 120.0), ("T3", 140.0), ("T4", 160.0), ("T5", 200.0)]
    one = _plist(vv_app, "All payers").content
    assert _plist(vv_app, "All payers").content == one
    assert _plist(vv_app, "Payer Z").status_code == 422


def test_visit_value_practices_csv_is_the_list(vv_app):
    """(feature) The list's CSV holds every practice in the same order with
    the same numbers, the per-code rates for a payer, and its labels."""
    import csv
    import io
    d = _plist(vv_app, "Payer B", subject=T3_TIN).json()
    r = vv_app.post("/api/schedule/visit/market/practices.csv",
                    json={**VV_BODY, "payer": "Payer B", "subject": T3_TIN})
    assert r.status_code == 200
    assert "visit_value_practices_Payer_B.csv" in r.headers["content-disposition"]
    text = r.text.lstrip("﻿")
    assert "Visit value by practice — Payer B, MO, PT" in text
    recs = list(csv.DictReader(io.StringIO(
        "\n".join(ln for ln in text.splitlines() if not ln.startswith("#")))))
    assert [(x["practice"], x["visit_value"], x["rank"]) for x in recs] == [
        (x["name"], f"{x['value']:.2f}", str(x["rank"])) for x in d["rows"]]
    t3 = next(x for x in recs if x["practice"] == "T3 REHAB")
    assert (t3["rate_97110"], t3["this_practice"], t3["npis"], t3["percentile"]) == (
        "35.00", "yes", T3_NPI, "50")
