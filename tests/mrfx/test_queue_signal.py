"""Idle download/parse threads sleep until the queue changes (round 5, P1.1).

Before: 6 downloader + 3 processor threads each re-queried the URL queue
every 0.5 s with nothing to do — ~30 DuckDB queries and 18 writer-lock grabs a
second, 7% of a core on the user's PC for weeks. Now every queue writer
signals (store._QUEUE_WRITERS_*) and the threads wait on that signal."""

from __future__ import annotations

import ast
import http.server
import json
import threading
import time
from pathlib import Path

import pytest

STORE_PY = Path(__file__).resolve().parents[2] / "mrfx" / "store.py"
# writes url_queue but needs no signal: byte-progress of a running download
# (no worker waits on it), and the helper only enqueue_url(s) call
EXEMPT = {"url_progress", "_enqueue_one"}


def test_every_url_queue_writer_signals_the_workers():
    import mrfx.store as st
    tree = ast.parse(STORE_PY.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Store")
    writers = set()
    for fn in cls.body:
        if isinstance(fn, ast.FunctionDef):
            src = ast.get_source_segment(STORE_PY.read_text(), fn) or ""
            if any(k in src for k in ("UPDATE url_queue", "INSERT INTO url_queue",
                                      "DELETE FROM url_queue")):
                writers.add(fn.name)
    signalled = set(st._QUEUE_WRITERS_ALWAYS) | set(st._QUEUE_WRITERS_IF_DONE)
    assert writers - EXEMPT <= signalled, f"url_queue writers that never signal: {writers - EXEMPT - signalled}"


def test_a_claim_that_finds_nothing_does_not_signal(store):
    e = store.queue_epoch()
    assert store.next_queued_url() is None and store.next_fetched_url() is None
    assert store.queue_epoch() == e                    # else idle workers wake each other forever
    store.enqueue_url("https://x.example/a.json", "https://x.example/a.json")
    assert store.queue_epoch() > e


def test_wait_returns_at_once_for_a_change_after_the_read(store):
    e = store.queue_epoch()
    store.queue_changed()
    t = time.monotonic()
    assert store.wait_queue_change(e, 10) and time.monotonic() - t < 0.5


@pytest.fixture
def tiny_server(tmp_path):
    doc = {"reporting_entity_name": "Signal Health", "reporting_entity_type": "issuer",
           "last_updated_on": "2026-05-01", "version": "2.0.0", "provider_references": [],
           "in_network": [{"negotiation_arrangement": "ffs", "name": "x", "billing_code_type": "CPT",
                           "billing_code_type_version": "2026", "billing_code": "97110",
                           "description": "x", "negotiated_rates": [{
                               "provider_groups": [{"npi": ["1990000001"],
                                                    "tin": {"type": "ein", "value": "990000001"}}],
                               "negotiated_prices": [{"negotiated_type": "negotiated",
                                                      "negotiated_rate": 40.0,
                                                      "billing_class": "professional",
                                                      "expiration_date": "9999-12-31"}]}]}]}
    (tmp_path / "www").mkdir()
    (tmp_path / "www" / "signal_in-network-rates.json").write_text(json.dumps(doc))

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(tmp_path / "www"), **k)

        def log_message(self, *a):
            pass
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_idle_workers_stay_quiet_and_new_work_starts_at_once(cfg, store, tiny_server, monkeypatch):
    """The serve-mode queue (run_queue with a stop event), idle: count the
    store connections its threads open in 3 s; then add a link and time how
    long until it is being worked on."""
    from mrfx.fetch import run_queue
    cfg.parallel_ingests = 1
    stop = threading.Event()
    th = threading.Thread(target=run_queue, args=(cfg, store), kwargs={"stop": stop}, daemon=True)
    th.start()
    time.sleep(1.5)                                    # start-up sweeps done; now idle
    n = {"c": 0}
    real = store.connect

    def counting():
        n["c"] += 1
        return real()
    monkeypatch.setattr(store, "connect", counting)
    time.sleep(3.0)
    idle_connections = n["c"]
    monkeypatch.setattr(store, "connect", real)
    t0 = time.monotonic()
    store.enqueue_url(f"{tiny_server}/signal_in-network-rates.json",
                      f"{tiny_server}/signal_in-network-rates.json")
    started = None
    while time.monotonic() - t0 < 20:
        st = {r["status"] for r in store.list_urls()}
        if started is None and st - {"queued"}:
            started = time.monotonic() - t0
        if "done" in st:
            break
        time.sleep(0.05)
    done = {r["status"] for r in store.list_urls()} == {"done"}
    stop.set()
    th.join(timeout=30)
    # coordinator: one count every 3 s idle; the worker threads: none
    assert idle_connections <= 3, idle_connections
    assert started is not None and started < 1.0, started
    assert done
