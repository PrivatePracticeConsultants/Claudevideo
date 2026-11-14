"""Does the sold market report state its as-of vintage and basis?"""
from fx import *
import re

if __name__ == "__main__":
    cfg, store = mk()
    T = {k: f"6{i}7000001" for i, k in enumerate("ABCDEFGH", start=1)}
    npis = {t: [f"16{i}7000001"] for i, t in enumerate(T.values(), start=1)}
    may = {"97110": {t: 40 + i for i, t in enumerate(T.values())}}
    jun = {"97110": {t: 50 + i for i, t in enumerate(T.values())}}
    ingest(cfg, store, [("r_2026-05.json", doc("Rho Health", "2026-05-01", may, npis)),
                        ("r_2026-06.json", doc("Rho Health", "2026-06-01", jun, npis))])
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", "MO", tax=PT)
    store.rebuild_rollups()
    from mrfx.dossier import build_market_report
    res = build_market_report(cfg, store, state="MO", code="97110", market={})
    txt = re.sub(r"<[^>]+>", " ", res["html"])
    txt = re.sub(r"\s+", " ", txt)
    i = txt.find("What 97110")
    print(txt[i:i+700])
    print("mentions 'as of'/'latest'/month:", any(s in txt.lower() for s in ("as of", "latest", "2026-06", "newest")))
    j = txt.find("Who holds"); print(txt[j:j+400])
