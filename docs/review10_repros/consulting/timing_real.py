"""Time + determinism of every Consulting/Growth endpoint on the 32M real store copy.
usage: timing_real.py <label> [port]  -> ../timing_<label>.json ; bodies in ../bodies_<label>/"""
import hashlib, json, os, re, sys, time, urllib.request, urllib.error
OUT = "/tmp/mrfx_review10/consulting"
NAME = "REHABAUTHORITY, LLC"; TIN = "731631366"
PAY = "Blue Cross Blue Shield of North Dakota"
M = {"month": "latest", "state": "ND"}
MP = {"month": "latest", "state": "ND", "payers": [PAY]}
VOL = {"97110": 3000, "97140": 1500, "97530": 800}

def calls():
    c = []
    g = lambda label, url: c.append((label, "GET", url, None))
    p = lambda label, url, body: c.append((label, "POST", url, body))
    for subj, sl in ((NAME, "name"), (TIN, "tin")):
        S = {"subject": subj, "market": M}
        p(f"benchmark ND [{sl}]", "/api/benchmark/market", S)
        p(f"benchmark national [{sl}]", "/api/benchmark/market", {"subject": subj, "market": {"month": "latest"}})
        p(f"opportunity [{sl}]", "/api/benchmark/opportunity", {**S, "volumes": VOL})
        p(f"pitch report [{sl}]", "/api/report/pitch", {**S, "volumes": VOL})
        p(f"negotiation one-pager [{sl}]", "/api/report/negotiation", {"subject": subj, "market": M, "volumes": VOL})
        p(f"negotiate compare [{sl}]", "/api/negotiate/compare", {**S, "payer": PAY})
        p(f"payer-compare report [{sl}]", "/api/report/payer-compare", {**S, "payer": PAY})
        p(f"proposal [{sl}]", "/api/negotiate/proposal", {**S, "payer": PAY, "volumes": VOL, "target": {"kind": "p50"}})
        p(f"proposal report [{sl}]", "/api/report/proposal", {**S, "payer": PAY, "volumes": VOL, "target": {"kind": "p50"}})
        p(f"fee schedule [{sl}]", "/api/schedule/fee", S)
        p(f"fee.csv [{sl}]", "/api/schedule/fee.csv", S)
        p(f"rate card [{sl}]", "/api/report/ratecard", S)
        p(f"contract gaps [{sl}]", "/api/contract-gaps", S)
        p(f"visit economics [{sl}]", "/api/economics/visit", S)
        p(f"weighted position [{sl}]", "/api/economics/weighted-position", {**S, "volumes": VOL})
        p(f"size premium [{sl}]", "/api/quality/size", S)
        p(f"subject-types [{sl}]", "/api/quality/subject-types", S)
        p(f"leverage [{sl}]", "/api/leverage", {**S, "payer": PAY})
        p(f"leverage summary [{sl}]", "/api/leverage/summary", S)
        p(f"dossier [{sl}]", "/api/report/dossier", S)
        p(f"dossier.json [{sl}]", "/api/report/dossier.json", S)
        p(f"org bundle zip [{sl}]", "/api/report/org-bundle.zip", S)
        p(f"medicare org [{sl}]", "/api/medicare/org", {"subject": subj})
        p(f"hospital parity [{sl}]", "/api/hospital/parity", {**S, "payer": PAY, "state": "ND"})
        p(f"floors compare [{sl}]", "/api/floors/compare", {**S, "state": "ND", "payer": PAY})
        p(f"changes subject [{sl}]", "/api/changes", S)
        p(f"visit price [{sl}]", "/api/schedule/visit", {**S, "preset": "ortho_standard"})
    p("leads ND", "/api/leads", {"market": M})
    p("leads national", "/api/leads", {"market": {"month": "latest"}})
    p("leads.csv ND", "/api/leads.csv", {"market": M})
    p("leaderboard ND", "/api/leaderboard", {"market": M})
    p("leaderboard national", "/api/leaderboard", {"market": {"month": "latest"}})
    p("roster ND", "/api/roster", {"market": M, "payer": PAY})
    p("roster national", "/api/roster", {"market": {"month": "latest"}, "payer": PAY})
    p("roster.csv ND", "/api/roster.csv", {"market": M, "payer": PAY})
    p("posture ND", "/api/quality/posture", {"market": M})
    p("posture national", "/api/quality/posture", {"market": {"month": "latest"}})
    p("types ND", "/api/quality/types", {"market": M})
    p("size ND (no subject)", "/api/quality/size", {"market": M})
    p("size national", "/api/quality/size", {"market": {"month": "latest"}})
    g("freshness", "/api/quality/freshness")
    p("trajectory ND", "/api/trajectory", {"market": M})
    p("trajectory national", "/api/trajectory", {"market": {"month": "latest"}})
    g("inflation status", "/api/inflation/status")
    p("market report ND", "/api/report/market", {"market": M, "state": "ND"})
    p("renewals", "/api/contracts/renewals", {"market": M})
    p("renewals subject", "/api/contracts/renewals", {"market": M, "subjects": [NAME]})
    g("expiry coverage", "/api/contracts/expiry-coverage")
    p("new-to-network ND", "/api/contracts/new-to-network", {"market": M})
    p("market sizing 58103", "/api/market/sizing", {"zip": "58103", "radius_miles": 25})
    g("medicare status", "/api/medicare/status")
    p("medicare leaders", "/api/medicare/leaders", {"zip": "58103", "radius_miles": 25})
    g("packets", "/api/packets")
    g("clients", "/api/clients")
    g("clients digest", "/api/clients/digest")
    g("hospital status", "/api/hospital/status")
    g("floors status", "/api/floors/status")
    g("outreach.csv entity ND", "/api/export/outreach.csv?grain=entity&dollar_only=1&state=ND")
    g("outreach.zip entity ND", "/api/export/outreach.zip?grain=entity&dollar_only=1&state=ND")
    g("outreach.csv tin ND", "/api/export/outreach.csv?grain=tin&dollar_only=1&state=ND")
    g("outreach.zip tin ALL (button)", "/api/export/outreach.zip?grain=tin&dollar_only=1")
    return c

STAMP = re.compile(rb"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?(\.\d+)?( UTC|Z)?")

def one(port, meth, url, body, save=None, timeout=900):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://localhost:{port}{url}", data=data, method=meth,
                                 headers={"Content-Type": "application/json"} if data else {})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = r.read(); code = r.status
    except urllib.error.HTTPError as e:
        out = e.read(); code = e.code
    except Exception as e:
        return -1, time.perf_counter() - t0, 0, repr(e)[:80]
    el = time.perf_counter() - t0
    h = hashlib.sha1(STAMP.sub(b"<ts>", out)).hexdigest()[:12]
    if save:
        os.makedirs(os.path.dirname(save), exist_ok=True)
        open(save, "wb").write(out)
    return code, el, len(out), h

if __name__ == "__main__":
    label = sys.argv[1]; port = int(sys.argv[2]) if len(sys.argv) > 2 else 8720
    only = sys.argv[3] if len(sys.argv) > 3 else None
    res = []
    for lab, meth, url, body in calls():
        if only and not any(o in lab for o in only.split(",")):
            continue
        fn = re.sub(r"[^a-z0-9]+", "_", lab.lower())
        a = one(port, meth, url, body, save=f"{OUT}/bodies_{label}/{fn}.1")
        b = one(port, meth, url, body, save=f"{OUT}/bodies_{label}/{fn}.2")
        load = open("/proc/loadavg").read().split()[0]
        same = "same" if a[3] == b[3] else "DIFF"
        res.append({"label": lab, "code": a[0], "first": round(a[1], 2), "second": round(b[1], 2),
                    "bytes": a[2], "load": load, "h1": a[3], "h2": b[3]})
        print(f"{lab:36s} {a[0]} first {a[1]:7.2f}s second {b[1]:7.2f}s bytes {a[2]:>9} load {load} {same}", flush=True)
        json.dump(res, open(f"{OUT}/timing_{label}.json", "w"), indent=1)
