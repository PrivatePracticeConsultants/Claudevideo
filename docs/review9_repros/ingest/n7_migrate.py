"""Row-rules migration from stores at rules 0/1/2 with messy stored rows."""
import json, logging, sys, io
from h import *
import mrfx.store as S
from mrfx.ingest import ingest_file

def one(i, ntype="negotiated", rate=40.0, mods=None):
    p = {"negotiated_type": ntype, "negotiated_rate": rate, "billing_class": "professional", "service_code": ["11"]}
    if mods: p["billing_code_modifier"] = mods
    return {**HDR, "in_network": [item("97110", groups=[grp([f"12345678{i:02d}"], f"1234567{i:02d}")], prices=[p])]}

def tamper(st, name, replace_sql, rules, extra_sql=""):
    """Rewrite a part as an older build stored it and stamp its row_rules."""
    path = st.rates_dir / f"{S.file_key(name)}.parquet"
    tmp = path.with_suffix(".old")
    with st.connect() as con:
        con.execute(f"COPY (SELECT * REPLACE ({replace_sql}) FROM read_parquet('{S.sql_path(path)}') {extra_sql}) "
                    f"TO '{S.sql_path(tmp)}' (FORMAT parquet)")
        con.execute("UPDATE files SET row_rules = ? WHERE filename = ?", [rules, name])
    tmp.replace(path)
    return path

def reopen(cfg, st):
    st.close()
    buf = io.StringIO(); h = logging.StreamHandler(buf); h.setLevel(logging.INFO)
    lg = logging.getLogger("mrfx.store"); lg.addHandler(h); lg.setLevel(logging.INFO)
    st2 = S.Store(cfg.store_dir)
    lg.removeHandler(h)
    return st2, [l for l in buf.getvalue().splitlines() if "rewriting" in l or "could not" in l or "up to date" in l or "one-time" in l]

def dump(st, label):
    print(f"   {label}")
    for r in q(st, "SELECT f.filename, f.row_rules, f.status, f.rows_emitted FROM files f ORDER BY 1"):
        try:
            rows = q(st, "SELECT negotiated_type, is_dollar_rate, count(*) FROM rates WHERE source_file = ? GROUP BY 1, 2 ORDER BY 1, 2", r[0])
        except Exception as e:
            rows = f"ERR {type(e).__name__}: {str(e)[:80]}"
        print("     ", r, "->", rows)

def case_messy(rules):
    print(f"CASE messy types, files at row_rules={rules}")
    cfg = mkcfg(f"n7_messy{rules}", move_processed=False); st = Store(cfg.store_dir)
    names = []
    for i, (label, sql) in enumerate([
            ("Fee_Schedule", "'Fee_Schedule' AS negotiated_type, false AS is_dollar_rate"),
            (" Negotiated ", "' Negotiated ' AS negotiated_type, false AS is_dollar_rate"),
            ("NULL type", "NULL::VARCHAR AS negotiated_type, false AS is_dollar_rate"),
            ("percentage", "'percentage' AS negotiated_type, false AS is_dollar_rate"),
            ("fee-schedule", "'fee-schedule' AS negotiated_type, false AS is_dollar_rate"),
            ("ok", "negotiated_type AS negotiated_type")]):
        n = f"acme{i}_2026-05.json"; names.append(n)
        ingest_file(cfg, st, put(cfg, n, one(i)))
        tamper(st, n, sql, rules)
    # a 0-row file and a failed file at the old rules
    ingest_file(cfg, st, put(cfg, "acme_zero_2026-05.json", {**HDR, "in_network": [item("99213", groups=[grp(["1234567893"], "123456789")])]}))
    with st.connect() as con:
        con.execute("UPDATE files SET row_rules = ? WHERE filename = 'acme_zero_2026-05.json'", [rules])
    st.rebuild_rollups()
    print("   rates_by_tin dollar rows before:", q(st, "SELECT count(*) FROM rates_by_tin")[0][0])
    st2, lines = reopen(cfg, st)
    for l in lines: print("   log:", l[:230])
    dump(st2, "after reopen (a re-ingest would store 'fee schedule'/true, 'negotiated'/true, ''/false, 'percentage'/false)")
    print("   rollups_stale after migration:", st2.rollups_stale(), "| reason:", st2.rollup_stale_reason()[:100])
    try:
        st2.update_rollups_incremental()
    except Exception as e:
        print("   incremental failed:", e); st2.rebuild_rollups()
    print("   rates_by_tin rows after the next rollup:", q(st2, "SELECT count(*) FROM rates_by_tin")[0][0],
          "(3 dollar files expected: Fee_Schedule, Negotiated, fee-schedule + ok = 4)")
    st2.close()

def case_missing_and_bad(rules):
    print(f"CASE a missing part and an unreadable part, files at row_rules={rules}")
    cfg = mkcfg(f"n7_bad{rules}", move_processed=False); st = Store(cfg.store_dir)
    paths = {}
    for i in range(4):
        n = f"acme{i}_2026-05.json"
        ingest_file(cfg, st, put(cfg, n, one(i)))
        paths[n] = tamper(st, n, "'fee_schedule' AS negotiated_type, false AS is_dollar_rate", rules)
    paths["acme1_2026-05.json"].unlink()                       # part gone (rows_emitted still 1)
    raw = bytearray(paths["acme2_2026-05.json"].read_bytes())   # PAR1 kept at both ends, middle damaged
    for k in range(4, len(raw) - 4): raw[k] = 0x41
    paths["acme2_2026-05.json"].write_bytes(bytes(raw))
    st2, lines = reopen(cfg, st)
    for l in lines: print("   log:", l[:260])
    for r in q(st2, "SELECT filename, row_rules, status, rows_emitted FROM files ORDER BY 1"):
        p = paths[r[0]]
        got = "part missing"
        if p.exists():
            try:
                with st2.connect() as con:
                    got = con.execute(f"SELECT negotiated_type, is_dollar_rate FROM read_parquet('{S.sql_path(p)}')").fetchall()
            except Exception as e:
                got = f"unreadable ({type(e).__name__})"
        print("     ", r, "->", got)
    print("   second start:")
    st3, lines = reopen(cfg, st2)
    for l in lines: print("   log:", l[:200])
    print("     row_rules:", q(st3, "SELECT filename, row_rules FROM files ORDER BY 1"))
    st3.close()

def case_dup_pairs():
    print("CASE one stored spelling with BOTH is_dollar values in a part (rows from two older builds merged by a rule-1/2 rewrite)")
    cfg = mkcfg("n7_dup", move_processed=False); st = Store(cfg.store_dir)
    d = {**HDR, "in_network": [item("97110", groups=[grp(["1234567801"], "123456701")], rate=40.0),
                               item("97112", groups=[grp(["1234567802"], "123456702")], rate=50.0)]}
    ingest_file(cfg, st, put(cfg, "acme_2026-05.json", d))
    tamper(st, "acme_2026-05.json", "'fee_schedule' AS negotiated_type, (billing_code = '97110') AS is_dollar_rate", 2)
    print("   rows before:", q(st, "SELECT count(*) FROM rates")[0][0])
    st2, lines = reopen(cfg, st)
    for l in lines: print("   log:", l[:200])
    print("   rows after:", q(st2, "SELECT billing_code, negotiated_type, is_dollar_rate, count(*) FROM rates GROUP BY ALL ORDER BY 1"))
    st2.close()

if __name__ == "__main__":
    which = sys.argv[1:] or ["messy2", "messy1", "messy0", "bad2", "bad1", "dup"]
    for w in which:
        if w.startswith("messy"): case_messy(int(w[-1]))
        elif w.startswith("bad"): case_missing_and_bad(int(w[-1]))
        else: case_dup_pairs()
