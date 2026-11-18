"""Start `mrfx serve` on the 99M store (Popen), time every output feature
cold / warm / variant through HTTP, walk the dashboard in Chromium, stop the
server. Everything is written under OUT. usage: driver.py [--skip-ui] [--only substr]"""
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE
PY = "/home/user/Claudevideo/.venv/bin/python"
CFG = "/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/ui7/scale.yaml"
PORT = 8392
BASE = f"http://localhost:{PORT}"
sys.path.insert(0, str(HERE))
from calls import calls, TIN  # noqa: E402

VOLATILE = [
    (re.compile(r"\b20\d\d-\d\d-\d\d[T ]\d\d:\d\d(:\d\d(\.\d+)?)?([+-]\d\d:\d\d|Z)?\b"), "<TS>"),
    (re.compile(r"Generated:? [0-9T:\-+. ]+"), "Generated <T>"),
    (re.compile(r"generated [A-Z][a-z]+ \d{1,2}, \d{4}[^<\"]*"), "generated <D>"),
    (re.compile(r"\"(generated(_at|_utc)?|generation|data_generation|now|as_of_time|elapsed|took_s|computed_at|server_time|cache_age_s|checked_at|built_at|uptime_s|cache_hit)\": ?[^,}]*"), "\"<vol>\": 0"),
]


def norm(body: bytes, ct: str) -> bytes:
    if body[:4] == b"PK\x03\x04":
        import io
        import zipfile
        try:
            z = zipfile.ZipFile(io.BytesIO(body))
            parts = []
            for n in sorted(z.namelist()):
                parts.append(n.encode() + b"\n" + norm(z.read(n), "text"))
            return b"\n".join(parts)
        except Exception:
            return body
    try:
        s = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    for rx, rep in VOLATILE:
        s = rx.sub(rep, s)
    return s.encode()


def call(method, url, body, timeout=900):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read()
            code = r.status
            ct = r.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        b = e.read()
        code = e.code
        ct = e.headers.get("content-type", "")
    except Exception as e:
        return {"status": -1, "ms": (time.perf_counter() - t0) * 1000, "bytes": 0, "sha": "", "err": repr(e)[:120], "body": b""}
    ms = (time.perf_counter() - t0) * 1000
    return {"status": code, "ms": ms, "bytes": len(b), "sha": hashlib.sha1(norm(b, ct)).hexdigest()[:12], "body": b, "ct": ct}


def wait_boot(proc, logf, limit=1800):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < limit:
        if proc.poll() is not None:
            raise SystemExit(f"server exited {proc.returncode}; see {logf}")
        try:
            with urllib.request.urlopen(BASE + "/api/boot", timeout=5) as r:
                r.read()
            return time.perf_counter() - t0
        except Exception:
            time.sleep(0.5)
    raise SystemExit("server never answered /api/boot")


def main():
    skip_ui = "--skip-ui" in sys.argv
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    bodies = OUT / "bodies"
    bodies.mkdir(exist_ok=True)
    logf = OUT / "serve.log"
    # best-effort page-cache drop so "cold" means cold on disk too
    try:
        subprocess.run(["sync"], check=False)
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")
        dropped = True
    except Exception as e:
        dropped = f"not dropped: {e!r}"
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    t_start = time.perf_counter()
    proc = subprocess.Popen([PY, "-m", "mrfx", "--config", CFG, "serve"], stdout=open(logf, "w"),
                            stderr=subprocess.STDOUT, env=env, cwd="/home/user/Claudevideo")
    try:
        boot_s = wait_boot(proc, logf)
        meta = {"page_cache_dropped": dropped, "boot_s": round(boot_s, 2), "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "head": subprocess.run(["git", "-C", "/home/user/Claudevideo", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()}
        (OUT / "run_meta.json").write_text(json.dumps(meta, indent=1))
        print("server up in", round(boot_s, 2), "s; cache drop:", dropped, flush=True)
        rows = []
        for name, view, method, url, body, var in calls():
            if only and only not in name:
                continue
            a = call(method, url, body)
            b = call(method, url, body)
            v = call(method, var[0], var[1] if method == "POST" else None) if var else None
            if a["status"] >= 0 and len(a["body"]) < 5_000_000:
                ext = "json" if "json" in a.get("ct", "") else ("html" if "html" in a.get("ct", "") else "bin")
                (bodies / f"{name}.{ext}").write_bytes(a["body"])
                if a["sha"] != b["sha"]:
                    (bodies / f"{name}.warm.{ext}").write_bytes(b["body"])
            load = open("/proc/loadavg").read().split()[0]
            row = {"name": name, "view": view, "method": method, "url": url,
                   "payload": json.dumps(body, sort_keys=True) if body is not None else "",
                   "status": a["status"], "cold_ms": round(a["ms"]), "warm_ms": round(b["ms"]),
                   "warm_status": b["status"], "bytes": a["bytes"], "same_bytes": a["sha"] == b["sha"],
                   "variant_url": var[0] if var else "", "variant_payload": json.dumps(var[1], sort_keys=True) if var and var[1] is not None else "",
                   "variant_ms": round(v["ms"]) if v else "", "variant_status": v["status"] if v else "",
                   "variant_bytes": v["bytes"] if v else "", "load": load, "err": a.get("err", "")}
            rows.append(row)
            print(f"{name:34s} {a['status']} cold {a['ms']:9.0f} warm {b['ms']:9.0f} var {row['variant_ms']!s:>7} bytes {a['bytes']:>9} same={row['same_bytes']} load {load}", flush=True)
            with open(OUT / "timings.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
        if not skip_ui:
            print("--- Chromium walk", flush=True)
            r = subprocess.run([PY, str(HERE / "ui_walk.py"), BASE, str(OUT / "ui"), TIN, "MO"],
                               capture_output=True, text=True, timeout=3600)
            (OUT / "ui_walk.out").write_text(r.stdout + "\n--- stderr\n" + r.stderr)
            print(r.stdout[-2000:], r.stderr[-2000:], flush=True)
        # the debug stacks endpoint and a final stats call to record memory
        try:
            rss = open(f"/proc/{proc.pid}/status").read()
            vm = [ln for ln in rss.splitlines() if ln.startswith(("VmRSS", "VmHWM"))]
            meta["server_mem"] = vm
            (OUT / "run_meta.json").write_text(json.dumps(meta, indent=1))
        except Exception:
            pass
    finally:
        proc.terminate()
        try:
            proc.wait(60)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("server stopped; total", round(time.perf_counter() - t_start), "s", flush=True)


if __name__ == "__main__":
    main()
