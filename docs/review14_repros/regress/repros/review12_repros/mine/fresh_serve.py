"""First run of the shipped zip: empty store, `mrfx serve`, the pages a new
user sees. Runs on a copy of the extracted build so the suite's copy is untouched."""
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

A = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit12")
SRC = A / "unzipped" / "Claudevideo"
DST = A / "fresh_install"
PORT = 8473


def main():
    if DST.exists():
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(".venv", "data", "tests", "docs"))
    cfg = DST / "config" / "mrfx.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("port: 8377", f"port: {PORT}"), encoding="utf-8")
    py = SRC / ".venv" / "bin" / "python"      # the zip's own environment
    env = dict(os.environ, PYTHONUTF8="1")
    env.pop("MRFX_CONFIG", None)
    log = open(A / "fresh_serve.log", "w")
    t0 = time.monotonic()
    p = subprocess.Popen([str(py), "-m", "mrfx", "serve"], cwd=DST, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        up = None
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=2).read()
                up = time.monotonic() - t0
                break
            except Exception:
                time.sleep(0.5)
        print("time to first answer:", up)
        for path in ["/", "/api/status", "/api/files", "/api/urls", "/api/rates?limit=5", "/api/payers",
                     "/api/export_methodology", "/nonexistent"]:
            try:
                r = urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=30)
                print(f"GET {path} -> {r.status} {len(r.read())} bytes")
            except urllib.error.HTTPError as e:
                print(f"GET {path} -> HTTP {e.code} {e.read()[:120]!r}")
            except Exception as e:
                print(f"GET {path} -> {e}")
        time.sleep(5)
    finally:
        p.terminate()
        try:
            p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()
    txt = (A / "fresh_serve.log").read_text(errors="replace")
    print("--- serve log ---")
    print(txt[:3000])
    print("data dir now:", sorted(str(x.relative_to(DST)) for x in (DST / "data").rglob("*") if x.is_file())[:30])


if __name__ == "__main__":
    main()
