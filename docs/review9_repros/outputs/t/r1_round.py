"""Rounding: half_up vs DuckDB round(); Python median vs DuckDB median on half-cent pairs."""
import sys, random, statistics
sys.path.insert(0, "/home/user/Claudevideo")
import duckdb
from mrfx.rounding import half_up
if __name__ == "__main__":
    con = duckdb.connect()
    random.seed(9)
    # (1) half_up vs round() on doubles, 0/1/2 dp, incl. negatives
    vals = [round(random.uniform(-500, 500), 3) for _ in range(40000)] + [x / 1000 for x in range(5, 200000, 10)]
    bad = {0: 0, 1: 0, 2: 0}
    ex = {}
    for d in (0, 1, 2):
        got = con.execute(f"SELECT round(v::DOUBLE, {d}) FROM (SELECT unnest(?::DOUBLE[]) v)", [vals]).fetchall()
        for v, (g,) in zip(vals, got):
            h = half_up(v, d)
            if float(h) != float(g):
                bad[d] += 1; ex.setdefault(d, (v, g, h))
    print("half_up vs duckdb round mismatches:", bad, ex)
    # (2) two-TIN entity: DuckDB median(a,b) rounded in SQL (Benchmark/rate card/Explorer) vs
    #     Python (a+b)/2 -> half_up (outreach entity grain, spark) vs statistics.median
    pairs = [(round(random.uniform(20, 120), 2), 0) for _ in range(60000)]
    pairs = [(a, round(a + random.choice([0.01, 0.03, 0.05, 0.07, 0.11, 1.01, 2.03]), 2)) for a, _ in pairs]
    rows = con.execute("SELECT a, b, round(median(x), 2), median(x) FROM (SELECT a, b, unnest([a, b]) x FROM (SELECT unnest(?::DOUBLE[]) a, unnest(?::DOUBLE[]) b)) GROUP BY a, b",
                       [[p[0] for p in pairs], [p[1] for p in pairs]]).fetchall()
    n = 0; shown = 0
    for a, b, sqlr, sqlm in rows:
        py = half_up((a + b) / 2, 2)
        py2 = half_up(statistics.median([a, b]), 2)
        if py != sqlr or py2 != sqlr:
            n += 1
            if shown < 6:
                shown += 1
                print("  pair", a, b, "SQL round(median)", sqlr, "(median repr", repr(sqlm), ") python half_up((a+b)/2)", py, repr((a + b) / 2))
    print("two-value median: SQL vs Python-then-half_up mismatches:", n, "of", len(rows))
    # (3) half-of-a-point positions: Python mean/median of SQL-rounded pct vs SQL
    print("half_up(12.5)=", half_up(12.5), "half_up(0.125,2)=", half_up(0.125, 2), "duckdb", con.execute("select round(12.5::DOUBLE), round(0.125::DOUBLE,2), round(2.675::DOUBLE,2), round(1.005::DOUBLE,2), round(-0.025::DOUBLE,1)").fetchall(), half_up(2.675, 2), half_up(1.005, 2), half_up(-0.025, 1))
    # (4) f-string formatting of an unrounded float vs half_up
    for v in (0.125, 2.675, 1.005, 45.125, 33.325, 12.5, 112.5, 0.5, 1.5, 2.5, 20.025):
        print(f"  v={v!r}: f'{{v:.2f}}'={v:.2f} half_up2={half_up(v,2)} | f'{{v:.0f}}'={v:.0f} half_up0={half_up(v)} | f'{{v:.1f}}'={v:.1f} half_up1={half_up(v,1)}")
