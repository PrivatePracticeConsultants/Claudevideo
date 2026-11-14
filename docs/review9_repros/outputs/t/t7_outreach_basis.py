"""Outreach merge fields (dashboard button = filterQuery() defaults) vs Benchmark."""
from fx import *
import csv, io, zipfile

if __name__ == "__main__":
    cfg, store = mk()
    T = {k: f"6{i}2000001" for i, k in enumerate("ABCDE", start=1)}
    npis = {t: [f"16{i}2000001"] for i, t in enumerate(T.values(), start=1)}
    A = T["A"]
    inst = dict(price(100)); inst["billing_class"] = "institutional"
    may = {"97110": {A: 40, T["B"]: 45, T["C"]: 50, T["D"]: 55, T["E"]: 60}}
    jun = {"97110": {A: [price(42), price(34, mods=("GP", "CQ")), inst],
                     T["B"]: 45, T["C"]: 50, T["D"]: 55, T["E"]: 60}}
    ingest(cfg, store, [("r_2026-05.json", doc("Rho Health", "2026-05-01", may, npis)),
                        ("r_2026-06.json", doc("Rho Health", "2026-06-01", jun, npis))])
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", "MO")
    store.rebuild_rollups()
    from mrfx.benchmark import compute_benchmark
    b = compute_benchmark(store, "A REHAB", {"month": "latest", "state": "MO"})
    print("benchmark A 97110:", [(r["subject_rate"], r["subject_percentile"]) for r in b["rows"]])
    c = client(cfg, store)
    # exactly what the Outreach button sends with untouched filters (filterQuery defaults)
    r = c.get("/api/export/outreach.zip", params={"grain": "entity", "dollar_only": "1", "state": "MO"})
    z = zipfile.ZipFile(io.BytesIO(r.content))
    name_csv = [n for n in z.namelist() if n.endswith(".csv")][0]
    rows = list(csv.DictReader(io.StringIO(z.read(name_csv).decode("utf-8-sig"))))
    for x in rows:
        print("  outreach", x["ORG_NAME"], x["C97110_RATE"], x["C97110_MKT_MEDIAN"], x["C97110_PCTL"], x["MONTHS"])
    meth = z.read([n for n in z.namelist() if n.endswith(".txt")][0]).decode()
    print("methodology mentions PCTL?", "PCTL" in meth, "| percentile?", "percentile" in meth.lower())
