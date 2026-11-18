"""4.1: the quartile now follows the printed percentile. Does it now contradict
the P25 / median / P75 printed on the SAME row (value below the printed P25 yet
Q2, at/above P75 yet Q3, ...)? Brute force over small tied markets."""
import itertools, sys
if __name__ == "__main__":
    from mrfx.visit_market import _stats, _position
    found = {}
    vals = [100.0, 105.44, 110.31, 120.0]
    for n in range(3, 8):
        for others in itertools.combinations_with_replacement(vals, n):
            for v in vals + [102.0, 107.0, 115.0]:
                others_l = list(others)
                st = _stats(others_l + [v])
                p = _position(v, others_l, st)
                q = p["quartile"]
                band = ("Q1" if v < st["p25"] else "Q2" if v < st["median"] else
                        "Q3" if v < st["p75"] else "Q4")
                if q != band:
                    key = (q, band)
                    if key not in found:
                        found[key] = (others_l, v, st["p25"], st["median"], st["p75"], p["percentile"])
    for (q, band), ex in sorted(found.items()):
        o, v, p25, med, p75, pct = ex
        print(f"prints {q} but value is in the {band} band of the printed P25/median/P75: "
              f"others={o} value={v} -> P25 {p25} median {med} P75 {p75}, p{pct} {q}")
