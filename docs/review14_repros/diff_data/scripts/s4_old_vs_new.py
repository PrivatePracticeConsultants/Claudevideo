"""Outreach CSV (tin + entity grain, with/without state & discipline) and the
Overview, on the SAME parts, old code (c8b80fc) vs new code, byte for byte."""
import hashlib, json, os, shutil, subprocess, sys
from pathlib import Path
D = Path(__file__).resolve().parent.parent

RUN = r'''
import sys, json, hashlib
sys.path.insert(0, %(scripts)r)
import mrfx
from mrfx.store import Store
from mrfx.api import FilterSet, rel_sql
from mrfx.outreach import build_outreach_rows, outreach_csv
from mrfx.overview import market_overview
s = Store(%(store)r)
s.rebuild_rollups()
out = {"mrfx": mrfx.__file__}
for grain in ("tin", "entity"):
    for qp in ({}, {"state": "MO"}, {"state": "KS", "discipline": "pt"}, {"cpt": "97110,97162"},
               {"payer": "Beta"}):
        fs = FilterSet(qp)
        market = {"month": fs.described.get("as_of_month") or "latest",
                  "payers": list(fs.described.get("payers") or [])}
        h, rows = build_outreach_rows(s, rel_sql(grain, fs), fs.params,
                                      fs.described.get("codes"), market=market)
        csv = outreach_csv(h, rows)
        out[f"outreach {grain} {json.dumps(qp)}"] = csv
for st in (None, "MO", "KS", "TX"):
    ov = market_overview(s, st)
    out[f"overview {st}"] = json.dumps(ov, sort_keys=True, default=str)
json.dump(out, open(%(dst)r, "w"))
'''


def run(pyroot, store, dst):
    env = dict(os.environ)
    if pyroot:
        env["PYTHONPATH"] = pyroot
    code = RUN % {"scripts": str(D / "scripts"), "store": str(store), "dst": str(dst)}
    p = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(D),
                       capture_output=True, text=True)
    if p.returncode:
        print(p.stderr[-3000:])
        raise SystemExit(1)
    return json.load(open(dst))


if __name__ == "__main__":
    sys.path.insert(0, str(D / "scripts"))
    import s4_seed
    from mrfx.store import Store
    work = D / "w_s4"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    s = Store(work / "seed")
    s4_seed.seed(s)
    del s
    shutil.copytree(work / "seed", work / "old")
    shutil.copytree(work / "seed", work / "new")
    old = run(str(D / "old"), work / "old", work / "old.json")
    new = run(None, work / "new", work / "new.json")
    print("old code:", old.pop("mrfx")); print("new code:", new.pop("mrfx"))
    for k in old:
        a, b = old[k], new.get(k)
        same = a == b
        info = ""
        if k.startswith("outreach"):
            info = f"{a.count(chr(10))} lines"
        print(("IDENTICAL " if same else "DIFFERENT ") + k, info)
        if not same:
            al, bl = a.splitlines(), (b or "").splitlines()
            for i, (x, y) in enumerate(zip(al, bl)):
                if x != y:
                    print("   old:", x[:300]); print("   new:", y[:300]); break
            if len(al) != len(bl):
                print("   line counts", len(al), len(bl))
