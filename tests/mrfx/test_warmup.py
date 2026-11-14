"""0.8.8 — the slow screens are prepared at launch (mrfx/warmup.py): serve
asks them itself; what the user waited on is remembered and asked next time;
a screen opened while it is being prepared WAITS for that one computation."""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.warmup import DEFAULT_URLS, learnable, plan, remember, warm
from tests.mrfx.conftest import drop


@pytest.fixture
def loaded(cfg, store):
    from mrfx.ingest import ingest_file
    ingest_file(cfg, store, drop(cfg, "innetwork_mixed.json"))
    store.rebuild_rollups()
    return store


def test_remembered_urls_and_the_plan(tmp_path):
    """Slow GETs are kept newest first, once each, at most 30; downloads,
    reports and live polls never are; the plan is the dashboard's boot
    questions followed by what the user waited on."""
    remember(tmp_path, "/api/overview?state=TN", 12.0)
    remember(tmp_path, "/api/code/97110?grain=tin", 4.0)
    remember(tmp_path, "/api/overview?state=TN", 9.0)         # again: moves to the front
    remember(tmp_path, "/api/export.csv?x=1", 60.0)           # a download: never
    remember(tmp_path, "/api/report/dossier?s=1", 60.0)       # a report: never
    remember(tmp_path, "/api/progress/abc", 30.0)             # a poll: never
    remember(tmp_path, "/api/trend?cpt=97110", 1.0)           # fast: not worth it
    rows = json.loads((tmp_path / "warm_urls.json").read_text())
    assert [r["url"] for r in rows] == ["/api/overview?state=TN", "/api/code/97110?grain=tin"]
    p = plan(tmp_path)
    assert p[:len(DEFAULT_URLS)] == list(DEFAULT_URLS)
    assert p[len(DEFAULT_URLS):] == ["/api/overview?state=TN", "/api/code/97110?grain=tin"]
    for i in range(40):
        remember(tmp_path, f"/api/code/9{i:04d}", 5.0)
    assert len(json.loads((tmp_path / "warm_urls.json").read_text())) == 30
    assert not learnable("/api/outreach.zip") and learnable("/api/overview")
    (tmp_path / "warm_urls.json").write_text("{not json")        # a damaged file: ignored
    assert plan(tmp_path) == list(DEFAULT_URLS)


def test_a_slow_dashboard_get_is_remembered_but_a_preload_is_not(cfg, loaded, monkeypatch):
    import mrfx.warmup as W
    monkeypatch.setattr(W, "LEARN_MIN_S", 0.0)
    client = TestClient(create_app(cfg, loaded), base_url="http://localhost")
    assert client.get("/api/overview?state=MO&progress=abcdef12").status_code == 200
    assert client.get("/api/payers", headers={"x-mrfx-warm": "1"}).status_code == 200
    urls = [r["url"] for r in json.loads((loaded.dir / "warm_urls.json").read_text())]
    assert urls == ["/api/overview?state=MO"]        # the progress token dropped


def test_warm_fills_the_caches_the_screens_read(cfg, loaded, monkeypatch):
    """After the pre-load, opening the Overview computes nothing."""
    import mrfx.overview as O
    calls = []
    real = O.market_overview
    monkeypatch.setattr(O, "market_overview", lambda *a, **k: calls.append(1) or real(*a, **k))
    client = TestClient(create_app(cfg, loaded), base_url="http://localhost")
    got = warm("http://localhost", list(DEFAULT_URLS), client=client)
    assert all(not isinstance(v, str) for v in got.values()), got
    assert len(calls) == 1
    assert client.get("/api/overview").status_code == 200
    assert len(calls) == 1


def test_an_overview_being_prepared_is_waited_for_not_refused(cfg, loaded):
    """was: HTTP 503 "overview is building" while the launch pre-load (or
    another tab) computed it."""
    app = create_app(cfg, loaded)
    client = TestClient(app, base_url="http://localhost")
    lock = app.state.overview_lock
    lock.acquire()
    threading.Timer(1.0, lock.release).start()
    t0 = time.monotonic()
    r = client.get("/api/overview")
    assert r.status_code == 200 and time.monotonic() - t0 >= 0.9


def test_the_same_cached_answer_is_computed_once(store):
    """Two callers asking the same whole-store question at once (the
    pre-load and the screen) share one computation."""
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.5)
        return {"v": 42}
    out = []
    ts = [threading.Thread(target=lambda: out.append(store.cached("t", "k", slow))) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert calls == [1] and out == [{"v": 42}] * 4

    def boom():
        raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        store.cached("t", "bad", boom)
    assert store.cached("t", "bad", lambda: 7) == 7          # a failure is not remembered
