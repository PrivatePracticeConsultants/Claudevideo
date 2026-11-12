"""Review 14 (0.8.0): every item of Groups 1-3 of docs/NEXT_STEPS_PROMPT_14.md,
pinned with a small hand-worked store. Each test FAILED on 9617c59 (0.7.7);
the value it got there is in its docstring ("was: …"), run from a `git
archive 9617c59` copy with the editable install's finder stripped (so the
old copy imports only itself — a test of a module 0.7.7 did not have fails
on its import there, and says so).

Group 1 is the user's question — do the analytics take in every newly
loaded file, and can we watch it happen — so its tests drive the real queue,
the real scheduler and the real HTTP routes."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import duckdb
import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from tests.mrfx.test_review13 import _market, doc, load, org, tins
from tests.mrfx.test_review11 import Srv, gz, rate_doc


def _wait(pred, timeout=30.0, step=0.1):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def _slow_server(seconds=120):
    """A link whose download takes `seconds` (64 KB a second): a queue that
    stays busy for the whole test."""
    body = gz(rate_doc(pad=0)) + b"\0" * (64 << 10) * seconds
    s = Srv({})
    s.routes["/slow.json.gz"] = lambda req: (200, {}, body, None, 1.0)
    return s


def _no_rollup_load(cfg, store, payer, prefix, rates, day="2026-06-01"):
    """A file loaded with no analytics update after it — the state a kill
    after 'done', a skipped best-effort refresh or a failed attempt leaves."""
    from mrfx.ingest import ingest_file
    T = tins(prefix, len(rates))
    npis = {t: [f"1{t}"] for t in T}
    d = doc(payer, day, {"97110": dict(zip(T, rates))}, npis)
    p = cfg.inbox_dir / f"{prefix}_{day[:7]}.json"
    p.write_text(json.dumps(d))
    r = ingest_file(cfg, store, p, rebuild_rollups=False)
    assert r["status"] == "done", r
    return T


def _run_queue_thread(cfg, store):
    from mrfx import fetch
    stop = threading.Event()
    t = threading.Thread(target=fetch.run_queue, args=(cfg, store), kwargs={"stop": stop},
                         daemon=True)
    t.start()
    return stop, t


# ---------------------------------------------------------------- group 1


def test_1_1a_a_file_with_no_credit_goes_in_while_a_download_runs(cfg, store):
    """1.1a (P1): a done file the analytics do not hold, with no queue
    credit (its refresh was skipped / a kill / a failure), and ONE long
    download in flight. was: never folded in — the idle probe needs an idle
    queue: after 25 s {'state': 'behind', 'reason': '1 newly loaded file not
    in the analytics yet — added when the queue is idle'}."""
    cfg.enrichment.mode = "off"
    _market(cfg, store, "Alpha Health", "71000", [40, 50, 60])
    _no_rollup_load(cfg, store, "Beta Health", "72000", [41, 51, 61])
    assert store.rollups_stale()
    s = _slow_server()
    try:
        from mrfx.fetch import add_urls
        add_urls(store, [s.base + "/slow.json.gz"])
        stop, t = _run_queue_thread(cfg, store)
        try:
            assert _wait(lambda: s.starts, 15)          # the download is running
            ok = _wait(lambda: not store.rollups_stale(), 25)
            cov = store.analytics_coverage(max_age_seconds=0)
            assert ok, cov
            assert cov["covered_files"] == cov["done_files"] == 2
            assert store.url_queue_counts().get("downloading", 0) == 1   # still busy
        finally:
            stop.set()
            t.join(20)
    finally:
        s.close()


def test_1_1b_the_header_says_when_not_when_the_queue_is_idle(cfg, store):
    """1.1b (P1): mid-grind the header gives the scheduler's real next time.
    was: no scheduler to ask (ImportError: cannot import name 'foldin');
    the header read '1 newly loaded file not in the analytics yet — added
    when the queue is idle' (test_1_1a's baseline value) while files were
    being added mid-grind."""
    from mrfx import foldin
    cfg.enrichment.mode = "off"
    _market(cfg, store, "Alpha Health", "71000", [40, 50, 60])
    f = foldin.ensure(store)
    stop = threading.Event()
    f.start(stop)
    try:
        f.last_end, f.last_duration = time.monotonic(), 40.0     # just updated
        f.note_queue(2)                                          # downloads running
        f.poke()
        _no_rollup_load(cfg, store, "Beta Health", "72000", [41, 51, 61])
        f.last_end, f.last_duration = time.monotonic(), 40.0
        f.note_queue(2)
        reason = TestClient(create_app(cfg, store)).get("/api/stats").json()["analytics"]["reason"]
        assert "when the queue is idle" not in reason
        assert "downloads are running" in reason and "min" in reason, reason
    finally:
        stop.set()
        f.stop()


def _rebuild_part_way(cfg, store, stop_after=3):
    """4 payers built, then a FULL plan over the existing spine stopped
    after `stop_after` slices (a kill / an out-of-memory stop)."""
    import mrfx.store as S
    for i, (payer, pre) in enumerate((("Alpha Health", "71000"), ("Beta Health", "72000"),
                                      ("Gamma Health", "73000"), ("Delta Health", "74000"))):
        _market(cfg, store, payer, pre, [40 + i, 50 + i, 60 + i])
    store.rebuild_rollups()
    with store.connect() as con:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_needs_full', '1')")
    real = S.Store._run_slice
    n = {"k": 0}

    def run_slice(self, con, plan, idx, log):
        if plan["slices"][idx]["t"] == "spine":
            n["k"] += 1
            if n["k"] > stop_after:
                raise RuntimeError("killed")
        return real(self, con, plan, idx, log)
    S.Store._run_slice = run_slice
    try:
        with pytest.raises(RuntimeError):
            store.rebuild_rollups()
    finally:
        S.Store._run_slice = real
    store._clear_rollup_failure()
    store._coverage_cache = None
    return store.rollup_plan()


def test_1_1c_a_rebuild_over_an_existing_spine_still_counts_its_payers(cfg, store):
    """1.1c (P2): during a full build over a spine that already holds every
    payer, the untouched payers' rows still answer every screen. was:
    covered_payers 1 while /api/payers marked 4 covered (assert 1 == 4),
    reason 'A full analytics build is part-way (3 of 6 parts saved) — it
    continues when the queue is idle'."""
    plan = _rebuild_part_way(cfg, store, stop_after=1)
    assert plan is not None and plan["kind"] == "full"
    c = TestClient(create_app(cfg, store))
    a = c.get("/api/stats").json()["analytics"]
    chips = c.get("/api/payers").json()
    assert a["loaded_payers"] == 4
    assert a["covered_payers"] == len(chips["covered"]) >= 3, (a, chips)
    assert "being rebuilt" in a["reason"]


def test_1_1d_an_out_of_memory_incremental_is_retried_not_escalated(cfg, store, monkeypatch):
    """1.1d (P2): a one-file update that runs out of memory at its finest
    slicing is retried as the same update; no whole-store build. was: the
    incremental's failure ran rebuild_rollups() — a full plan (assert
    1 == 0: one full build started)."""
    import mrfx.store as S
    from mrfx.ingest import _rebuild_rollups_best_effort
    _market(cfg, store, "Alpha Health", "71000", [40, 50, 60])
    _no_rollup_load(cfg, store, "Beta Health", "72000", [41, 51, 61])
    fulls = {"n": 0}
    real_full = S.Store.rebuild_rollups

    def full(self, *a, **k):
        fulls["n"] += 1
        return real_full(self, *a, **k)

    def oom(self, con, *a, **k):
        raise duckdb.OutOfMemoryException("Out of Memory Error: could not allocate block")
    monkeypatch.setattr(S.Store, "rebuild_rollups", full)
    monkeypatch.setattr(S.Store, "_apply_rollup_delta_sliced", oom)
    _rebuild_rollups_best_effort(store, "beta file")
    assert fulls["n"] == 0
    assert store.rollup_plan() is None
    a = store.analytics_coverage(max_age_seconds=0)
    assert a["state"] == "failed" and "memory" in a["reason"]


class _SqlLog:
    """Store.connect wrapper that records every statement the update runs."""

    def __init__(self, store, monkeypatch):
        self.sql: list[str] = []
        real = type(store).connect
        log = self.sql

        class Con:
            def __init__(self, c):
                self._c = c

            def execute(self, q, *a, **k):
                log.append(" ".join(str(q).split()))
                return self._c.execute(q, *a, **k)

            def __enter__(self):
                self._c.__enter__()
                return self

            def __exit__(self, *e):
                return self._c.__exit__(*e)

            def __getattr__(self, n):
                return getattr(self._c, n)
        monkeypatch.setattr(type(store), "connect", lambda s: Con(real(s)))


def _spine_dir(store):
    with store.connect() as con:
        sp = con.execute("SELECT * FROM rates_by_tin_tbl ORDER BY ALL").fetchall()
        di = con.execute("SELECT * FROM tin_directory_tbl ORDER BY ALL").fetchall()
    return sp, di


def test_1_1e_a_new_file_of_a_known_payer_recomputes_only_its_tins(cfg, store, monkeypatch):
    """1.1e (P2): the spine is recomputed for the payer's TINs the new file
    carries, and the directory reads only the parts holding them — the
    result byte-identical to a full rebuild. was: 'DELETE FROM
    rates_by_tin_tbl WHERE payer IN ('Alpha Health')' (the whole payer) and
    the directory read every part of the store ('FROM rates')."""
    _market(cfg, store, "Alpha Health", "71000", [40, 50, 60, 70])
    _market(cfg, store, "Beta Health", "72000", [41, 51, 61])
    store.rebuild_rollups()
    # a second file of Alpha touching two of its four TINs (and a new one)
    T = ["710000001", "710000002", "719999999"]
    npis = {t: [f"1{t}"] for t in T}
    d = doc("Alpha Health", "2026-07-01", {"97110": dict(zip(T, [45, 55, 65]))}, npis)
    (cfg.inbox_dir / "alpha_2026-07.json").write_text(json.dumps(d))
    from mrfx.ingest import ingest_file
    assert ingest_file(cfg, store, cfg.inbox_dir / "alpha_2026-07.json",
                       rebuild_rollups=False)["status"] == "done"
    log = _SqlLog(store, monkeypatch)
    store.update_rollups_incremental()
    monkeypatch.undo()
    deletes = [q for q in log.sql if q.startswith("DELETE FROM rates_by_tin_tbl")]
    dir_inserts = [q for q in log.sql if q.startswith("INSERT INTO tin_directory_tbl")]
    assert deletes and all("__delta_file_tins" in q for q in deletes), deletes
    assert dir_inserts and all("__rates_dir" in q for q in dir_inserts), dir_inserts[0][:200]
    inc = _spine_dir(store)
    store.rebuild_rollups()
    assert _spine_dir(store) == inc


def test_1_1f_an_update_moves_its_bar_and_says_what_it_does(cfg, store, monkeypatch):
    """1.1f (P3): an incremental feeds its partitions to the progress; a
    removal says it is taking numbers out. was: every sample (0, 'update',
    'Adding the newly loaded files to the analytics') — 0 % for the whole
    update, and the same words during a removal."""
    import mrfx.store as S
    _market(cfg, store, "Alpha Health", "71000", [40, 50, 60, 70, 80, 90])
    store.rebuild_rollups()
    _no_rollup_load(cfg, store, "Beta Health", "72000", [41, 51, 61, 71])
    monkeypatch.setattr(S, "ROLLUP_PARTITION_ROWS", 2)
    seen = []
    real_ins = S.Store._apply_rollup_delta_sliced

    def watch(self, con, *a, **k):
        stop = threading.Event()

        def sample():
            while not stop.is_set():
                j = self.rollup_job()
                if j:
                    seen.append((j["pct"], j["kind"], self.analytics_coverage(max_age_seconds=0)["reason"]))
                time.sleep(0.002)
        th = threading.Thread(target=sample, daemon=True)
        th.start()
        try:
            return real_ins(self, con, *a, **k)
        finally:
            stop.set()
            th.join(2)
    monkeypatch.setattr(S.Store, "_apply_rollup_delta_sliced", watch)
    store.update_rollups_incremental()
    assert max(p for p, _k, _r in seen) > 0, seen[:5]
    # a removal: forget a file -> the update that takes its numbers out
    seen.clear()
    from mrfx.ingest import forget_file
    forget_file(cfg, store, "72000_2026-06.json")
    reasons = {r for _p, k, r in seen if k == "remove"}
    assert reasons and all("removed file" in r for r in reasons), seen[:3]


def _activity(c):
    r = c.get("/api/analytics/activity")
    assert r.status_code == 200, r.text
    return r.json()


def test_1_2_the_panel_shows_a_file_waiting_then_added(cfg, store):
    """1.2: a file loaded while the app serves appears under "Waiting" with
    why, then under "Added" with its times, and the panel's numbers equal
    /api/stats.analytics. was: ImportError (no mrfx.foldin); the panel
    route answered 404."""
    from mrfx import foldin
    cfg.enrichment.mode = "off"
    _market(cfg, store, "Alpha Health", "71000", [40, 50, 60])
    c = TestClient(create_app(cfg, store))
    store.set_analytics_paused(True)          # hold it so "waiting" is observable
    _no_rollup_load(cfg, store, "Beta Health", "72000", [41, 51, 61])
    d = _activity(c)
    w = {r["file"]: r for r in d["waiting"]["files"]}
    assert "72000_2026-06.json" in w and "paused" in w["72000_2026-06.json"]["why"]
    stats = c.get("/api/stats").json()["analytics"]
    for k in ("covered_files", "done_files", "covered_payers", "loaded_payers", "state"):
        assert d["analytics"][k] == stats[k], k
    f = foldin.ensure(store)
    stop = threading.Event()
    f.start(stop)
    try:
        assert c.post("/api/analytics/pause", json={"paused": False}).json()["paused"] is False
        assert _wait(lambda: not store.rollups_stale(), 20)
        assert _wait(lambda: any(e["file"] == "72000_2026-06.json"
                                 for e in _activity(c)["added"]), 10)
        e = [e for e in _activity(c)["added"] if e["file"] == "72000_2026-06.json"][0]
        assert e["added_at"] and e["loaded_at"] and e["waited_s"] is not None
        assert e["kind"] == "payer update" and e["rows"] == 3
    finally:
        stop.set()
        f.stop()


def test_1_2_run_now_folds_in_during_a_busy_queue(cfg, store):
    """1.2: Run now folds in at the next tick even while the cadence says
    wait (downloads running). was: ImportError: cannot import name
    'foldin' (0.7.7 had no scheduler and no run-now route — 404)."""
    from mrfx import foldin
    cfg.enrichment.mode = "off"
    _market(cfg, store, "Alpha Health", "71000", [40, 50, 60])
    f = foldin.ensure(store)
    stop = threading.Event()
    f.start(stop)
    try:
        assert _wait(lambda: not store.rollups_stale(), 10)
        f.last_end, f.last_duration = time.monotonic(), 600.0    # next one in 30 min
        f.note_queue(3)
        _no_rollup_load(cfg, store, "Beta Health", "72000", [41, 51, 61])
        f.note_queue(3)
        time.sleep(3)
        assert store.rollups_stale()                             # the cadence holds it
        c = TestClient(create_app(cfg, store))
        assert c.post("/api/analytics/run-now").status_code == 200
        assert _wait(lambda: not store.rollups_stale(), 15)
    finally:
        stop.set()
        f.stop()


def test_1_2_pause_holds_fold_ins_and_the_header_says_so(cfg, store):
    """1.2: Pause (persisted) stops new fold-ins and the header says
    'paused'; it survives a restart, as does the panel's history. was:
    ImportError: cannot import name 'foldin' (and the pause route: 404)."""
    from mrfx import foldin
    from mrfx.store import Store
    cfg.enrichment.mode = "off"
    _market(cfg, store, "Alpha Health", "71000", [40, 50, 60])
    c = TestClient(create_app(cfg, store))
    assert c.post("/api/analytics/pause", json={"paused": True}).status_code == 200
    f = foldin.ensure(store)
    stop = threading.Event()
    f.start(stop)
    try:
        _no_rollup_load(cfg, store, "Beta Health", "72000", [41, 51, 61])
        time.sleep(4)
        assert store.rollups_stale()
        a = c.get("/api/stats").json()["analytics"]
        assert a["paused"] and "paused" in a["reason"].lower()
    finally:
        stop.set()
        f.stop()
    added_before = [e["file"] for e in store.analytics_events()]
    s2 = Store(cfg.store_dir)                           # a restart
    assert s2.analytics_paused() is True
    assert [e["file"] for e in s2.analytics_events()] == added_before
    s2.set_analytics_paused(False)
    assert not Store(cfg.store_dir).analytics_paused()


def test_1_2_outputs_are_byte_identical_with_the_panel_open(cfg, store):
    """1.2: polling the panel changes no output byte. was: the panel route
    did not exist ({"detail":"Not Found"}, assert 404 == 200)."""
    cfg.enrichment.mode = "off"
    _market(cfg, store, "Alpha Health", "71000", [40, 50, 60])
    _market(cfg, store, "Beta Health", "72000", [41, 51, 61])
    c = TestClient(create_app(cfg, store))
    paths = ["/api/rates?grain=tin", "/api/summary", "/api/code/97110", "/api/payers"]
    before = [c.get(p).content for p in paths]
    for _ in range(3):
        _activity(c)
    assert [c.get(p).content for p in paths] == before


def test_1_2_status_analytics_prints_the_panel(cfg, store, capsys):
    """1.2: `mrfx status --analytics` prints the same panel as text. was:
    AttributeError: module 'mrfx.cli' has no attribute '_status_analytics'
    (and `mrfx status --analytics` was an argparse error)."""
    from mrfx import cli
    _market(cfg, store, "Alpha Health", "71000", [40, 50, 60])
    cfg.port = 1          # nothing listens there: the store is read directly
    rc = cli._status_analytics(cfg)
    out = capsys.readouterr().out
    assert rc == 0 and "Analytics activity" in out and "Alpha Health" in out
    assert "What this means" in out
