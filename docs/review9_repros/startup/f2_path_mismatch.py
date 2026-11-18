"""0.7.2 _migrate_types_only maps DuckDB's `filename` column back to the stored file by
exact string. If the two spellings ever differ (simulated here: DuckDB answers with a
'./'-normalised or differently-separated path, e.g. another letter case, as a case-insensitive Windows path may), what happens to a
part that still holds 'fee_schedule' rows?"""
import json, shutil, sys
from pathlib import Path
sys.path.insert(0, "/home/user/Claudevideo")
import mrfx.store as S
from mrfx.config import MrfxConfig
from mrfx.ingest import ingest_file
R = Path("/tmp/mrfx_review9/startup/f2")
def doc():
    return {"reporting_entity_name": "Acme", "version": "1", "last_updated_on": "2026-05-01",
            "in_network": [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code": "97110",
              "negotiated_rates": [{"provider_groups": [{"npi": ["1234567893"], "tin": {"type": "ein", "value": "123456789"}}],
                 "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 50.0, "billing_class": "professional"}]}]}]}
class Proxy:
    def __init__(self, con): self._c = con
    def __getattr__(self, k): return getattr(self._c, k)
    def __enter__(self): self._c.__enter__(); return self
    def __exit__(self, *a): return self._c.__exit__(*a)
    def execute(self, sql, params=None):
        r = self._c.execute(sql, params) if params is not None else self._c.execute(sql)
        if "filename = true" in sql:
            rows = [(str(f).replace("/rates/", "/RATES/"), t, d) for f, t, d in r.fetchall()]
            class F:
                def fetchall(s): return rows
            return F()
        return r
if __name__ == "__main__":
    shutil.rmtree(R, ignore_errors=True)
    c = MrfxConfig(inbox_dir=R/"in", processed_dir=R/"p", failed_dir=R/"f", store_dir=R/"store",
                   downloads_dir=R/"dl", entity_map_path=R/"em.yaml"); c.ensure_dirs(); c.move_processed = False
    st = S.Store(c.store_dir)
    p = c.inbox_dir / "acme_2026-05.json"; p.write_text(json.dumps(doc())); ingest_file(c, st, p)
    part = st.rates_dir / f"{S.file_key(p.name)}.parquet"
    with st.connect() as con:
        con.execute(f"COPY (SELECT * REPLACE ('fee_schedule' AS negotiated_type, false AS is_dollar_rate) FROM read_parquet('{S.sql_path(part)}')) TO '{S.sql_path(part.with_suffix('.old'))}' (FORMAT parquet)")
        con.execute("UPDATE files SET row_rules = 2")
    part.with_suffix(".old").replace(part); st.close()
    real = S.Store.connect
    S.Store.connect = lambda self, *a, **k: Proxy(real(self, *a, **k))
    st2 = S.Store(c.store_dir)
    S.Store.connect = real
    with st2.connect() as con:
        print("stored rows  :", con.execute("SELECT negotiated_type, is_dollar_rate FROM rates").fetchall())
        print("files.row_rules:", con.execute("SELECT row_rules FROM files").fetchall(), "(3 = 'migrated')")
