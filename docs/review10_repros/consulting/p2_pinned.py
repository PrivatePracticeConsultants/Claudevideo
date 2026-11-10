"""Pinned-month sweep: P Health 2022-06 / 2023-06 pay x.00; 2024-06 pays x.77 (a cut) and adds NEWCO.
Any document pinned to 2023-06 that prints '.77', '2024' (other than its generated date/version) or NEWCO leaks."""
from fx import *  # noqa
from docs_dump import dump, text
import os, re, sys, glob, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = mk()
    T = {k: f"5{i}1000001" for i, k in enumerate("ABCDEFG", start=1)}
    npis = {t: ["1" + t] for t in T.values()}
    docs = []
    old = list("ABCDEF")
    for day in ("2022-06-01", "2023-06-01"):
        docs.append((f"p_{day}.json", doc("P Health", day, {
            "97110": {T[k]: 50 + i for i, k in enumerate(old)},
            "97140": {T[k]: 30 + 2 * i for i, k in enumerate(old)}}, npis)))
        docs.append((f"q_{day}.json", doc("Q Health", day, {
            "97110": {T[k]: 44 + i for i, k in enumerate(old)}}, npis)))
    docs.append(("p_2024-06-01.json", doc("P Health", "2024-06-01", {
        "97110": {T[k]: 40.77 + i for i, k in enumerate("ABCDEFG")},
        "97140": {T[k]: 20.77 + 2 * i for i, k in enumerate("ABCDEFG")}}, npis)))
    docs.append(("q_2024-06-01.json", doc("Q Health", "2024-06-01", {
        "97110": {T[k]: 60.77 + i for i, k in enumerate("ABCDEFG")}}, npis)))
    ingest(cfg, store, docs)
    for k, t in T.items():
        name(store, npis[t][0], "NEWCO THERAPY" if k == "G" else f"{k} REHAB", "MO")
    store.rebuild_rollups()
    c = client(cfg, store)
    out = "/tmp/mrfx_review10/consulting/out/pinned"
    codes = dump(c, out, "B REHAB", {"month": "2023-06", "state": "MO"}, "P Health")
    print({k: v for k, v in codes.items() if v != 200})
    for f in sorted(glob.glob(out + "/**/*", recursive=True)):
        if os.path.isdir(f) or f.endswith(".zip"):
            continue
        t = text(f)
        hits = set(re.findall(r"[^ ]{0,40}\d\.77[^ ]{0,10}", t)) | set(re.findall(r".{0,50}2024-0\d.{0,30}", t)) | set(re.findall(r".{0,40}NEWCO.{0,20}", t))
        hits = {h for h in hits}
        if hits:
            print("LEAK", os.path.relpath(f, out), list(hits)[:6])
    print("store", cfg.store_dir)
