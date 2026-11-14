"""After the rule-3 migration turns 'fee_schedule' rows into dollar rows, do the
analytics tables pick them up at the next start-up catch-up?"""
from h import *
import mrfx.store as S
from mrfx.ingest import ingest_file
from n7_migrate import one, tamper, reopen

if __name__ == "__main__":
    cfg = mkcfg("n7b", move_processed=False); st = Store(cfg.store_dir)
    ingest_file(cfg, st, put(cfg, "acme0_2026-05.json", one(0, rate=40.0)))
    ingest_file(cfg, st, put(cfg, "acme1_2026-05.json", one(1, rate=60.0)))
    tamper(st, "acme0_2026-05.json", "'fee_schedule' AS negotiated_type, false AS is_dollar_rate", 2)
    st.rebuild_rollups()
    cols = [r[0] for r in q(st, "DESCRIBE rates_by_tin")]
    print("rates_by_tin columns:", cols)
    sel = "SELECT tin_value, negotiated_rate, " + ", ".join(c for c in cols if c in ("is_dollar_rate", "negotiated_type")) + " FROM rates_by_tin ORDER BY 1"
    print("before migration:", q(st, sel))
    st2, lines = reopen(cfg, st)
    print("stale:", st2.rollups_stale(), st2.rollup_stale_reason()[:90])
    print("right after reopen (no rollup yet):", q(st2, sel))
    r = st2.update_rollups_incremental()
    print("incremental ->", r)
    print("after incremental:", q(st2, sel))
