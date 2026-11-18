"""Entity drawer rows (all months) for 'PRACTICE 18 · OR': rate = median of member TINs' variant rates, per month."""
import json, statistics, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from house import open_ro
S = "/tmp/claude-0/-home-user-Claudevideo/4633d594-0139-5f8e-a935-ae9c76b72000/scratchpad/scale4n/store"
if __name__ == "__main__":
    con = open_ro(S); con.execute("SET threads TO 4")
    ent = ["100000018", "100111018"]
    con.execute("""CREATE TEMP TABLE var AS
        SELECT payer, tin_value, tin_is_really_npi, billing_code,
               coalesce(array_to_string(billing_code_modifier, '|'), '') AS mods, billing_class,
               coalesce(array_to_string(service_code, '|'), '') AS pos, file_month, is_dollar_rate,
               round(coalesce(median(DISTINCT negotiated_rate) FILTER (NOT is_dollar_rate OR negotiated_rate > 0.01), median(DISTINCT negotiated_rate)), 4) AS rate
        FROM rates WHERE tin_value IN ('100000018', '100111018') GROUP BY ALL""")
    E = json.load(open(Path(__file__).resolve().parent / "bodies" / "entity_drawer_entity.json"))
    n = bad = 0
    for r in E["rates"]:
        vals = [x[0] for x in con.execute("SELECT rate FROM var WHERE payer=? AND billing_code=? AND mods=? AND billing_class=? AND pos=? AND file_month=? AND is_dollar_rate=?",
                                          [r["payer"], r["billing_code"], r["modifier_set"], r["billing_class"], r["service_code_set"], r["file_month"], r["is_dollar_rate"]]).fetchall()]
        exp = round(statistics.median(vals), 4) if vals else None
        n += 1
        if exp is None or abs(exp - r["negotiated_rate"]) > 0.00011 or len(vals) != r["tin_count"]:
            bad += 1
            if bad <= 5: print("DIFF", r["payer"], r["billing_code"], r["modifier_set"], r["service_code_set"], r["file_month"], "api", r["negotiated_rate"], r["tin_count"], "members", vals)
    print(f"entity drawer rows checked {n}, mismatches {bad}; sample api row:", {k: E["rates"][0][k] for k in ("payer","billing_code","modifier_set","service_code_set","file_month","negotiated_rate","tin_count","rate_variants")})
