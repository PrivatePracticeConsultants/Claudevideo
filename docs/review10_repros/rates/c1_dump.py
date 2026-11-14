"""Build the messy fixture (fxm) and dump every Rates/Practice endpoint, twice (determinism)."""
import json, sys, hashlib
sys.path.insert(0, "/tmp/mrfx_review10/rates/repros")
from fxm import *  # noqa

def J(r):
    try: return r.json()
    except Exception: return r.text

def main():
    cfg, store = build()
    c = client(cfg, store)
    out = {}
    def g(label, url):
        r = c.get(url); out[label] = (r.status_code, J(r))
    def p(label, url, body):
        r = c.post(url, json=body); out[label] = (r.status_code, J(r))
    M = {"month": "latest", "state": "MO"}
    g("overview", "/api/overview"); g("overview MO", "/api/overview?state=MO")
    for grain in ("tin", "entity", "npi"):
        for extra, lab in (("", "pooled"), ("&latest=1", "latest"), ("&month=2026-04", "m04")):
            g(f"rates {grain} {lab}", f"/api/rates?grain={grain}&dollar_only=1&cpt=97110{extra}&page_size=200")
            g(f"summary {grain} {lab}", f"/api/summary?grain={grain}&dollar_only=1{extra}")
        g(f"code {grain}", f"/api/code/97110?grain={grain}&dollar_only=1")
        g(f"code {grain} MO", f"/api/code/97110?grain={grain}&dollar_only=1&state=MO")
    g("trend", "/api/trend?cpt=97110&dollar_only=1")
    g("entity tin S1", "/api/entity/tin/" + S1)
    for ep in ("geography", "negotiability", "medicare", "differential"):
        p(f"market {ep}", f"/api/market/{ep}", {"code": "97110", "market": M})
        p(f"market {ep} nat", f"/api/market/{ep}", {"code": "97110", "market": {"month": "latest"}})
    p("terr local", "/api/territory/local", {"market": M, "state": "MO"})
    p("terr conc", "/api/territory/concentration", {"market": M})
    p("terr radius", "/api/territory/radius", {"market": M, "zip": "64105", "radius_miles": 25})
    p("report market", "/api/report/market", {"market": M, "state": "MO"})
    p("changes", "/api/changes", {"market": {"month": "latest"}})
    p("changes MO", "/api/changes", {"market": M})
    p("changes.csv", "/api/changes.csv", {"market": {"month": "latest"}})
    p("traj", "/api/trajectory", {"market": {"month": "latest"}})
    p("traj MO", "/api/trajectory", {"market": M})
    p("traj KS", "/api/trajectory", {"market": {"month": "latest", "state": "KS"}})
    g("known", "/api/known-sources"); g("clients", "/api/clients"); g("digest", "/api/clients/digest")
    g("export tin", "/api/export.csv?grain=tin&dollar_only=1&cpt=97110&latest=1")
    json.dump(out, open("/tmp/mrfx_review10/rates/c1_out.json", "w"), indent=1, default=str, sort_keys=True)
    for k, (s, b) in out.items():
        print(k, s, len(json.dumps(b, default=str)))

if __name__ == "__main__":
    main()
