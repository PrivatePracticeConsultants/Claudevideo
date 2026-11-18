"""Entity grain: the entity rate is the median of its member TINs' rates
(same payer / code / modifier set / class / POS set, each TIN at its newest
month). Compared with /api/rates?grain=entity rows from the real store."""
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from house import house_ctes, open_ro  # noqa: E402

S = "/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/audit13/perf/realstore/store"

if __name__ == "__main__":
    out = Path(sys.argv[1])
    con = open_ro(S)
    multi = con.execute("""SELECT display_name, primary_state, list(tin_value ORDER BY tin_value) FROM tin_directory
        WHERE entity_kind = 'org' AND display_name NOT LIKE 'TIN %' GROUP BY 1, 2 HAVING count(*) > 1 ORDER BY count(*) DESC, 1 LIMIT 8""").fetchall()
    print("multi-TIN entities (name, state, tins):", multi)
    rows = json.load(open(out / "rates_entity_97110.json"))["rows"]
    con.execute(f"CREATE TEMP TABLE v AS WITH {house_ctes()} SELECT * FROM variants")
    n = bad = 0
    for r in rows:
        if (r.get("tin_count") or 1) < 2:
            continue
        tins = [t.strip() for t in str(r["tin_value"]).split(";")]
        got = r["negotiated_rate"]
        vals = [x[0] for x in con.execute(
            "SELECT rate FROM v WHERE payer=? AND billing_code=? AND mods=? AND billing_class=? AND pos=? AND is_dollar_rate "
            "AND tin_value IN (SELECT unnest(?::VARCHAR[])) AND NOT tin_is_really_npi",
            [r["payer"], r["billing_code"], r["modifier_set"], r["billing_class"], r["service_code_set"], tins]).fetchall()]
        exp = round(statistics.median(vals), 4) if vals else None
        n += 1
        ok = exp is not None and abs(exp - got) < 0.00011
        if not ok:
            bad += 1
            print("DIFF", r["unit_id"], r["payer"], r["billing_code"], r["modifier_set"], r["service_code_set"], "api", got, "members", vals, "tins", tins)
        elif n <= 5:
            print("OK  ", r["unit_id"], r["payer"], r["billing_code"], r["modifier_set"], r["service_code_set"], "api", got, "member rates", vals)
    print("multi-TIN entity rows checked", n, "mismatches", bad)
