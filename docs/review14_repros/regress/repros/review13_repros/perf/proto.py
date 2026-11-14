"""Prototype queries for the speed opportunities (read-only on the 99M
store, server stopped). Each prints the current statement's time, the
prototype's time, and whether the numbers are identical."""
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from house import open_ro  # noqa: E402

S = "/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/scale4n/store"
B = Path(__file__).resolve().parent / "bodies"


def timed(con, label, sql, params=()):
    t = time.perf_counter()
    rows = con.execute(sql, list(params)).fetchall()
    el = time.perf_counter() - t
    print(f"  {el:7.2f}s  {label}  ({len(rows)} rows)", flush=True)
    return rows, el


def main():
    import mrfx.api as api
    from mrfx.config import MrfxConfig
    from mrfx.store import Store
    P = Path(S).parent
    cfg = MrfxConfig(inbox_dir=P / "in", processed_dir=P / "p", failed_dir=P / "f", store_dir=P / "store",
                     downloads_dir=P / "dl", entity_map_path=P / "em.yaml", port=8399, enrichment={"mode": "off"})
    st = Store(cfg.store_dir, keep_warm=True)
    api.TRUSTED_HOSTS.add("testserver")
    api.create_app(cfg, st)  # registers views etc.
    with st.connect() as con:
        con.execute("SET threads TO 4")
        # ---------------- 1. entity drawer: whole-store entity aggregation vs member-TIN-first
        print("1. entity drawer 'PRACTICE 18 · OR'")
        key = "PRACTICE 18 · OR"
        cur, t_cur = timed(con, "current: SELECT * FROM (_ENTITY_REL) WHERE unit_id = ?",
                           f"SELECT * FROM ({api.GRAIN_REL['entity']}) WHERE unit_id = ? ORDER BY billing_code, payer, modifier_set, file_month, billing_class, service_code_set, is_dollar_rate, tin_value, negotiated_rate", [key])
        # prototype: member TINs first (entity key is name + primary_state for org names), then the SAME entity relation over only their spine rows
        t = time.perf_counter()
        tins = [r[0] for r in con.execute(
            "SELECT tin_value FROM tin_directory WHERE entity_kind = 'org' AND display_name || coalesce(' · ' || primary_state, '') = ?", [key]).fetchall()]
        narrowed = api._entity_rel(api._TIN_REL.replace("FROM rates_by_tin t", "FROM (SELECT * FROM rates_by_tin WHERE tin_value IN (SELECT unnest(?::VARCHAR[]))) t"))
        pro = con.execute(f"SELECT * FROM ({narrowed}) WHERE unit_id = ? ORDER BY billing_code, payer, modifier_set, file_month, billing_class, service_code_set, is_dollar_rate, tin_value, negotiated_rate", [tins, key]).fetchall()
        t_pro = time.perf_counter() - t
        print(f"  {t_pro:7.2f}s  prototype: resolve member TINs ({tins}) then aggregate their spine rows only ({len(pro)} rows)")
        print("  identical rows:", cur == pro, f" speed-up {t_cur / t_pro:.0f}x")
        # ---------------- 2. validate: raw scan with OR vs file-pruned
        print("2. /api/validate 100049428 / 97110")
        cur, t_cur = timed(con, "current: FROM rates WHERE billing_code=? AND (tin_value=? OR npi=?)",
                           "SELECT payer, tin_value, npi, billing_code, negotiated_rate, file_month, source_file FROM rates WHERE billing_code = ? AND (tin_value = ? OR npi = ?) ORDER BY ALL", ["97110", "100049428", "100049428"])
        t = time.perf_counter()
        files = [r[0] for r in con.execute("SELECT DISTINCT unnest(string_split(source_files, ';')) FROM rates_by_tin WHERE tin_value = ? AND billing_code = ?", ["100049428", "97110"]).fetchall()]
        pro = con.execute("SELECT payer, tin_value, npi, billing_code, negotiated_rate, file_month, source_file FROM rates WHERE source_file IN (SELECT unnest(?::VARCHAR[])) AND billing_code = ? AND (tin_value = ? OR npi = ?) ORDER BY ALL", [files, "97110", "100049428", "100049428"]).fetchall()
        t_pro = time.perf_counter() - t
        print(f"  {t_pro:7.2f}s  prototype (TIN half): files from the spine ({len(files)} of 45) then the raw scan of those files only ({len(pro)} rows); identical: {cur == pro}; the NPI half still needs its own lookup (rate_npi_set has no TIN column)")
        # ---------------- 3. overview national from the materialised medians
        print("3. overview (national) payer index from practice_medians_*_v1 instead of a spine temp table")
        from mrfx.benchmark import PAYER_PRACTICE_MEDIANS, PRACTICE_MEDIANS, _house_table_sql
        pmp = st.spine_materialised(PAYER_PRACTICE_MEDIANS, *_house_table_sql(True))
        pmr = st.spine_materialised(PRACTICE_MEDIANS, *_house_table_sql(False))
        pro, t_pro = timed(con, "prototype payer_index", f"""
            WITH base AS (SELECT payer, billing_code, median(rate) AS payer_med FROM {pmp} GROUP BY 1, 2 HAVING count(DISTINCT tin_value) >= 3),
                 mkt AS (SELECT billing_code, median(rate) AS mkt_med FROM {pmr} GROUP BY 1),
                 pcnt AS (SELECT payer, count(DISTINCT tin_value) AS practices FROM {pmp} GROUP BY 1)
            SELECT b.payer, round(median(b.payer_med / m.mkt_med), 3) AS idx, count(DISTINCT b.billing_code), any_value(p.practices)
            FROM base b JOIN mkt m USING (billing_code) JOIN pcnt p USING (payer) WHERE m.mkt_med > 0
            GROUP BY b.payer HAVING count(DISTINCT b.billing_code) >= 3 ORDER BY idx DESC, b.payer""")
        ov = json.load(open(B / "overview.json"))["payer_index"]
        same = [(r[0], r[1], r[2], r[3]) for r in pro] == [(e["payer"], e["index"], e["codes"], e["practices"]) for e in ov]
        print("  identical to the served overview payer_index:", same, "(served cold 26.9 s over HTTP; 12.8 s in-process warm disk)")
        top, t_top = timed(con, "prototype top_codes", f"SELECT billing_code, count(DISTINCT tin_value) n, median(rate) FROM {pmr} GROUP BY 1 ORDER BY n DESC, 1 LIMIT 15")
        ovt = json.load(open(B / "overview.json"))["top_codes"]
        print("  top_codes identical:", [(r[0], r[1], round(r[2], 2)) for r in top] == [(e["billing_code"], e["practices"], e["median"]) for e in ovt])
        # ---------------- 4. tin -> npi pairs: raw scan vs a side table
        print("4. (tin_value, npi) pairs of MO practices — territory/local, market/sizing, contracts/new-to-network, steal-share all read this from raw rates")
        cur, t_cur = timed(con, "current: SELECT DISTINCT tin_value, npi FROM rates WHERE tin_value IN (MO tins)",
                           "SELECT DISTINCT tin_value, npi FROM rates WHERE tin_value IN (SELECT tin_value FROM tin_directory WHERE list_contains(states, 'MO')) AND npi IS NOT NULL")
        t = time.perf_counter()
        con.execute("CREATE TEMP TABLE tin_npi AS SELECT DISTINCT tin_value, npi FROM rates WHERE npi IS NOT NULL")
        t_build = time.perf_counter() - t
        n = con.execute("SELECT count(*) FROM tin_npi").fetchone()[0]
        pro, t_pro = timed(con, "prototype: the same from a (tin_value, npi) side table",
                           "SELECT tin_value, npi FROM tin_npi WHERE tin_value IN (SELECT tin_value FROM tin_directory WHERE list_contains(states, 'MO'))")
        print(f"  side table: {n:,} pairs, built once per spine generation in {t_build:.1f}s (one raw scan, like tin_zip_place); identical sets: {set(cur) == set(pro)}")
        # ---------------- 5. new-to-network: the one statement, is it cacheable per generation?
        print("5. new-to-network statement (see prof.json for its text) — it has no inputs but the generation when zip is null: cache per (generation, therapy_only) like changes/trajectory")
    print("done")


if __name__ == "__main__":
    main()
