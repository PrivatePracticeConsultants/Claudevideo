"""The Files tab's "stop" for a file that is being PROCESSED (parsed).

A wrong 22 GB paste used to have to run its course. Stop must work on both
parse paths (in-process, and a worker process via the parent's sentinel),
save nothing from the stopped run, leave an earlier version of the file
untouched, and — for a downloaded link — keep the download so "retry"
re-processes it without downloading again."""

from __future__ import annotations

import pytest

from mrfx.ingest import ParsePoolManager, ingest_file, set_parse_pool
from tests.mrfx.conftest import drop


def _stop_when_processing(store, monkeypatch):
    """Press "stop" the moment the file's row turns 'processing' — the same
    call the dashboard button makes (request_parse_stop)."""
    real = store.upsert_file
    pressed = []

    def upsert(name, **fields):
        real(name, **fields)
        if fields.get("status") == "processing" and not pressed:
            pressed.append(store.request_parse_stop(name))

    monkeypatch.setattr(store, "upsert_file", upsert)
    return pressed


def _part_rows(store, name):
    with store.connect() as con:
        return con.execute("SELECT count(*) FROM rates WHERE source_file = ?",
                           [name]).fetchone()[0]


@pytest.mark.parametrize("pooled", [False, True])
def test_stop_mid_parse_saves_nothing_and_says_so(cfg, store, monkeypatch, pooled):
    p = drop(cfg, "innetwork_mixed.json", gz=True)
    pressed = _stop_when_processing(store, monkeypatch)
    pool = ParsePoolManager(max_workers=1) if pooled else None
    if pool:
        set_parse_pool(pool)
    try:
        res = ingest_file(cfg, store, p)
    finally:
        if pool:
            set_parse_pool(None)
            pool.shutdown_now()
    assert pressed == [True]
    assert res["status"] == "stopped", res
    st = store.file_status(p.name)
    assert st["status"] == "stopped" and "stopped by you" in st["error"]
    assert _part_rows(store, p.name) == 0                 # nothing saved
    assert not store.is_parse_stopped(p.name)             # flag cleared for a retry
    # an inbox file is moved aside, or the watcher would just start it again
    assert not p.exists() and (cfg.failed_dir / p.name).exists()
    leftovers = [f.name for f in store.rates_dir.iterdir()
                 if f.name.endswith((".tmp", ".progress", ".stop"))]
    assert leftovers == []


def test_stopping_a_reingest_keeps_the_earlier_version(cfg, store, monkeypatch):
    p = drop(cfg, "innetwork_mixed.json", gz=True, rename="again.json.gz")
    assert ingest_file(cfg, store, p)["status"] == "done"
    before = _part_rows(store, "again.json.gz")
    assert before > 0
    p2 = drop(cfg, "innetwork_mixed.json", gz=True, rename="again.json.gz")
    _stop_when_processing(store, monkeypatch)
    assert ingest_file(cfg, store, p2)["status"] == "stopped"
    assert _part_rows(store, "again.json.gz") == before


def test_stop_is_refused_when_nothing_is_processing(cfg, store):
    assert store.request_parse_stop("never-seen.json") is False
    p = drop(cfg, "innetwork_mixed.json", gz=True)
    ingest_file(cfg, store, p)
    assert store.request_parse_stop(p.name) is False      # already done


def test_stop_api_endpoints(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app

    c = TestClient(create_app(cfg, store))
    assert c.post("/api/files/nothing.json/stop").status_code == 409
    store.upsert_file("busy.json.gz", status="processing")
    assert c.post("/api/files/busy.json.gz/stop").json() == {"status": "stopping"}
    assert store.is_parse_stopped("busy.json.gz")
    from mrfx.fetch import filename_for

    url = "https://x.example/files/linked_rates.json.gz?sig=abc"
    uid = store.enqueue_url(url, "https://x.example/files/linked_rates.json.gz")
    assert c.post(f"/api/urls/{uid}/stop").status_code == 409     # not processing yet
    store.upsert_file(filename_for(url), status="processing")
    assert c.post(f"/api/urls/{uid}/stop").json() == {"status": "stopping"}
    assert store.is_parse_stopped(filename_for(url))


@pytest.mark.parametrize("mode", ["single_read", "two_pass", "pooled_big"])
def test_stop_works_on_the_big_file_paths(cfg, store, monkeypatch, mode):
    """Multi-GB files take other readers: the single-read path (scan +
    spool replay) and the two-pass skim, in-process or in a worker."""
    import mrfx.ingest as ingest_mod

    real_pf = ingest_mod.preflight

    def big_pf(*a, **k):
        pf = real_pf(*a, **k)
        pf.est_uncompressed_bytes = 10 ** 12   # the worker decides from this
        return pf

    monkeypatch.setattr(ingest_mod, "preflight", big_pf)
    monkeypatch.setattr(ingest_mod, "LARGE_FILE_UNCOMPRESSED_BYTES", 1)
    if mode == "two_pass":
        monkeypatch.setattr(ingest_mod, "fast_big_file_available", lambda cfg: False)
    p = drop(cfg, "innetwork_mixed.json", gz=True)
    _stop_when_processing(store, monkeypatch)
    pool = ParsePoolManager(max_workers=1) if mode == "pooled_big" else None
    if pool:
        set_parse_pool(pool)
    try:
        res = ingest_file(cfg, store, p)
    finally:
        if pool:
            set_parse_pool(None)
            pool.shutdown_now()
    assert res["status"] == "stopped", res
    assert _part_rows(store, p.name) == 0
    assert [f.name for f in store.rates_dir.iterdir()
            if f.name.endswith((".tmp", ".progress", ".stop"))] == []


def test_a_stop_pressed_after_the_last_read_does_not_stop_the_next_ingest(cfg, store, monkeypatch):
    """Round 3 P1.3(a): a stop pressed after the parse's last read (during QA /
    finalize) was never honored by ParseStopped, so it stayed set for the
    server's life and the NEXT ingest of that filename died at its first read
    as "stopped by you". Any outcome now clears it."""
    import json

    import mrfx.ingest as mi
    from mrfx.ingest import ingest_file
    doc = {"reporting_entity_name": "Stopco", "reporting_entity_type": "issuer",
           "last_updated_on": "2026-05-01", "version": "2.0.0", "provider_references": [],
           "in_network": [{"negotiation_arrangement": "ffs", "name": "x",
                           "billing_code_type": "CPT", "billing_code_type_version": "2026",
                           "billing_code": "97110", "description": "x",
                           "negotiated_rates": [{
                               "provider_groups": [{"npi": ["1777000001"],
                                                    "tin": {"type": "ein", "value": "777000001"}}],
                               "negotiated_prices": [{"negotiated_type": "negotiated",
                                                      "negotiated_rate": 40.0,
                                                      "billing_class": "professional",
                                                      "expiration_date": "9999-12-31"}]}]}]}
    p = cfg.inbox_dir / "stopco_2026-05.json"
    p.write_text(json.dumps(doc))
    real_qa = mi.qa_report

    def qa_then_stop(store_, qa, name, agg=None):
        store_.request_parse_stop(name)          # the click lands during QA
        return real_qa(store_, qa, name, agg=agg)
    monkeypatch.setattr(mi, "qa_report", qa_then_stop)
    assert ingest_file(cfg, store, p)["status"] == "done"
    assert not store.is_parse_stopped(p.name)


def test_a_parse_cancelled_by_a_pool_restart_requeues_itself(cfg, store, monkeypatch):
    """Round 3 P1.3(c): force_heal(cancel_futures=True) turned OTHER threads'
    pending parses into 'failed' rows needing a manual retry, although the log
    said they "re-queue automatically". Now the file goes back to queued and
    stays where it is (inbox / download), and the URL row returns to 'fetched'."""
    import json

    import mrfx.ingest as mi
    from mrfx.ingest import PoolRestarted, ingest_file

    class FakePool:
        pass
    monkeypatch.setattr(mi, "_parse_pool", FakePool(), raising=False)
    monkeypatch.setattr(mi, "get_parse_pool", lambda: FakePool(), raising=False)

    def cancelled(*a, **k):
        raise PoolRestarted("parse was cancelled by a worker-pool restart before it began")
    monkeypatch.setattr(mi, "_ingest_in_network_pooled", cancelled)
    p = cfg.inbox_dir / "poolco_2026-05.json"
    p.write_text(json.dumps({"reporting_entity_name": "Poolco", "last_updated_on": "2026-05-01",
                             "version": "2.0.0", "in_network": []}))
    assert ingest_file(cfg, store, p)["status"] == "requeued"
    assert store.file_status(p.name)["status"] == "queued" and p.exists()
