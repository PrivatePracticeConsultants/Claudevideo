"""Idle CPU of `mrfx serve` on an EMPTY scratch store (round 5 P1.1 claim: 0.4% of a core)."""
import subprocess, sys, time, os, pathlib
ROOT = pathlib.Path(sys.argv[1]); CFG = pathlib.Path(sys.argv[2]); LOG = CFG.parent.parent / "serve.log"
def cpu(pid):
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return int(f[11]) + int(f[12]), int(f[17])   # utime+stime jiffies, threads
def main():
    env = dict(os.environ, PYTHONUTF8="1")
    with open(LOG, "w") as lf:
        p = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-m", "mrfx", "--config", str(CFG), "serve"],
                             cwd=str(CFG.parent.parent), stdout=lf, stderr=subprocess.STDOUT, env=env)
    try:
        time.sleep(20)                       # let it finish starting
        j0, _ = cpu(p.pid); t0 = time.time()
        time.sleep(60)
        j1, thr = cpu(p.pid); t1 = time.time()
        hz = os.sysconf("SC_CLK_TCK")
        print(f"idle serve: {j1-j0} jiffies in {t1-t0:.0f}s = {100*(j1-j0)/hz/(t1-t0):.2f}% of one core; threads={thr}; children={len(os.listdir('/proc')) and subprocess.run(['pgrep','-cP',str(p.pid)],capture_output=True,text=True).stdout.strip()}")
    finally:
        p.terminate()
        try: p.wait(20)
        except subprocess.TimeoutExpired: p.kill()
if __name__ == "__main__":
    main()
