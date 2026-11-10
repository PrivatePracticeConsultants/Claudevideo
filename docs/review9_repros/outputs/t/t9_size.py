"""Size premium still scores ties with percent_rank (a flat schedule = p0)."""
from fx import *

if __name__ == "__main__":
    cfg, store = mk()
    T = {k: f"6{i}4000001" for i, k in enumerate("ABCDE", start=1)}
    npis = {t: [f"16{i}400000{j}" for j in range(1, 2 + i)] for i, t in enumerate(T.values(), start=1)}
    pr = {c: {t: r for t in T.values()} for c, r in (("97110", 40), ("97140", 35), ("97530", 50))}
    ingest(cfg, store, [("r_2026-06.json", doc("Rho Health", "2026-06-01", pr, npis))])
    for k, t in T.items():
        for n in npis[t]:
            name(store, n, f"{k} REHAB", "MO", tax=PT)
    store.rebuild_rollups()
    from mrfx.quality import size_premium
    from mrfx.benchmark import compute_benchmark
    m = {"month": "latest", "state": "MO", "therapy_only": False}
    sp = size_premium(store, m, subject="A REHAB", min_practices=1)
    print("bands:", [(b["band"], b["n_practices"], b["avg_percentile"]) for b in sp["bands"]])
    print("subject:", sp["subject"])
    print("headline:", sp.get("headline"))
    print("benchmark A:", [(r["billing_code"], r["subject_percentile"]) for r in compute_benchmark(store, "A REHAB", m)["rows"]])
