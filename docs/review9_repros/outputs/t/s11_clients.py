"""Clients digest + packet vs Benchmark/changes for ACME REHAB (same-name, MO+KS) and BIG GROUP."""
from fxm import *  # noqa
import json, re, warnings, os
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    t05 = {"97116": {S1: 80.00, B: 80, C: 82, D: 84, E: 86, F: 88}}
    t06 = {"97116": {S1: 72.00, B: 80, C: 82, D: 84, E: 86, F: 88}}     # a 10% cut for S1 (MO)
    cfg, store = build([("theta_2026-05.json", doc("Theta Health", "2026-05-01", t05, NPIS)),
                        ("theta_2026-06.json", doc("Theta Health", "2026-06-01", t06, NPIS))])
    cfg.packets_dir = cfg.store_dir.parent / "packets"
    c = client(cfg, store)
    for s in ("ACME REHAB", "BIG GROUP", "ACME REHAB · KS"):
        print("add", s, c.post("/api/clients", json={"subject": s}).status_code)
    d = c.get("/api/clients/digest").json()
    print(json.dumps(d)[:3000])
    r = c.post("/api/packets/build", json={"clients": True})
    print("packets", r.status_code, json.dumps(r.json())[:900])
    for root, _d, files in os.walk(cfg.store_dir.parent):
        for f in files:
            if f.endswith(".html") and "packet" in root.lower():
                h = open(os.path.join(root, f)).read()
                txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h))
                print("PACKET", f, len(h)); print("   ", txt[:2600])
    MO = {"month": "latest", "state": "MO"}
    for s in ("ACME REHAB", "ACME REHAB · KS"):
        b = c.post("/api/benchmark/market", json={"subject": s, "market": MO}).json()
        print("bench MO", s, [(x["billing_code"], x["subject_rate"], x["subject_percentile"]) for x in b["rows"]])
        b = c.post("/api/benchmark/market", json={"subject": s, "market": {"month": "latest", "allow_national": True}}).json()
        print("bench NAT", s, [(x["billing_code"], x["subject_rate"], x["subject_percentile"]) for x in b["rows"]])
