"""Time every output endpoint the dashboard calls on the 99M scratch store.
usage: timing.py <label> [port]   -> writes timing_<label>.json
First call and second call per endpoint, sequential, one request at a time."""
import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

OUT = "/tmp/mrfx_review9/outputs"
NAME = "PRACTICE 0"          # an NPPES name shared by TINs in several states
TIN = "100000000"            # its MO TIN
M = {"month": "latest", "state": "MO"}
MP = {"month": "latest", "state": "MO", "payers": ["Payer 00"]}
VOL = {"97110": 3000, "97140": 1500, "97530": 800}


def calls():
    c = []
    g = lambda label, url: c.append((label, "GET", url, None))
    p = lambda label, url, body: c.append((label, "POST", url, body))
    g("stats", "/api/stats")
    g("overview", "/api/overview")
    g("overview MO", "/api/overview?state=MO")
    g("rates default (tin, latest)", "/api/rates?grain=tin&dollar_only=1&sort=negotiated_rate&dir=desc&page=1&page_size=100")
    g("rates tin 2026-10", "/api/rates?grain=tin&month=2026-10&dollar_only=1&page=1&page_size=100")
    g("rates entity MO", "/api/rates?grain=entity&dollar_only=1&state=MO&payer=Payer%2000&cpt=97110&page=1&page_size=100")
    g("summary latest", "/api/summary?grain=tin&dollar_only=1")
    g("summary MO", "/api/summary?grain=tin&dollar_only=1&state=MO")
    g("code page 97110", "/api/code/97110?grain=tin&dollar_only=1")
    g("trend 97110", "/api/trend?cpt=97110&dollar_only=1")
    g("entity view tin", "/api/entity/tin/" + TIN + "?dollar_only=1")
    g("files", "/api/files?limit=500")
    g("urls", "/api/urls")
    g("clients digest", "/api/clients/digest")
    g("freshness", "/api/quality/freshness")
    g("benchmark subjects", "/api/benchmark/subjects?q=practice%2012")
    for subj, sl in ((TIN, "tin"), (NAME, "name")):
        p(f"benchmark MO [{sl}]", "/api/benchmark/market", {"subject": subj, "market": M})
        p(f"benchmark national [{sl}]", "/api/benchmark/market", {"subject": subj, "market": {"month": "latest"}})
        p(f"opportunity MO [{sl}]", "/api/benchmark/opportunity", {"subject": subj, "market": M, "volumes": VOL})
        p(f"pitch report MO [{sl}]", "/api/report/pitch", {"subject": subj, "market": M, "volumes": VOL})
        p(f"negotiation one-pager MO Payer00 [{sl}]", "/api/report/negotiation",
          {"subject": subj, "market": MP, "volumes": VOL})
        p(f"negotiate compare MO Payer00 [{sl}]", "/api/negotiate/compare",
          {"subject": subj, "payer": "Payer 00", "market": M, "comparables": ["PRACTICE 1", "PRACTICE 2"]})
        p(f"payer-compare report [{sl}]", "/api/report/payer-compare",
          {"subject": subj, "payer": "Payer 00", "market": M, "comparables": ["PRACTICE 1", "PRACTICE 2"]})
        p(f"proposal [{sl}]", "/api/negotiate/proposal",
          {"subject": subj, "payer": "Payer 00", "market": M, "volumes": VOL, "target": {"kind": "p50"}})
        p(f"rate card MO [{sl}]", "/api/report/ratecard", {"subject": subj, "market": M})
        p(f"fee schedule MO [{sl}]", "/api/schedule/fee", {"subject": subj, "market": M})
        p(f"contract gaps MO [{sl}]", "/api/contract-gaps", {"subject": subj, "market": M})
        p(f"visit economics [{sl}]", "/api/economics/visit", {"subject": subj, "market": M})
        p(f"size premium MO [{sl}]", "/api/quality/size", {"subject": subj, "market": M})
        p(f"changes subject [{sl}]", "/api/changes", {"subject": subj, "market": {"month": "latest", "state": "MO"}})
        p(f"dossier [{sl}]", "/api/report/dossier", {"subject": subj, "market": M})
    p("pitch report national [tin]", "/api/report/pitch", {"subject": TIN, "market": {"month": "latest", "allow_national": True}})
    p("trajectory MO", "/api/trajectory", {"market": M})
    p("trajectory national", "/api/trajectory", {"market": {"month": "latest"}})
    p("changes MO", "/api/changes", {"market": {"month": "latest", "state": "MO"}})
    p("changes national", "/api/changes", {"market": {"month": "latest"}})
    p("leads MO", "/api/leads", {"market": M})
    p("leads national", "/api/leads", {"market": {"month": "latest"}})
    p("leaderboard MO", "/api/leaderboard", {"market": M})
    p("roster MO Payer00", "/api/roster", {"market": M, "payer": "Payer 00"})
    p("posture MO", "/api/quality/posture", {"market": M})
    p("types MO", "/api/quality/types", {"market": M})
    for ep in ("geography", "negotiability", "medicare", "differential"):
        p(f"market {ep} 97110 MO", f"/api/market/{ep}", {"code": "97110", "market": M})
    p("market report MO", "/api/report/market", {"market": M, "state": "MO"})
    p("renewals", "/api/contracts/renewals", {"market": M})
    g("export.csv MO 97110", "/api/export.csv?grain=tin&month=2026-10&dollar_only=1&state=MO&cpt=97110")
    g("export.zip MO 97110", "/api/export.zip?grain=tin&month=2026-10&dollar_only=1&state=MO&cpt=97110")
    # the outreach button sends filterQuery(): grain + dollar_only (+ whatever filters are set)
    g("outreach.zip entity MO (button)", "/api/export/outreach.zip?grain=entity&dollar_only=1&state=MO")
    g("outreach.csv entity MO", "/api/export/outreach.csv?grain=entity&dollar_only=1&state=MO")
    g("outreach.zip tin MO", "/api/export/outreach.zip?grain=tin&dollar_only=1&state=MO")
    g("outreach.csv tin MO", "/api/export/outreach.csv?grain=tin&dollar_only=1&state=MO")
    g("outreach.zip entity MO 2026-10", "/api/export/outreach.zip?grain=entity&dollar_only=1&state=MO&month=2026-10")
    g("outreach.zip tin ALL (button, no filter)", "/api/export/outreach.zip?grain=tin&dollar_only=1")
    g("outreach.zip entity ALL (button, no filter)", "/api/export/outreach.zip?grain=entity&dollar_only=1")
    return c


SAVE = None
body_json = None


def body_in(b):
    return b


def one(port, meth, url, body, timeout=900):
    global body_json
    body_json = body
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://localhost:{port}{url}", data=data, method=meth,
                                 headers={"Content-Type": "application/json"} if data else {})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        body = e.read()
        code = e.code
    except Exception as e:  # timeout etc.
        return -1, time.perf_counter() - t0, repr(e)[:80], ""
    el = time.perf_counter() - t0
    h = hashlib.sha1(body).hexdigest()[:12]
    if SAVE and len(body) < 3_000_000:
        os.makedirs(SAVE, exist_ok=True)
        open(os.path.join(SAVE, hashlib.sha1((meth + url + json.dumps(body_in(body_json))).encode()).hexdigest()[:10] + "_" + h), "wb").write(body)
    return code, el, len(body), h


if __name__ == "__main__":
    label = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8392
    only = sys.argv[3] if len(sys.argv) > 3 else None
    res = []
    globals()['SAVE'] = f"{OUT}/bodies_{label}"
    for lab, meth, url, body in calls():
        if only and not any(o in lab for o in only.split(",")):
            continue
        a = one(port, meth, url, body)
        b = one(port, meth, url, body)
        load = open("/proc/loadavg").read().split()[0]
        res.append({"label": lab, "code": a[0], "first": round(a[1], 2), "second": round(b[1], 2),
                    "bytes": a[2], "load": load, "h1": a[3], "h2": b[3], "url": url, "body": body})
        print(f"{lab:48s} {a[0]} first {a[1]:7.2f}s second {b[1]:7.2f}s bytes {a[2]} load {load}", flush=True)
        json.dump(res, open(f"{OUT}/timing_{label}{'_sub' if only else ''}.json", "w"), indent=1)
