"""Read-only look at the 99M store BEFORE the server starts (one process at a time)."""
import duckdb, sys, json
S = "/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/scale4n/store"
if __name__ == "__main__":
    con = duckdb.connect(S + "/mrfx.duckdb", read_only=True)
    q = lambda s, *a: con.execute(s, list(a)).fetchall()
    print("tables", q("select table_name, table_type from information_schema.tables order by 1"))
    print("meta", q("select * from meta") if q("select count(*) from information_schema.tables where table_name='meta'")[0][0] else None)
    print("files", q("select count(*), min(file_month), max(file_month), count(distinct payer) from files"))
    print("files sample", q("select filename, payer, file_month, status, rows_emitted from files order by 1 limit 5"))
    print("spine rows", q("select count(*) from rates_by_tin_tbl"))
    print("payers", q("select payer, count(*) from rates_by_tin_tbl group by 1 order by 1"))
    print("months", q("select file_month, count(*) from rates_by_tin_tbl group by 1 order by 1"))
    print("dir cols", [c[0] for c in con.execute("describe tin_directory_tbl").fetchall()])
    print("spine cols", [c[0] for c in con.execute("describe rates_by_tin_tbl").fetchall()])
    print("dir count", q("select count(*), count(display_name), sum(is_therapy::int) from tin_directory_tbl"))
    print("states", q("select s, count(*) from (select unnest(states) s from tin_directory_tbl) group by 1 order by 2 desc"))
    print("codes", q("select billing_code, count(*) from rates_by_tin_tbl group by 1 order by 1"))
    print("classes", q("select billing_class, count(*) from rates_by_tin_tbl group by 1"))
    print("mods", q("select modifier_set, count(*) from rates_by_tin_tbl group by 1 order by 2 desc limit 10"))
    print("tin sample MO Payer 03", q("""SELECT t.tin_value, d.display_name, d.npi_count, d.states, d.cities, d.primary_state
        FROM rates_by_tin_tbl t JOIN tin_directory_tbl d USING (tin_value)
        WHERE t.payer = 'Payer 03' AND list_contains(d.states, 'MO')
        GROUP BY ALL ORDER BY count(DISTINCT t.billing_code) DESC, count(*) DESC, 1 LIMIT 3"""))
    print("npi for tin", q("select min(npi) from rates where tin_value = (SELECT t.tin_value FROM rates_by_tin_tbl t JOIN tin_directory_tbl d USING (tin_value) WHERE t.payer = 'Payer 03' AND list_contains(d.states, 'MO') GROUP BY 1 ORDER BY count(DISTINCT t.billing_code) DESC, count(*) DESC, 1 LIMIT 1)"))
    print("names shared across states", q("select display_name, count(*) from tin_directory_tbl group by 1 having count(*)>1 order by 2 desc limit 3"))
    print("entity_map", q("select count(*) from entity_map"))
    print("clients", q("select * from clients limit 5") if q("select count(*) from information_schema.tables where table_name='clients'")[0][0] else "no clients table")
    print("mpfs", q("select count(*) from mpfs") if q("select count(*) from information_schema.tables where table_name='mpfs'")[0][0] else "no mpfs table")
    for t in ("peersets","engagements","utilization","demographics","floors","hospital_charges","medicare_eligibility","medicare_referrals","plan_index","content_fingerprints","url_queue"):
        if q("select count(*) from information_schema.tables where table_name=?", t)[0][0]:
            print(t, q(f"select count(*) from {t}"))
    print("zip sample MO", q("select postal_code, count(*) from npi_directory where state='MO' group by 1 order by 2 desc limit 3"))
    print("npi_directory", q("select count(*), count(taxonomy), count(state) from npi_directory") if q("select count(*) from information_schema.tables where table_name='npi_directory'")[0][0] else "none")
    print("npi_directory cols", [c[0] for c in con.execute("describe npi_directory").fetchall()])
    print("npi dir sample", q("select * from npi_directory limit 2"))
