"""Outreach merge fields vs Benchmark on the messy fixture."""
from fxm import *  # noqa
import csv, io, json, zipfile, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = build()
    c = client(cfg, store)
    def out(q):
        r = c.get("/api/export/outreach.csv?" + q)
        if r.status_code != 200:
            print(q, r.status_code, r.text[:200]); return
        rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))
        print("==", q, "rows", len(rows))
        if rows:
            print("   headers:", [h for h in rows[0].keys() if not h.startswith("C9")])
        for x in rows:
            print("   ", (x.get("ORG_NAME") or x.get("display_name") or list(x.values())[0])[:18].ljust(18), x.get("STATE"), x.get("TIN") or x.get("TINS"),
                  "| 97110", x.get("C97110_RATE"), x.get("C97110_MKT_MEDIAN"), x.get("C97110_PCTL"), x.get("C97110_GAP_TO_MEDIAN"),
                  "| 97140", x.get("C97140_RATE"), x.get("C97140_PCTL"), "| 97530", x.get("C97530_RATE"), x.get("C97530_PCTL"))
    out("grain=entity&dollar_only=1")
    out("grain=tin&dollar_only=1")
    out("grain=entity&dollar_only=1&state=MO")
    out("grain=tin&dollar_only=1&state=MO")
    out("grain=entity&dollar_only=1&state=MO&month=2026-04")
    out("grain=entity&dollar_only=1&payer=Beta%20Health")
    out("grain=entity&dollar_only=1&state=MO&billing_class=institutional")
    out("grain=entity&dollar_only=0&state=KS&therapy_only=1")
    for subj, m in (("ACME REHAB · MO", {"month": "latest", "allow_national": True}), ("ACME REHAB", {"month": "latest", "state": "MO"}),
                    ("BIG GROUP", {"month": "latest", "state": "MO"}), ("ACME REHAB", {"month": "2026-04", "state": "MO"}),
                    ("ACME REHAB", {"month": "latest", "state": "MO", "payers": ["Beta Health"]})):
        d = c.post("/api/benchmark/market", json={"subject": subj, "market": m}).json()
        print("bench", subj, m, [(x["billing_code"], x["subject_rate"], x["p50"], x["subject_percentile"], x["n_peers"]) for x in d.get("rows", [])], d.get("detail"))
    r = c.get("/api/export/outreach.zip?grain=entity&dollar_only=1&state=MO")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    for n in z.namelist():
        if n.endswith(".txt"):
            print(z.read(n).decode()[:3000])
