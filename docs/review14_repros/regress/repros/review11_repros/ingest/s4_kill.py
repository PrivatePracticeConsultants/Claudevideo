"""Scenario 4: kill the ingesting process at random instants (plus the commit window) and
restart. Two real payer files (~57 MB gz) through a 2-worker parse pool via scan_inbox.
After restart: each file done exactly once with the clean-run row count, no temp/spool
leftovers, no orphan workers, rollups rebuilt, store readable.
usage: s4_kill.py [trials] | s4_kill.py child <run> [marker-dir]"""
import os, random, subprocess, sys
from h import *
from mrfx.ingest import scan_inbox, ParsePoolManager, set_parse_pool
import mrfx.ingest as I

REAL = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad")
FILES = [REAL / "real/bcbsms_2026-10_320_33B0_in-network-rates_01_of_10.json.gz",
         REAL / "real/uhc_2026-11-01_UnitedHealthcare-Insurance-Company-of-New-York_Third-Party-Administrator_Empire_CSP-377-A351_in-network-rates.json.gz"]
MARK = "MRFX_R11_TRIAL"


def child(run, marker_dir=None):
    cfg = mkcfg(run, fresh=False); store = Store(cfg.store_dir)
    store.recover_stuck_files()
    pool = ParsePoolManager(2); set_parse_pool(pool)
    if marker_dir:
        md = Path(marker_dir)
        real_fin = store.finalize_rates_part
        real_commit = I._commit_done
        real_ok = I._finish_ok
        def fin(name, tmp, n):
            (md / "before_finalize").touch(); real_fin(name, tmp, n); (md / "after_finalize").touch()
        def commit(st, name, **f):
            (md / "before_commit").touch(); real_commit(st, name, **f); (md / "after_commit").touch()
        def ok(c, s, p):
            (md / "before_finish_ok").touch(); real_ok(c, s, p); (md / "after_finish_ok").touch()
        store.finalize_rates_part = fin; I._commit_done = commit; I._finish_ok = ok
    try:
        for r in scan_inbox(cfg, store):
            print("child:", r.get("file"), r.get("status"), r.get("rows"), flush=True)
    finally:
        pool.shutdown_now(); set_parse_pool(None)
    print("child: done", flush=True)


def orphans(tag):
    out = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit(): continue
        try:
            env = (p / "environ").read_bytes()
        except OSError: continue
        if f"{MARK}={tag}".encode() in env and int(p.name) != os.getpid():
            out.append(int(p.name))
    return out


def run_child(run, env_tag, marker_dir=None):
    env = dict(os.environ, PYTHONPATH="/home/user/Claudevideo", **{MARK: env_tag})
    args = [sys.executable, __file__, "child", run] + ([str(marker_dir)] if marker_dir else [])
    return subprocess.Popen(args, env=env, stdout=open(LOGS / f"s4_{env_tag}.log", "w"), stderr=subprocess.STDOUT)


DUP_SQL = "SELECT count(*) FROM (SELECT source_file, billing_code, tin_value, npi, negotiated_rate, billing_code_modifier, service_code, billing_class, negotiated_type, expiration_date FROM rates GROUP BY ALL HAVING count(*) > 1)"


def inspect(cfg, store, expected, label):
    files = q(store, "SELECT filename, status, rows_emitted, substr(error,1,70) FROM files ORDER BY 1")
    rc = rate_counts(store)
    tmps = [p for p in parts(store) if not p.endswith(".parquet") and p != ".verified_parts.json"]
    ok = all(s == "done" and n == expected[f] and rc.get(f) == expected[f] for f, s, n, _ in files) and len(files) == len(expected)
    dup = q(store, DUP_SQL)[0][0]
    return ok, files, rc, tmps, dup


if __name__ == "__main__":
    if sys.argv[1:2] == ["child"]:
        child(*sys.argv[2:4]); sys.exit(0)
    trials = int(sys.argv[1]) if sys.argv[1:] else 20
    seed = int(time.time()) % 100000; rng = random.Random(seed); print("seed:", seed)
    # clean run: expected rows + duration
    run = "s4_clean"; cfg = mkcfg(run)
    for f in FILES: shutil.copy(f, cfg.inbox_dir / f.name)
    t0 = time.time(); p = run_child(run, "clean"); p.wait(); T = time.time() - t0
    store = Store(cfg.store_dir)
    expected = {f: n for f, s, n, _ in q(store, "SELECT filename, status, rows_emitted, error FROM files")}
    DUP0 = q(store, DUP_SQL)[0][0]
    print(f"clean run: {T:.1f}s, rows {expected}, rates {rate_counts(store)}, within-file repeated lines in the clean store: {DUP0}")
    store.close(); shutil.rmtree(BASE / run)
    results = []
    plan = [("random", rng.uniform(1.0, T * 0.95)) for _ in range(trials)] + \
           [("before_finalize", None), ("after_finalize", None), ("before_commit", None), ("after_commit", None), ("before_finish_ok", None)]
    for i, (kind, at) in enumerate(plan):
        run = f"s4_t{i}"; cfg = mkcfg(run)
        for f in FILES: shutil.copy(f, cfg.inbox_dir / f.name)
        md = BASE / run / "markers"; md.mkdir()
        tag = f"t{i}_{seed}"
        p = run_child(run, tag, md if at is None else None)
        if at is not None:
            time.sleep(at)
        else:
            t0 = time.time()
            while not (md / kind).exists() and time.time() - t0 < T * 3 and p.poll() is None: time.sleep(0.002)
        p.kill(); p.wait()
        life = {}
        for wait_s in (1, 5, 20, 45):
            time.sleep(wait_s - sum(life.keys()) if not life else wait_s - max(life.keys()))
            alive = orphans(tag)
            life[wait_s] = [(o, float(subprocess.run(["ps", "-o", "%cpu=", "-p", str(o)], capture_output=True, text=True).stdout or 0)) for o in alive]
            if not alive: break
        orph_after_kill = life.get(1, []); orph_later = life.get(45, life[max(life)])
        for o, _ in orph_later:
            try: print(f"  (report only, not killed) orphan pid {o} still alive at 45 s", flush=True)
            except OSError: pass
        store = Store(cfg.store_dir)
        pre = q(store, "SELECT filename, status, rows_emitted FROM files ORDER BY 1")
        pre_tmps = [x for x in parts(store) if not x.endswith(".parquet") and x != ".verified_parts.json"]
        stale_pre = store.rollups_stale()
        rec = store.recover_stuck_files()
        store.close()
        # restart: a second child drains the inbox
        p2 = run_child(run, tag + "r"); rc2 = p2.wait(timeout=T * 4 + 60)
        store = Store(cfg.store_dir)
        ok, files, rc, tmps, dup = inspect(cfg, store, expected, run)
        stale = store.rollups_stale()
        if stale:
            store.rebuild_rollups(); stale = store.rollups_stale()
        inbox_left = sorted(x.name for x in cfg.inbox_dir.iterdir())
        res = dict(trial=i, kind=kind, at=round(at, 1) if at else None, ok=ok, files=files, rates=rc, tmps_after_kill=pre_tmps,
                   tmps_after_restart=tmps, dup_rows=dup, orphans_1s=len(orph_after_kill), orphans_4s=len(orph_later), orphan_life=life,
                   recovered=rec, pre=pre, stale_pre=stale_pre, stale_after=stale, inbox_left=inbox_left, restart_exit=rc2)
        results.append(res)
        flag = ("OK " if ok and not tmps and dup == DUP0 and not stale else "BAD") + ("+orphans" if orph_later else "")
        print(f"{flag} trial {i:2} {kind:16} at={res['at']} pre={[(f[:8], s, n) for f, s, n in pre]} tmps_after_kill={len(pre_tmps)} "
              f"orphans(1s/45s)={len(orph_after_kill)}/{len(orph_later)} life={ {k: [c for _, c in v] for k, v in life.items()} } recovered={rec} -> files={[(f[:8], s, n) for f, s, n, _ in files]} "
              f"tmps={tmps} dup={dup} stale={stale}", flush=True)
        store.close(); shutil.rmtree(BASE / run, ignore_errors=True)
    bad = [r for r in results if not (r["ok"] and not r["tmps_after_restart"] and r["dup_rows"] == DUP0 and not r["stale_after"])]
    orph = sum(1 for r in results if r["orphans_4s"])
    print(f"\n{len(results) - len(bad)}/{len(results)} trials correct (rows/state/temps/rollups); {orph}/{len(results)} left pool workers alive 45 s after the kill; seed {seed}")
    for r in bad:
        print("  BAD:", json.dumps(r, default=str)[:600])
