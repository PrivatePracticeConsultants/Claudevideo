from fxf import *  # noqa
from docs_dump import dump, text
import json, warnings
warnings.filterwarnings("ignore")
if __name__ == "__main__":
    cfg, store = build_full()
    c = client(cfg, store)
    MO = {"month": "latest", "state": "MO"}
    out = "/tmp/mrfx_review10/consulting/out/full"
    for k, s in (("acme", "ACME REHAB"), ("acme_tin", S1), ("big", "BIG GROUP"), ("b", "B REHAB")):
        codes = dump(c, f"{out}/{k}", s, MO, "Alpha Health")
        print(k, {a: b for a, b in codes.items() if b != 200})
    for k in ("acme", "acme_tin"):
        for f in ("visit.json", "weighted.json", "floors.json", "hospital_parity.json", "medicare_org.json"):
            print("==", k, f); print(open(f"{out}/{k}/{f}").read()[:1500])
    print(cfg.store_dir)
