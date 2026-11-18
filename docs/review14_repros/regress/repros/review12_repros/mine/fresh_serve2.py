"""Second start of the same fresh install after the first was stopped mid-way
through building the NPPES cache: is the leftover nppes_cache.parquet.tmp
handled, and is the 1.1 GB bulk file reused rather than downloaded again?"""
import os
import subprocess
import time
import urllib.request
from pathlib import Path

A = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit12")
SRC = A / "unzipped" / "Claudevideo"
DST = A / "fresh_install"
PORT = 8473


def main():
    py = SRC / ".venv" / "bin" / "python"
    env = dict(os.environ, PYTHONUTF8="1")
    env.pop("MRFX_CONFIG", None)
    store = DST / "data" / "mrfx_store"
    print("before:", sorted(p.name for p in store.iterdir()))
    log = open(A / "fresh_serve2.log", "w")
    p = subprocess.Popen([str(py), "-m", "mrfx", "serve"], cwd=DST, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/files", timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        time.sleep(90)
    finally:
        p.terminate()
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()
    print("after:", sorted(p.name for p in store.iterdir()))
    print((A / "fresh_serve2.log").read_text(errors="replace")[:4000])


if __name__ == "__main__":
    main()
