"""Surfaces that still resolve the subject their own way (resolve_subject_tins, not subject_tins_for_market).
'ACME REHAB' is one NPPES name used by an MO practice (TIN S1, Kansas City MO 64105) and a KS practice
(TIN S2, Wichita 67202, 3 NPIs). With market state MO every round-9 surface means S1.
Alpha pays S1 $40 and S2 $60 for 97110."""
from fxf import *  # noqa
import json, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    NPIS[S2] = ["1" + S2, "1610000092", "1610000093"]
    cfg, store = build_full()
    for n in NPIS[S2]:
        name(store, n, "ACME REHAB", "KS", zipc="67202", city="Wichita")
    for t in (K1, K2, K3):
        name(store, "1" + t, LABEL[t], "KS", zipc="66101", city="Kansas City")   # 3 miles from 64105, across the line
    store.rebuild_rollups()
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    P = lambda url, body: c.post(url, json=body)
    d = P("/api/benchmark/market", {"subject": "ACME REHAB", "market": MO}).json()
    print("Benchmark   tins", d["subject_tins"], "97110", [r["subject_rate"] for r in d["rows"] if r["billing_code"] == "97110"])
    d = P("/api/schedule/fee", {"subject": "ACME REHAB", "market": MO}).json()["fee_schedule"]
    print("Rate card   Alpha 97110", d["codes"][0]["rates"]["Alpha Health"]["rate"])
    print("--- (a) remit check: Alpha paid $45 for 97110")
    for subj in ("ACME REHAB", S1):
        r = P("/api/remits/check", {"subject": subj, "market": MO, "text": "code,payer,allowed_amount\n97110,Alpha Health,45.00\n"})
        d = r.json()
        print(f"   subject={subj!r:14} ->", r.status_code, json.dumps({k: d.get(k) for k in ("headline", "flagged", "matched")})[:420])
    print("--- (b) leverage vs Alpha, 15 miles")
    for subj, m in (("ACME REHAB", MO), (S1, MO), (S1, {"month": "latest"})):
        d = P("/api/leverage", {"subject": subj, "payer": "Alpha Health", "market": m, "radius_miles": 15}).json()
        print(f"   subject={subj!r:14} market={m} -> tins {d.get('subject_tins')} home_zip {d.get('home_zip')} n_alt {d.get('n_alternatives')} | {str(d.get('headline') or d)[:150]}")
    d = P("/api/report/dossier.json", {"subject": "ACME REHAB", "market": MO}).json()
    print("   dossier facts", d["facts"], [s for s in d["skipped"] if "everage" in s])
    print("--- (c) visit economics: MO office bills 3000/1000/500 Medicare units; the KS office 9000")
    for subj in ("ACME REHAB", S1):
        d = P("/api/economics/visit", {"subject": subj, "market": MO}).json()
        print(f"   subject={subj!r:14} total units {d.get('total_annual_units')}", [(r["payer"], r["own_codes_value_per_billed_unit"], r["annual_value_at_this_rate"]) for r in d["rows"]])
    print("--- (d) org bundle")
    import zipfile, io
    for subj in ("ACME REHAB", S1):
        z = zipfile.ZipFile(io.BytesIO(P("/api/report/org-bundle.zip", {"subject": subj, "market": MO}).content))
        prof = z.read("profile.txt").decode()
        print(f"   subject={subj!r:14}", [l for l in prof.splitlines() if l.startswith(("Tax IDs", "Location", "NPIs", "States"))], "rates.csv rows", len(z.read("rates.csv").decode().splitlines()) - 1)
    print("--- (e) weighted position with Medicare volumes (no volumes supplied)")
    for subj in ("ACME REHAB", S1):
        d = P("/api/economics/weighted-position", {"subject": subj, "market": MO}).json()
        print(f"   subject={subj!r:14}", {k: d.get(k) for k in ("weighted_percentile", "unweighted_percentile", "heaviest_code_share_pct")})
    print("--- (f) utilization volumes ('Fill from Medicare')")
    for subj in ("ACME REHAB", S1):
        d = P("/api/utilization/volumes", {"subject": subj}).json()
        print(f"   subject={subj!r:14}", json.dumps(d)[:260])
