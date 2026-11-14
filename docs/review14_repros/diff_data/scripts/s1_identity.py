"""S1: sliced spine/directory vs the OLD (c8b80fc) one-shot build, hard shapes."""
import json, os, pickle, shutil, subprocess, sys
from pathlib import Path

D = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(D / "scripts"))


def old_build(store_dir, out):
    code = f"""
import sys, pickle
sys.path.insert(0, {str(D/'scripts')!r})
import mrfx; assert 'diff_data/old' in mrfx.__file__, mrfx.__file__
from mrfx.store import Store
import shapes
s = Store({str(store_dir)!r})
s.rebuild_rollups()
pickle.dump(shapes.dump(s), open({str(out)!r}, 'wb'))
"""
    env = dict(os.environ, PYTHONPATH=str(D / "old"))
    subprocess.run([sys.executable, "-c", code], check=True, env=env, cwd=str(D))
    return pickle.load(open(out, "rb"))


def as_dicts(t):
    spine, d, cols = t
    return sorted(json.dumps(dict(zip(cols, r)), default=str, sort_keys=True) for r in spine), \
        sorted(json.dumps(list(r), default=str) for r in d)


if __name__ == "__main__":
    import shapes
    from mrfx.store import Store
    work = D / "w_s1"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    s = Store(work / "store")
    shapes.seed(s)
    with s.connect() as con:
        print("raw rows", con.execute("SELECT count(*) FROM rates").fetchone()[0])
    s.close() if hasattr(s, "close") else None
    shutil.copytree(work / "store", work / "store_old")
    old = as_dicts(old_build(work / "store_old", work / "old.pkl"))
    for rpp in (10_000_000, 400, 60, 7):
        shutil.rmtree(work / f"store_{rpp}", ignore_errors=True)
        shutil.copytree(work / "store", work / f"store_{rpp}")
        n = Store(work / f"store_{rpp}")
        n._rollup_partition_rows = lambda split=1, threads=1, _r=rpp: _r
        seen = []
        real = n._run_slice
        def spy(con, plan, idx, log, real=real):
            seen.append(plan["slices"][idx]); return real(con, plan, idx, log)
        n._run_slice = spy
        n.rebuild_rollups()
        new = as_dicts(shapes.dump(n))
        print(f"rows_per_slice={rpp}: slices={len(seen)} hash_split={sum(1 for x in seen if x.get('k',1)>1)} "
              f"spine old={len(old[0])} new={len(new[0])} identical={old[0]==new[0]} "
              f"dir old={len(old[1])} new={len(new[1])} identical={old[1]==new[1]}")
        if old[0] != new[0]:
            a, b = set(old[0]), set(new[0])
            print("  only old:", list(a - b)[:2]); print("  only new:", list(b - a)[:2])
        if old[1] != new[1]:
            a, b = set(old[1]), set(new[1])
            print("  dir only old:", list(a - b)[:2]); print("  dir only new:", list(b - a)[:2])
