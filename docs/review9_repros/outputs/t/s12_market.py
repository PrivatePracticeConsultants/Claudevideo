"""Market median for 97110 in MO on every surface. hand: 11 MO practices 20,41,45,46,47,48,50,50,51,52,53.5 -> $48.00"""
from fxm import *  # noqa
import json, re, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = build()
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    g = c.post("/api/market/geography", json={"code": "97110", "market": MO}).json()
    print("geography:", {k: v for k, v in g.items() if k not in ("note",)} if len(json.dumps(g)) < 900 else json.dumps(g)[:900])
    n = c.post("/api/market/negotiability", json={"code": "97110", "market": MO}).json()
    print("negotiability:", [(r["payer"], r["n_practices"], r["p10"], r["p50"], r["p90"], r["spread_pct"]) for r in n.get("rows", n.get("payers", []))], n.get("headline", "")[:200])
    m = c.post("/api/market/medicare", json={"code": "97110", "market": MO}).json()
    print("medicare:", json.dumps(m)[:500])
    d = c.post("/api/market/differential", json={"code": "97110", "market": MO}).json()
    print("differential:", json.dumps(d)[:700])
    h = c.post("/api/report/market", json={"market": MO, "state": "MO", "code": "97110"}).text
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h))
    print("market report:", t[:2500])
    o = c.get("/api/overview?state=MO").json()
    print("overview:", json.dumps(o)[:1500])
    cp = c.get("/api/code/97110?grain=tin&dollar_only=1&state=MO&modifier=base").json()
    print("code page keys:", list(cp.keys()), json.dumps({k: cp[k] for k in cp if k in ("summary", "stats", "payer_rank")})[:900])
    t = c.post("/api/territory/local", json={"subject": S1, "market": MO, "code": "97110"})
    print("territory local:", t.status_code, json.dumps(t.json())[:600])
