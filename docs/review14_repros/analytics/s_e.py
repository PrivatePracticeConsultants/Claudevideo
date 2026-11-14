"""Scenario E: kill `mrfx serve` (Popen.kill) at several instants of a fold-in,
restart with NO new activity, check it resumes, finishes, and that the spine is
byte-identical to the uninterrupted build of the same 8 real files (ref8)."""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import h  # noqa: E402
from mk_base import LATE  # noqa: E402

HOOKS = str(h.ROOT / "hooks")
PORT, FPORT = 8473, 8474


def events(p: Path):
    try:
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    except OSError:
        return []


def copy_base(name, dst):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(h.ROOT / "bases" / name, dst)
    return h.write_cfg(dst, PORT)


def wait(pred, timeout, every=0.3):
    t = time.time()
    while time.time() - t < timeout:
        v = pred()
        if v:
            return v
        time.sleep(every)
    return None


def stats(port):
    try:
        return h.api(port, "/api/stats", timeout=30).get("analytics") or {}
    except Exception:  # noqa: BLE001
        return {}


def restart_and_watch(d, cfgp, tag, timeout=900):
    """Restart serve with NO hooks and NO new activity; time to current."""
    srv = h.Serve(cfgp, PORT, d / f"serve_{tag}.log")
    t0 = time.time()
    srv.wait_ready()
    poll = h.Poller(PORT, every=0.5)
    poll.start()
    ok = wait(lambda: (lambda a: a.get("state") == "current" and a.get("covered_files") == a.get("done_files")
                       and a.get("done_files", 0) > 0 and a)(stats(PORT)), timeout)
    took = time.time() - t0
    time.sleep(2)
    payers = h.api(PORT, "/api/payers")
    poll.stop()
    srv.stop()
    return {"restart_to_current_s": round(took, 1), "ok": bool(ok), "final": ok,
            "timeline": h.states_timeline(poll.rows, t0), "payers_api": payers,
            "log": h.grep_log(d / f"serve_{tag}.log", "analytics", "rollup", "catch")}


def trial_before_fold(path_kind="inbox"):
    d = h.ROOT / f"E1_{path_kind}"
    cfgp = copy_base("base7_built", d)
    evp = d / "events.jsonl"
    env = {"PYTHONPATH": HOOKS, "MRFX_AUDIT_HOOKS": "1", "MRFX_AUDIT_EVENTS": str(evp),
           "MRFX_AUDIT_DELAY_ROLLUP": "300"}
    fs = None
    srv = h.Serve(cfgp, PORT, d / "serve_1.log", env_extra=env)
    srv.wait_ready()
    if path_kind == "inbox":
        shutil.copy(LATE, d / "inbox" / (".tmp_" + LATE.name))
        (d / "inbox" / (".tmp_" + LATE.name)).rename(d / "inbox" / LATE.name)
    else:
        web = d / "web"
        web.mkdir()
        shutil.copy(LATE, web / LATE.name)
        fs = h.FileServer(web, FPORT)
        h.add_links(PORT, [fs.url(LATE.name)])
    a = wait(lambda: (lambda a: a.get("done_files") == 8 and a)(stats(PORT)), 300)
    time.sleep(2)
    a = stats(PORT)
    ev = events(evp)
    srv.kill()
    if fs:
        fs.close()
    out = {"at_kill": a, "events_before_kill": [e["ev"] for e in ev]}
    out["restart"] = restart_and_watch(d, cfgp, "2")
    t = h.run_truth_subprocess(d / "store")
    out["truth"] = h.short_truth(t)
    return out


def trial_during_incremental():
    d = h.ROOT / "E2_incr"
    cfgp = copy_base("base7_built", d)
    evp = d / "events.jsonl"
    env = {"PYTHONPATH": HOOKS, "MRFX_AUDIT_HOOKS": "1", "MRFX_AUDIT_EVENTS": str(evp),
           "MRFX_AUDIT_HOLD_DELETE": "120"}
    srv = h.Serve(cfgp, PORT, d / "serve_1.log", env_extra=env)
    srv.wait_ready()
    shutil.copy(LATE, d / "inbox" / (".tmp_" + LATE.name))
    (d / "inbox" / (".tmp_" + LATE.name)).rename(d / "inbox" / LATE.name)
    got = wait(lambda: any(e["ev"] == "delta_delete_held" for e in events(evp)), 300)
    time.sleep(1)
    a = stats(PORT)
    srv.kill()
    out = {"held": bool(got), "at_kill": a, "events_before_kill": [e["ev"] for e in events(evp)]}
    out["restart"] = restart_and_watch(d, cfgp, "2")
    out["truth"] = h.short_truth(h.run_truth_subprocess(d / "store"))
    return out


def trial_during_full(frac: float):
    d = h.ROOT / f"E3_full_{int(frac * 100)}"
    cfgp = copy_base("base8_unbuilt", d)
    evp = d / "events.jsonl"
    env = {"PYTHONPATH": HOOKS, "MRFX_AUDIT_HOOKS": "1", "MRFX_AUDIT_EVENTS": str(evp),
           "MRFX_AUDIT_PARTITION_ROWS": "150000", "MRFX_AUDIT_SLICE_SLEEP": "0.7"}
    srv = h.Serve(cfgp, PORT, d / "serve_1.log", env_extra=env)
    srv.wait_ready()
    poll = h.Poller(PORT, every=0.3)
    poll.start()

    def at_frac():
        ev = [e for e in events(evp) if e["ev"] == "slice_done"]
        if ev and (ev[-1]["idx"] + 1) / ev[-1]["n"] >= frac:
            return ev[-1]
        return None
    hit = wait(at_frac, 600, every=0.1)
    a = stats(PORT)
    srv.kill()
    poll.stop()
    out = {"killed_after_slice": hit, "at_kill": a,
           "timeline_1": h.states_timeline(poll.rows, poll.rows[0]["t"] if poll.rows else 0)}
    # the plan as the kill left it (read-only, server down)
    import duckdb
    con = duckdb.connect(str(d / "store" / "mrfx.duckdb"), read_only=True)
    raw = con.execute("SELECT value FROM meta WHERE key='rollup_plan'").fetchone()
    con.close()
    plan = json.loads(raw[0]) if raw else None
    out["plan_left"] = plan and {"next": plan["next"], "slices": len(plan["slices"]),
                                 "kind": plan["kind"]}
    out["restart"] = restart_and_watch(d, cfgp, "2")
    out["truth"] = h.short_truth(h.run_truth_subprocess(d / "store"))
    return out


def main():
    which = sys.argv[1:] or ["e1i", "e1u", "e2", "e30", "e70"]
    ref = h.short_truth(h.run_truth_subprocess(h.ROOT / "bases" / "ref8" / "store"))
    res = {"ref8": {k: ref[k] for k in ("hash_spine", "hash_dir", "spine_rows")}}
    for w in which:
        t = time.time()
        if w == "e1i":
            r = trial_before_fold("inbox")
        elif w == "e1u":
            r = trial_before_fold("url")
        elif w == "e2":
            r = trial_during_incremental()
        elif w == "e30":
            r = trial_during_full(0.30)
        elif w == "e70":
            r = trial_during_full(0.70)
        r["identical_to_ref8"] = (r["truth"]["hash_spine"] == ref["hash_spine"],
                                  r["truth"]["hash_dir"] == ref["hash_dir"])
        r["wall_s"] = round(time.time() - t, 1)
        res[w] = r
        h.save(f"e_{w}.json", r)
        print(w, json.dumps({"at_kill": {k: r["at_kill"].get(k) for k in ("state", "covered_files", "done_files", "sentence")},
                             "restart_to_current_s": r["restart"]["restart_to_current_s"],
                             "ok": r["restart"]["ok"], "identical": r["identical_to_ref8"],
                             "missing": r["truth"]["missing"], "extra": r["truth"]["extra"],
                             "plan_left": r.get("plan_left"), "killed_after": r.get("killed_after_slice")},
                            default=str), flush=True)
    h.save("e_all.json", res)


if __name__ == "__main__":
    main()
