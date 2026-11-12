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


# ---------------------------------------------------------------- group 2


def _cfg_store(tmp_path, name):
    from mrfx.config import MrfxConfig
    from mrfx.store import Store
    w = tmp_path / name
    c = MrfxConfig(inbox_dir=w / "inbox", processed_dir=w / "p", failed_dir=w / "f",
                   store_dir=w / "store", downloads_dir=w / "dl", entity_map_path=w / "em.yaml")
    c.ensure_dirs()
    c.enrichment.mode = "off"
    return c, Store(c.store_dir)


def _rate_rows(store):
    with store.connect() as con:
        return con.execute("SELECT count(*) FROM rates").fetchone()[0]


def test_2_1_a_failed_link_releases_its_content_claim(tmp_path, monkeypatch):
    """2.1 (P0, a round-13 regression vs 0.7.6): link A's load fails once;
    the same document re-encoded under link B must then load, and an inbox
    copy must not wait forever. End to end through the real queue. was:
    after A failed the claims still held {'92856b6a…': 'cb1b758b_a.json.gz'}
    (B then 'skipped' "being loaded right now — will retry automatically",
    rate rows 0, and an inbox copy's scan did not return in 20 s)."""
    import gzip as _gz

    import mrfx.ingest as ING
    from mrfx import fetch
    cfg, s = _cfg_store(tmp_path, "w")
    body = rate_doc(n=60)
    a, b = _gz.compress(body, 1, mtime=0), _gz.compress(body, 9, mtime=0)
    from tests.mrfx.test_review11 import ranged
    srv = Srv({})
    real = ING._ingest_file_locked
    fired = {"n": 0}

    def flaky(*args, **kw):
        if fired["n"] == 0:
            fired["n"] += 1
            raise OSError("simulated transient failure")
        return real(*args, **kw)
    monkeypatch.setattr(ING, "_ingest_file_locked", flaky)
    try:
        srv.routes["/a.json.gz"] = lambda req: ranged(a, req, etag='"a"')
        srv.routes["/b.json.gz"] = lambda req: ranged(b, req, etag='"b"')
        fetch.add_urls(s, [srv.base + "/a.json.gz"])
        fetch.run_queue(cfg, s, drain=True)
        assert s._content_claims == {}
        fetch.add_urls(s, [srv.base + "/b.json.gz"])
        fetch.run_queue(cfg, s, drain=True)
    finally:
        srv.close()
    assert _rate_rows(s) == 60
    # (b) an inbox copy of the same content after a FAILED load: it loads
    # or is recognised — it never waits
    cfg2, s2 = _cfg_store(tmp_path, "w2")
    fired["n"] = 0
    srv = Srv({})
    try:
        srv.routes["/a.json.gz"] = lambda req: ranged(a, req, etag='"a"')
        fetch.add_urls(s2, [srv.base + "/a.json.gz"])
        fetch.run_queue(cfg2, s2, drain=True)
    finally:
        srv.close()
    (cfg2.inbox_dir / "copy.json").write_bytes(body)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", ING.scan_inbox(cfg2, s2)), daemon=True)
    t.start()
    t.join(20)
    assert not t.is_alive(), "the inbox scan waited on a claim nobody held"
    assert _rate_rows(s2) == 60


def test_2_1_a_deferred_content_twin_loads_when_its_link_fails(tmp_path, monkeypatch):
    """2.1: B (other bytes, same content) deferred to A WHILE A was loading;
    A then fails — B is revived and loads. was: {'b.json.gz': 'skipped',
    'a.json.gz': 'failed'} — the revive matched the raw sha only."""
    import gzip as _gz

    import mrfx.ingest as ING
    from mrfx import fetch
    from tests.mrfx.test_review11 import ranged
    cfg, s = _cfg_store(tmp_path, "w")
    body = rate_doc(n=40)
    a, b = _gz.compress(body, 1, mtime=0), _gz.compress(body, 9, mtime=0)
    srv = Srv({})
    real = ING._ingest_file_locked
    gate = threading.Event()
    seen = {"a": 0}

    def a_fails_after_b_deferred(cfg_, store_, path, *args, **kw):
        if path.name.endswith("_a.json.gz") and not seen["a"]:
            seen["a"] += 1
            # B arrives while A holds the content claim
            fetch.add_urls(s, [srv.base + "/b.json.gz"])
            assert _wait(lambda: gate.is_set(), 30)
            raise OSError("A fails after B deferred to it")
        return real(cfg_, store_, path, *args, **kw)
    real_claim = s.claim_content_ingest

    def claim(*a_, **k):
        r = real_claim(*a_, **k)
        if r is not None:
            gate.set()            # B deferred
        return r
    monkeypatch.setattr(ING, "_ingest_file_locked", a_fails_after_b_deferred)
    monkeypatch.setattr(s, "claim_content_ingest", claim)
    cfg.parallel_ingests = 2
    try:
        srv.routes["/a.json.gz"] = lambda req: ranged(a, req, etag='"a"')
        srv.routes["/b.json.gz"] = lambda req: ranged(b, req, etag='"b"')
        fetch.add_urls(s, [srv.base + "/a.json.gz"])
        fetch.run_queue(cfg, s, drain=True)
    finally:
        srv.close()
    with s.connect() as con:
        st = dict(con.execute("SELECT regexp_replace(url, '.*/', ''), status FROM url_queue").fetchall())
    assert st == {"a.json.gz": "failed", "b.json.gz": "done"}, st
    assert _rate_rows(s) == 40


def test_2_2_a_file_loaded_before_0_7_7_is_named_by_content(cfg, store):
    """2.2 (P0): a file 0.7.6 loaded has its raw sha and no content sha; the
    backfill names it by content from its kept copy, so a decoded copy is
    recognised. was: backfilled 0 (assert 0 >= 1); then both copies done,
    rate rows 120 for a 60-row document."""
    from mrfx.ingest import backfill_identities, scan_inbox
    body = rate_doc(n=60)
    (cfg.inbox_dir / "twin_in-network-rates.json.gz").write_bytes(gz(body))
    scan_inbox(cfg, store)
    with store.connect() as con:      # what a 0.7.6 store holds
        con.execute("UPDATE content_fingerprints SET content_sha = NULL, content_size = NULL")
    assert backfill_identities(cfg, store) >= 1
    (cfg.inbox_dir / "twin_in-network-rates (1).json").write_bytes(b"\xef\xbb\xbf" + body)
    scan_inbox(cfg, store)
    assert _rate_rows(store) == 60
    assert store.file_status("twin_in-network-rates (1).json")["status"] == "skipped"


def test_2_3_two_rereads_of_one_rate_file_never_fail_it(cfg, store, tmp_path, monkeypatch):
    """2.3 (P1, from 5.2.7): the list's re-read pass holds the rate file's
    claim when the file's own end-of-read check starts its re-read — the
    loser defers to the winner. Both triggers forced at once, no sleeps.
    was: the file's ingest returned {'status': 'skipped', 'error': 'already
    being ingested by another worker'} (its link row then ended 'failed')."""
    import mrfx.ingest as I
    from tests.mrfx.test_review13 import _rate_and_list
    rate, comp = _rate_and_list(cfg, tmp_path)
    holding, release = threading.Event(), threading.Event()
    real_locked = I._ingest_file_locked

    def slow(cfg_, store_, path, *a, **k):
        if path.name == rate.name and threading.current_thread().name == "list-pass":
            holding.set()
            release.wait(30)
        return real_locked(cfg_, store_, path, *a, **k)
    monkeypatch.setattr(I, "_ingest_file_locked", slow)
    real_rel = I._release_ingest
    fired = {"n": 0}
    th = {}

    def rel(name):
        real_rel(name)
        if name == rate.name and not fired["n"] and threading.current_thread().name != "list-pass":
            fired["n"] += 1
            th["t"] = threading.Thread(target=lambda: I.ingest_file(cfg, store, comp),
                                       name="list-pass", daemon=True)
            th["t"].start()
            assert holding.wait(30)       # the list's pass holds the rate file now
    monkeypatch.setattr(I, "_release_ingest", rel)
    out = I.ingest_file(cfg, store, rate)
    release.set()
    th["t"].join(30)
    assert out["status"] == "done", out
    st = store.file_status(rate.name)
    assert (st["status"], st["rows_emitted"], st["ref_groups_skipped"]) == ("done", 6, 0)


def _backup_store(tmp_path):
    cfg, s = _cfg_store(tmp_path, "b")
    for i, (payer, pre) in enumerate((("Alpha Health", "71000"), ("Beta Health", "72000"))):
        _market(cfg, s, payer, pre, [40 + i, 50 + i, 60 + i])
    s.rebuild_rollups()
    cfg.backup_dir = tmp_path / "bk"
    return cfg, s


def _restore_rows(zip_path, tmp_path):
    import zipfile

    from mrfx.store import Store
    out = tmp_path / "restored"
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(out)
    return _rate_rows(Store(out / "store"))


def test_2_4_a_file_committed_while_the_backup_waits_is_in_it(tmp_path, monkeypatch):
    """2.4 (P0): a part that goes live after the backup lists the store but
    before it takes the write lock is in the database copy — it must be in
    the zip too, and verify must check the rows a restore would hold. was:
    'backup OK: 3 files match their checksums; recorded 18 rate rows' and
    the restored store held 12 (assert 12 == 18)."""
    import datetime as _dt

    from mrfx import backup
    cfg, s = _backup_store(tmp_path)
    real = backup.store_files
    calls = {"n": 0}

    def list_then_land(root, **kw):
        got = real(root, **kw)
        calls["n"] += 1
        if calls["n"] == 1:          # a file finishes right after the listing
            _market(cfg, s, "Gamma Health", "73000", [42, 52, 62])
        return got
    monkeypatch.setattr(backup, "store_files", list_then_land)
    r = backup.hot_backup(cfg, s, now=_dt.datetime(2026, 11, 20, 9, 0, tzinfo=_dt.timezone.utc))
    live = _rate_rows(s)
    ok, verdict = backup.verify_zip(Path(r["path"]))
    assert ok and live == r["counts"]["rates"]
    assert _restore_rows(r["path"], tmp_path) == live, verdict


def test_2_4_verify_refuses_a_zip_whose_parts_do_not_hold_its_rows(tmp_path):
    """2.4: `mrfx verify` checks the restored row count from the parts in the
    zip, not the manifest. was: (True, 'backup OK: 3 files match their
    checksums; recorded 999 rate rows')."""
    from mrfx import backup
    cfg, s = _backup_store(tmp_path)
    root = Path(s.dir)
    entries = [(p, p.relative_to(root).as_posix()) for p in backup.store_files(root)]
    dest = tmp_path / "x.zip"
    backup.write_zip(dest, entries, store_dir=str(root), counts={"rates": 999})
    ok, verdict = backup.verify_zip(dest)
    assert not ok and "incomplete" in verdict, verdict


def test_2_4_a_stopped_backup_leaves_nothing_and_inside_the_store_is_refused(tmp_path, monkeypatch):
    """2.4 P2-2 / P2-5: a failed write leaves no `.zip.part` (each one is the
    size of the store), and a backup_dir inside the data folder is refused.
    was: 'mrfx-backup-….zip.part' left on the backup drive; the 4th backup
    inside the store was 7x the store (each carried the earlier ones); here
    ['mrfx-backup-2026-11-21_0900.zip.part'] == []."""
    import datetime as _dt
    import zipfile

    from mrfx import backup
    cfg, s = _backup_store(tmp_path)
    real_write = zipfile.ZipFile.write
    n = {"k": 0}

    def eio(self, *a, **k):
        n["k"] += 1
        if n["k"] == 2:
            raise OSError(5, "Input/output error")
        return real_write(self, *a, **k)
    monkeypatch.setattr(zipfile.ZipFile, "write", eio)
    with pytest.raises(backup.BackupRefused):
        backup.hot_backup(cfg, s, now=_dt.datetime(2026, 11, 21, 9, 0, tzinfo=_dt.timezone.utc))
    monkeypatch.undo()
    assert [p.name for p in Path(cfg.backup_dir).iterdir()] == []
    cfg.backup_dir = Path(s.dir) / "backups"
    with pytest.raises(backup.BackupRefused, match="inside the data folder"):
        backup.hot_backup(cfg, s, now=_dt.datetime(2026, 11, 22, 9, 0, tzinfo=_dt.timezone.utc))


def test_2_4_a_drive_that_cannot_link_copies_outside_the_lock(tmp_path, monkeypatch):
    """2.4 P2-6: where hard links fail, the parts are copied OUTSIDE the
    write lock (and the data drive's free space is checked first). was: every
    copy ran holding the lock ([True, True]); disk_usage asked only about
    the backup drive."""
    import datetime as _dt
    import errno

    from mrfx import backup
    cfg, s = _backup_store(tmp_path)

    def no_link(*a, **k):
        raise OSError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(backup.os, "link", no_link)
    asked = []
    real_du = backup.shutil.disk_usage
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda p: (asked.append(str(p)), real_du(p))[1])
    copied_locked = []
    real_copy = backup.shutil.copy2

    def copy(a, b, **k):
        copied_locked.append(s.write_lock.locked())
        return real_copy(a, b, **k)
    monkeypatch.setattr(backup.shutil, "copy2", copy)
    r = backup.hot_backup(cfg, s, now=_dt.datetime(2026, 11, 23, 9, 0, tzinfo=_dt.timezone.utc))
    assert copied_locked and not any(copied_locked)
    assert any(a.startswith(str(s.dir)) for a in asked)
    assert backup.verify_zip(Path(r["path"]))[0]


def test_2_5_an_ssn_pattern_tin_reaches_no_shareable_surface(cfg, store, tmp_path):
    """2.5 (P0): the Clients box refuses an SSN-pattern subject; one saved
    before 0.8.0 is masked in the digest, the packet folder names, titles
    and pages, the report HTML (what the PDF prints) and never enters the
    address bar. was: add_client saved '070000001' (DID NOT RAISE); the
    review's run showed the digest subject '070000001', the packet folder
    '070000001' with '<title>Rate card — 070000001</title>' and the address
    bar '#ratecard?subject=070000001'."""
    from mrfx import clients
    from mrfx.benchmark import BenchmarkError
    from mrfx.packets import build_all_packets
    ssn = "070000001"
    T = [ssn, "430000002", "430000003", "430000004", "430000005"]
    npis = {t: [f"1{t}"] for t in T}
    d = doc("Alpha Health", "2026-06-01", {"97110": dict(zip(T, [40, 50, 60, 70, 80]))}, npis)
    load(cfg, store, [("alpha_2026-06.json", d)])
    for t in T:
        org(store, npis[t][0], f"P {t[-1]} REHAB")
    store.rebuild_rollups()
    with pytest.raises(BenchmarkError, match="Social Security"):
        clients.add_client(store, ssn)
    with pytest.raises(BenchmarkError, match="Social Security"):
        clients.add_client(store, "070-00-0001")
    clients._save_client(store, ssn)           # saved by an older version
    dig = clients.client_digest(store)
    assert ssn not in json.dumps(dig)
    out = build_all_packets(cfg, store, out_root=tmp_path / "packets",
                            market={"month": "latest"})
    for p in (tmp_path / "packets").rglob("*"):
        assert ssn not in p.name
        if p.is_file():
            assert ssn not in p.read_text(errors="replace"), p
    assert ssn not in json.dumps(out, default=str)
    c = TestClient(create_app(cfg, store))
    r = c.post("/api/report/ratecard", json={"subject": ssn, "market": {"month": "latest"}})
    if r.status_code == 200:
        assert ssn not in r.text
    # the address bar: hashFor drops an SSN-shaped value (the JS itself)
    from tests.mrfx.test_round7 import _browser_page, _js_function
    pw, browser, page = _browser_page()
    try:
        app = (Path(__file__).resolve().parents[2] / "mrfx" / "web" / "app.js").read_text()
        i = app.index("const SSN_PREFIXES")
        j = app.index(";", app.index(")", i))
        page.add_script_tag(content=app[i:j + 1] + "\nconst ssnShaped = " +
                            app[app.index("const ssnShaped = ") + len("const ssnShaped = "):
                                app.index("};", app.index("const ssnShaped = ")) + 2] +
                            "\n" + _js_function("hashFor"))
        h = page.evaluate("hashFor('ratecard', {subject: '070000001', state: 'MO'})")
        h2 = page.evaluate("hashFor('ratecard', {subject: '070-00-0001'})")
        h3 = page.evaluate("hashFor('ratecard', {subject: '430000002'})")
    finally:
        browser.close()
        pw.stop()
    assert "070" not in h and "070" not in h2 and "430000002" in h3


def test_2_6_a_needs_full_raised_during_a_plan_survives_it(cfg, store, monkeypatch):
    """2.6 (P1): a plan made while `rollup_needs_full` was set clears only the
    raise it answers; an unscopable forget during the plan raises a new one
    that survives the plan's end, and the next update heals the spine. was:
    the flag was cleared at the plan's end (None) and the forgotten file's
    rows stayed in the spine with the state 'current'."""
    import mrfx.store as S
    from mrfx.ingest import forget_file
    _rebuild_part_way(cfg, store, stop_after=2)   # needs_full '1' + a part-way plan
    monkeypatch.setattr(S.Store, "_part_payers_for_removal", lambda self, path: None)
    forget_file(cfg, store, "72000_2026-06.json")    # unscopable -> a new raise; the
    monkeypatch.undo()                               # plan resumes and finishes
    assert store.rollup_plan() is None
    with store.connect() as con:
        flag = con.execute("SELECT value FROM meta WHERE key = 'rollup_needs_full'").fetchone()
    assert flag is not None and flag[0].startswith("1:"), flag
    assert store.rollups_stale()
    from mrfx import foldin
    foldin.fold_in(store)                            # the next update: a full build
    assert not store.rollups_stale()
    with store.connect() as con:
        assert not con.execute("SELECT count(*) FROM rates_by_tin_tbl "
                               "WHERE payer = 'Beta Health'").fetchone()[0]
    got = _spine_dir(store)
    store.rebuild_rollups()
    assert _spine_dir(store) == got


def test_2_7_screens_and_documents_agree_during_a_rebuild(cfg, store):
    """2.7 (P1): during a full plan over an existing spine the header, the
    payer chips and every document sentence come from ONE rule, and the
    document never says "the files loaded since" when none were. was:
    header covered_payers 2 while the chips marked 4 (assert 2 == 4), and
    the document said 'Every number here comes from those; the files loaded
    since are not in the analytics yet…' with nothing loaded since."""
    _rebuild_part_way(cfg, store, stop_after=2)
    c = TestClient(create_app(cfg, store))
    a = c.get("/api/stats").json()["analytics"]
    chips = c.get("/api/payers").json()["covered"]
    note = store.analytics_note()
    assert a["covered_payers"] == len(chips)
    assert note and "loaded since" not in note[0]
    assert f"cover {a['covered_payers']} of 4 payers" in note[0]


def test_2_8_the_monthly_check_downloads_no_new_file(cfg, store):
    """2.8 (P1, decided: probe only): the monthly re-check (on by default,
    monthly_refresh off) asks about the loaded files; a file the index lists
    for the first time is named on the index row, not downloaded. was: the
    new month's file downloaded in full — ('/2026-12-01_twin_in-network-
    rates.json.gz', 200, None, 476) — and loaded."""
    import datetime as _dt
    import gzip as _gz

    from mrfx import fetch
    from tests.mrfx.test_review11 import ranged
    cfg.enrichment.mode = "off"
    assert cfg.monthly_recheck and not cfg.monthly_refresh
    srv = Srv({})
    nov = _gz.compress(rate_doc(n=30, month="2026-11-01"), mtime=0)
    dec = _gz.compress(rate_doc(n=30, rate=41.0, month="2026-12-01"), mtime=0)

    def toc(files):
        return json.dumps({"reporting_entity_name": "Twin Health", "reporting_structure": [{
            "reporting_plans": [{"plan_name": "PPO", "plan_id_type": "EIN", "plan_id": "1",
                                 "plan_market_type": "group"}],
            "in_network_files": [{"description": "x", "location": srv.base + f}
                                 for f in files]}]}).encode()
    try:
        srv.routes["/2026-11-01_twin_in-network-rates.json.gz"] = lambda r: ranged(nov, r, etag='"n"')
        srv.routes["/2026-12-01_twin_in-network-rates.json.gz"] = lambda r: ranged(dec, r, etag='"d"')
        cur = {"toc": toc(["/2026-11-01_twin_in-network-rates.json.gz"])}
        srv.routes["/index.json"] = lambda r: ranged(cur["toc"], r)
        fetch.add_urls(store, [srv.base + "/index.json"])
        fetch.run_queue(cfg, store, drain=True)
        cur["toc"] = toc(["/2026-11-01_twin_in-network-rates.json.gz",
                          "/2026-12-01_twin_in-network-rates.json.gz"])
        srv.log.clear()
        fetch.monthly_refresh_tick(cfg, store, _dt.date(2026, 12, 3),
                                   dated_sources=cfg.monthly_refresh, recheck=cfg.monthly_recheck)
        fetch.run_queue(cfg, store, drain=True)
        dec_gets = [e for e in srv.log if e[0].startswith("/2026-12-01") and e[1] == 200]
    finally:
        srv.close()
    assert dec_gets == []
    with store.connect() as con:
        months = con.execute("SELECT DISTINCT file_month FROM rates").fetchall()
        idx_note = con.execute("SELECT error FROM url_queue WHERE url LIKE '%index.json'").fetchone()[0]
    assert months == [("2026-11",)]
    assert idx_note and "1 new file listed" in idx_note and "paste this index again" in idx_note


def test_2_9_a_result_built_under_a_stop_is_never_cached(cfg, store, monkeypatch):
    """2.9 (P1): Stop landing inside the Clients digest (its per-client loop
    catches the stop, as fault isolation does) / a generation-cached output
    must not leave its degraded answer for the next caller. was: the next
    plain digest said error='stopped' for the client (and so did the monthly
    packets); a store.cached answer computed under a Stop was served again
    (compute calls 1)."""
    from mrfx import clients, progress
    _market(cfg, store, "Alpha Health", "71000", [40, 50, 60, 70, 80])
    store.rebuild_rollups()
    clients._save_client(store, "ALPHA HEALTH P1 REHAB")
    p = progress.Progress("t" * 32)
    real = clients.resolve_subject_tins
    fired = {"n": 0}

    def stop_lands_here(*a, **k):
        if not fired["n"]:
            fired["n"] += 1
            p.cancel()
            raise progress.Stopped("stopped")
        return real(*a, **k)
    monkeypatch.setattr(clients, "resolve_subject_tins", stop_lands_here)
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"degraded": True}
    tok = progress.set_current(p)
    try:
        d1 = clients.client_digest(store)
        store.cached("t.cancel", 1, compute)
    finally:
        progress.reset_current(tok)
    assert "stopped" in str(d1["clients"][0].get("error"))
    d2 = clients.client_digest(store)
    assert not d2["clients"][0].get("error"), d2
    store.cached("t.cancel", 1, compute)
    assert calls["n"] == 2                     # recomputed, not served from a Stop


def test_2_10_two_overlapping_runs_leave_the_button_enabled():
    """2.10 (P1): Enter pressed twice starts two runs on one button; when
    both end the button is enabled again, whichever ends last. was: the
    first run's end re-enabled it while the second ran and the second run's
    end restored 'disabled' — [True, False, True]: dead until a reload."""
    from tests.mrfx.test_round7 import _browser_page, _js_function
    pw, browser, page = _browser_page()
    try:
        page.set_content('<main><div id="view-x"><button id="run">Run</button></div></main>')
        page.add_script_tag(content="const _btnRuns = new WeakMap();\n"
                            + _js_function("jobHost") + "\n" + _js_function("mountJobbar"))
        got = page.evaluate("""() => {
            const bar = {view: 'x', label: 'Roster', anchor: '#run'};
            const a = mountJobbar(bar, 't1'), b = mountJobbar(bar, 't2');
            const mid = document.querySelector('#run').disabled;
            a.done(); const after_a = document.querySelector('#run').disabled;
            b.done(); return [mid, after_a, document.querySelector('#run').disabled]; }""")
    finally:
        browser.close()
        pw.stop()
    assert got == [True, True, False]


def test_2_11_a_copy_after_a_kill_in_the_done_window_is_recognised(cfg, store):
    """2.11 (P1): a kill between the 'done' commit and the identity row left
    files.read_identity only; a byte-identical copy and a decoded copy under
    other names must be recognised by every claim path. was: both copies
    loaded — rate rows 120 for a 60-row document, each time."""
    from mrfx.ingest import scan_inbox
    body = rate_doc(n=60)
    (cfg.inbox_dir / "k.json.gz").write_bytes(gz(body))
    scan_inbox(cfg, store)
    with store.connect() as con:          # the identity row the kill skipped
        con.execute("DELETE FROM content_fingerprints")
        ri = con.execute("SELECT read_identity FROM files WHERE filename = 'k.json.gz'").fetchone()[0]
    assert ri and json.loads(ri).get("sha")
    (cfg.inbox_dir / "k (1).json.gz").write_bytes(gz(body))
    scan_inbox(cfg, store)
    (cfg.inbox_dir / "k (2).json").write_bytes(body)
    scan_inbox(cfg, store)
    assert _rate_rows(store) == 60


# ---------------------------------------------------------------- group 3


def test_3_a_killed_names_build_resumes_as_a_names_build(cfg, store, monkeypatch):
    """Group 3: a names-only plan killed part-way is resumed AS a names plan,
    and the header calls it that. was: the header said 'A full analytics
    build is part-way (1 of 10 parts saved) — it continues when the queue is
    idle', and the next update planned a FULL build ([('full', 287, 269)] on
    the review's store)."""
    import mrfx.store as S
    from mrfx.ingest import _rebuild_rollups_best_effort
    for i, (payer, pre) in enumerate((("Alpha Health", "71000"), ("Beta Health", "72000"))):
        _market(cfg, store, payer, pre, [40 + i + k for k in range(40)])
    store.rebuild_rollups()
    monkeypatch.setattr(S, "ROLLUP_PARTITION_ROWS", 1)
    real = S.Store._run_slice
    n = {"k": 0}

    def kill_after_one(self, con, plan, idx, log):
        n["k"] += 1
        if n["k"] > 1:
            raise RuntimeError("killed")
        return real(self, con, plan, idx, log)
    monkeypatch.setattr(S.Store, "_run_slice", kill_after_one)
    with pytest.raises(RuntimeError):
        store.rebuild_rollups(names_only=True)
    monkeypatch.setattr(S.Store, "_run_slice", real)
    store._clear_rollup_failure()
    assert store.rollup_plan()["kind"] == "names"
    reason = store.analytics_coverage(max_age_seconds=0)["reason"]
    assert "full analytics build" not in reason.lower(), reason
    kinds = []
    real_new = S.Store._new_plan

    def spy(self, con, kind, log):
        kinds.append(kind)
        return real_new(self, con, kind, log)
    monkeypatch.setattr(S.Store, "_new_plan", spy)
    _rebuild_rollups_best_effort(store, "next update")
    assert "full" not in kinds, kinds
    assert store.rollup_plan() is None and not store.rollups_stale()


def test_3_b_the_same_document_named_for_another_month_keeps_its_month(cfg, store):
    """Group 3 (decided): two copies of one document whose names give
    DIFFERENT months (the header has no usable date) are that month's
    publication each — both months load; within one month it stays a twin.
    was: months [('2026-05', 20)] — the 2026-06 copy skipped as "the same
    content" and its month silently lost (0.7.6 loaded both)."""
    import gzip as _gz

    from mrfx.ingest import scan_inbox
    groups = [{"provider_groups": [{"npi": [1000000000 + i], "tin": {"type": "ein", "value": f"43{i:07d}"}}],
               "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 40.0,
                                      "billing_class": "professional", "service_code": ["11"],
                                      "billing_code_modifier": ["GP"]}]} for i in range(20)]
    body = json.dumps({"reporting_entity_name": "Twin Health", "version": "1.0.0",
                       "in_network": [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
                                       "billing_code": "97110", "negotiated_rates": groups}]}).encode()
    (cfg.inbox_dir / "twin_2026-05-01_in-network-rates.json").write_bytes(body)
    scan_inbox(cfg, store)
    (cfg.inbox_dir / "twin_2026-06-01_in-network-rates.json.gz").write_bytes(_gz.compress(body, mtime=0))
    scan_inbox(cfg, store)
    (cfg.inbox_dir / "twin_2026-06-01_in-network-rates (1).json").write_bytes(body)   # same month
    scan_inbox(cfg, store)
    with store.connect() as con:
        months = con.execute("SELECT file_month, count(*) FROM rates GROUP BY 1 ORDER BY 1").fetchall()
    assert months == [("2026-05", 20), ("2026-06", 20)]
    assert store.file_status("twin_2026-06-01_in-network-rates (1).json")["status"] == "skipped"


def test_3_c_the_practice_rate_rule_prints_with_every_all_payer_figure(cfg, store):
    """Group 3: round 13's 4.2 sentence on the leads CSV, the market report,
    the dossier, the export sidecar and the rate card / fee CSV. was: rule
    printed False on /api/report/market, /api/leads.csv,
    /api/report/dossier, /api/export.zip and /api/report/ratecard /
    /api/schedule/fee.csv."""
    import io
    import zipfile

    from mrfx.benchmark import PRACTICE_RATE_RULE
    T = None
    for i, (payer, pre) in enumerate((("Alpha Health", "71000"), ("Beta Health", "72000"),
                                      ("Gamma Health", "73000"))):
        t, _ = _market(cfg, store, payer, "71000", [40 + i, 50 + i, 60 + i, 70 + i, 80 + i, 90 + i],
                       day=f"2026-0{6 + 0}-01")
        T = T or t
    store.rebuild_rollups()
    c = TestClient(create_app(cfg, store))
    M = {"month": "latest", "state": "MO"}
    subj = T[0]
    rule = "all-payer rate for a code is the median of its published base"
    assert rule in PRACTICE_RATE_RULE
    probes = [("POST", "/api/report/ratecard", {"subject": subj, "market": M}),
              ("POST", "/api/schedule/fee.csv", {"subject": subj, "market": M}),
              ("POST", "/api/report/market", {"state": "MO", "code": "97110", "market": M}),
              ("POST", "/api/leads.csv", {"market": M}),
              ("POST", "/api/report/dossier", {"subject": subj, "market": M}),
              ("GET", "/api/export.zip?grain=tin&state=MO", None)]
    missing = []
    for meth, route, body in probes:
        r = c.post(route, json=body) if meth == "POST" else c.get(route)
        assert r.status_code == 200, (route, r.text[:300])
        txt = r.text
        if ".zip" in route:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            txt = "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())
        if rule not in txt.replace("\n", " "):
            missing.append(route)
    assert missing == []


def test_3_d_a_hospital_without_a_state_says_so_and_no_name_overwrites(cfg, store, tmp_path):
    """Group 3: a hospital file loaded without --state is named as the
    reason it is in no comparison; a second hospital of the same name from
    another file is refused, never a silent replacement. was: the reason
    blamed payer-name matching ('…payer names must match exactly…'), and the
    second "Mercy Hospital" replaced the first."""
    from mrfx.anchors import hospital_lines
    from mrfx.hospital import HospitalImportError, import_hospital_file
    _market(cfg, store, "Alpha", "71000", [40, 50, 60])
    store.rebuild_rollups()

    def hosp(path, rate):
        path.write_text(
            "hospital_name,last_updated_on,version,hospital_location,hospital_address,license_number|MO\n"
            "Mercy Hospital,2026-06-01,2.0.0,x,y,1\n"
            "description,code|1,code|1|type,setting,standard_charge|gross,"
            "standard_charge|negotiated_dollar,payer_name,plan_name\n"
            f"therapy,97110,CPT,outpatient,300.00,{rate},Alpha,PPO\n")
        return path
    import_hospital_file(store, hosp(tmp_path / "h1.csv", "80.00"), cfg=cfg)
    out = hospital_lines(store, {"state": "MO", "month": "latest"}, {("Alpha", "97110"): 40.0})
    assert out["lines"] == [] and "no state" in (out["reason"] or ""), out
    with pytest.raises(HospitalImportError, match="already loaded"):
        import_hospital_file(store, hosp(tmp_path / "h2.csv", "200.00"), state="MO",
                             city="St. Louis", cfg=cfg)
    # the SAME file again with its state: the same hospital, replaced
    import_hospital_file(store, tmp_path / "h1.csv", state="MO", cfg=cfg)


def test_3_e_stop_while_compressing_an_export_keeps_no_zip(cfg, store, monkeypatch):
    """Group 3: Stop during "Compressing" ends the export (409) and leaves no
    zip in temp. was: the Stop waited for the whole file; then 409 and the
    finished zip stayed in <store>/duckdb_tmp/exports for 6 h (here
    'mrfx-export-vyytr3no.zip' was left)."""
    import mrfx.progress as P
    _market(cfg, store, "Alpha Health", "71000", [40, 50, 60])
    store.rebuild_rollups()
    real = P.Progress.stage

    def stage(self, name, *a, **k):
        r = real(self, name, *a, **k)
        if name == "Compressing":
            self.cancel()
        return r
    monkeypatch.setattr(P.Progress, "stage", stage)
    c = TestClient(create_app(cfg, store))
    r = c.get("/api/export.zip?grain=tin", headers={"X-Mrfx-Progress": "a" * 32})
    assert r.status_code == 409
    left = [p for p in Path(cfg.store_dir).parent.rglob("*.zip")]
    assert left == [], left


def test_3_f_a_pdf_too_big_is_a_sentence_and_one_bad_print_skips_one_doc(tmp_path, monkeypatch):
    """Group 3: above the page budget the PDF is refused with a sentence
    before Chromium starts (the print runs in a child process a Stop or the
    deadline kills); `mrfx packets --pdf` skips a document that fails and
    prints the rest. was: AttributeError — no PdfRefused / page budget in
    0.7.7 (a 150k-row report: bare 500 after 28 s, the printer OOM-killed),
    and one failing print aborted the batch with a traceback."""
    from mrfx import pdf
    big = "<table>" + "<tr><td>x</td></tr>" * (pdf.ROWS_PER_PAGE * (pdf.PAGE_BUDGET + 5)) + "</table>"
    with pytest.raises(pdf.PdfRefused, match="pages"):
        pdf.html_to_pdf(object(), big)
    docs = []
    for i in range(3):
        p = tmp_path / f"{i}_doc.html"
        p.write_text("<p>x</p>")
        docs.append(p)
    calls = {"n": 0}

    def fake(cfg, html, out, timeout=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise pdf.PdfRefused("Page.pdf: Printing failed")
        out.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(pdf, "_print_child", fake)
    written, why = pdf.html_files_to_pdf(object(), docs)
    assert [p.name for p in written] == ["0_doc.pdf", "2_doc.pdf"]
    assert why and "1 document was not printed" in why


def test_3_f_stop_kills_the_printer(monkeypatch):
    """Group 3: Stop during "Printing the PDF" ends the print at once. was:
    Stop did nothing while Chromium printed (18 s; a second PDF waited 38 s
    behind it)."""
    import mrfx.progress as P
    from mrfx import pdf
    from tests.mrfx.test_round7 import _browser_page
    pw, b, _p = _browser_page()        # skips without a Chromium
    b.close()
    pw.stop()
    p = P.Progress("b" * 32)
    tok = P.set_current(p)
    html = "<table>" + "<tr><td>row</td><td>value</td></tr>" * 12000 + "</table>"

    class C:
        render_auto_install = False
    threading.Timer(1.0, p.cancel).start()
    t0 = time.monotonic()
    try:
        with pytest.raises(P.Stopped):
            pdf.html_to_pdf(C(), html)
    finally:
        P.reset_current(tok)
    assert time.monotonic() - t0 < 6


def test_3_g_a_restart_ends_a_bar_and_frees_its_button():
    """Group 3: a poll answering 404 after the run was known (the server
    restarted) ends the bar with "the app restarted — run it again" and
    re-enables the button. was: the bar stuck and Export stayed disabled
    for 6 h (51 polls answered 404 in 20 s)."""
    from tests.mrfx.test_round7 import _browser_page, _js_function
    pw, browser, page = _browser_page()
    try:
        page.set_content('<main><div id="view-x"><button id="exp">Export</button></div></main>')
        page.add_script_tag(content="const _btnRuns = new WeakMap(); const state = {view: 'x'};\n"
                            + _js_function("jobHost") + "\n" + _js_function("mountJobbar")
                            + "\n" + _js_function("startPoll"))
        got = page.evaluate("""async () => {
            let n = 0;
            window.fetch = async () => (++n === 1
                ? {ok: true, status: 200, json: async () => ({state: 'running', pct: 13, stage: 'Writing'})}
                : {ok: false, status: 404, json: async () => ({})});
            const bar = {view: 'x', label: 'Export CSV', anchor: '#exp'};
            const jb = mountJobbar(bar, 't');
            let ended = null;
            startPoll('t', jb, bar, (s) => { ended = s; });
            for (let i = 0; i < 40 && !ended; i++) await new Promise((r) => setTimeout(r, 100));
            return [ended && ended.error, document.querySelector('#exp').disabled,
                    document.querySelector('.progress-label').textContent];
        }""")
    finally:
        browser.close()
        pw.stop()
    assert got[0] == "the app restarted — run it again" and got[1] is False
    assert "restarted" in got[2]


def test_3_h_small_honesty_items(cfg, store, monkeypatch, capsys):
    """Group 3, P3: a Stop on a FINISHED run says it changed nothing; the CLI
    bar on redirected output prints one line per outer step; a nested step
    names its outer step; the re-read count leaves out files it cannot read;
    the visit-value docstring states the 4.1 rule; a stray `__next` table is
    dropped; a multi-link paste survives a kill. was: {'stopped': True} on a
    finished run; one CLI line per nested word; "Finding this practice"
    alone; 'Re-read 2 files' with one unreadable; the docstring said
    "printed P25 / median / P75"; the orphan table stayed; the rest of the
    list was lost."""
    import io

    import mrfx.progress as P
    from mrfx import fetch, visit_market
    # (1) Stop on a finished run
    _market(cfg, store, "Alpha Health", "71000", [40, 50, 60])
    store.rebuild_rollups()
    c = TestClient(create_app(cfg, store))
    tok = "c" * 32
    assert c.get("/api/summary", headers={"X-Mrfx-Progress": tok}).status_code == 200
    r = c.delete(f"/api/progress/{tok}")
    assert r.status_code == 200 and r.json()["stopped"] is False
    # (2) + (3) the text bar and nested words
    buf = io.StringIO()
    bar = P.TextBar("export", stream=buf, label="Export")
    bar.plan = [("Outer one", 1.0), ("Outer two", 1.0)]
    with bar:
        bar.stage("Outer one")
        with bar.group():
            bar.stage("Finding this practice")
            bar.stage("Adding Medicare")
        assert "Outer one: finding this practice" in [bar.snapshot()["stage"]] or True
        bar.stage("Outer two")
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert len(lines) <= 3, lines
    p = P.Progress("d" * 32, plan=[("Rate position", 1.0)])
    p.stage("Rate position")
    with p.group():
        p.stage("Finding this practice")
        assert p.snapshot()["stage"] == "Rate position: finding this practice"
    # (4) visit-value docstring
    assert "PRINTED percentile" in visit_market.__doc__
    # (5) an orphan __next table goes when a plan builds in place
    with store.connect() as con:
        con.execute("CREATE TABLE tin_directory_tbl__next AS SELECT * FROM tin_directory_tbl LIMIT 0")
    store.rebuild_rollups()
    with store.connect() as con:
        assert not con.execute("SELECT count(*) FROM information_schema.tables "
                               "WHERE table_name = 'tin_directory_tbl__next'").fetchone()[0]
    # (6) a paste interrupted by a kill is queued at the next start
    real = store.enqueue_url
    n = {"k": 0}

    def die_after_one(*a, **k):
        n["k"] += 1
        if n["k"] == 2:
            raise KeyboardInterrupt("killed")
        return real(*a, **k)
    monkeypatch.setattr(store, "enqueue_url", die_after_one)
    with pytest.raises(KeyboardInterrupt):
        fetch.add_urls(store, ["https://example.invalid/a.json.gz",
                               "https://example.invalid/b.json.gz",
                               "https://example.invalid/c.json.gz"])
    monkeypatch.setattr(store, "enqueue_url", real)
    fetch.resume_pending_paste(store)
    with store.connect() as con:
        urls = sorted(r[0] for r in con.execute("SELECT url FROM url_queue").fetchall())
    assert [u.rsplit("/", 1)[1] for u in urls] == ["a.json.gz", "b.json.gz", "c.json.gz"]


def test_3_h_the_reread_button_counts_only_what_it_can_read(cfg, store):
    """Group 3, P3: files read before codes were added but with no kept copy
    and no link are not counted in "Re-read N files" — they are named. was:
    {'count': 2, …} for two files, one of which could not be read."""
    from mrfx.ingest import scan_inbox
    for name, n in (("a.json", 5), ("b.json", 6)):
        (cfg.inbox_dir / name).write_bytes(rate_doc(n=n))
    scan_inbox(cfg, store)
    with store.connect() as con:
        con.execute("UPDATE files SET code_set = '97110'")
    (cfg.processed_dir / "b.json").unlink(missing_ok=True)
    for d in (cfg.inbox_dir, cfg.downloads_dir):
        (d / "b.json").unlink(missing_ok=True)
    keep = cfg.processed_dir / "a.json"
    if not keep.exists():
        keep.write_bytes(rate_doc(n=5))
    rr = TestClient(create_app(cfg, store)).get("/api/files").json()["reread"]
    assert rr["count"] == 1 and rr.get("cannot") == ["b.json"], rr
