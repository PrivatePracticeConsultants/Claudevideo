"""Dossier header vs its rates table for a same-name multi-state subject and a mapped entity."""
from fxm import *  # noqa
import re, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = build()
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    for subj in ("ACME REHAB", "BIG GROUP"):
        h = c.post("/api/report/dossier", json={"subject": subj, "market": MO}).text
        t = re.sub(r"\s+", " ", re.sub(r"<style.*?</style>", " ", h, flags=re.S))
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))
        i = t.find("Practice profile")
        print(subj, "::", t[i:i + 900])
        b = c.post("/api/benchmark/market", json={"subject": subj, "market": MO}).json()
        print("   benchmark tins:", b["subject_tins"])
        d = c.get("/api/clients/digest").json()
