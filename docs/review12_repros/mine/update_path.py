"""The user's real update path: a 0.7.4 install with data loaded, then
`python -m mrfx update <0.7.5 zip>`, then start the server on that store.
Everything lives under audit12/old_install; nothing touches a real store."""
import csv
import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

A = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit12")
REPO = Path("/home/user/Claudevideo")
ZIP = A.parent / "mrfx_project.zip"
OLD = A / "old_install"
REAL = A.parent / "real"
PORT = 8471
SMALL = ["uhc_2026-11-01_HEALTHSCOPE-BENEFITS_Third-Party-Administrator_SHO_U1_3342_in-network-rates.json.gz",
         "moda_ModaTXPriceTrans_20261101.json.gz",
         "wy_2026-10-27_000005300_in-network-rates_05_of_09.json.gz"]


def sh(cmd, cwd, env=None, check=True, timeout=1800):
    print(f"\n$ {' '.join(str(c) for c in cmd)}  (cwd={cwd.name})", flush=True)
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, text=True,
                       capture_output=True, timeout=timeout)
    out = (r.stdout + r.stderr)
    print(out[-4000:])
    if check and r.returncode != 0:
        raise SystemExit(f"FAILED rc={r.returncode}")
    return out


def sha_csv(p):
    rows = sorted(open(p, newline="", encoding="utf-8").read().splitlines())
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()[:16], len(rows)


def main():
    env = dict(os.environ, PYTHONUTF8="1")
    env.pop("MRFX_CONFIG", None)
    if OLD.exists():
        shutil.rmtree(OLD)
    OLD.mkdir(parents=True)
    subprocess.run(f"git archive 1064203 | tar -x -C {OLD}", shell=True, cwd=REPO, check=True)
    py = OLD / ".venv" / "bin" / "python"
    sh([sys.executable, "-m", "venv", OLD / ".venv"], OLD)
    sh([py, "-m", "pip", "install", "-q", "-r", "requirements.txt"], OLD)
    sh([py, "-m", "pip", "install", "-q", "--no-build-isolation", "--no-deps", "-e", "."], OLD)
    # a user edit in the config: port + a comment line
    cfg = OLD / "config" / "mrfx.yaml"
    text = cfg.read_text(encoding="utf-8").replace("port: 8377", f"port: {PORT}")
    text += "\n# my note: edited by the user on purpose\nparallel_downloads: 1\n"
    cfg.write_text(text, encoding="utf-8")
    # data under 0.7.4
    inbox = OLD / "data" / "inbox"
    inbox.mkdir(parents=True)
    for f in SMALL:
        shutil.copy(REAL / f, inbox / f)
    sh([py, "-m", "mrfx", "ingest"], OLD, env=env)
    status_old = sh([py, "-m", "mrfx", "status"], OLD, env=env)
    sh([py, "-m", "mrfx", "export", str(A / "export_074.csv")], OLD, env=env)
    cfg_before = cfg.read_bytes()
    # THE UPDATE
    t0 = time.monotonic()
    out = sh([py, "-m", "mrfx", "update", ZIP], OLD, env=env)
    print(f"update took {time.monotonic()-t0:.1f}s")
    ver = (OLD / "mrfx" / "__init__.py").read_text().strip().splitlines()
    print("installed version line:", [l for l in ver if "__version__" in l])
    print("config unchanged:", cfg.read_bytes() == cfg_before, "| .new present:", (OLD / "config" / "mrfx.yaml.new").exists())
    print("backups:", sorted(p.name for p in (OLD / "data" / "update_backups").glob("*")))
    print("leftover .updating files:", list(OLD.rglob("*.updating")))
    # status + export under 0.7.5
    status_new = sh([py, "-m", "mrfx", "status"], OLD, env=env)
    sh([py, "-m", "mrfx", "export", str(A / "export_075.csv")], OLD, env=env)
    print("export 0.7.4:", sha_csv(A / "export_074.csv"), " export 0.7.5:", sha_csv(A / "export_075.csv"))
    # serve on the updated install
    log = open(A / "serve_after_update.log", "w")
    p = subprocess.Popen([str(py), "-m", "mrfx", "serve"], cwd=OLD, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        ok = False
        for _ in range(90):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=2).read()
                ok = True
                break
            except Exception:
                time.sleep(1)
        print("server answered:", ok)
        for path in ["/api/status", "/api/files", "/api/urls", "/", "/api/rates?limit=5"]:
            try:
                r = urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=30)
                body = r.read()
                print(f"GET {path} -> {r.status} {len(body)} bytes")
            except Exception as e:
                print(f"GET {path} -> {e}")
        time.sleep(8)
    finally:
        p.terminate()
        try:
            p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()
    txt = (A / "serve_after_update.log").read_text(errors="replace")
    print("\n--- serve log (first 60 lines) ---")
    print("\n".join(txt.splitlines()[:60]))
    bad = [l for l in txt.splitlines() if "Traceback" in l or "Error" in l]
    print("error-ish lines:", bad[:10])


if __name__ == "__main__":
    main()
