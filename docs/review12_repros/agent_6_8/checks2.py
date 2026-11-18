if __name__ == "__main__":
    import duckdb
    from mrfx.store import mask_tin
    from mrfx.api import _mask_tin_sql
    from mrfx.rounding import half_up
    con = duckdb.connect()
    con.execute("CREATE TABLE t(v VARCHAR)")
    vals = [f"{p:02d}1234567" for p in range(100)] + ["431234567; 961234567", "1234567890", "000123456", "666123456", "900123456", "43-1234567"]
    con.executemany("INSERT INTO t VALUES (?)", [[v] for v in vals])
    sq = dict(con.execute(f"SELECT v, {_mask_tin_sql('v')} FROM t").fetchall())
    mism = [(v, "; ".join(mask_tin(x) for x in v.split("; ")), sq[v]) for v in vals if "; ".join(mask_tin(x) for x in v.split("; ")) != sq[v]]
    print("mask parity mismatches:", mism, "| masked count:", sum(1 for v in vals if sq[v] != v))
    # half_up vs DuckDB round on tie cases
    cases = [(12.5,0),(112.5,0),(0.125,2),(2.675,2),(1.005,2),(-0.5,0),(45/40*100,0),(0.5,0),(1.5,0),(2.5,0)]
    bad = []
    for x, nd in cases:
        d = con.execute(f"SELECT round(?::DOUBLE, {nd})", [x]).fetchone()[0]
        h = half_up(x, nd) if nd else half_up(x)
        if float(d) != float(h): bad.append((x, nd, d, h))
    print("half_up vs duckdb round mismatches:", bad)
    from mrfx.inflation import deflator
    idx = {"values": {2023: 304.702, 2025: 160.0}, "names": {2023: "CPI-U", 2025: "MEI"}, "sources": {}}
    print("deflator mixed:", deflator(idx, "2023-06", "2025-06")["reason"])
    from mrfx.modifiers import normalize
    for m in (["GP","59","GP"], ["11"], ["59","GP"], ["GP","11"], ["21","31","22","24"], ["CQ"], None, "GP", ["XE"], ["gp"]):
        try: print("mod", m, "->", normalize(m))
        except Exception as e: print("mod", m, "EXC", type(e).__name__, e)
    from mrfx.benchmark import normalize_market
    print("normalize_market:", normalize_market({"state": "Missouri", "payers": "Aetna", "month": "LATEST"}))
    from mrfx.states import state_code
    print("state_code missouri:", state_code("missouri"))
