# 0.7.6 instrumentation (PYTHONPATH=hooks076): while $MRFX_AUDIT_FAIL_FLAG
# exists, every RATE rollup (incremental or full) raises DuckDB's
# OutOfMemoryException — the user's "rebuild keeps running out" state.
import importlib.abc, importlib.util, json, os, sys, time
_EV = os.environ.get("MRFX_AUDIT_EVENTS")
def ev(kind, **kw):
    if _EV:
        with open(_EV, "a") as f:
            f.write(json.dumps({"t": time.time(), "ev": kind, **kw}) + "\n")
def _patch(m):
    import duckdb
    S = m.Store
    flag = os.environ.get("MRFX_AUDIT_FAIL_FLAG")
    o_i, o_f = S.update_rollups_incremental, S.rebuild_rollups
    def boom(what):
        ev("fail_injected", what=what)
        raise duckdb.OutOfMemoryException("Out of Memory Error: failed to pin block (audit injected, 0.7.6)")
    def inc(self, *a, **k):
        if flag and os.path.exists(flag):
            boom("incremental")
        ev("incr"); return o_i(self, *a, **k)
    def full(self, *a, **k):
        names_only = k.get("names_only", a[0] if a else False)
        if flag and os.path.exists(flag) and not names_only:
            boom("full")
        ev("full", names_only=names_only); return o_f(self, *a, **k)
    S.update_rollups_incremental, S.rebuild_rollups = inc, full
    ev("patched", version=getattr(sys.modules.get("mrfx"), "__version__", "?"))
class F(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "mrfx.store":
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        o = spec.loader.exec_module
        def ex(mod):
            o(mod); _patch(mod)
        spec.loader.exec_module = ex
        return spec
if os.environ.get("MRFX_AUDIT_HOOKS") == "1":
    sys.meta_path.insert(0, F())
