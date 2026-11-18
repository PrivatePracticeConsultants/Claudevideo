"""The spine's stored `is_latest` (round 4, P0.1).

"Latest" used to be worked out per request: a window over the WHOLE spine
(`QUALIFY file_month = max(file_month) OVER (PARTITION BY payer, tin_value,
tin_is_really_npi, billing_code)`) under every Explorer, Code page, Benchmark,
report and lead query — 4.4 s of WINDOW on the real 1.41M-row spine before the
first row came back, ~15 s a click at 300M rates. The spine now stores the
answer. These tests pin that the stored flag IS that window, row for row, on
every path that writes the spine: the first build, a payer delta (a newer
month lands), a removal (the newer month is forgotten), the in-place upgrade
of an older store, and the fallback while a store has no flag yet.

Data (two payers):
  Gamma  T1 97110  May $40, June $42           -> latest June only
         T1 97140  May $30 (absent in June)    -> May stays latest (its newest)
         T2 97110  May $44                     -> latest May
         T1 typed npi (same digits as the EIN!) 97110  May $35 -> its OWN contract
  Delta  T1 97110  June $50, July $55          -> latest July
"""

from __future__ import annotations

import json

import pytest

T1, T2, NP = "210000001", "220000002", "1210000001"


def _price(rate):
    return {"negotiated_type": "negotiated", "negotiated_rate": rate,
            "billing_class": "professional", "service_code": ["11"],
            "billing_code_modifier": ["GP"], "expiration_date": "9999-12-31"}


def _file(payer, day, items):
    return {"reporting_entity_name": payer, "reporting_entity_type": "issuer",
            "last_updated_on": day, "version": "2.0.0", "provider_references": [],
            "in_network": [{"negotiation_arrangement": "ffs", "name": c,
                            "billing_code_type": "CPT", "billing_code_type_version": "2026",
                            "billing_code": c, "description": c,
                            "negotiated_rates": [{"provider_groups": [
                                {"npi": [NP], "tin": {"type": ttype, "value": tin}}],
                                "negotiated_prices": [_price(r)]} for tin, ttype, r in rows]}
                           for c, rows in items.items()]}


GAMMA_MAY = _file("Gamma Health", "2026-05-01", {
    "97110": [(T1, "ein", 40.0), (T2, "ein", 44.0), (T1, "npi", 35.0)],
    "97140": [(T1, "ein", 30.0)]})
GAMMA_JUNE = _file("Gamma Health", "2026-06-01", {"97110": [(T1, "ein", 42.0)]})
DELTA_JUNE = _file("Delta Health", "2026-06-01", {"97110": [(T1, "ein", 50.0)]})
DELTA_JULY = _file("Delta Health", "2026-07-01", {"97110": [(T1, "ein", 55.0)]})


def _ingest(cfg, store, name, doc):
    from mrfx.ingest import scan_inbox
    (cfg.inbox_dir / name).write_text(json.dumps(doc))
    list(scan_inbox(cfg, store))


def _window_mismatches(store) -> tuple[int, int]:
    """(flagged-but-not-window, window-but-not-flagged) over the whole spine."""
    from mrfx.store import LATEST_PARTITION
    with store.connect() as con:
        cols = ", ".join(r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'rates_by_tin_tbl' AND column_name <> 'is_latest' "
            "ORDER BY ordinal_position").fetchall())
        a = f"SELECT {cols} FROM rates_by_tin WHERE is_latest"
        b = (f"SELECT {cols} FROM rates_by_tin QUALIFY file_month = max(file_month) "
             f"OVER (PARTITION BY {LATEST_PARTITION})")
        return (con.execute(f"SELECT count(*) FROM ({a} EXCEPT ALL {b})").fetchone()[0],
                con.execute(f"SELECT count(*) FROM ({b} EXCEPT ALL {a})").fetchone()[0])


def _latest(store):
    with store.connect() as con:
        return sorted(con.execute(
            "SELECT payer, tin_value, tin_is_really_npi, billing_code, file_month, negotiated_rate "
            "FROM rates_by_tin WHERE is_latest").fetchall())


@pytest.fixture
def two_payers(cfg, store):
    _ingest(cfg, store, "gamma_2026-05_in-network.json", GAMMA_MAY)
    _ingest(cfg, store, "gamma_2026-06_in-network.json", GAMMA_JUNE)
    _ingest(cfg, store, "delta_2026-06_in-network.json", DELTA_JUNE)
    return store


EXPECTED = [
    ("Delta Health", T1, False, "97110", "2026-06", 50.0),
    ("Gamma Health", T1, False, "97110", "2026-06", 42.0),
    ("Gamma Health", T1, False, "97140", "2026-05", 30.0),
    ("Gamma Health", T1, True, "97110", "2026-05", 35.0),     # the npi-typed twin
    ("Gamma Health", T2, False, "97110", "2026-05", 44.0),
]


def test_flag_is_the_window_after_each_ingest(two_payers):
    assert _latest(two_payers) == EXPECTED
    assert _window_mismatches(two_payers) == (0, 0)


def test_a_newer_month_moves_the_flag_and_a_removal_moves_it_back(cfg, two_payers):
    from mrfx.ingest import forget_file
    _ingest(cfg, two_payers, "delta_2026-07_in-network.json", DELTA_JULY)
    assert ("Delta Health", T1, False, "97110", "2026-07", 55.0) in _latest(two_payers)
    assert ("Delta Health", T1, False, "97110", "2026-06", 50.0) not in _latest(two_payers)
    assert _window_mismatches(two_payers) == (0, 0)
    forget_file(cfg, two_payers, "delta_2026-07_in-network.json")
    assert _latest(two_payers) == EXPECTED
    assert _window_mismatches(two_payers) == (0, 0)
    two_payers.rebuild_rollups()                       # the full build agrees
    assert _latest(two_payers) == EXPECTED and _window_mismatches(two_payers) == (0, 0)


def _strip_flag(store):
    """A spine as an older version built it: no is_latest column."""
    with store.write_lock, store.connect() as con:
        con.execute("ALTER TABLE rates_by_tin_tbl DROP COLUMN is_latest")
        store._register_views(con)


def _api_answers(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    c = TestClient(create_app(cfg, store))
    out = []
    for url in ("/api/rates?grain=tin&latest=1&limit=100",
                "/api/rates?grain=tin&latest=1&cpt=97110&rate_max=43&limit=100",
                "/api/rates?grain=entity&latest=1&limit=100",
                "/api/summary?grain=tin&latest=1",
                "/api/code/97110?grain=tin"):
        out.append(c.get(url).json())
    out.append(c.post("/api/benchmark/market", json={
        "subject": T1, "market": {"payers": ["Gamma Health"], "month": "latest"}}).json())
    return out


def test_an_older_store_gains_the_flag_in_place_with_identical_answers(cfg, two_payers):
    from mrfx.store import Store
    before = _api_answers(cfg, two_payers)
    _strip_flag(two_payers)
    # until the column exists, the VIEW supplies it — the same answers
    assert _api_answers(cfg, two_payers) == before
    two_payers.close()
    st = Store(cfg.store_dir)                          # open = _migrate_latest_flag
    with st.connect() as con:
        assert "is_latest" in {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'rates_by_tin_tbl'").fetchall()}
    assert _latest(st) == EXPECTED and _window_mismatches(st) == (0, 0)
    assert _api_answers(cfg, st) == before
    # and a payer delta after the in-place upgrade writes the column BY NAME
    _ingest(cfg, st, "delta_2026-07_in-network.json", DELTA_JULY)
    assert _window_mismatches(st) == (0, 0)
    st.close()


def test_latest_relations_plan_no_window(two_payers):
    """The point of the flag: no WINDOW over the spine in a TIN/entity-grain
    or Benchmark 'latest' plan (the NPI grain keeps its own, narrowed)."""
    from mrfx.api import FilterSet, rel_sql
    from mrfx.benchmark import _rates_relation
    with two_payers.connect() as con:
        for grain, qp in (("tin", {"latest": "1"}), ("tin", {"latest": "1", "state": "MO"}),
                          ("entity", {"latest": "1"})):
            fs = FilterSet(qp)
            plan = con.execute("EXPLAIN " + rel_sql(grain, fs), fs.params).fetchall()[0][1]
            assert "WINDOW" not in plan, (grain, qp)
        plan = con.execute("EXPLAIN SELECT * FROM "
                           + _rates_relation({"month": "latest"}) + " t").fetchall()[0][1]
        assert "WINDOW" not in plan


def test_aggregate_views_skip_the_name_joins_when_no_filter_needs_them(cfg, two_payers):
    """The summary strip, Code page histogram / payer leaderboard and trend
    read only spine columns: with no name/geo/search filter they read the bare
    spine (no join, no window) — and give the joined relation's answers."""
    from fastapi.testclient import TestClient

    from mrfx.api import FilterSet, create_app, rel_sql, stat_rel
    with two_payers.connect() as con:
        for qp in ({"latest": "1"}, {"latest": "1", "cpt": "97110", "modifier": "base"},
                   {"hide_outliers": "1"}):
            fs = FilterSet(qp)
            plan = con.execute("EXPLAIN " + stat_rel("tin", fs), fs.params).fetchall()[0][1]
            # no name/geo join (the outlier band's own join to its per-code
            # medians is the only one), no window
            assert plan.count("HASH_JOIN") == (1 if fs.hide_outliers else 0), qp
            assert "WINDOW" not in plan, qp
            cols = "unit_id, payer, billing_code, file_month, negotiated_rate, is_dollar_rate"
            a = con.execute(f"SELECT {cols} FROM ({stat_rel('tin', fs)}) ORDER BY ALL",
                            fs.params).fetchall()
            b = con.execute(f"SELECT {cols} FROM ({rel_sql('tin', fs)}) ORDER BY ALL",
                            fs.params).fetchall()
            assert a == b and a, qp
        # a joined column: the practices are resolved first and the spine
        # read by unit (review 10 item 1.3) — the same rows as the joined view
        fs = FilterSet({"latest": "1", "state": "MO"})
        cols = "unit_id, payer, billing_code, file_month, negotiated_rate, is_dollar_rate"
        a = con.execute(f"SELECT {cols} FROM ({stat_rel('tin', fs)}) ORDER BY ALL",
                        fs.params).fetchall()
        b = con.execute(f"SELECT {cols} FROM ({rel_sql('tin', fs)}) ORDER BY ALL",
                        fs.params).fetchall()
        assert a == b
    c = TestClient(create_app(cfg, two_payers))
    s = c.get("/api/summary?grain=tin&latest=1").json()
    # T1 and its npi-typed twin share digits, so one unit_id: 2 practices
    assert (s["n"], s["entities"], s["codes"], s["median"]) == (5, 2, 2, 42.0)


def test_flag_is_exact_through_sliced_builds(cfg, two_payers, monkeypatch):
    """Big stores build the spine in hash slices (and deltas too); the tiny
    test stores above take the one-shot path. An UPDATE that set the flag
    after the slice INSERTs marked 0 of 79M rows on a 99M-rate store — DuckDB
    does not show rows appended in the same transaction to that UPDATE — while
    every one-shot test passed. Force slices of 2 rows."""
    from mrfx.ingest import forget_file
    monkeypatch.setattr(two_payers, "_rollup_partition_rows",
                        lambda split=1, threads=1: 2)
    two_payers.rebuild_rollups()
    with two_payers.connect() as con:
        assert con.execute("SELECT count(*) FROM rates_by_tin_tbl").fetchone()[0] == 6
    assert _latest(two_payers) == EXPECTED and _window_mismatches(two_payers) == (0, 0)
    _ingest(cfg, two_payers, "delta_2026-07_in-network.json", DELTA_JULY)
    assert ("Delta Health", T1, False, "97110", "2026-07", 55.0) in _latest(two_payers)
    assert _window_mismatches(two_payers) == (0, 0)
    forget_file(cfg, two_payers, "delta_2026-07_in-network.json")
    assert _latest(two_payers) == EXPECTED and _window_mismatches(two_payers) == (0, 0)
