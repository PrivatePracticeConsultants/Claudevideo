"""Fetch every search-path URL twice from a running server; save the response bodies (for a
byte-for-byte A/B) and the timings. usage: bodies.py <port> <label> <cfg.json>"""
import hashlib
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path("/tmp/mrfx_review10/search/out")


def load():
    return subprocess.run(["uptime"], capture_output=True, text=True).stdout.split("load average:")[-1].strip()


def urls(c):
    q = urllib.parse.quote
    name, part, tin, npi, st, cpt, payer = c["name"], c["partial"], c["tin"], c["npi"], c["state"], c["cpt"], c["payer"]
    base = "grain=tin&dollar_only=1"
    filt = {
        "default": base,
        "q full name": f"{base}&q={q(name)}",
        "q partial name": f"{base}&q={q(part)}",
        "q one letter": f"{base}&q={q(part[0])}",
        "q two letters": f"{base}&q={q(part[:2])}",
        "q 9-digit TIN": f"{base}&q={tin}",
        "q 10-digit NPI": f"{base}&q={npi}",
        "q partial digits": f"{base}&q={tin[:5]}",
        "state": f"{base}&state={st}",
        "state + partial name": f"{base}&state={st}&q={q(part)}",
        "cpt": f"{base}&cpt={cpt}",
        "cpt + partial name": f"{base}&cpt={cpt}&q={q(part)}",
        "payer + partial name": f"{base}&payer={q(payer)}&q={q(part)}",
        "latest + full name": f"{base}&latest=1&q={q(name)}",
        "therapy only + partial name": f"{base}&therapy_only=1&q={q(part)}",
        "hide outliers + full name": f"{base}&hide_outliers=1&q={q(name)}",
        "dollar off + full name": f"grain=tin&dollar_only=0&q={q(name)}",
        "city + state": f"{base}&state={st}&city={q(c.get('city', 'CITY 1'))}",
        "NPI grain, NPI": f"grain=npi&dollar_only=1&q={npi}",
        "NPI grain, TIN": f"grain=npi&dollar_only=1&q={tin}",
        "entity grain payer+cpt+name": f"grain=entity&dollar_only=1&payer={q(payer)}&cpt={cpt}&q={q(part)}",
        "entity grain payer+cpt+state": f"grain=entity&dollar_only=1&payer={q(payer)}&cpt={cpt}&state={st}",
    }
    out = []
    for k, f in filt.items():
        out.append((f"rates | {k}", f"/api/rates?{f}&sort=negotiated_rate&dir=desc&page=1&page_size=100"))
        out.append((f"summary | {k}", f"/api/summary?{f}"))
    out.append(("rates | q full name, page 2", f"/api/rates?{base}&q={q(name)}&sort=negotiated_rate&dir=desc&page=2&page_size=100"))
    out.append(("rates | q full name, sort name", f"/api/rates?{base}&q={q(name)}&sort=display_name&dir=asc&page=1&page_size=100"))
    out.append(("rates | q partial, sort payer asc", f"/api/rates?{base}&q={q(part)}&sort=payer&dir=asc&page=1&page_size=250"))
    out.append(("rates | default sort name", f"/api/rates?{base}&sort=display_name&dir=asc&page=1&page_size=100"))
    out.append(("code page", f"/api/code/{cpt}?grain=tin"))
    out.append(("code page + state", f"/api/code/{cpt}?grain=tin&state={st}"))
    out.append(("trend", f"/api/trend?cpt={cpt}&grain=tin"))
    out.append(("drawer tin", f"/api/entity/tin/{tin}"))
    out.append(("drawer npi", f"/api/entity/npi/{npi}"))
    out.append(("subjects ''", "/api/benchmark/subjects"))
    out.append(("subjects partial", f"/api/benchmark/subjects?q={q(part)}"))
    out.append(("subjects TIN", f"/api/benchmark/subjects?q={tin}"))
    out.append(("export.csv full name", f"/api/export.csv?{base}&q={q(name)}&sort=negotiated_rate&dir=desc&view=explorer"))
    return out


def main():
    port, label, cfgp = sys.argv[1:4]
    c = json.loads(Path(cfgp).read_text())
    d = OUT / f"bodies_{label}"
    d.mkdir(exist_ok=True)
    rows = []
    for i, (title, u) in enumerate(urls(c)):
        ts, status, body = [], None, b""
        for _ in range(2):
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(f"http://localhost:{port}{u}", timeout=600) as r:
                    body, status = r.read(), r.status
            except urllib.error.HTTPError as e:
                body, status = e.read(), e.code
            ts.append(round(time.perf_counter() - t0, 3))
        (d / f"{i:02d}.body").write_bytes(body)
        rows.append({"i": i, "title": title, "url": u, "status": status, "bytes": len(body), "first_s": ts[0], "second_s": ts[1],
                     "sha": hashlib.sha256(body).hexdigest()[:16], "load": load()})
        print(f"{i:02d} {title:38s} {status} {len(body):>8}b first {ts[0]:7.3f}s second {ts[1]:7.3f}s  sha {rows[-1]['sha']}  load {rows[-1]['load']}", flush=True)
    (OUT / f"bodies_{label}.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
