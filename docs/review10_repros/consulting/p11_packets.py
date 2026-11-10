"""The Clients tab's 'Build monthly packets' (POST /api/packets/build, no body) for one MO client."""
from fxf import *  # noqa
import json, time, warnings, os
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = build_full()
    c = client(cfg, store)
    print(c.post("/api/clients", json={"subject": "B REHAB"}).status_code)
    print(c.post("/api/packets/build", json={}).json())
    for _ in range(60):
        j = c.get("/api/packets").json()
        if j["state"] != "running": break
        time.sleep(0.5)
    res = j.get("result") or {}
    for p in res.get("packets", []):
        print("written:", p["written"]); [print("OMITTED:", s[:300]) for s in p["skipped"]]
    print(res.get("dir"))
