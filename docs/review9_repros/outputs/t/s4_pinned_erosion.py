"""A pitch report pinned to 2023-06: does 'In real terms' stop at 2023-06?
P Health: 2022-06 $50, 2023-06 $50, 2024-06 $40 (a 20% cut that happened AFTER the report's as-of month).
Q Health: 39.99 after 40.00 -> -0.025% -> prints as?"""
from fx import *  # noqa
import re, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = mk()
    T = {k: f"5{i}1000001" for i, k in enumerate("ABCDE", start=1)}
    npis = {t: ["1" + t] for t in T.values()}
    docs = []
    for day, r in (("2022-06-01", 50), ("2023-06-01", 50), ("2024-06-01", 40)):
        docs.append((f"p_{day}.json", doc("P Health", day, {"97110": {t: r for t in T.values()}}, npis)))
    for day, r in (("2023-06-01", 40.00), ("2024-06-01", 39.99)):
        docs.append((f"q_{day}.json", doc("Q Health", day, {"97110": {t: r + i for i, t in enumerate(T.values())}}, npis)))
    ingest(cfg, store, docs)
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", "MO")
    store.rebuild_rollups()
    c = client(cfg, store)
    d = c.post("/api/trajectory", json={"market": {"state": "MO"}}).json()
    for p in d["payers"]:
        print(p["payer"], p["first_month"], p["last_month"], "cum", repr(p["cumulative_pct"]), "real", p["real_cumulative_pct"], p["direction"], "|", p["erosion_line"])
    for mon in ("2023-06", "latest"):
        r = c.post("/api/report/pitch", json={"subject": "A REHAB", "market": {"month": mon, "state": "MO"}})
        h = r.text
        asof = re.findall(r"As of[^·<]{0,60}", h)[:1]
        i = h.find("In real terms")
        blk = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h[i:i + 1700])) if i >= 0 else "(no block)"
        rate = re.findall(r"97110.{0,400}", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)))[:1]
        print("\nPITCH pinned", mon, asof)
        print("  table row:", rate[0][:160] if rate else None)
        print("  ", blk[:1100])
