"""Kill the drain at random instants (N trials, seeded), restart, and check:
no row left in flight, every file's rows ingested exactly once, files table
== parquet parts, no orphan .part/.fetchmeta/.parquet.tmp left behind."""
import json
import os
import random
import signal
import subprocess
import sys
import time
from h import *  # noqa

PY = "/home/user/Claudevideo/.venv/bin/python"
HERE = Path(__file__).parent


def inspect(root):
    out = subprocess.run([PY, str(HERE / "s5_inspect.py"), str(root)], capture_output=True, text=True, timeout=120)
    if out.returncode:
        return {"error": out.stderr[-800:]}
    return json.loads(out.stdout.strip().splitlines()[-1])


def run_driver(root, urls=(), wait=None):
    p = subprocess.Popen([PY, str(HERE / "s5_driver.py"), str(root), *urls],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if wait is not None:
        try:
            p.wait(wait)
        except subprocess.TimeoutExpired:
            p.kill(); p.wait()
            return None
    return p


if __name__ == "__main__":
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else int(time.time()) % 100000
    trials = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    rng = random.Random(seed)
    print(f"seed={seed} trials={trials}")
    # the world: 6 index children (paced ~0.4 s each), 2 direct files, a twin pair, a ref file
    srv = Server({})
    expected = {}
    for i in range(6):
        srv.routes[f"/c{i}.json.gz"] = {"body": rate_file_bytes(f"c{i}", n_rows=2, pad=600_000), "piece": 64 << 10, "pace": 0.04}
        expected[f"c{i}.json.gz"] = 6
    for name in ("d1", "d2"):
        srv.routes[f"/{name}.json.gz"] = {"body": rate_file_bytes(name, n_rows=3, pad=300_000), "piece": 64 << 10, "pace": 0.04}
        expected[f"{name}.json.gz"] = 9
    twin = rate_file_bytes("tw", n_rows=2, pad=300_000)
    srv.routes["/ta/x.json.gz"] = {"body": twin, "piece": 64 << 10, "pace": 0.04}
    srv.routes["/tb/x.json.gz"] = {"body": twin, "piece": 64 << 10, "pace": 0.04}
    expected["x.json.gz"] = 6     # exactly one of the twins
    srv.routes["/toc.json"] = toc_bytes([srv.url(f"/c{i}.json.gz") for i in range(6)])
    urls = [srv.url("/toc.json"), srv.url("/d1.json.gz"), srv.url("/d2.json.gz"),
            srv.url("/ta/x.json.gz"), srv.url("/tb/x.json.gz")]
    # a clean reference run to learn the duration
    root = SCRATCH / "s5_ref"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    t0 = time.monotonic(); run_driver(root, urls, wait=180); ref_secs = time.monotonic() - t0
    ref = inspect(root)
    print(f"reference run: {ref_secs:.1f}s; rates={ {r['source_file'].split('_',1)[1]: r['n'] for r in ref['rates']} }")
    problems = []
    for t in range(trials):
        root = SCRATCH / f"s5_t{t}"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        at = rng.uniform(0.3, max(1.0, ref_secs * 0.9))
        p = run_driver(root, urls)
        time.sleep(at)
        alive = p.poll() is None
        p.kill(); p.wait()
        after_kill = inspect(root)
        inflight = [r for r in after_kill["queue"] if r["status"] in ("downloading", "fetched", "expanding", "ingesting")]
        orphans_before = after_kill["downloads"]
        proc_files = [f for f in after_kill["files"] if f["status"] == "processing"]
        # restart
        t1 = time.monotonic(); p2 = run_driver(root, (), wait=180); restart_secs = time.monotonic() - t1
        fin = inspect(root)
        statuses = {r["status"] for r in fin["queue"]}
        stuck = [r for r in fin["queue"] if r["status"] not in ("done", "skipped", "failed")]
        got = {r["source_file"].split("_", 1)[1]: r["n"] for r in fin["rates"]}
        # twins: one done, one skipped-duplicate, bytes once
        tw = [r for r in fin["queue"] if r["url"].endswith("x.json.gz")]
        tw_ok = sorted(r["status"] for r in tw) == ["done", "skipped"]
        dup_rows = {k: v for k, v in got.items() if v != expected.get(k)}
        files_vs_parts = (len([f for f in fin["files"] if f["status"] == "done"]), len([p for p in fin["parts"] if p.endswith(".parquet")]))
        tmp_parts = [p for p in fin["parts"] if not p.endswith(".parquet")]
        leftovers = fin["downloads"]
        verdict = "OK"
        if stuck or dup_rows or not tw_ok or tmp_parts or files_vs_parts[0] != files_vs_parts[1] or p2 is None:
            verdict = "PROBLEM"
        line = (f"trial {t:2d} kill@{at:4.1f}s alive={alive} inflight_after_kill={[(r['id'], r['status']) for r in inflight]} "
                f"processing_files={len(proc_files)} dl_after_kill={len(orphans_before)} | restart {restart_secs:.1f}s "
                f"stuck={[(r['id'], r['status']) for r in stuck]} rows_mismatch={dup_rows} twins={sorted(r['status'] for r in tw)} "
                f"files/parts={files_vs_parts} tmp={tmp_parts} downloads_left={leftovers} -> {verdict}")
        print(line, flush=True)
        if verdict != "OK":
            problems.append((t, line, fin))
        else:
            shutil.rmtree(root, ignore_errors=True)
    print(f"\n{len(problems)} problem trial(s) of {trials}")
    for t, line, fin in problems:
        print("----", t); print(json.dumps(fin["queue"], indent=0, default=str)[:3000]); print(fin["files"], fin["parts"], fin["downloads"])
    srv.stop()
