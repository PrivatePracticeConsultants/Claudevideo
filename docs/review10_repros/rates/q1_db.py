"""Read-only questions to the 32M copy (server stopped)."""
import sys
sys.path.insert(0, "/home/user/Claudevideo")
from mrfx.config import load_mrfx_config
from mrfx.store import Store
from mrfx.benchmark import _market_where, _rates_relation, normalize_market

def main():
    cfg = load_mrfx_config("/tmp/mrfx_review10/rates/big/mrfx.yaml")
    store = Store(cfg.store_dir)
    m = normalize_market({"month": "latest"})
    where, params = _market_where(m, False, False)
    rel = _rates_relation(m)
    with store.connect() as con:
        print("== Overview 'Typical rate by discipline': which codes are behind each discipline's one number")
        for d in ("pt", "ot", "slp"):
            rows = con.execute(f"""
                WITH per AS (SELECT t.billing_code, t.tin_value, median(t.negotiated_rate) r
                             FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
                             WHERE {where} AND lower(t.discipline) = ? GROUP BY 1, 2)
                SELECT billing_code, count(*) n, round(median(r), 2) med FROM per GROUP BY 1 ORDER BY n DESC, 1 LIMIT 8""",
                [*params, d]).fetchall()
            tot = con.execute(f"""SELECT count(DISTINCT t.billing_code), round(min(t.negotiated_rate),2), round(max(t.negotiated_rate),2)
                 FROM {rel} t LEFT JOIN tin_directory td USING (tin_value) WHERE {where} AND lower(t.discipline) = ?""", [*params, d]).fetchone()
            print("  ", d, "codes:", tot[0], "rate range", tot[1], "-", tot[2], "| biggest:", rows)
        print("  shared codes left out (discipline 'unspecified'):", con.execute(f"""
            SELECT count(*), count(DISTINCT t.billing_code) FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
            WHERE {where} AND lower(t.discipline) NOT IN ('pt','ot','slp')""", params).fetchone())
        print("== states list")
        st = store.available_states()
        from mrfx.states import state_code_or_none
        print("  ", len(st), "values; not a US state/territory code:", [s for s in st if len(s) != 2])
        print("== contract lines that differ only by class / POS set (twin rows on Rate changes)")
        print("  ", con.execute("""
            WITH g AS (SELECT payer, tin_value, billing_code, modifier_set, file_month, count(*) n
                       FROM rates_by_tin WHERE is_latest AND is_dollar_rate AND billing_class = 'professional'
                       GROUP BY ALL)
            SELECT count(*) AS groups, count(*) FILTER (n > 1) AS with_twins,
                   round(100.0 * count(*) FILTER (n > 1) / count(*), 1) AS pct FROM g""").fetchone())
        print("== NPI grain size:", con.execute("select count(*) from rates_dedup").fetchone() if True else "")

if __name__ == "__main__":
    main()
