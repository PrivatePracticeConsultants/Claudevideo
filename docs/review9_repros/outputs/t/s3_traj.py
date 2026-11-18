"""Trajectory on the messy fixture + a payer whose panel does not overlap."""
from fxm import *  # noqa
import json, re, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    # Epsilon: 04 prices B,C; 06 prices D,E -> no matched pair at all
    e04 = {"97110": {B: 40, C: 40}}
    e06 = {"97110": {D: 20, E: 20}}
    # Zeta: a real, uniform 10% cut on 2 codes 04->06, plus an unchanged middle month
    z04 = {"97110": {B: 50, C: 60}, "97140": {B: 30, C: 40}}
    z05 = {"97110": {B: 50, C: 60}, "97140": {B: 30, C: 40}}
    z06 = {"97110": {B: 45, C: 54}, "97140": {B: 27, C: 36}}
    extra = [("eps_2026-04.json", doc("Epsilon Health", "2026-04-01", e04, NPIS)),
             ("eps_2026-06.json", doc("Epsilon Health", "2026-06-01", e06, NPIS)),
             ("zeta_2026-04.json", doc("Zeta Health", "2026-04-01", z04, NPIS)),
             ("zeta_2026-05.json", doc("Zeta Health", "2026-05-01", z05, NPIS)),
             ("zeta_2026-06.json", doc("Zeta Health", "2026-06-01", z06, NPIS))]
    cfg, store = build(extra)
    c = client(cfg, store)
    for m in ({}, {"state": "MO"}, {"state": "KS"}, {"month": "2026-05"}, {"payers": ["Alpha Health"]}):
        r = c.post("/api/trajectory", json={"market": m})
        d = r.json()
        print("market", m, r.status_code, d.get("detail"), d.get("months"))
        for p in d.get("payers", []):
            print("   ", p["payer"], p["first_month"], p["last_month"], "n_months", p["n_months"], "panel", p["panel_codes"], p["panel_practices"],
                  "cum", p["cumulative_pct"], "real", p["real_cumulative_pct"], "streak", p["cut_streak"], p["direction"],
                  [(s["month"], s["index_pct"], s["n_codes"], s["n_practices"]) for s in p["series"]])
            print("        ", p["erosion_line"], "|", p["real_reason"])
    # matched-line changes 05 -> 06 for Alpha: is anything a cut?
    r = c.post("/api/changes", json={"market": {"month": "2026-06", "prev_month": "2026-05", "payers": ["Alpha Health"]}})
    d = r.json()
    print("changes 05->06 Alpha:", d.get("summary") or {k: d[k] for k in d if k not in ("rows",)})
    for x in d.get("rows", []):
        print("    ", x.get("payer"), x.get("billing_code"), x.get("practice") or x.get("display_name"), x.get("old_rate"), x.get("new_rate"), x.get("pct_change"), x.get("direction"))
    # the pitch report pinned to 2026-05: which months does the 'In real terms' block span?
    for mon in ("2026-05", "latest"):
        r = c.post("/api/report/pitch", json={"subject": "C REHAB", "market": {"month": mon, "state": "MO"}})
        h = r.text
        i = h.find("In real terms")
        blk = re.sub(r"<[^>]+>", " ", h[i:i + 2600]) if i >= 0 else "(no block)"
        asof = re.findall(r"[Aa]s of[^<]{0,80}", h)[:3]
        print("PITCH month", mon, r.status_code, "| as-of text:", asof)
        print("   ", re.sub(r"\s+", " ", blk)[:1500])
