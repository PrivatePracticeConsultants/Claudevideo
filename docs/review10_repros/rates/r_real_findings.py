"""Findings reproduced on the 32M real-data copy (served on 8710). Read-only."""
import io, json, re, urllib.request, urllib.parse, zipfile
B = "http://localhost:8710"
def get(u): return json.load(urllib.request.urlopen(B + u))
def raw(u): return urllib.request.urlopen(B + u).read()
def post(u, b):
    r = urllib.request.Request(B + u, data=json.dumps(b).encode(), headers={"content-type": "application/json"})
    return json.load(urllib.request.urlopen(r))

def main():
    print("== 1. Markets, state AL picked: the 'by state' table and its 'National median'")
    nat = post("/api/market/geography", {"code": "97110", "market": {"month": "latest"}})
    al = post("/api/market/geography", {"code": "97110", "market": {"month": "latest", "state": "AL"}})
    n = {s["state"]: s for s in nat["states"]}; a = {s["state"]: s for s in al["states"]}
    print(f"   no state : national_median {nat['national_median']} / {nat['national_practices']} practices, {len(nat['states'])} states")
    print(f"   state AL : 'national_median' {al['national_median']} / {al['national_practices']} practices, {len(al['states'])} states listed")
    for st in ("TX", "NY", "MA", "WA"):
        print(f"   {st}: national view ${n[st]['median_rate']} ({n[st]['n_practices']} practices) | AL view prints ${a[st]['median_rate']} ({a[st]['n_practices']} practices)" if st in a else f"   {st}: not in AL view")

    print("== 2. Rate changes with a month picked (the 'compare to' box on auto)")
    for m in get("/api/months")["months"][:2]:
        try:
            r = post("/api/changes", {"market": {"month": m}})
            print(f"   month {m}: compares {r['prev_month']} -> {r['new_month']}; count {r['count']}; says who was compared: {'compared' in r}")
        except urllib.error.HTTPError as e:
            print("   month", m, e.code, e.read()[:200])
    r = post("/api/changes", {"market": {"month": "latest"}})
    print("   payer months:", [(c["payer"][:28], c["prev_month"], c["new_month"]) for c in r["compared"]], "| single-month payers:", len(r["not_compared"]))
    print("   -> no payer published in BOTH 2026-10 and 2026-11, so '2026-10 -> 2026-11: No rate moves' compared nothing")

    print("== 3. Code page: 'Which payer pays best' vs 'Median by month' for the payer's only month")
    code = get("/api/code/97110?grain=tin&modifier=base"); tr = get("/api/trend?cpt=97110&grain=tin&modifier=base")["rows"]
    for p in code["payer_rank"]:
        t = [x for x in tr if x["payer"] == p["payer"]]
        print(f"   {p['payer'][:44]:44s} payer table ${p['median_rate']:.2f} ({p['n_entities']} practices) | month table " + ", ".join(f"{x['file_month']} ${x['median_rate']:.2f} ({x['entities']})" for x in t))

    print("== 4. Export methodology: the file list's 'month' vs the rows' file_month")
    z = zipfile.ZipFile(io.BytesIO(raw("/api/export.zip?grain=tin&dollar_only=1&payer=" + urllib.parse.quote("HEALTHSCOPE BENEFITS"))))
    meth = [z.read(n).decode() for n in z.namelist() if n.endswith("_methodology.txt")][0]
    import csv as _csv
    rows = list(_csv.DictReader(io.StringIO([z.read(n).decode("utf-8-sig") for n in z.namelist() if n.endswith(".csv")][0])))
    print("   sidecar file list says:", re.findall(r"uhc_2026-11-01_HEALTHSCOPE[^;]*", meth)[:1])
    print("   rows' file_month values:", sorted({r_["file_month"] for r_ in rows}), "last_updated_on:", sorted({r_["last_updated_on"] for r_ in rows}), f"({len(rows)} rows)")
    print("   sidecar Filters line:", [l for l in meth.splitlines() if l.startswith("Filters:")])

    print("== 5. Overview: 'States with data', freshness")
    ov = get("/api/overview?state=AL"); st = get("/api/states")["states"]
    print("   overview(state=AL).states =", ov["states"], "| /api/states non-codes:", [s for s in st if len(s) != 2], "| military/unknown:", [s for s in st if s in ("AA", "AE", "AP", "NA")])
    fr = get("/api/quality/freshness")
    for p in fr["payers"]:
        if p["age_days"] is not None and (p["age_days"] < 0 or (p["newest_file_month"] or "") < (p["published"] or "")[:7]):
            print("   freshness:", p["payer"], "published", p["published"], "age", p["age_days"], "days | newest month of its rates", p["newest_file_month"], "| files", p["n_files"])
    files = get("/api/files?limit=50")["files"]
    print("   ND files:", [(f["filename"][:38], f["last_updated_on"], f["rows_emitted"]) for f in files if "North Dakota" in (f.get("payer") or "")])

    print("== 6. Sources tab vs the tested-sources list")
    ks = get("/api/known-sources")["sources"]
    for stc in ("WV", "WY", "ND"):
        s = get("/api/sources?state=" + stc)
        print("   ", stc, "Sources tab:", [(x["name"], "verified" if x.get("verified") else "unverified") for x in s["state"]],
              "| tested list:", [(k["name"], "auto-queueable" if k["queueable"] else "portal", k["verified"]) for k in ks if stc in k["states"]])

if __name__ == "__main__":
    main()
