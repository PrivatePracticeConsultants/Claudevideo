"""Round 10 headline numbers on the scratch store, in-process via TestClient."""
import sys, time, threading, json
from pathlib import Path
import mrfx.api as api; api.TRUSTED_HOSTS.add("testserver")
from fastapi.testclient import TestClient
from mrfx.store import Store
sys.path.insert(0, str(Path(__file__).parent))
from build_store import cfg_for
def t(c, url, hdr=None, label=None):
    t0 = time.perf_counter(); r = c.get(url, headers=hdr or {}); dt = time.perf_counter() - t0
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    extra = ""
    if isinstance(body, dict):
        extra = f" total={body.get('total')} rows={len(body.get('rows', []) or [])}" + (f" hint={body.get('hint')!r}" if body.get("hint") else "") + (f" detail={str(body.get('detail'))[:140]!r}" if r.status_code >= 400 else "")
    print(f"{dt*1000:8.0f} ms  {r.status_code}  {label or url}{extra}", flush=True)
    return r, dt
if __name__ == "__main__":
    root = Path(sys.argv[1]); c = cfg_for(root)
    t0 = time.perf_counter(); st = Store(c.store_dir, keep_warm=True); print("Store open %.2f s" % (time.perf_counter() - t0))
    cl = TestClient(api.create_app(c, st))
    with st.connect() as con:
        print("rates:", con.execute("select count(*) from rates").fetchone()[0], "spine:", con.execute("select count(*) from rates_by_tin").fetchone()[0], "tins:", con.execute("select count(*) from tin_directory").fetchone()[0])
    print("--- boot / first screen (fresh process)")
    t(cl, "/api/boot"); t(cl, "/api/stats"); t(cl, "/api/stats", label="/api/stats 2nd")
    t(cl, "/api/rates?grain=tin&latest=1", label="rates default 1st"); t(cl, "/api/rates?grain=tin&latest=1", label="rates default 2nd")
    t(cl, "/api/summary?grain=tin&latest=1", label="summary default 1st"); t(cl, "/api/summary?grain=tin&latest=1", label="summary default 2nd")
    t(cl, "/api/payers"); t(cl, "/api/months")
    print("--- searches")
    for q in ("PR", "PRA", "PRACTICE 12", "PRACTICE 1234 REHAB", "3000", "300001234", "1300001234"):
        t(cl, f"/api/rates?grain=tin&latest=1&q={q}", label=f"rates q={q!r}")
        t(cl, f"/api/summary?grain=tin&latest=1&q={q}", label=f"summary q={q!r}")
    t(cl, "/api/rates?grain=tin&latest=1&state=MO", label="rates state=MO"); t(cl, "/api/summary?grain=tin&latest=1&state=MO", label="summary state=MO")
    t(cl, "/api/rates?grain=tin&latest=1&sort=display_name", label="rates sort=name")
    t(cl, "/api/rates?grain=tin&latest=1&q=__", label="rates q=__ (literal)"); t(cl, "/api/rates?grain=tin&latest=1&modifier=%25", label="rates modifier=%")
    print("--- entity / npi grains")
    t(cl, "/api/rates?grain=entity&latest=1", label="entity default 1st"); t(cl, "/api/rates?grain=entity&latest=1", label="entity default 2nd")
    t(cl, "/api/rates?grain=entity&latest=1&page=2", label="entity page 2"); t(cl, "/api/rates?grain=entity&latest=1&sort=display_name", label="entity sort=name")
    t(cl, "/api/rates?grain=entity&latest=1&q=1300001234", label="entity q=NPI")
    t(cl, "/api/rates?grain=npi&latest=1", label="npi default"); t(cl, "/api/summary?grain=npi&latest=1", label="npi summary")
    t(cl, "/api/rates?grain=npi&latest=1&cpt=97110", label="npi cpt only")
    print("--- superseded (two overlapping requests, same view key)")
    hdr = {"X-Mrfx-View": "p1:explorer"}
    res = {}
    def go(k, q):
        tt = time.perf_counter(); r = cl.get(f"/api/rates?grain=tin&q={q}&sort=display_name", headers=hdr); res[k] = (r.status_code, time.perf_counter() - tt)
    a = threading.Thread(target=go, args=("first", "PR")); b = threading.Thread(target=go, args=("second", "PRACTICE 77"))
    a.start(); time.sleep(0.02); b.start(); a.join(); b.join(); print("superseded:", res)
    res = {}
    def go2(k, q):
        tt = time.perf_counter(); r = cl.get(f"/api/summary?grain=tin&q={q}", headers=hdr); res[k] = (r.status_code, round(time.perf_counter() - tt, 3))
    a = threading.Thread(target=go2, args=("first", "PR")); b = threading.Thread(target=go2, args=("second", "PRACTICE 77"))
    a.start(); time.sleep(0.02); b.start(); a.join(); b.join(); print("summary superseded/503?:", res)
    print("--- bad input")
    bad = [("/api/benchmark/market", {"market": {"billing_class": ["professional"]}}), ("/api/benchmark/market", {"market": {"city": 5}}),
           ("/api/trajectory", {"market": 5}), ("/api/leads", {"market": {"discipline": {"a": 1}}}), ("/api/contract-gaps", {"min_peers": 1e308, "subject": "PRACTICE 1 REHAB"}),
           ("/api/contracts/renewals", {"subjects": "x" * 500}), ("/api/org-website", {"tins": 5}), ("/api/benchmark/market", {"market": {"_plan_files": ["payer0_2026-05.json"], "state": "MO"}, "subject": "PRACTICE 1 REHAB"})]
    for url, body in bad:
        r = cl.post(url, json=body); txt = r.text
        print(f"POST {url} {json.dumps(body)[:60]} -> {r.status_code} LINE-in-body={'LINE ' in txt} path={'/tmp/' in txt} {txt[:110]!r}")
    r = cl.post("/api/upload", files={"file": ("x" * 300 + ".json", b"{}")}); print("upload 300-char name ->", r.status_code, r.text[:120])
    t(cl, "/api/files?offset=99999999999999999999", label="files offset overflow")
    print("--- quality subject-types twice")
    for i in range(2):
        t0 = time.perf_counter(); r = cl.post("/api/quality/subject-types", json={"subject": "PRACTICE 1 REHAB", "market": {"state": "MO"}}); print(f"{(time.perf_counter()-t0)*1000:8.0f} ms  {r.status_code}  subject-types #{i+1}")
    print("--- trajectory first/second")
    for i in range(2):
        t0 = time.perf_counter(); r = cl.post("/api/trajectory", json={"market": {"state": "MO"}}); print(f"{(time.perf_counter()-t0)*1000:8.0f} ms  {r.status_code}  trajectory MO #{i+1}")
    print("--- overview / summary")
    t(cl, "/api/overview"); t(cl, "/api/overview", label="/api/overview 2nd")
    print("--- materialised files beside the store")
    for p in sorted((st.rates_dir.parent).rglob("*")):
        if p.is_file() and ("spine" in p.name or "materialised" in p.name or p.suffix in (".json",)) and "rates/" not in str(p):
            print("  ", p.relative_to(st.rates_dir.parent), p.stat().st_size)
    st.close()
