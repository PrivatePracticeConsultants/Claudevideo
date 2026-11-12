"""Behaviour checks for rules claimed fixed in rounds 2-5 (pure functions, no store)."""
import duckdb, re
from mrfx import modifiers as M
from mrfx.store import looks_like_ssn, mask_tin, SSN_PREFIXES, _VALID_EIN_PREFIXES
from mrfx.api import _mask_tin_sql, _SSN_PREFIXES
from mrfx.parser import file_month_for, published_month_of

def main():
    # --- modifiers (round 3 P0.1)
    print("split 52,53      ->", M.normalize(["52,53"], []))
    print("[GP,59]==[59,GP] ->", M.normalize(["GP","59"], []) == M.normalize(["59","GP"], []), M.normalize(["59","GP"], []))
    print("Alabama POS list ->", M.normalize(["21","31","22","24"], ["01"]))
    print("Alabama POS list2->", M.normalize(["32","12","11","10"], ["01"]))
    print("First Choice     ->", M.normalize(["26","TC","RR","NU","UE"], []))
    print("ND bare 22       ->", M.normalize(["22"], ["11"]))
    print("idempotent       ->", M.normalize(*M.normalize(["21","31","22","24"], ["01"])[:2]) == ([], ["01","21","22","24","31"], False, False))
    print("base_sql         ->", M.base_sql())
    print("base+asst        ->", M.base_sql(include_assistant=True))
    # --- SSN masking: Python rule == SQL twin for every 2-digit prefix (round 2 P0.5 / round 6 one rule)
    con = duckdb.connect()
    mism = []
    for i in range(100):
        t = f"{i:02d}1234567"
        sql = con.execute(f"SELECT {_mask_tin_sql('t')} FROM (SELECT ? AS t)", [t]).fetchone()[0]
        if sql != mask_tin(t):
            mism.append((t, sql, mask_tin(t)))
    print("SSN rule SQL==py over 100 prefixes: mismatches =", mism)
    print("NPI (10 digits) never masked:", mask_tin("1234567890"), "| 8 digits:", mask_tin("12345678"))
    hard = ('00','07','08','09','17','18','19','28','29','49','69','70','78','79','89','96','97')
    print("TIN_DIRECTORY hardcoded prefixes == SSN_PREFIXES:", tuple(sorted(hard)) == tuple(sorted(SSN_PREFIXES)), len(SSN_PREFIXES))
    # list form (entity): [EIN; SSN]
    print("list form:", con.execute(f"SELECT {_mask_tin_sql('t')} FROM (SELECT ? AS t)", ["123456789; 001234567"]).fetchone()[0])
    # --- month rule (round 3 P0.6)
    print("future header 2026-12-01 read 2026-11, name 2026-11-01 ->", file_month_for("2026-12-01", "2026-11-01_x.json", "2026-11"))
    print("future header, no name date ->", file_month_for("2026-12-01", "x.json", "2026-11"))
    print("header 2026-08-25, name 2026-10-01 ->", file_month_for("2026-08-25", "2026-10-01_x.json", "2026-11"))
    print("no header, name ->", file_month_for(None, "2026-10-01_x.json", "2026-11"))
    print("nothing ->", file_month_for(None, "x.json", "2026-11"))
    print("published_month_of ->", published_month_of("2026-10-01_x.json", "2026-11"), published_month_of("x.json", "2026-11"))

if __name__ == "__main__":
    main()
