"""Determinism on the fresh real store: every output route of calls_real.py
(main + variant) called A, B without a progress token and C with one;
bodies compared byte for byte (raw) and after stripping only the documented
stamps. usage: det.py <config> <port> <outdir>"""
import json, re, subprocess, sys, time, urllib.error, urllib.request, uuid, hashlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from calls_real import calls  # noqa: E402

PY = "/home/user/Claudevideo/.venv/bin/python"
# the documented stamps: "Generated <date time> UTC by MRF Explorer vX" lines
# (tests/mrfx/test_determinism.py strips exactly "Generated YYYY-MM-DD HH:MM UTC")
STAMP = re.compile(rb"Generated:? \d{4}-\d\d-\d\d[ T]\d\d:\d\d(:\d\d)?( UTC)?")
GENLONG = re.compile(rb"[Gg]enerated [A-Z][a-z]+ \d{1,2}, \d{4}( at [0-9: apmAPM]+)?( UTC)?")

def norm(b):
    import io, zipfile
    if b[:4] == b"PK\x03\x04":
        try:
            z = zipfile.ZipFile(io.BytesIO(b))
            return b"\n".join(n.encode() + b"\n" + norm(z.read(n)) for n in sorted(z.namelist()))
        except Exception:
            return b
    return GENLONG.sub(b"Generated <D>", STAMP.sub(b"Generated <T>", b))

def call(base, method, url, body, token=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if data else {}
    if token:
        h["X-Mrfx-Progress"] = token
    req = urllib.request.Request(base + url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def main():
    cfg, port, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    base = f"http://localhost:{port}"
    proc = subprocess.Popen([PY, "-m", "mrfx", "--config", cfg, "serve"], stdout=open(out / "serve.log", "w"),
                            stderr=subprocess.STDOUT, cwd="/home/user/Claudevideo")
    rows = []
    try:
        for _ in range(600):
            try:
                urllib.request.urlopen(base + "/api/boot", timeout=3).read(); break
            except Exception:
                time.sleep(0.5)
        todo = []
        for name, view, method, url, body, var in calls():
            todo.append((name, method, url, body))
            if var:
                todo.append((name + "__variant", method, var[0], var[1] if var[1] is not None else body))
        for name, method, url, body in todo:
            if name == "packets_build":
                continue          # writes files; run once at the very end, not compared
            sa, a = call(base, method, url, body)
            sb, b = call(base, method, url, body)
            tok = "det" + uuid.uuid4().hex[:24]
            sc, c = call(base, method, url, body, token=tok)
            r = {"name": name, "status": [sa, sb, sc], "bytes": len(a),
                 "raw_AB": a == b, "raw_AC": a == c, "norm_AB": norm(a) == norm(b), "norm_AC": norm(a) == norm(c)}
            if not (r["norm_AB"] and r["norm_AC"]):
                for tag, x in (("A", a), ("B", b), ("C", c)):
                    (out / f"{name}.{tag}").write_bytes(x)
            rows.append(r)
            print(f"{name:40s} {sa} {sb} {sc} {len(a):>9} rawAB={r['raw_AB']} rawAC={r['raw_AC']} normAB={r['norm_AB']} normAC={r['norm_AC']}", flush=True)
        (out / "det.json").write_text(json.dumps(rows, indent=1))
    finally:
        proc.terminate(); proc.wait(60)

if __name__ == "__main__":
    main()
