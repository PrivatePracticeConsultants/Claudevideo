"""0.8.2 — the one-pass name-directory source on a store bigger than memory.

Field log (1.18 B rates, duckdb_memory_gb 8, a 0.8.0 plan resumed by 0.8.1 at
directory part 59 of 74): the one-pass source aggregated the whole store in
ONE statement, ran out of memory after ~11-13 minutes ("could not allocate
block ... 7.4 GiB/7.4 GiB used"), fell back to the raw read (right, slower) —
and then tried the doomed pass again before EVERY remaining directory part,
so each part cost ~16 minutes instead of ~5.

Each test fails on 0.8.1 (d962004) with the value in its docstring.
"""
from __future__ import annotations

import json
import logging

import duckdb
import pytest

from mrfx.store import DIR_SOURCE_TABLE

from .test_v081 import _dir_rows, _one_shot, _seed, _spine_rows

LOG = logging.getLogger("t")


class _Con:
    """A connection whose source-piece INSERTs can be made to fail."""

    def __init__(self, con, fail):
        self._con, self._fail = con, fail

    def execute(self, sql, *a, **k):
        if sql.startswith(f"INSERT INTO {DIR_SOURCE_TABLE}"):
            self._fail(sql)
        return self._con.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._con, name)


def _wrap(store, monkeypatch, fail):
    """Route the source's piece INSERTs through `fail` (re-wrapping replaces
    the hook, never stacks a second one)."""
    real = getattr(store, "_dir_source_unwrapped", None) or store._dir_source
    monkeypatch.setattr(store, "_dir_source_unwrapped", real, raising=False)
    monkeypatch.setattr(store, "_dir_source",
                        lambda con, plan, log: real(_Con(con, fail), plan, log))


def test_the_source_is_built_in_memory_sized_pieces(cfg, store, monkeypatch):
    """(P0) The source is aggregated piece by piece (payer x code ranges),
    never in one statement over the whole store, and the directory is byte-
    identical to the one-shot build.
    was: ONE insert over every rate (the field OOM at 7.4 GiB)."""
    _seed(store)
    ref = _one_shot(store)
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: 3)
    inserts = []
    _wrap(store, monkeypatch, inserts.append)
    store.rebuild_rollups()
    assert (_spine_rows(store), _dir_rows(store)) == ref
    assert len(inserts) >= 4                       # one per payer at least
    assert all("payer" in q for q in inserts)      # each reads its own row groups


def test_a_piece_that_runs_out_of_memory_is_halved_not_abandoned(cfg, store, monkeypatch):
    """(P0) A piece that hits the memory limit is split in two and retried;
    the source still serves every directory part and the result is identical.
    was: the single statement failed and every directory part fell back to a
    raw read of the whole store (no part read the source)."""
    _seed(store)
    ref = _one_shot(store)
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: 3)
    state = {"oom": 0}

    def first_two_oom(sql):
        if state["oom"] < 2:
            state["oom"] += 1
            raise duckdb.OutOfMemoryException("could not allocate block (test)")
    _wrap(store, monkeypatch, first_two_oom)
    used = []
    real_run = store._run_slice

    def spy(con, plan, idx, log):
        out = real_run(con, plan, idx, log)
        if plan["slices"][idx]["t"] == "dir":       # the part read the source
            used.append(store._meta_get(con, "rollup_dir_source") == plan["id"])
        return out
    monkeypatch.setattr(store, "_run_slice", spy)
    store.rebuild_rollups()
    assert state["oom"] == 2
    assert len(used) > 2 and all(used)
    assert (_spine_rows(store), _dir_rows(store)) == ref


def test_a_source_that_cannot_be_built_is_tried_once_per_plan(cfg, store, monkeypatch):
    """(P0 — the field cost) A source that fails for good is remembered for
    the plan: ONE attempt, then every directory part reads the store
    directly — identical rows.
    was: one ~11-minute doomed attempt before EVERY directory part
    (attempts == number of directory parts)."""
    _seed(store)
    ref = _one_shot(store)
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: 3)
    attempts = {"n": 0}

    def always_fails(sql):
        attempts["n"] += 1
        raise duckdb.IOException("disk full (test)")
    _wrap(store, monkeypatch, always_fails)
    store.rebuild_rollups()
    with store.connect() as con:
        n_dir = 0  # the plan is gone; count from a fresh plan of the same store
        plan = store._new_plan(con, "full", LOG)
        n_dir = sum(1 for s in plan["slices"] if s["t"] == "dir")
        con.execute("DELETE FROM meta WHERE key = 'rollup_plan'")
    assert n_dir > 2
    assert attempts["n"] == 1
    assert (_spine_rows(store), _dir_rows(store)) == ref


def test_a_stop_mid_source_continues_from_the_last_piece(cfg, store, monkeypatch):
    """(P1) Ctrl+C part-way through the source keeps the committed pieces:
    the next start continues at the next piece (no piece read twice, no
    count doubled) and the directory is identical. A DuckDB interrupt is a
    stop, never recorded as 'could not be built'.
    was: the one statement was all-or-nothing, and 0.8.1 caught the
    interrupt as a failure."""
    _seed(store)
    ref = _one_shot(store)
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: 3)
    seen = []

    def stop_at_third(sql):
        seen.append(sql)
        if len(seen) == 3:
            raise duckdb.InterruptException("Interrupted! (test)")
    _wrap(store, monkeypatch, stop_at_third)
    with pytest.raises(duckdb.InterruptException):
        store.rebuild_rollups()
    with store.connect() as con:
        st = json.loads(store._meta_get(con, "rollup_dir_source"))
    assert st["next"] == 2 and not st.get("failed")
    total = len(st["chunks"])
    seen.clear()
    _wrap(store, monkeypatch, seen.append)
    store.rebuild_rollups()
    assert len(seen) == total - 2              # pieces 3..n only
    assert (_spine_rows(store), _dir_rows(store)) == ref


def test_a_source_finished_by_081_is_reused(cfg, store, monkeypatch):
    """(no step backwards) A source 0.8.1 finished (meta = the plan id) is
    used as it stands — no second pass after the update."""
    _seed(store)
    ref = _one_shot(store)
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: 3)
    with store.connect() as con:
        plan = store._new_plan(con, "full", LOG)
        assert store._dir_source(con, plan, LOG) == DIR_SOURCE_TABLE
        assert store._meta_get(con, "rollup_dir_source") == plan["id"]
    seen = []
    _wrap(store, monkeypatch, seen.append)
    store.rebuild_rollups()
    assert seen == []
    assert (_spine_rows(store), _dir_rows(store)) == ref
