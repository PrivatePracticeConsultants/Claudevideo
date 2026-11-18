"""How common is 'one NPPES organization name, several TINs, several states' on the 32M real store copy?"""
import duckdb
if __name__ == "__main__":
    con = duckdb.connect("/tmp/mrfx_review10/consulting/store/mrfx.duckdb", read_only=True)
    print(con.execute("SELECT count(*), count(DISTINCT display_name) FROM tin_directory WHERE display_name IS NOT NULL").fetchall())
    q = """WITH n AS (SELECT lower(display_name) AS nm, count(*) AS tins, count(DISTINCT primary_state) AS states, sum(npi_count) AS npis
                    FROM tin_directory WHERE display_name IS NOT NULL AND entity_kind = 'org' GROUP BY 1)
           SELECT count(*) FILTER (WHERE tins >= 2) AS names_multi_tin, count(*) FILTER (WHERE states >= 2) AS names_multi_state,
                  sum(tins) FILTER (WHERE states >= 2) AS tins_under_them, count(*) AS names FROM n"""
    print(con.execute(q).fetchall())
    print(con.execute("""SELECT lower(display_name) nm, count(*) tins, list(DISTINCT primary_state) st, sum(npi_count) FROM tin_directory
        WHERE display_name IS NOT NULL AND entity_kind='org' GROUP BY 1 HAVING count(DISTINCT primary_state) >= 2 ORDER BY 4 DESC, 1 LIMIT 12""").fetchall())
    print(con.execute("DESCRIBE tin_directory").fetchall())
