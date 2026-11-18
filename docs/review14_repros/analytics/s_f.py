"""Scenario F: a queue that never goes idle.
F1: real 7-file store (built), 25 small links, one every 10 s, each download
    spread over 14 s (one download slot) -> the queue is busy for ~6 min. A
    30 s hold inside every incremental emulates a fold-in that takes 30 s.
F2: a part-way FULL plan left by a kill, then the same busy trickle.
F3: a part-way plan whose start-up catch-up FAILS, then one slow download
    (busy, but nothing finishes for ~6 min): when does the plan resume?"""
import datetime as dt
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402

HOOKS = str(h.ROOT / "hooks")
PORT, FPORT = 8475, 8476


def events(p):
    try:
        return [json.loads(x) for x in Path(p).read_text().splitlines() if x.strip()]
    except OSError:
        return []


def thr_server(d, secs):
    import os
    env = dict(os.environ, THR_SECONDS=str(secs))
    p = subprocess.Popen([sys.executable, "-I", str(h.ROOT / "thr_server.py"), str(FPORT), str(d)],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    return p


def utc_to_epoch(s):
    if not s:
        return None
    s = s.split(".")[0] if "." in s else s
    return dt.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()


def gen_trickle(web, n, prefix):
    names = []
    for i in range(n):
        # every third file is a NEW payer; the rest add to two existing payers
        payer = f"Trickle New {i:02d}" if i % 3 == 0 else ("Trickle Repeat A" if i % 2 else "Trickle Repeat B")
        nm = f"{prefix}_{i:02d}.json.gz"
        h.gen_mrf(web / nm, payer, 20000 + i * 100, 60, seed=100 + i, rate_base=50 + i)
        names.append(nm)
    return names


def run_trickle(d, cfgp, env, names, every, label, kill_at_frac=None, evp=None, stop_after=1800):
    srv = h.Serve(cfgp, PORT, d / f"serve_{label}.log", env_extra=env)
    srv.wait_ready()
    T0 = time.time()
    poll = h.Poller(PORT, every=0.5)
    poll.start()
    files_seen = {}
    added = []
    i = 0
    next_add = time.time()
    done_all = None
    busy_states = []
    while time.time() - T0 < stop_after:
        if i < len(names) and time.time() >= next_add:
            h.add_links(PORT, [f"http://127.0.0.1:{FPORT}/{names[i]}"])
            added.append((names[i], time.time() - T0))
            i += 1
            next_add += every
        try:
            fl = h.api(PORT, "/api/files?limit=500", timeout=30)
            for r in fl.get("files") or []:
                if r.get("status") == "done" and r["filename"] not in files_seen:
                    files_seen[r["filename"]] = {"seen": time.time() - T0,
                                                 "finished": (utc_to_epoch(r.get("finished_at")) or 0) - T0}
            q = h.api(PORT, "/api/urls", timeout=30)
            busy_states.append((round(time.time() - T0, 1), json.dumps(q.get("counts") or q.get("summary") or "")[:200]))
        except Exception as e:  # noqa: BLE001
            busy_states.append((round(time.time() - T0, 1), repr(e)[:100]))
        a = (poll.rows[-1].get("a") if poll.rows else None) or {}
        if i >= len(names) and a.get("state") == "current" and a.get("covered_files") == a.get("done_files") \
                and len(files_seen) >= a.get("done_files", 0) and done_all is None:
            done_all = time.time() - T0
            break
        time.sleep(1.0)
    time.sleep(1)
    poll.stop()
    srv.stop()
    return {"T0": T0, "added": added, "files_seen": files_seen, "poll": poll.rows,
            "done_all_s": done_all, "busy": busy_states,
            "log": h.grep_log(d / f"serve_{label}.log", "analytics", "rollup", "refresh took")}


def coverage_gaps(run, base_files):
    """Per trickle file: finished -> covered (covered_files reaches its rank)."""
    rows = run["poll"]
    T0 = run["T0"]
    order = sorted(((v["finished"], k) for k, v in run["files_seen"].items()))
    out = []
    for rank, (fin, name) in enumerate(order, start=base_files + 1):
        cov_t = None
        for r in rows:
            a = r.get("a") or {}
            if (a.get("covered_files") or 0) >= rank and r["t"] - T0 >= fin - 1:
                cov_t = r["t"] - T0
                break
        out.append({"file": name, "finished": round(fin, 1),
                    "covered": cov_t and round(cov_t, 1),
                    "gap_s": cov_t and round(cov_t - fin, 1)})
    return out


def busy_header(run):
    """What the header said while the queue was busy (sampled)."""
    seen = []
    for r in run["poll"]:
        a = r.get("a") or {}
        s = (a.get("state"), (a.get("reason") or "")[:110], f"{a.get('covered_files')}/{a.get('done_files')}")
        if not seen or tuple(seen[-1][1:]) != s:
            seen.append((round(r["t"] - run["T0"], 1), *s))
    return seen


def f1():
    d = h.ROOT / "F1"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(h.ROOT / "bases" / "base7_built", d)
    cfgp = h.write_cfg(d, PORT, parallel_downloads=1, parallel_ingests=1, small_file_downloads=0)
    web = d / "web"
    web.mkdir()
    names = gen_trickle(web, 25, "trk")
    fs = thr_server(web, 14)
    evp = d / "events.jsonl"
    env = {"PYTHONPATH": HOOKS, "MRFX_AUDIT_HOOKS": "1", "MRFX_AUDIT_EVENTS": str(evp),
           "MRFX_AUDIT_HOLD_DELETE": "30"}
    try:
        run = run_trickle(d, cfgp, env, names, 10, "f1")
    finally:
        fs.terminate()
    gaps = coverage_gaps(run, 0)
    ev = events(evp)
    T0 = run["T0"]
    rebuilds = [(round(e["t"] - T0, 1), e["ev"]) for e in ev if e["ev"] in ("incr_enter", "incr_exit", "full_enter", "full_exit")]
    res = {"poll": [(round(r["t"] - T0, 1), (r.get("a") or {}).get("state"), (r.get("a") or {}).get("covered_files"), (r.get("a") or {}).get("done_files")) for r in run["poll"]],
           "gaps": gaps, "rebuilds": rebuilds, "header": busy_header(run), "done_all_s": run["done_all_s"],
           "log": run["log"], "added_last": run["added"][-1], "busy_sample": run["busy"][::15]}
    res["truth"] = h.short_truth(h.run_truth_subprocess(d / "store", hash_rows=False))
    h.save("f1.json", res)
    shutil.rmtree(d / "store", ignore_errors=True)
    return res


def kill_mid_plan(d, frac, env_extra=None):
    cfgp = h.write_cfg(d, PORT, parallel_downloads=1, parallel_ingests=1, small_file_downloads=0)
    evp = d / "events_1.jsonl"
    env = {"PYTHONPATH": HOOKS, "MRFX_AUDIT_HOOKS": "1", "MRFX_AUDIT_EVENTS": str(evp),
           "MRFX_AUDIT_PARTITION_ROWS": "150000", "MRFX_AUDIT_SLICE_SLEEP": "0.7"}
    srv = h.Serve(cfgp, PORT, d / "serve_1.log", env_extra=env)
    srv.wait_ready()
    t = time.time()
    while time.time() - t < 600:
        ev = [e for e in events(evp) if e["ev"] == "slice_done"]
        if ev and (ev[-1]["idx"] + 1) / ev[-1]["n"] >= frac:
            break
        time.sleep(0.1)
    srv.kill()
    return cfgp, ev[-1] if ev else None


def f2():
    d = h.ROOT / "F2"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(h.ROOT / "bases" / "base8_unbuilt", d)
    cfgp, killed = kill_mid_plan(d, 0.4)
    web = d / "web"
    web.mkdir()
    names = gen_trickle(web, 12, "trk2")
    fs = thr_server(web, 14)
    evp = d / "events_2.jsonl"
    # the resumed plan keeps its 0.7 s per slice so it overlaps the trickle
    env = {"PYTHONPATH": HOOKS, "MRFX_AUDIT_HOOKS": "1", "MRFX_AUDIT_EVENTS": str(evp),
           "MRFX_AUDIT_SLICE_SLEEP": "0.7"}
    try:
        run = run_trickle(d, cfgp, env, names, 10, "f2")
    finally:
        fs.terminate()
    ev = events(evp)
    T0 = run["T0"]
    sl = [e for e in ev if e["ev"] == "slice_done"]
    res = {"killed_after": killed, "slices_after_restart": len(sl),
           "first_slice_s": sl and round(sl[0]["t"] - T0, 1), "last_slice_s": sl and round(sl[-1]["t"] - T0, 1),
           "added": run["added"], "gaps": coverage_gaps(run, 0), "header": busy_header(run),
           "rebuilds": [(round(e["t"] - T0, 1), e["ev"]) for e in ev if e["ev"].endswith(("enter", "exit", "raise"))],
           "done_all_s": run["done_all_s"], "log": run["log"]}
    res["truth"] = h.short_truth(h.run_truth_subprocess(d / "store", hash_rows=False))
    h.save("f2.json", res)
    shutil.rmtree(d / "store", ignore_errors=True)
    return res


def f3():
    d = h.ROOT / "F3"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(h.ROOT / "bases" / "base8_unbuilt", d)
    cfgp, killed = kill_mid_plan(d, 0.4)
    web = d / "web"
    web.mkdir()
    names = gen_trickle(web, 1, "slow")
    fs = thr_server(web, 420)        # one download that takes 7 min
    flag = d / "FAIL"
    flag.write_text("1")
    evp = d / "events_2.jsonl"
    env = {"PYTHONPATH": HOOKS, "MRFX_AUDIT_HOOKS": "1", "MRFX_AUDIT_EVENTS": str(evp),
           "MRFX_AUDIT_FAIL_FLAG": str(flag)}
    srv = h.Serve(cfgp, PORT, d / "serve_2.log", env_extra=env)
    srv.wait_ready()
    T0 = time.time()
    poll = h.Poller(PORT, every=1.0)
    poll.start()
    # wait for the start-up catch-up to fail
    t = time.time()
    while time.time() - t < 300 and not any(e["ev"] == "full_raise" for e in events(evp)):
        time.sleep(0.5)
    t_fail = time.time() - T0
    h.add_links(PORT, [f"http://127.0.0.1:{FPORT}/{names[0]}"])
    t_add = time.time() - T0
    flag.unlink()                    # the failure has stopped
    t_clear = time.time() - T0
    resumed = None
    while time.time() - T0 < 1200:
        ev = events(evp)
        if resumed is None and any(e["ev"] == "slice_done" for e in ev):
            resumed = time.time() - T0
        a = (poll.rows[-1].get("a") if poll.rows else None) or {}
        if a.get("state") == "current" and a.get("done_files") == 9 and a.get("covered_files") == 9:
            break
        time.sleep(1)
    t_end = time.time() - T0
    poll.stop()
    srv.stop()
    fs.terminate()
    ev = events(evp)
    file_done = None
    import duckdb
    con = duckdb.connect(str(d / "store" / "mrfx.duckdb"), read_only=True)
    fin = con.execute("SELECT finished_at::VARCHAR FROM files WHERE filename LIKE '%slow_00%'").fetchone()
    con.close()
    file_done = fin and (utc_to_epoch(fin[0]) - T0)
    res = {"killed_after": killed, "catchup_failed_at_s": round(t_fail, 1), "link_added_s": round(t_add, 1),
           "failure_cleared_s": round(t_clear, 1), "slow_file_done_s": file_done and round(file_done, 1),
           "plan_resumed_s": resumed and round(resumed, 1), "current_at_s": round(t_end, 1),
           "header": [(round(r["t"] - T0, 1), (r.get("a") or {}).get("state"), ((r.get("a") or {}).get("sentence") or "")[:160])
                      for r in poll.rows][::10],
           "events": [(round(e["t"] - T0, 1), e["ev"]) for e in ev if e["ev"] != "slice_done"][:40],
           "log": h.grep_log(d / "serve_2.log", "analytics", "rollup", "catch")}
    res["truth"] = h.short_truth(h.run_truth_subprocess(d / "store", hash_rows=False))
    h.save("f3.json", res)
    shutil.rmtree(d / "store", ignore_errors=True)
    return res


if __name__ == "__main__":
    for w in sys.argv[1:] or ["f1", "f2", "f3"]:
        r = {"f1": f1, "f2": f2, "f3": f3}[w]()
        print(w, json.dumps({k: v for k, v in r.items() if k not in ("log", "header", "events", "busy_sample")},
                            default=str)[:3000], flush=True)
