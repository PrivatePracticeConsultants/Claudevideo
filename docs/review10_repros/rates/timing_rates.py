"""Time every Rates/Practice endpoint on the 32M copy. usage: timing_rates.py <label> [port]
Three passes (call 1 = cold for this server process, 2, 3), body sha per pass -> determinism too."""
import hashlib, json, os, sys, time, urllib.request, urllib.error, urllib.parse

OUT = "/tmp/mrfx_review10/rates"
ST = "AL"
PAY = "Blue Cross and Blue Shield of Alabama"
M = {"month": "latest", "state": ST}
q = urllib.parse.quote

def calls():
    c = []
    g = lambda label, url: c.append((label, "GET", url, None))
    p = lambda label, url, body: c.append((label, "POST", url, body))
    g("stats", "/api/stats"); g("months", "/api/months"); g("payers", "/api/payers"); g("states", "/api/states")
    g("overview", "/api/overview"); g("overview AL", "/api/overview?state=AL")
    g("clients", "/api/clients"); g("clients digest", "/api/clients/digest")
    R = "/api/rates?dollar_only=1&sort=negotiated_rate&dir=desc&page=1&page_size=100"
    S = "/api/summary?dollar_only=1"
    for lab, extra in (("tin pooled", "&grain=tin"), ("tin latest", "&grain=tin&latest=1"),
                       ("tin 2026-10", "&grain=tin&month=2026-10"),
                       ("tin latest AL", "&grain=tin&latest=1&state=AL"),
                       ("tin latest city", "&grain=tin&latest=1&city=BIRMINGHAM"),
                       ("tin latest therapy", "&grain=tin&latest=1&therapy_only=1"),
                       ("tin latest outliers", "&grain=tin&latest=1&hide_outliers=1"),
                       ("tin latest q=therapy", "&grain=tin&latest=1&q=therapy"),
                       ("tin latest 97110", "&grain=tin&latest=1&cpt=97110"),
                       ("tin latest payer", "&grain=tin&latest=1&payer=" + q(PAY)),
                       ("tin latest base prof", "&grain=tin&latest=1&modifier=base&billing_class=professional"),
                       ("entity latest", "&grain=entity&latest=1"),
                       ("entity latest AL", "&grain=entity&latest=1&state=AL"),
                       ("entity latest payer+code", "&grain=entity&latest=1&cpt=97110&payer=" + q(PAY)),
                       ("npi latest payer+code", "&grain=npi&latest=1&cpt=97110&payer=" + q(PAY)),
                       ("npi latest", "&grain=npi&latest=1"),
                       ("npi latest AL 97110", "&grain=npi&latest=1&state=AL&cpt=97110")):
        g("rates " + lab, R + extra); g("summary " + lab, S + extra)
    g("rates tin latest page 500", R.replace("page=1&", "page=500&") + "&grain=tin&latest=1")
    g("rates tin latest sort name", "/api/rates?dollar_only=1&sort=display_name&dir=asc&page=1&page_size=100&grain=tin&latest=1")
    g("rates tin latest sort npi_count", "/api/rates?dollar_only=1&sort=npi_count&dir=desc&page=1&page_size=100&grain=tin&latest=1")
    for code in ("97110", "97530", "92507"):
        g(f"code {code}", f"/api/code/{code}?grain=tin"); g(f"trend {code}", f"/api/trend?cpt={code}&grain=tin")
    g("code 97110 base AL", "/api/code/97110?grain=tin&modifier=base&state=AL")
    g("trend 97110 base AL", "/api/trend?cpt=97110&grain=tin&modifier=base&state=AL")
    for ep in ("geography", "negotiability", "medicare", "differential"):
        p(f"market {ep} 97110 AL", f"/api/market/{ep}", {"code": "97110", "market": M})
        p(f"market {ep} 97110 national", f"/api/market/{ep}", {"code": "97110", "market": {"month": "latest"}})
    p("territory local AL", "/api/territory/local", {"market": M, "code": "97110", "state": "AL"})
    p("territory radius 35203/25", "/api/territory/radius", {"market": M, "code": "97110", "zip": "35203", "radius_miles": 25})
    p("territory concentration AL", "/api/territory/concentration", {"market": M, "code": "97110"})
    p("territory concentration national", "/api/territory/concentration", {"market": {"month": "latest"}, "code": "97110"})
    p("report market AL", "/api/report/market", {"market": M, "state": "AL"})
    p("changes national", "/api/changes", {"market": {"month": "latest"}})
    p("changes AL", "/api/changes", {"market": M})
    p("changes.csv national", "/api/changes.csv", {"market": {"month": "latest"}})
    p("trajectory national", "/api/trajectory", {"market": {"month": "latest"}})
    p("trajectory AL", "/api/trajectory", {"market": M})
    g("known-sources", "/api/known-sources")
    g("export.csv AL 97110 latest", "/api/export.csv?grain=tin&latest=1&dollar_only=1&state=AL&cpt=97110")
    g("export.zip AL 97110 latest", "/api/export.zip?grain=tin&latest=1&dollar_only=1&state=AL&cpt=97110")
    g("export.csv tin 2026-10 97110", "/api/export.csv?grain=tin&month=2026-10&dollar_only=1&cpt=97110")
    return c

def one(base, method, url, body):
    t = time.time()
    try:
        req = urllib.request.Request(base + url, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            b = r.read(); st = r.status
    except urllib.error.HTTPError as e:
        b = e.read(); st = e.code
    return time.time() - t, st, b

def main():
    label = sys.argv[1]; port = int(sys.argv[2]) if len(sys.argv) > 2 else 8710
    base = f"http://localhost:{port}"
    res = []
    print("load", os.getloadavg())
    for lab, m, u, b in calls():
        ts, shas, st = [], [], None
        for _ in range(3):
            t, st, body = one(base, m, u, b)
            ts.append(round(t, 3)); shas.append(hashlib.sha256(body).hexdigest()[:10])
            if len(set(shas)) > 1:
                os.makedirs(f"{OUT}/diff_bodies", exist_ok=True)
                open(f"{OUT}/diff_bodies/{label}_{lab.replace(' ', '_').replace('/', '_')}_{len(shas)}.bin", "wb").write(body)
            if len(shas) == 1: first = body
        if len(set(shas)) > 1:
            open(f"{OUT}/diff_bodies/{label}_{lab.replace(' ', '_').replace('/', '_')}_1.bin", "wb").write(first)
        res.append({"label": lab, "url": u, "status": st, "t": ts, "same": len(set(shas)) == 1, "sha": shas[0], "bytes": len(body)})
        flag = "" if len(set(shas)) == 1 else "  <-- BODY DIFFERS"
        print(f"{lab:42s} {st} {ts[0]:7.2f} {ts[1]:7.2f} {ts[2]:7.2f} {len(body):9d}{flag}", flush=True)
    print("load", os.getloadavg())
    json.dump({"load": os.getloadavg(), "res": res}, open(f"{OUT}/timing_{label}.json", "w"), indent=1)

if __name__ == "__main__":
    main()
