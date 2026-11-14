"""'Therapy market report — ND' on the 32M real store lists Tampa, Birmingham, Tuscaloosa ... as ND city markets.
Cause: territory.local_rate_map explodes tin_directory.cities (EVERY city of a TIN that has ANY location in the state).
Prototype fix (scratch only): a TIN's cities IN THE STATE = cities of its NPIs whose NPPES state is that state."""
import sys, time
sys.path.insert(0, "/home/user/Claudevideo")
if __name__ == "__main__":
    from mrfx.store import Store
    from mrfx.territory import local_rate_map, MIN_CELL
    from mrfx.benchmark import _market_where, _rates_relation, normalize_market, resolve_plan_scope
    store = Store("/tmp/mrfx_review10/consulting/store")
    for st, code in (("ND", "97110"), ("WY", "97110"), ("MA", "97110")):
        t0 = time.perf_counter()
        d = local_rate_map(store, code, {"month": "latest"}, state=st)
        t_old = time.perf_counter() - t0
        with store.connect() as con:
            real = {r[0] for r in con.execute("SELECT DISTINCT upper(trim(city)) FROM npi_directory WHERE state = ?", [st]).fetchall()}
        old = [(r["city"], r["n_practices"], r["median_rate"]) for r in d["cities"]]
        alien = [c for c, _n, _m in old if c not in real]
        print(f"== {st} {code}: shipped: {len(old)} cities, {len(alien)} with NO provider located in {st}: {alien[:8]}  ({t_old:.2f}s)")
        print("   shipped headline:", d["headline"])
        m = resolve_plan_scope(store, normalize_market({"month": "latest", "codes": [code], "state": st}))
        where, params = _market_where(m, False, False)
        rel = _rates_relation(m)
        sql = f"""
        WITH base AS (
            SELECT t.tin_value, median(t.negotiated_rate) AS rate
            FROM {rel} t JOIN tin_directory td USING (tin_value) WHERE {where} GROUP BY t.tin_value),
        tin_city AS (
            SELECT DISTINCT r.tin_value, upper(trim(n.city)) AS city
            FROM (SELECT DISTINCT tin_value, npi FROM rates WHERE tin_value IN (SELECT tin_value FROM base)) r
            JOIN npi_directory n ON n.npi = r.npi WHERE n.state = ? AND coalesce(trim(n.city), '') <> '')
        SELECT city, count(DISTINCT tin_value) n, round(median(rate), 2) med
        FROM tin_city JOIN base USING (tin_value) GROUP BY city HAVING count(DISTINCT tin_value) >= ?
        ORDER BY med DESC, city LIMIT 60"""
        t0 = time.perf_counter()
        with store.connect() as con:
            new = con.execute(sql, [*params, st, MIN_CELL]).fetchall()
        print(f"   prototype: {len(new)} cities: {new[:8]}  ({time.perf_counter() - t0:.2f}s)")
