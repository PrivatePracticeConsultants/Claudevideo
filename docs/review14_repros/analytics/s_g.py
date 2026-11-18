"""Scenario G (+H2).
G1: an out-of-memory error injected into every rollup slice (incremental and
    full) while a pasted link's file folds in; the failure stops after the
    2nd failed attempt. Header, back-off, recovery, nothing lost.
G2: duckdb_memory_gb: 1 (the config floor) on a never-built 6.77M-row store.
H2: the other 0.7.6 leftover: `rollup_needs_full` flagged + a file not
    covered, first 0.7.7 start with no activity."""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402
from mk_base import LATE  # noqa: E402

PORT, FPORT = 8483, 8484
HOOKS = str(h.ROOT / "hooks")


def events(p):
    try:
        return [json.loads(x) for x in Path(p).read_text().splitlines() if x.strip()]
    except OSError:
        return []


def g1():
    d = h.ROOT / "G1"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(h.ROOT / "bases" / "base7_built", d)
    cfgp = h.write_cfg(d, PORT)
    web = d / "web"
    web.mkdir()
    shutil.copy(LATE, web / LATE.name)
    fs = h.FileServer(web, FPORT)
    flag = d / "FAIL"
    flag.write_text("1")
    evp = d / "events.jsonl"
    env = {"PYTHONPATH": HOOKS, "MRFX_AUDIT_HOOKS": "1", "MRFX_AUDIT_EVENTS": str(evp),
           "MRFX_AUDIT_FAIL_FLAG": str(flag)}
    srv = h.Serve(cfgp, PORT, d / "serve.log", env_extra=env)
    srv.wait_ready()
    T0 = time.time()
    poll = h.Poller(PORT, every=0.5, light=False)
    poll.start()
    h.add_links(PORT, [fs.url(LATE.name)])
    t_clear = None
    while time.time() - T0 < 1500:
        ev = events(evp)
        n_fail = sum(1 for e in ev if e["ev"] == "full_raise")
        if t_clear is None and n_fail >= 2:
            flag.unlink()
            t_clear = time.time() - T0
        a = (poll.rows[-1].get("a") if poll.rows else None) or {}
        if t_clear and a.get("state") == "current" and a.get("covered_files") == a.get("done_files") == 8:
            break
        time.sleep(0.5)
    t_end = time.time() - T0
    time.sleep(1)
    poll.stop()
    payers = h.api(PORT, "/api/payers")
    srv.stop()
    fs.close()
    ev = events(evp)
    res = {"failure_cleared_s": t_clear and round(t_clear, 1), "current_s": round(t_end, 1),
           "timeline": h.states_timeline(poll.rows, T0), "payers_end": payers,
           "events": [(round(e["t"] - T0, 1), e["ev"], (e.get("err") or "")[:60]) for e in ev
                      if e["ev"] not in ("slice_done", "slice_fail_injected", "delta_fail_injected")],
           "n_slice_fail_injected": sum(1 for e in ev if e["ev"] == "slice_fail_injected"),
           "n_delta_fail_injected": sum(1 for e in ev if e["ev"] == "delta_fail_injected"),
           "slices_run_after_clear": sum(1 for e in ev if e["ev"] == "slice_done" and t_clear and e["t"] - T0 > t_clear),
           "slices_total_last": max([e["n"] for e in ev if e["ev"] == "slice_done"] or [0]),
           "log": h.grep_log(d / "serve.log", "analytics", "rollup", "memory", "halv", "cutting")}
    res["truth"] = h.short_truth(h.run_truth_subprocess(d / "store", hash_rows=True))
    ref = h.short_truth(h.run_truth_subprocess(h.ROOT / "bases" / "ref8" / "store", hash_rows=True))
    res["identical_to_ref8_dir"] = res["truth"]["hash_dir"] == ref["hash_dir"]
    h.save("g1.json", res)
    shutil.rmtree(d / "store", ignore_errors=True)
    return res


def g2():
    d = h.ROOT / "G2"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(h.ROOT / "bases" / "base8_unbuilt", d)
    cfgp = h.write_cfg(d, PORT, duckdb_memory_gb=1)
    srv = h.Serve(cfgp, PORT, d / "serve.log")
    srv.wait_ready()
    T0 = time.time()
    poll = h.Poller(PORT, every=0.5)
    poll.start()
    peak = 0
    while time.time() - T0 < 900:
        peak = max(peak, srv.rss_mb())
        a = (poll.rows[-1].get("a") if poll.rows else None) or {}
        if a.get("state") == "current" and a.get("covered_files") == a.get("done_files") == 8:
            break
        if a.get("state") == "failed" and time.time() - T0 > 30:
            break
        time.sleep(0.5)
    t_end = time.time() - T0
    poll.stop()
    srv.stop()
    res = {"end_s": round(t_end, 1), "peak_rss_mb": round(peak), "timeline": h.states_timeline(poll.rows, T0),
           "log": h.grep_log(d / "serve.log", "analytics", "memory", "cutting", "budget")}
    res["truth"] = h.short_truth(h.run_truth_subprocess(d / "store", hash_rows=True))
    h.save("g2.json", res)
    shutil.rmtree(d / "store", ignore_errors=True)
    return res


def h2():
    d = h.ROOT / "H2"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(h.ROOT / "bases" / "base7_built", d)
    cfgp = h.write_cfg(d, PORT)
    import subprocess
    code = f"""
import shutil, duckdb
from pathlib import Path
from mrfx.config import load_mrfx_config
from mrfx.ingest import ingest_file
from mrfx.store import Store
cfg = load_mrfx_config({str(cfgp)!r})
st = Store(cfg.store_dir, 2)
dst = cfg.inbox_dir / {LATE.name!r}
shutil.copy({str(LATE)!r}, dst)
print(ingest_file(cfg, st, dst, rebuild_rollups=False).get('status'))
"""
    if __name__ == "__main__":
        r = subprocess.run([h.PY, "-c", code], capture_output=True, text=True, cwd=str(d))
    import duckdb
    con = duckdb.connect(str(d / "store" / "mrfx.duckdb"))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_needs_full', '1')")
    con.close()
    srv = h.Serve(cfgp, PORT, d / "serve.log")
    T0 = time.time()
    first = None
    while time.time() - T0 < 120:
        try:
            first = {"dt": round(time.time() - T0, 1), "a": h.api(PORT, "/api/stats", timeout=10).get("analytics")}
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    poll = h.Poller(PORT, every=0.5, light=False)
    poll.start()
    while time.time() - T0 < 900:
        a = (poll.rows[-1].get("a") if poll.rows else None) or {}
        if a.get("state") == "current" and a.get("covered_files") == a.get("done_files") == 8:
            break
        time.sleep(0.5)
    t_end = time.time() - T0
    time.sleep(1)
    poll.stop()
    payers = h.api(PORT, "/api/payers")
    srv.stop()
    res = {"ingest": r.stdout[-200:] + r.stderr[-300:], "first_answer": first, "current_s": round(t_end, 1),
           "timeline": h.states_timeline(poll.rows, T0), "payers_end": payers,
           "log": h.grep_log(d / "serve.log", "analytics", "catch", "full")}
    res["truth"] = h.short_truth(h.run_truth_subprocess(d / "store", hash_rows=True))
    ref = h.short_truth(h.run_truth_subprocess(h.ROOT / "bases" / "ref8" / "store", hash_rows=True))
    res["identical_to_ref8"] = (res["truth"]["hash_spine"] == ref["hash_spine"], res["truth"]["hash_dir"] == ref["hash_dir"])
    h.save("h2.json", res)
    shutil.rmtree(d / "store", ignore_errors=True)
    return res


if __name__ == "__main__":
    for w in sys.argv[1:] or ["g1", "g2", "h2"]:
        r = {"g1": g1, "g2": g2, "h2": h2}[w]()
        print(w, json.dumps({k: v for k, v in r.items() if k not in ("log", "events")}, default=str)[:3000], flush=True)
