"""API fuzz (in-process, messy fixture): every GET/POST/DELETE route of /openapi.json with wrong types,
huge values, empty strings, unicode and path tricks. Finding = 5xx, traceback / filesystem path in a body, > 30 s."""
from fxm import *  # noqa
import json, re, sys, time, warnings, itertools, urllib.parse
warnings.filterwarnings("ignore")

LEAK = re.compile(r"Traceback|site-packages|/home/user|/tmp/claude|File \"|\.py\"|duckdb\.duckdb|line \d+, in ")
SCAL = [None, 5, 10 ** 30, True, "a" * 3000, "\x00'; DROP TABLE rates;--📈", "../../etc/passwd", [], {"a": 1}, [None, 5, "x"]]
QVALS = ["", "-1", "99999999999999999999", "nan", "'; DROP TABLE x;--", "%00", "2026-13", "a" * 3000, "📈", "../../etc/passwd"]
QKEYS = ["grain", "payer", "state", "city", "cpt", "code", "month", "q", "rate_min", "rate_max", "page", "page_size", "sort", "dir",
         "limit", "offset", "subject", "view", "hide_outliers", "dollar_only", "therapy_only", "modifier", "mod_has", "mod_not",
         "billing_class", "pos", "discipline", "hide_tin_npi", "year", "kind", "name", "path", "locality", "cf", "vintage"]
MKEYS = ["month", "state", "city", "payers", "codes", "discipline", "therapy_only", "plan", "billing_class", "pos", "target_percentile",
         "curated_tins", "peer_set", "include_assistant", "include_non_dollar", "base_only", "prev_month", "allow_national", "_plan_files"]
SKIP = ("/api/debug/stacks",)


def base_body(path):
    b = {"subject": "ACME REHAB", "market": {"month": "latest", "state": "MO"}, "payer": "Alpha Health", "code": "97110",
         "state": "MO", "zip": "64105", "radius_miles": 25, "volumes": {"97110": 100}, "target": {"kind": "p50"},
         "comparables": ["B REHAB"], "subjects": ["ACME REHAB"], "text": "97110, 40.00", "limit": 10, "name": "x",
         "urls": ["https://example.invalid/a.json"], "year": "2024", "path": "/nonexistent/x.csv", "kind": "medicaid",
         "label": "L", "tins": ["610000001"], "lines": [{"code": "97110", "units": 2}], "values": {"2026": 330.0}}
    return b


if __name__ == "__main__":
    cfg, store = build()
    import mrfx.api as api
    from fastapi.testclient import TestClient
    app = api.create_app(cfg, store)
    c = TestClient(app, raise_server_exceptions=False)
    spec = c.get("/openapi.json").json()
    bad, slow, n = [], [], 0
    t_start = time.time()

    def rec(meth, path, what, r, dt):
        body = r.text[:6000] if r is not None else ""
        st = r.status_code if r is not None else "EXC"
        if st == "EXC" or st >= 500 or LEAK.search(body):
            bad.append((meth, path, what, st, body[:300].replace("\n", " ")))
        if dt > 30:
            slow.append((meth, path, what, round(dt, 1), st))

    def call(meth, path, what, **kw):
        global n
        t = time.time()
        try:
            r = c.request(meth, path, **kw)
        except Exception as e:  # noqa
            bad.append((meth, path, what, "EXC", repr(e)[:300]))
            return None
        n_dt = time.time() - t
        rec(meth, path, what, r, n_dt)
        return r

    allp = list(spec["paths"].items())
    start = [p for p, _ in allp].index("/api/clients")
    part, nparts = int(sys.argv[1]), int(sys.argv[2])
    todo = [x for i, x in enumerate(allp[start:]) if i % nparts == part]
    for path, ops in todo:
        print("PATH", path, "bad so far", len(bad), "secs", round(time.time() - t_start), flush=True)
        if path in SKIP or path in ("/api/packets/build", "/api/report/dossier", "/api/report/dossier.json", "/api/report/org-bundle.zip"):
            continue
        variants = [path.replace("{grain}", "tin").replace("{unit_id}", "610000001").replace("{code}", "97110")
                    .replace("{filename}", "nope.json").replace("{url_id}", "999999").replace("{name}", "nope")
                    .replace("{kind}", "medicaid").replace("{state}", "MO")]
        if "{" in path:
            for trick in ("..%2F..%2Fetc%2Fpasswd", "%00", "a" * 3000, "99999999999999999999"):
                variants.append(re.sub(r"\{[^}]+\}", trick, path))
        for meth in ops:
            M = meth.upper()
            if M == "GET":
                for v in variants:
                    n += 1; call("GET", v, "plain")
                    for k in QKEYS:
                        for q in QVALS:
                            n += 1; call("GET", v + "?" + urllib.parse.urlencode({k: q}), f"{k}={q[:30]}")
            elif M == "DELETE":
                for v in variants[1:] + [variants[0]]:
                    n += 1; call("DELETE", v, "delete")
            elif M == "POST":
                is_upload = "multipart" in json.dumps(ops[meth].get("requestBody", {}))
                v0 = variants[0]
                if is_upload:
                    for fname, content in (("x.json", b"{}"), ("../../evil.json", b"{"), ("a" * 300 + ".json", b"[]"), ("x.exe", b"MZ"),
                                           ("📈.json", b"\xff\xfe"), ("", b""), ("x.csv", b"a,b\n1,2\n")):
                        n += 1; call("POST", v0, f"upload {fname[:20]!r}", files={"file": (fname, content)})
                    continue
                for v in variants:
                    for raw in (b"", b"{", b"[]", b"null", b'{"market": 5}'):
                        n += 1; call("POST", v, f"raw {raw[:12]!r}", content=raw, headers={"Content-Type": "application/json"})
                bb = base_body(path)
                n += 1; call("POST", v0, "base", json=bb)
                for k in list(bb):
                    for s in SCAL:
                        b2 = dict(bb); b2[k] = s
                        n += 1; call("POST", v0, f"{k}={str(s)[:24]!r}", json=b2)
                for mk in MKEYS:
                    for s in SCAL:
                        b2 = dict(bb); b2["market"] = {**bb["market"], mk: s}
                        n += 1; call("POST", v0, f"market.{mk}={str(s)[:24]!r}", json=b2)
                for extra in ("threshold_percentile", "min_codes", "min_pct", "min_peers", "conservative_percentile", "sort", "units_per_visit",
                              "assume_inflation_pct", "within_days", "days", "multiplier", "from_year", "to_year", "preset", "basis", "id",
                              "expected_rate", "add_tins", "remove_tins", "url", "dataset_id", "min_patients", "max_miles", "min_practices",
                              "therapy_only", "index_name", "source", "mode", "notes", "key", "mrf_url", "paths"):
                    for s in (None, -1, 10 ** 30, "abc", []):
                        b2 = dict(bb); b2[extra] = s
                        n += 1; call("POST", v0, f"{extra}={str(s)[:24]!r}", json=b2)
    print("calls", n, "secs", round(time.time() - t_start), "bad", len(bad), "slow", len(slow))
    seen = {}
    for b in bad:
        sig = (b[0], b[1], b[3], b[4][:60])
        seen.setdefault(sig, []).append(b[2])
    for sig, whats in seen.items():
        print("BAD", sig[0], sig[1], sig[2], "| n", len(whats), "| e.g.", whats[:4], "|", sig[3])
    for s in slow:
        print("SLOW", s)
    json.dump({"bad": bad, "slow": slow, "calls": n}, open(f"fuzz_result_{sys.argv[1]}.json", "w"), default=str)
