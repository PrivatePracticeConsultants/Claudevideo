"""`mrfx add`'s name-lookup tail: it must say how many NPIs remain, show
progress, and let Ctrl-C skip the rest without a traceback (the rates are
already saved). It used to print one line and go quiet for 15+ minutes."""

from __future__ import annotations

import _thread
import threading
import time

from mrfx import cli


class _Store:
    def enrichment_progress(self, max_age_seconds=60.0):
        return {"total": 9, "enriched": 2, "named": 2, "remaining": 7}


def test_progress_lines_and_final_count(cfg, monkeypatch, capsys):
    def fake(cfg_, store, stop=None, final_refresh=True, on_progress=None):
        for i in range(1, 4):
            time.sleep(0.05)
            on_progress(i)
        return 3

    monkeypatch.setattr(cli, "run_enrichment", fake)
    monkeypatch.setattr(cli, "ENRICH_TICK_S", 0.0)
    cli._run_enrichment_best_effort(cfg, _Store(), interactive=True)
    out = capsys.readouterr().out
    assert "for 7 NPI(s)" in out and "Ctrl-C to skip" in out
    assert " of 7 looked up" in out          # a progress tick printed
    assert "names: 3 looked up" in out


def test_ctrl_c_skips_cleanly_and_stops_the_worker(cfg, monkeypatch, capsys):
    seen = {}

    def fake(cfg_, store, stop=None, final_refresh=True, on_progress=None):
        on_progress(1)
        stop.wait(10)                 # a long lookup pass, until told to stop
        seen["stopped"] = stop.is_set()
        return 1

    monkeypatch.setattr(cli, "run_enrichment", fake)
    threading.Timer(0.3, _thread.interrupt_main).start()   # the user's Ctrl-C
    t = time.monotonic()
    cli._run_enrichment_best_effort(cfg, _Store(), interactive=True)   # no raise
    assert time.monotonic() - t < 5
    assert seen == {"stopped": True}
    out = capsys.readouterr().out
    assert "skipping the rest" in out and "skipped: 1 names looked up" in out


def test_a_failure_is_reported_not_raised(cfg, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("bad bulk csv")

    monkeypatch.setattr(cli, "run_enrichment", boom)
    cli._run_enrichment_best_effort(cfg, _Store(), interactive=True)
    assert "hit a problem (bad bulk csv)" in capsys.readouterr().out
