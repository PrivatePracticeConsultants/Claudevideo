"""mrfx/modifiers.py — the modifier-field rule — and the one-time rewrite of
parts written under the old rule (Store._migrate_modifier_fields)."""

from __future__ import annotations

import json

import duckdb
import pytest

from mrfx.modifiers import base_sql, is_pos_list, normalize, split_modifiers


@pytest.mark.parametrize("raw, pos, want", [
    # BCBS Alabama: POS codes in the modifier field (21/31/11/12/10 are no modifier)
    (["21", "31", "22", "24"], ["01"], ([], ["01", "21", "22", "24", "31"], False, True)),
    (["32", "12", "11", "10"], ["01"], ([], ["01", "10", "11", "12", "32"], False, True)),
    (["11"], [], ([], ["11"], False, True)),
    (["02", "17", "92"], ["93", "99"], ([], ["02", "17", "92", "93", "99"], False, True)),
    # BCBS ND / WY: valid numeric modifiers stay modifiers, even POS-looking ones
    (["22"], ["11"], (["22"], ["11"], False, False)),
    (["55"], ["CSTM-00"], (["55"], ["CSTM-00"], False, False)),
    (["53"], [], (["53"], [], False, False)),
    # BCBS ND: two modifiers in one element
    (["52,53"], ["CSTM-00"], (["52", "53"], ["CSTM-00"], True, False)),
    ([" gp; 59 "], [], (["59", "GP"], [], True, False)),
    (["GP 59", "GP"], [], (["59", "GP"], [], True, False)),
    # a split that yields POS codes is still a POS list
    (["11,12"], [], ([], ["11", "12"], True, True)),
    # First Choice: a real (different) published rate under a modifier list — kept
    (["26", "TC", "RR", "NU", "UE"], [], (["26", "NU", "RR", "TC", "UE"], [], False, False)),
    # mixed letters + numbers: not a POS list
    (["GP", "11"], [], (["11", "GP"], [], False, False)),
    # ordinary cases unchanged
    (["59", "GP"], ["11"], (["59", "GP"], ["11"], False, False)),
    ([], ["11"], ([], ["11"], False, False)),
    (["", "  "], [], ([], [], False, False)),
    ([97], [], (["97"], [], False, False)),          # numeric JSON modifier, valid
    ([11], [], ([], ["11"], False, True)),           # numeric JSON POS code
])
def test_normalize_cases(raw, pos, want):
    assert normalize(raw, pos) == want


@pytest.mark.parametrize("raw, pos", [
    (["21", "31"], ["01"]), (["52,53"], ["11"]), (["gp", "59"], []), (["11,GP"], []),
    (["26", "TC"], []), ([], []),
])
def test_normalize_is_idempotent(raw, pos):
    m, p, _, _ = normalize(raw, pos)
    assert normalize(m, p)[:2] == (m, p)


def test_split_and_pos_helpers():
    assert split_modifiers(None) == []
    assert split_modifiers("52,53") == ["52", "53"]
    assert not is_pos_list([])
    assert not is_pos_list(["22", "24"])          # both valid modifiers
    assert is_pos_list(["21"])                    # deleted modifier, POS 21


def test_base_sql_truth_table():
    sets = ["", "GP", "GO|GP", "GN|GO|GP", "59|GP", "GP|KX", "CQ|GP", "CO", "52|53",
            "26|NU|RR|TC|UE", "GP|XE"]
    con = duckdb.connect()
    con.execute("CREATE TABLE t (modifier_set VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?)", [[s] for s in sets])
    base = {r[0] for r in con.execute(f"SELECT modifier_set FROM t WHERE {base_sql()}").fetchall()}
    assert base == {"", "GP", "GO|GP", "GN|GO|GP"}
    asst = {r[0] for r in con.execute(
        f"SELECT modifier_set FROM t WHERE {base_sql(include_assistant=True)}").fetchall()}
    assert asst == {"", "GP", "GO|GP", "GN|GO|GP", "CQ|GP", "CO"}


def _old_rule(raw, pos):
    """The pre-round-3 parser: strip/upper/dedup/sort only."""
    return sorted({str(m).strip().upper() for m in raw if str(m).strip()}), pos, False, False


def _rows(path):
    con = duckdb.connect()
    return sorted(con.execute(
        f"SELECT * EXCLUDE (ingested_at) FROM read_parquet('{path}/*.parquet')").fetchall(),
        key=repr)


def test_old_store_is_fixed_once_at_open_exactly_like_a_reingest(cfg, tmp_path, monkeypatch):
    """A store whose parts were written by the old parser is rewritten at open
    to exactly what the new parser writes — no re-download — its payer is
    queued for the incremental rollup, the Files tab's QA line gains the
    counters, and the second open does nothing."""
    from mrfx.config import MrfxConfig
    from mrfx.ingest import scan_inbox
    from mrfx.store import Store

    from .test_known_answer_modifiers import FILE, NAME

    # the reference: a fresh ingest by the current parser
    ref_cfg = MrfxConfig(**{**cfg.__dict__, "inbox_dir": tmp_path / "ref_in",
                            "processed_dir": tmp_path / "ref_done",
                            "store_dir": tmp_path / "ref_store"})
    ref_cfg.ensure_dirs()
    (ref_cfg.inbox_dir / NAME).write_text(json.dumps(FILE))
    ref = Store(ref_cfg.store_dir)
    list(scan_inbox(ref_cfg, ref))
    ref.close()

    # an "old" store: same file through the old rule, rules marker unset
    monkeypatch.setattr("mrfx.parser.normalize_modifiers", _old_rule)
    old = Store(cfg.store_dir)
    (cfg.inbox_dir / NAME).write_text(json.dumps(FILE))
    list(scan_inbox(cfg, old))
    with old.connect() as con:
        con.execute("UPDATE files SET modifier_rules = NULL")
        con.execute("UPDATE files SET qa = json_object('rows', 8)")
    old.update_rollups_incremental()
    old.close()
    monkeypatch.undo()
    rates = cfg.store_dir / "rates"
    assert _rows(rates) != _rows(ref_cfg.store_dir / "rates")   # really old-style

    st = Store(cfg.store_dir)
    assert _rows(rates) == _rows(ref_cfg.store_dir / "rates")
    with st.connect() as con:
        rules, qa = con.execute("SELECT modifier_rules, qa FROM files").fetchone()
    assert rules == 1
    qa = json.loads(qa)
    assert (qa["pos_in_modifier_rows"], qa["modifier_split_rows"], qa["rows"]) == (2, 1, 8)
    assert "Beta Health" in st.meta_get("rollup_pending_payers")
    assert st.rollups_stale()
    st.update_rollups_incremental()
    assert not st.rollups_stale()
    with st.connect() as con:
        base_tins = {r[0] for r in con.execute(
            f"SELECT DISTINCT tin_value FROM rates_by_tin WHERE {base_sql()}").fetchall()}
    assert base_tins == {"440000004", "550000005", "660000006", "770000007"}
    part = next(rates.glob("*.parquet"))
    stamp = part.stat().st_mtime_ns
    st.close()
    Store(cfg.store_dir).close()                                  # second open: no-op
    assert part.stat().st_mtime_ns == stamp


def test_failed_rewrite_leaves_rows_and_retries_next_start(cfg, monkeypatch, caplog):
    """A part that can't be rewritten (locked by a reader, disk full) keeps its
    rows exactly as they were, the app still opens, and the next start
    retries."""
    from mrfx.ingest import scan_inbox
    from mrfx.store import Store

    from .test_known_answer_modifiers import FILE, NAME

    monkeypatch.setattr("mrfx.parser.normalize_modifiers", _old_rule)
    old = Store(cfg.store_dir)
    (cfg.inbox_dir / NAME).write_text(json.dumps(FILE))
    list(scan_inbox(cfg, old))
    with old.connect() as con:
        con.execute("UPDATE files SET modifier_rules = NULL")
    old.close()
    monkeypatch.undo()
    rates = cfg.store_dir / "rates"
    before = _rows(rates)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(Store, "_rewrite_part_modifiers", boom)
    with caplog.at_level("WARNING"):
        st = Store(cfg.store_dir)
    assert "will retry at the next start" in caplog.text
    assert _rows(rates) == before
    assert not list(rates.glob(".*.tmp"))
    with st.connect() as con:
        assert con.execute("SELECT modifier_rules FROM files").fetchone()[0] is None
    st.close()
    monkeypatch.undo()
    st = Store(cfg.store_dir)
    with st.connect() as con:
        assert con.execute("SELECT modifier_rules FROM files").fetchone()[0] == 1
    assert _rows(rates) != before
    st.close()
