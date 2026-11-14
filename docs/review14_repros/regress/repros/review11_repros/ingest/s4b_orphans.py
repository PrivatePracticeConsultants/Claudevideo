"""Scenario 4b: how long do parse-pool workers outlive a killed parent? Parent (child process
of this driver) runs a 2-worker pool over one ~118 MB gz file (big-file path, >1 chunk of
64 MB) and is killed mid-parse; the workers are watched for up to 4 minutes: does the ACTIVE
worker stop at its next chunk boundary (ParentGone), does the IDLE one ever stop?
usage: s4b_orphans.py [child <run>]"""
import os, subprocess, sys
from h import *
from mrfx.ingest import scan_inbox, ParsePoolManager, set_parse_pool

REAL = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad")
BIG = REAL / "real/wy_2026-11_710_26B0_in-network-rates.json.gz"
SMALL = REAL / "real/uhc_2026-11-01_HEALTHSCOPE-BENEFITS_Third-Party-Administrator_SHO_U1_3342_in-network-rates.json.gz"
MARK = "MRFX_R11_ORPH"


def child(run):
    cfg = mkcfg(run, fresh=False); store = Store(cfg.store_dir)
    pool = ParsePoolManager(2); set_parse_pool(pool)
    for r in scan_inbox(cfg, store):
        print("child:", r.get("file"), r.get("status"), r.get("rows"), flush=True)
    pool.shutdown_now()


def workers(tag):
    out = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit(): continue
        try:
            env = (p / "environ").read_bytes(); cmd = (p / "cmdline").read_bytes()
        except OSError: continue
        if f"{MARK}={tag}".encode() in env and b"multiprocessing" in cmd and int(p.name) != os.getpid():
            st = (p / "stat").read_text().split()
            out.append((int(p.name), int(st[13]) + int(st[14])))   # pid, cpu ticks
    return sorted(out)


if __name__ == "__main__":
    if sys.argv[1:2] == ["child"]:
        child(sys.argv[2]); sys.exit(0)
    for label, src, kill_at in (("big (118 MB gz, 2 chunks, big-file path)", BIG, 60.0), ("small (2 MB gz, 1 chunk)", SMALL, 30.0)):
        run = "s4b"; cfg = mkcfg(run); shutil.copy(src, cfg.inbox_dir / src.name)
        tag = f"{label[:3]}{int(time.time())}"
        env = dict(os.environ, PYTHONPATH="/home/user/Claudevideo", **{MARK: tag})
        p = subprocess.Popen([sys.executable, __file__, "child", run], env=env, stdout=open(LOGS / "s4b.log", "w"), stderr=subprocess.STDOUT)
        # kill as soon as a worker is ACTIVE (its progress sidecar / temp part exists), else at kill_at
        t0 = time.time()
        while time.time() - t0 < kill_at and not list((cfg.store_dir / "rates").glob(".*.progress*")) \
                and not list((cfg.store_dir / "rates").glob(".*.parquet.tmp")):
            time.sleep(0.02)
        kill_at = round(time.time() - t0, 2)
        before = workers(tag)
        p.kill(); p.wait()
        print(f"== {label}: parent killed at {kill_at}s; workers before kill: {[w for w, _ in before]}")
        t0 = time.time(); last = None
        while time.time() - t0 < 240:
            w = workers(tag)
            snap = [(pid, ticks) for pid, ticks in w]
            if snap != last:
                print(f"   t+{time.time()-t0:5.1f}s alive: {[(pid, f'cpu={ticks/100:.1f}s') for pid, ticks in snap]}", flush=True)
                last = snap
            if not w: break
            time.sleep(5)
        print("   final:", "all workers gone" if not workers(tag) else f"{len(workers(tag))} worker(s) still alive after 240 s (idle, ppid 1) -> killed now")
        for pid, _ in workers(tag):
            try: os.kill(pid, 9)
            except OSError: pass
        st = Store(cfg.store_dir)
        print("   leftovers in rates/:", [x for x in parts(st) if not x.endswith(".parquet") and x != ".verified_parts.json"],
              "| files:", q(st, "SELECT filename, status FROM files"))
        st.close(); shutil.rmtree(BASE / run, ignore_errors=True)
