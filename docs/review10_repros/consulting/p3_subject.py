"""Subject rule sweep: 'ACME REHAB' / 'ACME' / 'acme rehab' / TIN / NPI with state MO must give the same documents;
out-of-state K1 REHAB vs MO must be priced on every subject surface."""
from fxm import *  # noqa
from docs_dump import dump, text
import os, re, glob, warnings, filecmp
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = build()
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    base = "/tmp/mrfx_review10/consulting/out/subj"
    variants = {"full": "ACME REHAB", "partial": "ACME", "lower": "acme rehab", "tin": S1, "npi": "1" + S1, "dashed": "61-0000001", "keyed": "ACME REHAB · MO"}
    for k, s in variants.items():
        codes = dump(c, f"{base}/{k}", s, MO, "Alpha Health")
        bad = {a: b for a, b in codes.items() if b != 200}
        if bad: print(k, "non-200:", bad)
    ref = f"{base}/full"
    for k, s in variants.items():
        if k == "full": continue
        for f in sorted(glob.glob(ref + "/**/*", recursive=True)):
            if os.path.isdir(f) or f.endswith(".zip"): continue
            g = f.replace(ref, f"{base}/{k}")
            if not os.path.exists(g): print(k, "missing", os.path.relpath(f, ref)); continue
            norm = lambda p, subj: re.sub(r"\d{4}-\d\d-\d\d[ _]\d\d:?\d\d( UTC)?", "<ts>", open(p, errors="replace").read().replace(subj, "<S>").replace(subj.replace("·", "&#183;"), "<S>"))
            a, b = norm(f, "ACME REHAB"), norm(g, s)
            b = b.replace("ACME REHAB", "<S>") ; a = a.replace("ACME REHAB", "<S>")
            if a != b:
                # first difference
                i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
                print(f"DIFF {k:8s} {os.path.relpath(f, ref):28s} full: …{a[max(0,i-60):i+80]!r}\n{'':38s} {k}: …{b[max(0,i-60):i+80]!r}")
    print("==== out-of-state K1 REHAB vs MO peers")
    codes = dump(c, f"{base}/k1", "K1 REHAB", MO, "Alpha Health")
    print({a: b for a, b in codes.items() if b != 200})
    import json
    for f in ("benchmark.json", "compare.json", "fee.json", "gaps.json", "size.json", "subject_types.json", "dossier.json", "leverage.json", "leverage_summary.json", "weighted.json", "visit.json", "renewals.json", "changes.json", "hospital_parity.json", "floors.json"):
        print(f, open(f"{base}/k1/{f}").read()[:420])
