import sys, math
if __name__ == "__main__":
    from mrfx.parser import clean_rate
    print("clean_rate:", {s: clean_rate(s) for s in ["nan", "Infinity", "-inf", "1e999", True, "34.50", "$34.50", "2 x 34.50", "123456789.0"]})
    from mrfx.rounding import half_up
    print("half_up:", half_up(12.5), half_up(112.5), half_up(0.125, 2), half_up(2.675, 2), round(12.5), half_up(-0.5))
    from mrfx.store import looks_like_ssn, mask_tin, SSN_PREFIXES
    import duckdb
    from mrfx.api import _mask_tin_sql
    con = duckdb.connect()
    mism = []
    for p in range(100):
        t = f"{p:02d}1234567"
        py = mask_tin(t)
        sq = con.execute(f"SELECT {_mask_tin_sql('?')}", [t]).fetchone()[0]
        if py != sq: mism.append((t, py, sq))
    # also entity lists and NPIs
    for t in ["431234567; 961234567", "1234567890", "000123456", "666123456", "900123456"]:
        py = "; ".join(mask_tin(x) for x in t.split("; "))
        sq = con.execute(f"SELECT {_mask_tin_sql('?')}", [t]).fetchone()[0]
        if py != sq: mism.append((t, py, sq))
    print("mask parity mismatches:", mism)
    from mrfx.inflation import deflator
    idx = {"values": {2023: 304.702, 2025: 160.0}, "names": {2023: "CPI-U", 2025: "MEI"}, "sources": {}}
    print("deflator mixed:", deflator(idx, "2023-06", "2025-06"))
    from mrfx.modifiers import normalize
    for m in (["GP","59","GP"], ["11"], ["59","GP"], ["GP","11"], ["21","31","22","24"], ["CQ"], None, "GP", ["XE"]):
        try: print("mod", m, "->", normalize(m))
        except Exception as e: print("mod", m, "EXC", e)
