"""0.8.1 — the analytics on a billion-rate store, from the field log.

The user's store (1.18 B rates, 50 payers, 465 files) sat all night at "3 of
50 payers": the update for 456 new files ran as ONE transaction cut into 296
hash partitions, each re-reading all 1.135 B affected rows (a hash predicate
cannot skip a row group) — nothing visible until the last part, everything
lost on a restart; the full build that replaced it read the whole store once
PER directory part (74 x ~4 min) before any payer appeared; and the header
poll waited behind both (one /api/stats took 55 minutes).

Each test fails on 0.8.0 (7f60026) with the value in its docstring.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

import mrfx.api as api_mod
from mrfx import rollplan
from mrfx.api import create_app
from mrfx.store import FINISHED_NOW, RollupNeedsFull


def _spine_rows(store):
    with store.connect() as con:
        return con.execute("SELECT * FROM rates_by_tin_tbl ORDER BY ALL").fetchall()


def _dir_rows(store):
    with store.connect() as con:
        return con.execute("SELECT * FROM tin_directory_tbl ORDER BY ALL").fetchall()


def _write(store, name, payer, recs, finished=FINISHED_NOW):
    recs = [dict(r, source_file=name) for r in recs]
    with store.rates_part_writer(name) as w:
        w.write_batch(recs)
    store.upsert_file(name, payer=payer, file_type="in_network", status="done",
                      rows_emitted=len(recs), finished_at=finished)


def _recs(payer, pi, n_tins=12, edge=True):
    """Rows of one payer with the shapes the directory must carry exactly:
    an NPI-in-the-TIN-slot row, a row with no NPI, a TIN under two TIN types,
    an 'unspecified' discipline, two disciplines on one TIN."""
    out = []
    for t in range(n_tins):
        for ci, code in enumerate(["97110", "97112", "97140", "92507"]):
            for k in range(1 + (t % 3)):
                out.append(dict(
                    payer=payer, tin_value=f"4{pi}{t:07d}",
                    tin_type="npi" if (edge and t == 3 and k == 1) else "ein",
                    npi=None if (edge and t == 5 and k == 0) else f"1{pi}{t:04d}{k:04d}",
                    source_file=f"f_{payer}.json", billing_code=code,
                    billing_code_type="CPT",
                    discipline=("unspecified" if (edge and t == 7) else
                                "slp" if (code == "92507" and t % 2) else "pt"),
                    is_timed=True, billing_class="professional",
                    negotiated_rate=40.0 + pi + ci + (t % 5) + k * 0.5,
                    negotiated_type="negotiated", is_dollar_rate=True,
                    modifier_set=["GP"] if k % 2 else [], service_code=["11"],
                    file_month="2026-06", last_updated_on="2026-06-01",
                    expiration_date=None, schema_version="2.0.0",
                    tin_is_really_npi=bool(edge and t == 9), state="MO"))
    return out


def _seed(store, payers=("P-A", "P-B", "P-C", "P-D")):
    for pi, p in enumerate(payers):
        _write(store, f"f_{p}.json", p, _recs(p, pi))


def _one_shot(store):
    with store.connect() as con:
        store._build_rollup_tables(con, split=1, threads=1)
    return _spine_rows(store), _dir_rows(store)


def test_directory_from_the_one_pass_source_is_byte_identical(cfg, store, monkeypatch):
    """(P0 — the numbers path) A directory cut into k > 1 hash classes reads
    the one-pass source (`Store._dir_source`) and ends with the SAME rows as
    the one-shot build and as k raw reads — NPI-in-the-slot rows, a missing
    NPI, two TIN types, 'unspecified' and mixed disciplines included.
    was: no source existed (AttributeError: '_dir_source'); each of the k
    directory slices read the whole raw store."""
    _seed(store)
    ref = _one_shot(store)
    reads = []
    real = store._dir_source

    def spy(con, plan, log):
        got = real(con, plan, log)
        reads.append(got)
        return got
    monkeypatch.setattr(store, "_dir_source", spy)
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: 3)
    store.rebuild_rollups()
    assert (_spine_rows(store), _dir_rows(store)) == ref
    plan_dirs = [r for r in reads]
    assert plan_dirs and all(r == "rollup_dir_source" for r in plan_dirs)
    # dropped with the plan: never serves the next build
    with store.connect() as con:
        assert not con.execute("SELECT count(*) FROM information_schema.tables "
                               "WHERE table_name = 'rollup_dir_source'").fetchone()[0]


def test_the_source_is_built_once_per_plan_and_survives_a_kill(cfg, store, monkeypatch):
    """(P1) The source is ONE pass per plan: built at the first directory
    slice, reused by the rest, and kept across a kill (the next attempt
    resumes with it, no second pass); the result is identical.
    was: every directory slice was its own pass over the raw rates."""
    _seed(store)
    ref = _one_shot(store)
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: 3)
    built = {"n": 0}
    sql_inserts = []
    real_src = store._dir_source

    def counting(con, plan, log):
        before = store._meta_get(con, "rollup_dir_source")
        got = real_src(con, plan, log)
        if before != plan.get("id"):
            built["n"] += 1
        sql_inserts.append(got)
        return got
    monkeypatch.setattr(store, "_dir_source", counting)
    real = store._run_slice
    state = {"dirs": 0}

    class Killed(Exception):
        pass

    def dies_in_the_directory(con, plan, idx, log):
        if plan["slices"][idx]["t"] == "dir":
            state["dirs"] += 1
            if state["dirs"] == 3:
                raise Killed("process killed between two directory parts")
        return real(con, plan, idx, log)
    monkeypatch.setattr(store, "_run_slice", dies_in_the_directory)
    with pytest.raises(Killed):
        store.rebuild_rollups()
    assert built["n"] == 1
    monkeypatch.setattr(store, "_run_slice", real)
    store.rebuild_rollups()
    assert built["n"] == 1            # the resumed build reused the kept source
    assert (_spine_rows(store), _dir_rows(store)) == ref


def test_a_new_plan_adds_payers_before_the_name_directory(cfg, store, monkeypatch):
    """(P1) A payer is on every screen when its spine slices commit; 0.8.1
    plans them FIRST and the (independent) name directory last.
    was (0.8.0): the plan began with every directory slice — on the field
    store no payer appeared for ~5 hours ('part 1..74 of 1609: practice names
    and places')."""
    _seed(store)
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: 3)
    import logging
    with store.connect() as con:
        plan = store._new_plan(con, "full", logging.getLogger("t"))
    kinds = [s["t"] for s in plan["slices"]]
    assert kinds[0] == "spine"
    assert max(i for i, k in enumerate(kinds) if k == "spine") < kinds.index("prune") \
        < min(i for i, k in enumerate(kinds) if k == "dir")
    # the ETA weights: the first directory slice carries the one pass, the
    # rest are small — never k passes
    dirs = [s for s in plan["slices"] if s["t"] == "dir"]
    assert len(dirs) > 1 and dirs[0]["work"] > 5 * dirs[1]["work"]


def test_a_plan_stored_directory_first_resumes_identically(cfg, store, monkeypatch):
    """(P0 for the field store) A plan made by 0.8.0 — directory first — that
    is part-way when 0.8.1 starts continues in ITS order (no restart from
    scratch: the plan's versions are unchanged) and ends identical; its
    remaining directory parts read the one-pass source.
    was: n/a on 0.8.0 (no source); pinned so 0.8.1 never discards the
    user's running build."""
    _seed(store)
    ref = _one_shot(store)
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: 3)
    import json
    import logging
    with store.connect() as con:
        plan = store._new_plan(con, "full", logging.getLogger("t"))
        sl = plan["slices"]
        old_order = ([s for s in sl if s["t"] == "dir"] + [s for s in sl if s["t"] == "prune"]
                     + [s for s in sl if s["t"] == "spine"])
        plan["slices"] = old_order
        con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_plan', ?)", [json.dumps(plan)])
    real = store._run_slice
    done = {"n": 0}

    class Killed(Exception):
        pass

    def stop_after_two(con, plan, idx, log):
        if done["n"] == 2:
            raise Killed("stopped part-way through the directory")
        done["n"] += 1
        return real(con, plan, idx, log)
    monkeypatch.setattr(store, "_run_slice", stop_after_two)
    with pytest.raises(Killed):
        store.rebuild_rollups()
    p = store.rollup_plan()
    assert p is not None and p["next"] == 2 and p["slices"][0]["t"] == "dir"
    monkeypatch.setattr(store, "_run_slice", real)
    store.rebuild_rollups()
    assert (_spine_rows(store), _dir_rows(store)) == ref


def test_a_large_update_goes_through_the_durable_build(cfg, store, monkeypatch):
    """(P0 — the field store) An update that would re-read its affected rows
    more than one whole-store pass's worth (parts x rows > all rows) is handed
    to the payer-by-payer build instead: `update_rollups_incremental` raises
    RollupNeedsFull('a large update …'), `foldin.fold_in` runs the plan, and
    the result equals a full build.
    was: the incremental ran it as one transaction of 296 hash partitions
    (on the field store 456 files / 1.135 B rows never finished in a night)."""
    from mrfx.foldin import fold_in
    _seed(store, payers=("P-A",))
    store.rebuild_rollups()                       # a built spine: 1 payer
    for pi, p in enumerate(("P-B", "P-C", "P-D"), start=1):
        _write(store, f"f_{p}.json", p, _recs(p, pi))
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: 25)
    with pytest.raises(RollupNeedsFull, match="large update"):
        store.update_rollups_incremental()
    assert store.rollup_plan() is None            # nothing written by the refusal
    seen = []
    real = store._run_slice
    monkeypatch.setattr(store, "_run_slice",
                        lambda con, plan, idx, log: (seen.append(plan["kind"]),
                                                     real(con, plan, idx, log))[1])
    fold_in(store, "test")
    assert seen and set(seen) == {"full"}
    assert not store.rollups_stale()
    got = (_spine_rows(store), _dir_rows(store))
    assert got == _one_shot(store)


def test_a_small_update_stays_payer_scoped(cfg, store, monkeypatch):
    """(no step backwards) A new file small against the store stays the fast
    payer-scoped incremental (one partition), not a whole-store plan."""
    _seed(store)
    store.rebuild_rollups()
    _write(store, "f_new.json", "P-A", _recs("P-A", 0, n_tins=1, edge=False)[:4])
    called = []
    real = store._run_slice
    monkeypatch.setattr(store, "_run_slice",
                        lambda *a, **k: (called.append(1), real(*a, **k))[1])
    store.update_rollups_incremental()
    assert not called and not store.rollups_stale()


def test_store_stats_never_counts_distinct_over_the_raw_rates(cfg, store, monkeypatch):
    """(P1 — the 55-minute header) While the analytics are behind and the
    per-part sets are not complete, the header counts never run a
    count(DISTINCT) over the raw rates.
    was: `SELECT count(*), count(DISTINCT tin_value), count(DISTINCT payer)
    FROM rates` on every recount — 1.18 B rows behind a running update."""
    _seed(store)
    store.rebuild_rollups()
    _write(store, "f_late.json", "P-Z", _recs("P-Z", 7))      # analytics now behind
    monkeypatch.setattr(store, "loaded_inventory",
                        lambda *a, **k: {"payers": [], "months": [], "tins": 0,
                                         "rows_by_payer": {}, "complete": False})
    seen = []
    real_connect = store.connect

    class Spy:
        def __init__(self, cm):
            self.cm = cm

        def __enter__(self):
            con = self.cm.__enter__()
            real_exec = con.execute

            class C:
                def __getattr__(_, n):
                    return getattr(con, n)

                def execute(_, sql, *a, **k):
                    seen.append(sql)
                    return real_exec(sql, *a, **k)
            return C()

        def __exit__(self, *a):
            return self.cm.__exit__(*a)
    monkeypatch.setattr(store, "connect", lambda *a, **k: Spy(real_connect(*a, **k)))
    store._store_stats_cache = None
    got = store.store_stats(max_age_seconds=0)
    raw_distinct = [s for s in seen if "count(DISTINCT" in s and "FROM rates" in s
                    and "rates_by_tin" not in s]
    assert not raw_distinct, raw_distinct
    assert got["payers"] == 5 and got["rates"] > 0


def test_the_header_poll_answers_while_its_counts_are_slow(cfg, store, monkeypatch):
    """(P1 — the 55-minute header) /api/stats answers within STATS_WAIT_S even
    when computing its body takes far longer, with the last body; concurrent
    polls start ONE computation, not one each.
    was: every poll computed inline — 5 s .. 3,323 s per call in the field
    log, a thread per poll stacked behind the update."""
    _seed(store)
    store.rebuild_rollups()
    monkeypatch.setattr(api_mod, "STATS_WAIT_S", 0.5)
    app = create_app(cfg, store)
    c = TestClient(app)
    first = c.get("/api/stats").json()
    assert first["payers"] == 4
    calls = {"n": 0}
    real = store.store_stats

    def slow(*a, **k):
        calls["n"] += 1
        time.sleep(3.0)
        return real(*a, **k)
    monkeypatch.setattr(store, "store_stats", slow)
    t0 = time.monotonic()
    out = []
    ths = [threading.Thread(target=lambda: out.append(c.get("/api/stats").json()))
           for _ in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    took = time.monotonic() - t0
    assert took < 2.5, took
    assert all(o["payers"] == 4 for o in out)       # the last body, not an error
    time.sleep(3.5)
    assert calls["n"] == 1                           # one computation for four polls


def test_coverage_is_single_flight(cfg, store, monkeypatch):
    """(P1) Concurrent coverage callers start one computation; the others get
    the last answer. was: every caller recomputed (four header / Files / panel
    polls in parallel on a billion rows)."""
    _seed(store)
    store.rebuild_rollups()
    store.analytics_coverage(max_age_seconds=0)
    calls = {"n": 0}
    real = store._analytics_coverage

    def slow():
        calls["n"] += 1
        time.sleep(1.0)
        return real()
    monkeypatch.setattr(store, "_analytics_coverage", slow)
    ths = [threading.Thread(target=lambda: store.analytics_coverage(max_age_seconds=0))
           for _ in range(5)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert calls["n"] == 1


def test_dir_slices_weights():
    """The plan's ETA weights: one pass at the first directory slice."""
    one = rollplan.dir_slices(1000, 10)
    assert len(one) == 7 and one[0]["work"] == 250 and all(s["work"] < 10 for s in one[1:])
    assert rollplan.dir_slices(10, 10) == [{"t": "dir", "k": 1, "i": 0, "rows": 10, "work": 2}]
