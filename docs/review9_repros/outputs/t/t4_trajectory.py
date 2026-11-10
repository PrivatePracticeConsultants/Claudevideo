"""Payer trajectory pools every code into one median per practice-month: a payer
adding a cheaper code (no rate moved) reads as a cut."""
from fx import *

if __name__ == "__main__":
    cfg, store = mk()
    T = {k: f"9{i}0000001" for i, k in enumerate("ABCDE", start=1)}
    npis = {t: [f"19{i}0000001"] for i, t in enumerate(T.values(), start=1)}
    may = {"97110": {t: 50 for t in T.values()}}
    jun = {"97110": {t: 50 for t in T.values()}, "97140": {t: 30 for t in T.values()}}
    ingest(cfg, store, [("p_2023-06.json", doc("P Health", "2023-06-01", may, npis)),
                        ("p_2024-06.json", doc("P Health", "2024-06-01", jun, npis))])
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", "MO", tax=PT, etype="NPI-2")
    store.rebuild_rollups()
    from mrfx.monitor import compute_payer_trajectory, compute_rate_changes
    m = {"state": "MO", "therapy_only": False}
    tr = compute_payer_trajectory(store, m)
    for p in tr["payers"]:
        print(p["payer"], [(s["month"], s["index_pct"]) for s in p["series"]], p["cumulative_pct"],
              p["real_cumulative_pct"], "|", p["erosion_line"])
    ch = compute_rate_changes(store, {"month": "latest", "state": "MO", "therapy_only": False})
    print("matched-line changes:", ch["count"])
    from mrfx.benchmark import compute_benchmark, render_pitch_report
    b = compute_benchmark(store, "A REHAB", {"month": "latest", "state": "MO", "therapy_only": False})
    html = render_pitch_report(cfg, store, b)
    import re
    i = html.find("In real terms")
    print(re.sub(r"<[^>]+>", " ", html[i:i+1500]) if i >= 0 else "no erosion block")
