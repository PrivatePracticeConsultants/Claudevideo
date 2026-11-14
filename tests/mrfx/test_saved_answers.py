"""0.8.8 — answers kept across restarts: a whole-store answer computed by
`serve` is saved under the identity of the data it was computed from and read
back by the next start; ANY change to that data is a new identity, so a saved
answer is never served for other data."""
from __future__ import annotations

from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.store import _MISSING, Store
from tests.mrfx.conftest import drop


def _loaded(cfg, store):
    from mrfx.ingest import ingest_file
    ingest_file(cfg, store, drop(cfg, "innetwork_mixed.json"))
    store.rebuild_rollups()


def _serve_store(cfg) -> Store:
    s = Store(cfg.store_dir, cfg.duckdb_memory_gb, temp_dir=cfg.duckdb_temp_dir)
    s.persist_answers = True          # what serve (keep_warm) turns on
    return s


def test_a_saved_answer_is_read_back_by_the_next_start(cfg, store):
    _loaded(cfg, store)
    a = _serve_store(cfg)
    calls = []
    assert a.cached("t", "k", lambda: calls.append(1) or {"v": 1.5}) == {"v": 1.5}
    b = _serve_store(cfg)                                   # "the next start"
    assert b.cached("t", "k", lambda: calls.append(1) or {"v": 99}) == {"v": 1.5}
    assert calls == [1]


def test_any_change_to_the_data_is_a_new_identity(cfg, store):
    """A rollup / name refresh / user edit (each bumps the generation, in
    any process) and an app update all make the saved answer unreachable."""
    _loaded(cfg, store)
    a = _serve_store(cfg)
    a.cached("t", "k", lambda: "old")
    other = Store(cfg.store_dir, cfg.duckdb_memory_gb, temp_dir=cfg.duckdb_temp_dir)
    other.note_user_edit()                                  # e.g. a CLI writer
    b = _serve_store(cfg)
    assert b.cached("t", "k", lambda: "new") == "new"
    other.rebuild_rollups()                                 # the data itself
    c = _serve_store(cfg)
    assert c.answer_get("t", "k") is _MISSING
    import mrfx
    d = _serve_store(cfg)
    d.cached("t", "k2", lambda: "v1")
    orig = mrfx.__version__
    try:
        mrfx.__version__ = orig + ".test"                   # an app update
        e = _serve_store(cfg)
        assert e.answer_get("t", "k2") is _MISSING
    finally:
        mrfx.__version__ = orig


def test_only_serve_saves_and_reset_clears(cfg, store):
    _loaded(cfg, store)
    store.cached("t", "k", lambda: 1)                       # a CLI / test store
    assert not (store.dir / "answers").exists()
    s = _serve_store(cfg)
    s.cached("t", "k", lambda: 1)
    assert list((store.dir / "answers").rglob("*.pkl"))
    s.reset()
    assert not (store.dir / "answers").exists()


def test_the_overview_is_instant_after_a_restart(cfg, store, monkeypatch):
    """The Market overview computed once is served by the next start without
    reading the store; a damaged saved file is simply recomputed."""
    import mrfx.overview as O
    _loaded(cfg, store)
    calls = []
    real = O.market_overview
    monkeypatch.setattr(O, "market_overview", lambda *a, **k: calls.append(1) or real(*a, **k))
    first = TestClient(create_app(cfg, _serve_store(cfg)), base_url="http://localhost")
    one = first.get("/api/overview").json()
    second = TestClient(create_app(cfg, _serve_store(cfg)), base_url="http://localhost")
    assert second.get("/api/overview").json() == one
    assert len(calls) == 1
    for f in (store.dir / "answers").rglob("*.pkl"):
        f.write_bytes(b"damaged")
    third = TestClient(create_app(cfg, _serve_store(cfg)), base_url="http://localhost")
    assert third.get("/api/overview").json() == one
    assert len(calls) == 2
