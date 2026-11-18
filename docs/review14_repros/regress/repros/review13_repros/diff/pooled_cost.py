"""Cost of quality.pooled_month_files' spine aggregate per data generation, on a
synthetic rates_by_tin-shaped table (the five columns the query reads; 20M rows)."""
import time
import duckdb

if __name__ == "__main__":
    con = duckdb.connect()
    con.execute("SET threads=4")
    t0 = time.monotonic()
    con.execute("""CREATE TABLE rates_by_tin AS
        SELECT 'Payer ' || (i % 12)::VARCHAR AS payer,
               '2026-' || lpad(((i % 6) + 1)::VARCHAR, 2, '0') AS file_month,
               lpad(((i * 7919) % 2000000)::VARCHAR, 9, '0') AS tin_value,
               CASE WHEN i % 10 = 0 THEN 2 ELSE 1 END AS source_count,
               CASE WHEN i % 40 = 0 THEN 2 ELSE 1 END AS rate_variants
        FROM range(20000000) t(i)""")
    print(f"built 20M rows in {time.monotonic() - t0:.1f} s")
    sql = """SELECT payer, file_month,
                   count(DISTINCT tin_value) FILTER (WHERE source_count > 1),
                   count(*) FILTER (WHERE source_count > 1),
                   count(*) FILTER (WHERE source_count > 1 AND rate_variants > 1)
            FROM rates_by_tin GROUP BY 1, 2"""
    for _ in range(3):
        t0 = time.monotonic()
        con.execute(sql).fetchall()
        print(f"aggregate over 20M rows: {time.monotonic() - t0:.2f} s  (-> x5 for 99M)")
