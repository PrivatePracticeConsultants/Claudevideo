"""Scenario 9: how many times is a file read per ingest? rchar (bytes read via syscalls, from
/proc/self/io) divided by the file size, and openat() calls on the file (strace -f), per path:
inbox in-process (fast / classic), inbox pooled, and the URL path (local server), with and
without a companion. Plus rebuild_rollups vs ingest time on the resulting store.
usage: s9_eff.py child <mode> <run>"""
import subprocess, sys, os
from dl import *
LOGS = HERE / "logs"
import mrfx.fetch as F
import mrfx.ingest as I
from mrfx.ingest import scan_inbox, ingest_file, ParsePoolManager, set_parse_pool

REAL = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad")
SRC = REAL / "real2/nd_hmhs_2026-10-27_000005200_in-network-rates_01_of_10.json.gz"   # 22 MB gz


def rchar(pid="self"):
    for line in open(f"/proc/{pid}/io"):
        if line.startswith("rchar"):
            return int(line.split()[1])


def child(mode, run):
    cfg = mk_cfg(run, fresh=False, fast=(mode != "classic"))[0]; store = Store(cfg.store_dir)
    p = cfg.inbox_dir / SRC.name
    r0 = rchar(); t0 = time.time()
    if mode in ("fast", "classic"):
        r = ingest_file(cfg, store, p)
    elif mode == "pooled":
        pool = ParsePoolManager(1); set_parse_pool(pool)
        try: r = ingest_file(cfg, store, p)
        finally: pool.shutdown(); set_parse_pool(None)
    elif mode == "url":
        srv = Srv({"/x/" + SRC.name: p.read_bytes()}); p.unlink()
        r0 = rchar()
        F.add_urls(store, [srv.base + "/x/" + SRC.name]); F.run_queue(cfg, store, drain=True)
        r = {"status": q(store, "SELECT status FROM url_queue")[0][0], "rows": q(store, "SELECT rows_emitted FROM url_queue")[0][0]}
        srv.close()
    dt_ = time.time() - t0
    t1 = time.time(); store.rebuild_rollups(); rb = time.time() - t1
    print(json.dumps({"mode": mode, "status": r["status"], "rows": r.get("rows"), "rchar_mb": round((rchar() - r0) / 1e6, 1),
                      "ingest_s": round(dt_, 1), "rebuild_s": round(rb, 1), "rows_total": q(store, "SELECT count(*) FROM rates")[0][0]}))


if __name__ == "__main__":
    if sys.argv[1:2] == ["child"]:
        child(sys.argv[2], sys.argv[3]); sys.exit(0)
    size = SRC.stat().st_size
    print(f"file: {SRC.name} {size/1e6:.1f} MB gz")
    for mode in ("fast", "classic", "pooled", "url"):
        run = "s9_" + mode; cfg, st = mk_cfg(run); st.close()
        shutil.copy(SRC, cfg.inbox_dir / SRC.name)
        env = dict(os.environ, PYTHONPATH="/home/user/Claudevideo")
        trace = LOGS / f"s9_{mode}.strace"
        cmd = ["strace", "-f", "-e", "trace=openat", "-o", str(trace), sys.executable, __file__, "child", mode, run]
        out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1200)
        opens = sum(1 for l in open(trace) if SRC.name in l and "O_RDONLY" in l and "ENOENT" not in l)
        parquet_opens = sum(1 for l in open(trace) if ".parquet" in l and "ENOENT" not in l and "rates/" in l)
        line = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else out.stderr[-300:]
        try:
            d = json.loads(line)
            print(f"  {mode:8}: opens of the file={opens:2}  bytes read (main process rchar) = {d['rchar_mb']:.0f} MB = {d['rchar_mb']*1e6/size:.1f}x file size  "
                  f"ingest {d['ingest_s']}s, rebuild_rollups {d['rebuild_s']}s on {d['rows_total']:,} rows; parquet opens {parquet_opens}")
        except ValueError:
            print(f"  {mode}: FAILED {line}")
        shutil.rmtree(HERE / "runs" / run, ignore_errors=True)
    # worker-process reads are not in the parent's rchar: count read syscalls per pid for the pooled trace
    tr = LOGS / "s9_pooled.strace"
    print("  pooled: per-pid openat of the source file:", {pid: n for pid, n in __import__("collections").Counter(
        l.split()[0] for l in open(tr) if SRC.name in l and "ENOENT" not in l).items()})
