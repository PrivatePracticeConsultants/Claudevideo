"""Scenario 7: the parse-worker pool. A worker that dies (os._exit(1) in its first parquet
write, via /tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/ingest/site/sitecustomize.py) and a worker that hangs forever.
Does the file end 'failed' with a sentence, does the pool keep serving the next file, what
temp files / orphan processes are left? Re-runs itself as a child with the shim on PYTHONPATH.
usage: s7_pool.py [child exit|hang]"""
import os, subprocess, sys
from h import *
from mrfx.ingest import ingest_file, ParsePoolManager, set_parse_pool
import mrfx.ingest as ing

SITE = "/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/ingest/site"
FLAG = "/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit14/regress/r11tmp/ingest/runs/s7_flag"


def children():
    out = subprocess.run(["ps", "-o", "pid=,ppid=,stat=,cmd=", "--ppid", str(os.getpid())], capture_output=True, text=True).stdout
    return [l.strip()[:90] for l in out.splitlines() if "python" in l]


class FastClock:
    """time.monotonic that runs `k` times faster — shrinks the 90-minute stall deadline of
    _ingest_in_network_pooled to seconds without touching mrfx/ (parent process only)."""
    def __init__(self, k):
        self.k = k; self.t0 = time.monotonic()
    def monotonic(self):
        return self.t0 + (time.monotonic() - self.t0) * self.k
    def __getattr__(self, n):
        return getattr(time, n)


def child(mode):
    cfg = mkcfg("s7_" + mode); store = Store(cfg.store_dir)
    pool = ParsePoolManager(2); set_parse_pool(pool)
    open(FLAG, "w").close()
    a = put(cfg, "acme_a_2026-05.json.gz", doc(n=20, pad_mb=1.0), gz=True)
    b = put(cfg, "acme_b_2026-05.json.gz", doc(n=25, seed=2, payer="Beta Health"), gz=True)
    if mode == "hang":
        ing.time = FastClock(2500)
    t0 = time.time()
    r = ingest_file(cfg, store, a)
    print(f"A ({mode}) -> {r['status']} in {time.time()-t0:.1f}s | error: {(r.get('error') or '')[:200]}")
    print("  files row:", [(f, s, n, (e or '')[:160]) for f, s, _, n, e in files_rows(store)])
    print("  temp leftovers in rates/:", [p for p in parts(store) if not p.endswith('.parquet')])
    print("  where A:", where_is(cfg, a.name))
    if mode == "hang":
        ing.time = time
    os.unlink(FLAG)
    t0 = time.time()
    r = ingest_file(cfg, store, b)
    print(f"B (healthy) on the same pool -> {r['status']} rows={r.get('rows')} in {time.time()-t0:.1f}s")
    # and A again (retry) now that the shim is off: the file is in failed/
    src = cfg.failed_dir / a.name
    if src.exists():
        r = ingest_file(cfg, store, src)
        print(f"A retried from failed/ -> {r['status']} rows={r.get('rows')}")
    print("  rates:", rate_counts(store), "| rollups_stale:", store.rollups_stale())
    print("  python children of this process before shutdown:", len(children()))
    pool.shutdown_now(); set_parse_pool(None)
    time.sleep(0.5)
    print("  python children after shutdown_now:", children())


if __name__ == "__main__":
    if sys.argv[1:2] == ["child"]:
        child(sys.argv[2]); sys.exit(0)
    for mode in ("exit", "hang"):
        env = dict(os.environ, PYTHONPATH=f"{SITE}:/home/user/Claudevideo", MRFX_R11_KILL=mode, MRFX_R11_FLAG=FLAG)
        p = subprocess.Popen([sys.executable, __file__, "child", mode], env=env,
                             stdout=subprocess.PIPE, stderr=open(LOGS / f"s7_{mode}.err", "w"), text=True)
        # the child must see its own pid as MRFX_R11_PARENT: set after spawn is impossible, so the
        # shim keys on "not the driver": pass the pid through a file the shim can't know... simplest:
        # the driver pid is never a worker, and the child's own pid is excluded by MRFX_R11_PARENT below
        out, _ = p.communicate(timeout=900)
        print(f"===== mode={mode} (exit {p.returncode})\n{out}")
        err = open(LOGS / f"s7_{mode}.err").read()
        keep = [l for l in err.splitlines() if any(k in l for k in ("WARNING", "ERROR", "sitecustomize", "wedged", "died", "restart"))]
        print("  stderr highlights:", *keep[:12], sep="\n    ")
