"""A/B prototype: Explorer entity-grain page = pick the page's group keys first (median only),
then build the list columns for those 100 groups. 32M copy, server stopped. Read-only."""
import sys, time, os, json, hashlib
sys.path.insert(0, "/home/user/Claudevideo")
from mrfx.config import load_mrfx_config
from mrfx.store import Store
import mrfx.api as api

KEYS = "entity_key, payer, billing_code, discipline, modifier_set, billing_class, service_code_set, is_dollar_rate"

def proto(fs, sort_dir="DESC", limit=100, offset=0):
    tin_rel = api._TIN_REL_LATEST
    inner = f"SELECT * FROM ({tin_rel}) WHERE {fs.where_of('key', 'unit')}"
    keyed = api._entity_keyed(inner)
    page = f"""
      SELECT {KEYS},
             round(coalesce(median(negotiated_rate) FILTER (NOT is_dollar_rate OR negotiated_rate > 0.01),
                            median(negotiated_rate)), 4) AS negotiated_rate,
             max(file_month) AS file_month
      FROM kk GROUP BY {KEYS}
      HAVING is_dollar_rate AND negotiated_rate > 0.01
      ORDER BY negotiated_rate {sort_dir} NULLS LAST, entity_key, billing_code, modifier_set, payer, file_month,
               billing_class, service_code_set, is_dollar_rate
      LIMIT {limit} OFFSET {offset}"""
    sel = f"""SELECT s.* FROM kk s SEMI JOIN pg ON
              s.entity_key = pg.entity_key AND s.payer = pg.payer AND s.billing_code = pg.billing_code
              AND s.discipline IS NOT DISTINCT FROM pg.discipline AND s.modifier_set = pg.modifier_set
              AND s.billing_class IS NOT DISTINCT FROM pg.billing_class AND s.service_code_set = pg.service_code_set
              AND s.is_dollar_rate = pg.is_dollar_rate"""
    # _entity_rel re-keys its input; feed it the page's TIN rows only (drop our entity_key first)
    ent = api._entity_rel(f"SELECT * EXCLUDE (entity_key) FROM ({sel})", True)
    return (f"WITH kk AS MATERIALIZED ({keyed}), pg AS MATERIALIZED ({page}) "
            f"SELECT * FROM ({ent}) WHERE {fs.where_of('ident', 'row')} {api.order_sql('negotiated_rate', sort_dir)}")

def main():
    cfg = load_mrfx_config("/tmp/mrfx_review10/rates/big/mrfx.yaml")
    store = Store(cfg.store_dir)
    for extra in ({}, {"state": "AL"}):
        fs = api.FilterSet({"grain": "entity", "latest": "1", "dollar_only": "1", **extra})
        cur = f"{api.rel_sql('entity', fs)} {api.order_sql('negotiated_rate', 'desc')} LIMIT ? OFFSET ?"
        with store.connect() as con:
            for off in (0, 49900):
                t = time.time(); a = con.execute(cur, [*fs.params, 100, off]).fetchall(); ta = time.time() - t
                # the prototype binds key+unit params once for kk, then ident+row params
                ku = [p for k in ("key", "unit") for p in fs._buckets[k][1]]
                ir = [p for k in ("ident", "row") for p in fs._buckets[k][1]]
                t = time.time(); b = con.execute(proto(fs, offset=off), [*ku, *ir]).fetchall(); tb = time.time() - t
                print(extra, "offset", off, "current %.2fs  prototype %.2fs  identical rows: %s  load %s" % (ta, tb, a == b, os.getloadavg()[0]))

if __name__ == "__main__":
    main()
