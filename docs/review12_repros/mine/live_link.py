"""The user's main path, live: start the shipped 0.7.5 on an empty store,
paste a real payer link (UHC, 2 MB) on the Files tab's route, watch the queue
to done, compare the row count with the same file loaded through the inbox
under 0.7.4 (252 rows). Then paste the SAME link again (expect: nothing
re-downloaded) and drop the same bytes in the inbox (expect: skipped)."""
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

A = Path("/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit12")
SRC = A / "unzipped" / "Claudevideo"
DST = A / "live_install"
REAL = A.parent / "real"
PORT = 8475
NAME = "2026-11-01_HEALTHSCOPE-BENEFITS_Third-Party-Administrator_SHO_U1_3342_in-network-rates.json.gz"
URL = "https://transparency-in-coverage.uhc.com/api/v1/uhc/blobs/download/2026-11-01/" + NAME


def get(path):
    r = urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=60)
    return json.loads(r.read())


def post(path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def queue():
    return [(u["id"], u["status"], u.get("kind"), u.get("bytes_done"), u.get("bytes_total"), (u.get("error") or "")[:90])
            for u in get("/api/urls")["urls"]]


def wait_settled(secs):
    t = time.monotonic() + secs
    while time.monotonic() < t:
        q = queue()
        if q and all(s in ("done", "failed", "skipped", "oversize", "paused") for _, s, *_ in q):
            return q
        time.sleep(2)
    return queue()


def main():
    if DST.exists():
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(".venv", "data", "tests", "docs"))
    cfg = DST / "config" / "mrfx.yaml"
    text = cfg.read_text(encoding="utf-8").replace("port: 8377", f"port: {PORT}")
    cfg.write_text(text + "\nenrichment:\n  mode: off\n", encoding="utf-8")   # no 1.1 GB NPPES pull here
    py = SRC / ".venv" / "bin" / "python"
    env = dict(os.environ, PYTHONUTF8="1")
    env.pop("MRFX_CONFIG", None)
    log = open(A / "live_link.log", "w")
    p = subprocess.Popen([str(py), "-m", "mrfx", "serve"], cwd=DST, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                get("/api/files")
                break
            except Exception:
                time.sleep(0.5)
        t0 = time.monotonic()
        print("add:", post("/api/urls", {"urls": [URL]}))
        q = wait_settled(600)
        print(f"after {time.monotonic()-t0:.0f}s:", q)
        files = get("/api/files")
        print("files:", [(f.get("filename"), f.get("status"), f.get("rows_emitted")) for f in files.get("files", files if isinstance(files, list) else [])])
        # paste the same link again
        print("add again:", post("/api/urls", {"urls": [URL]}))
        time.sleep(15)
        print("queue after re-add:", queue())
        # same bytes in the inbox
        shutil.copy(REAL / ("uhc_" + NAME), DST / "data" / "inbox" / ("uhc_" + NAME))
        time.sleep(40)
        files = get("/api/files")
        print("files after inbox copy:", [(f.get("filename"), f.get("status"), f.get("rows_emitted"), (f.get("error") or "")[:80])
                                        for f in files.get("files", files if isinstance(files, list) else [])])
        print("downloads dir:", sorted(x.name for x in (DST / "data" / "downloads").glob("*")) if (DST / "data" / "downloads").exists() else "none")
    finally:
        p.terminate()
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()
    txt = (A / "live_link.log").read_text(errors="replace")
    print("--- log lines of interest ---")
    for l in txt.splitlines():
        if any(k in l for k in ("fetch", "ingest", "Traceback", "Error", "WARN")):
            print(l[:220])


if __name__ == "__main__":
    main()
