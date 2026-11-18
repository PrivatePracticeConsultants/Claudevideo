"""Make beta's stored part look as a pre-0.7.0 build wrote it ('fee_schedule', not dollars, rules 2)."""
import sys, json
from pathlib import Path
I = Path(sys.argv[1]); sys.path.insert(0, str(I))
import duckdb
import mrfx.store as S
if __name__ == "__main__":
    d = I / "data" / "mrfx_store"
    part = d / "rates" / f"{S.file_key('beta_2026-05.json')}.parquet"
    con = duckdb.connect(str(d / "mrfx.duckdb"))
    con.execute(f"COPY (SELECT * REPLACE ('fee_schedule' AS negotiated_type, false AS is_dollar_rate) FROM read_parquet('{S.sql_path(part)}')) TO '{S.sql_path(part.with_suffix('.old'))}' (FORMAT parquet)")
    con.execute("UPDATE files SET row_rules = 2")
    con.close(); part.with_suffix(".old").replace(part)
    print("aged:", part.name)
