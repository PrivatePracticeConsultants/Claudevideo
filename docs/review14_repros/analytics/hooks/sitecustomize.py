# Audit instrumentation for a `mrfx serve` subprocess (PYTHONPATH=hooks).
# Every knob is OFF unless its env var is set; nothing here changes a number:
# it only widens timing windows (sleeps), forces slicing (the module ceiling
# the tests already use), or injects a failure. Each event is appended to
# $MRFX_AUDIT_EVENTS as one JSON line with a wall-clock time.
import importlib.abc
import importlib.util
import json
import os
import sys
import time

_EV = os.environ.get("MRFX_AUDIT_EVENTS")


def ev(kind, **kw):
    if not _EV:
        return
    try:
        with open(_EV, "a") as f:
            f.write(json.dumps({"t": time.time(), "pid": os.getpid(), "ev": kind, **kw}) + "\n")
    except OSError:
        pass


def _patch_store(m):
    import duckdb
    S = m.Store
    pr = os.environ.get("MRFX_AUDIT_PARTITION_ROWS")
    if pr:
        m.ROLLUP_PARTITION_ROWS = int(pr)
        ev("partition_rows", n=int(pr))

    o_incr = S.update_rollups_incremental
    o_full = S.rebuild_rollups
    o_slice = S._run_slice
    o_delta = S._apply_rollup_delta_sliced
    o_dir = S.refresh_directory_incremental
    delay = float(os.environ.get("MRFX_AUDIT_DELAY_ROLLUP", "0") or 0)
    slice_sleep = float(os.environ.get("MRFX_AUDIT_SLICE_SLEEP", "0") or 0)
    hold_delete = float(os.environ.get("MRFX_AUDIT_HOLD_DELETE", "0") or 0)
    fail_flag = os.environ.get("MRFX_AUDIT_FAIL_FLAG")
    dir_sleep = float(os.environ.get("MRFX_AUDIT_DIR_SLEEP", "0") or 0)

    def failing():
        return bool(fail_flag) and os.path.exists(fail_flag)

    def incr(self, *a, **k):
        ev("incr_enter")
        if delay:
            time.sleep(delay)
        t = time.time()
        try:
            r = o_incr(self, *a, **k)
            ev("incr_exit", took=time.time() - t, ret=r)
            return r
        except BaseException as e:
            ev("incr_raise", err=repr(e)[:200])
            raise

    def full(self, *a, **k):
        names_only = k.get("names_only", a[0] if a else False)
        ev("full_enter", names_only=names_only)
        if delay:
            time.sleep(delay)
        t = time.time()
        try:
            if names_only:
                _tl.names = True
            r = o_full(self, *a, **k)
            ev("full_exit", took=time.time() - t, ret=r)
            return r
        except BaseException as e:
            ev("full_raise", err=repr(e)[:200])
            raise
        finally:
            _tl.names = False

    def run_slice(self, con, plan, idx, log):
        sl = plan["slices"][idx]
        if failing() and sl.get("t") == "spine":
            ev("slice_fail_injected", idx=idx, n=len(plan["slices"]))
            raise duckdb.OutOfMemoryException(
                "Out of Memory Error: could not allocate block of size 256 KiB (audit injected)")
        t = time.time()
        o_slice(self, con, plan, idx, log)
        ev("slice_done", idx=idx, n=len(plan["slices"]), t_=sl.get("t"),
           payer=sl.get("payer"), took=time.time() - t)
        if slice_sleep:
            time.sleep(slice_sleep)

    trace = os.environ.get("MRFX_AUDIT_TRACE_SQL") == "1"

    class _HoldCon:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            t0 = time.time()
            r = self._con.execute(sql, *a, **k)
            if trace:
                ev("sql", s=" ".join(sql.split())[:140], took=round(time.time() - t0, 3))
            if hold_delete and sql.startswith("DELETE FROM rates_by_tin_tbl"):
                ev("delta_delete_held", secs=hold_delete)
                time.sleep(hold_delete)
            return r

        def __getattr__(self, n):
            return getattr(self._con, n)

    def delta(self, con, *a, **k):
        if failing():
            ev("delta_fail_injected")
            raise duckdb.OutOfMemoryException(
                "Out of Memory Error: could not allocate block of size 256 KiB (audit injected)")
        ev("delta_enter")
        r = o_delta(self, _HoldCon(con) if (hold_delete or trace) else con, *a, **k)
        ev("delta_exit")
        return r

    import threading as _th
    _tl = _th.local()
    o_bump = S._bump_generation

    def bump(self, *a, **k):
        r = o_bump(self, *a, **k)
        if dir_sleep and getattr(_tl, "names", False):
            # the name refresh holds the rollup lock this much longer
            ev("names_hold", secs=dir_sleep)
            _tl.names = False
            time.sleep(dir_sleep)
        return r

    def dir_incr(self, *a, **k):
        ev("dir_enter")
        _tl.names = True
        try:
            r = o_dir(self, *a, **k)
        finally:
            _tl.names = False
        ev("dir_exit", ret=r)
        return r
    S._bump_generation = bump

    import functools as _ft
    # keep the real signatures visible: ingest._skip_kw inspects
    # update_rollups_incremental for `skip_if_busy` (without this the inbox
    # path's best-effort refresh BLOCKED instead of skipping)
    incr = _ft.wraps(o_incr)(incr)
    full = _ft.wraps(o_full)(full)
    dir_incr = _ft.wraps(o_dir)(dir_incr)
    S.update_rollups_incremental = incr
    S.rebuild_rollups = full
    S._run_slice = run_slice
    S._apply_rollup_delta_sliced = delta
    S.refresh_directory_incremental = dir_incr


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "mrfx.store":
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None:
            return None
        orig = spec.loader.exec_module

        def exec_module(module):
            orig(module)
            try:
                _patch_store(module)
            except Exception as e:  # noqa: BLE001
                ev("patch_error", err=repr(e))
        spec.loader.exec_module = exec_module
        return spec


if os.environ.get("MRFX_AUDIT_HOOKS") == "1":
    sys.meta_path.insert(0, _Finder())
