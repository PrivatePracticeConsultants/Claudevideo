from fxm import *  # noqa
import json, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = build()
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    d = c.post("/api/leaderboard", json={"market": MO, "min_codes": 1, "sort": "paid"}).json()
    for x in d["practices"]:
        print("LB ", x["display_name"].ljust(14), {k: x[k] for k in x if k in ("n_codes", "median_percentile", "median_rate", "npi_count", "state")})
    for payer in ("Alpha Health", "Beta Health"):
        d = c.post("/api/roster", json={"market": MO, "payer": payer, "min_codes": 1}).json()
        for x in d["organizations"]:
            print("ROSTER", payer, x["display_name"].ljust(14), x["n_codes"], x["median_percentile"], x["median_rate"])
    for subj in ("ACME REHAB", "BIG GROUP"):
        d = c.post("/api/economics/visit", json={"market": MO, "subject": subj}).json()
        print("VISIT", subj, json.dumps(d)[:300])
        d = c.post("/api/report/dossier.json", json={"market": MO, "subject": subj}).json()
        print("DOSSIER", subj, json.dumps(d)[:1800])
