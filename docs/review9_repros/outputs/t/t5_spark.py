"""Sparkline vs the printed number: (a) pinned as-of month; (b) pooled lines."""
from fx import *
import re

if __name__ == "__main__":
    cfg, store = mk()
    T = {k: f"5{i}1000001" for i, k in enumerate("SBCDE", start=1)}
    npis = {t: [f"15{i}1000001"] for i, t in enumerate(T.values(), start=1)}
    S = T["S"]
    def month(day, s_rate, s_b):
        a = {"97110": {S: s_rate, T["B"]: 45, T["C"]: 50, T["D"]: 55, T["E"]: 60}}
        b = {"97110": {S: s_b}}
        return a, b
    docs = []
    for day, sr, sb in (("2026-04-01", 40, [price(50), price(70, pos=("22",)), price(70, pos=("19",))]),
                        ("2026-05-01", 40, [price(50), price(70, pos=("22",)), price(70, pos=("19",))]),
                        ("2026-06-01", 30, [price(50), price(70, pos=("22",)), price(70, pos=("19",))])):
        a, b = month(day, sr, sb)
        docs.append((f"a_{day}.json", doc("A Health", day, a, npis)))
        docs.append((f"b_{day}.json", doc("B Health", day, b, npis)))
    ingest(cfg, store, docs)
    for k, t in T.items():
        name(store, npis[t][0], f"{k} REHAB", "MO")
    store.rebuild_rollups()
    from mrfx.benchmark import compute_benchmark, render_pitch_report
    from mrfx.schedule import compute_fee_schedule, payer_scorecard, render_rate_card
    for mon in ("2026-05", "latest"):
        m = {"month": mon, "state": "MO", "therapy_only": False}
        b = compute_benchmark(store, "S REHAB", m)
        print(mon, "benchmark subject_rate", [(r["billing_code"], r["subject_rate"]) for r in b["rows"]])
        html = render_pitch_report(cfg, store, b)
        print("   pitch spark:", re.findall(r"<title>(97110:[^<]*)</title>", html))
        fs = compute_fee_schedule(store, "S REHAB", m)
        print("   rate card:", [(c["billing_code"], {p: v["rate"] for p, v in c["rates"].items()}) for c in fs["codes"]])
        rc = render_rate_card(cfg, store, fs, payer_scorecard(fs))
        print("   card spark:", re.findall(r"<title>(97110:[^<]*)</title>", rc))
