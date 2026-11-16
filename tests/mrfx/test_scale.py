"""2-5 B-rate scale (0.8.9) — what grows with the store, bounded.

Measured on a 1 B-rate store (10 hardlinked copies of a 99 M store) and
projected to 2-5 B: one new file of a big payer escalated to a full rebuild of
EVERY payer (~1.5 h at 1.5 B rates, 5+ h at 5 B); and every exact median /
quantile over the whole latest spine held every value in memory (DuckDB
cannot spill them) — past an 8 GB budget near a billion spine rows.

Each test pins the fix to the numbers of the code it replaced: identical rows,
bit for bit.
"""
from __future__ import annotations

import random

import duckdb
import pytest
from fastapi.testclient import TestClient

import mrfx.api as api_mod
from mrfx import benchmark as bm
from mrfx.quantiles import exact_quantiles_sql
from mrfx.store import RollupNeedsFull, RollupScoped, Store
from tests.mrfx.test_v081 import _dir_rows, _one_shot, _recs, _seed, _spine_rows, _write

# --------------------------------------------------------------------------
# exact quantiles from counts == quantile_cont, bit for bit


def test_exact_quantiles_equal_quantile_cont_bit_for_bit():
    """400 random sets — cents, half-cents, heavy ties, wide doubles, sizes
    1..5001. was: the textbook `lo + d*(hi - lo)` differed from DuckDB in the
    last bit in 57 of 400 sets (enough to flip a half-cent when rounded)."""
    con = duckdb.connect()
    rnd = random.Random(7)
    qs = [0.25, 0.5, 0.75]
    for _ in range(400):
        n = rnd.choice([1, 2, 3, 4, 5, 10, 11, 100, 101, 1000, 5001])
        mode = rnd.choice(["cents", "half", "ties", "wide"])
        vals = []
        for _ in range(n):
            if mode == "cents":
                vals.append(round(rnd.uniform(5, 200), 2))
            elif mode == "half":
                vals.append(round(rnd.uniform(5, 200) * 2, 0) / 2 + rnd.choice([0, 0.005, 0.01]))
            elif mode == "ties":
                vals.append(rnd.choice([23.52, 23.53, 71.99, 20.73, 21.04]))
            else:
                vals.append(rnd.uniform(0.02, 5000))
        con.execute("CREATE OR REPLACE TABLE t(x DOUBLE)")
        con.executemany("INSERT INTO t VALUES (?)", [[v] for v in vals + [None]])
        want = con.execute(f"SELECT quantile_cont(x, {qs}) FROM t").fetchone()[0]
        got = con.execute(exact_quantiles_sql("SELECT x FROM t", qs)).fetchone()[0]
        assert got == want, (mode, n)
        want_r = con.execute(
            f"SELECT [round(v, 2) FOR v IN quantile_cont(x, {qs})] FROM t").fetchone()[0]
        got_r = con.execute(exact_quantiles_sql("SELECT x FROM t", qs, round_to=2)).fetchone()[0]
        assert got_r == want_r


def test_exact_quantiles_of_nothing_is_null():
    con = duckdb.connect()
    con.execute("CREATE TABLE t(x DOUBLE)")
    assert con.execute(exact_quantiles_sql("SELECT x FROM t", [0.5])).fetchone()[0] is None
    con.execute("INSERT INTO t VALUES (NULL)")
    assert con.execute(exact_quantiles_sql("SELECT x FROM t", [0.5])).fetchone()[0] is None


# --------------------------------------------------------------------------
# the materialised practice medians, built in code slices


def _mat_rows(store, name, built):
    for f in store.dir.glob(f"{name}.*.parquet"):
        f.unlink()
    store._spine_mat.pop(name, None)
    rel = store.spine_materialised(name, *built)
    with store.connect() as con:
        return con.execute(f"SELECT * FROM {rel} ORDER BY ALL").fetchall()


@pytest.mark.parametrize("name,built", [
    (bm.PRACTICE_MEDIANS, lambda: bm._house_table_sql(False)),
    (bm.PAYER_PRACTICE_MEDIANS, lambda: bm._house_table_sql(True)),
    (bm.DISC_PAYER_PRACTICE_MEDIANS, bm._house_disc_table_sql),
])
def test_sliced_practice_medians_equal_one_statement(store, monkeypatch, name, built):
    """A spine bigger than MATERIALISE_SLICE_ROWS builds each median table
    per batch of codes; the rows are identical to the one statement (groups
    never span codes). was: one statement, every value of the latest spine
    held in memory at once."""
    _seed(store)
    store.rebuild_rollups()
    one = _mat_rows(store, name, built())
    assert one
    monkeypatch.setattr(Store, "MATERIALISE_SLICE_ROWS", 5)
    copies = []
    real = store._materialise_sliced

    def spy(con, sql, params, out):
        copies.append(sql)
        return real(con, sql, params, out)
    monkeypatch.setattr(store, "_materialise_sliced", spy)
    assert _mat_rows(store, name, built()) == one
    assert copies and "{code_pred}" in copies[0]
    assert not list(store.dir.glob("*.slices"))          # the work folder is gone


# --------------------------------------------------------------------------
# the Explorer summary strip: quartiles from counts, per-code in batches

URLS = ["/api/summary?grain=tin&latest=1&dollar_only=1",
        "/api/summary?grain=tin&latest=0",
        "/api/summary?grain=npi&latest=1&dollar_only=1&cpt=97110,97140",
        "/api/summary?grain=tin&latest=1&dollar_only=1&payer=P-B"]


def _summaries(cfg):
    for f in cfg.store_dir.glob("summary_strips.*.json"):
        f.unlink()
    st = Store(cfg.store_dir)
    c = TestClient(api_mod.create_app(cfg, st))
    out = {}
    for u in URLS:
        r = c.get(u)
        assert r.status_code == 200, (u, r.text)
        body = r.json()
        for k in ("generation", "data_generation", "computed_at", "cache_age_s"):
            body.pop(k, None)
        out[u] = body
    return out


def test_summary_batched_by_code_equals_one_statement(cfg, store, monkeypatch):
    """The strip's quartiles from counts and its per-code table in batches
    equal the one-statement numbers. was: quantile_cont + a median per
    practice over the whole view in one statement — the 1 B-row run's
    out-of-memory refusal (HTTP 503) on the national summary."""
    _seed(store)
    store.rebuild_rollups()
    monkeypatch.setattr(api_mod, "SUMMARY_SLICE_ROWS", 10 ** 12)
    one = _summaries(cfg)
    monkeypatch.setattr(api_mod, "SUMMARY_SLICE_ROWS", 7)
    assert _summaries(cfg) == one
    assert one[URLS[0]]["by_code"]


def test_summary_quartiles_equal_quantile_cont(cfg, store):
    """The strip's p25 / median / p75 are the values quantile_cont returns
    over the same rows (rounded half-up to the cent)."""
    _seed(store)
    store.rebuild_rollups()
    got = _summaries(cfg)[URLS[0]]
    s = got.get("summary") or got
    with store.connect() as con:
        want = con.execute(
            "SELECT [round(v, 2) FOR v IN quantile_cont(negotiated_rate, [0.25, 0.5, 0.75])] "
            "FROM rates_by_tin WHERE is_latest AND is_dollar_rate AND negotiated_rate > 0.01"
        ).fetchone()[0]
    assert [s["p25"], s["median"], s["p75"]] == want


# --------------------------------------------------------------------------
# a big update rebuilds ITS payers durably, never every payer


def _too_big(store, monkeypatch):
    monkeypatch.setattr(store, "_rollup_partition_rows", lambda split=1, threads=1: 25)


def _spy_slices(store, monkeypatch):
    seen = []
    real = store._run_slice

    def spy(con, plan, idx, log):
        seen.append(dict(plan["slices"][idx]))
        return real(con, plan, idx, log)
    monkeypatch.setattr(store, "_run_slice", spy)
    return seen


def test_a_big_update_rebuilds_only_its_payers(cfg, store, monkeypatch):
    """New big files of two payers (one new) on a built 4-payer store: the
    update hands over a payer-SCOPED durable build — spine slices for those
    two payers only — and the tables equal a full one-shot build.
    was: RollupNeedsFull -> rebuild_rollups() of every payer of the store
    (~1.5 h at 1.5 B rates; 5+ h at 5 B)."""
    from mrfx.foldin import fold_in
    _seed(store)
    store.rebuild_rollups()
    _write(store, "f_P-B2.json", "P-B", _recs("P-B", 5))
    _write(store, "f_P-E.json", "P-E", _recs("P-E", 6))
    _too_big(store, monkeypatch)
    with pytest.raises(RollupScoped) as ei:
        store.update_rollups_incremental()
    assert sorted(ei.value.scope["payers"]) == ["P-B", "P-E"]
    assert store.rollup_plan() is None
    seen = _spy_slices(store, monkeypatch)
    fold_in(store, "test")
    spine_payers = {s["payer"] for s in seen if s["t"] == "spine"}
    assert spine_payers == {"P-B", "P-E"}
    assert any(s["t"] == "dir" for s in seen)
    assert not store.rollups_stale() and store.rollup_plan() is None
    assert (_spine_rows(store), _dir_rows(store)) == _one_shot(store)


def test_a_file_landing_during_the_scoped_build_stays_for_the_next_update(cfg, store, monkeypatch):
    """A file of a payer OUTSIDE the scope that finishes after the update
    looked is not marked covered by the scoped build (its payer was never
    rebuilt): the store stays stale, and the next update folds it in.
    was: (a full plan's fresh snapshot would have covered it unbuilt)."""
    _seed(store)
    store.rebuild_rollups()
    _write(store, "f_P-B2.json", "P-B", _recs("P-B", 5))
    _too_big(store, monkeypatch)
    with pytest.raises(RollupScoped) as ei:
        store.update_rollups_incremental()
    _write(store, "f_P-A2.json", "P-A", _recs("P-A", 7, n_tins=2, edge=False))
    store.rebuild_rollups(scope=ei.value.scope)
    assert store.rollups_stale()
    monkeypatch.undo()
    store.update_rollups_incremental()
    assert not store.rollups_stale()
    assert (_spine_rows(store), _dir_rows(store)) == _one_shot(store)


def test_a_scoped_build_resumes_after_a_stop(cfg, store, monkeypatch):
    """Killed after its first slice, the scoped build continues from the
    stored plan at the next fold-in (still scoped) and ends identical."""
    from mrfx.foldin import fold_in
    _seed(store)
    store.rebuild_rollups()
    _write(store, "f_P-C2.json", "P-C", _recs("P-C", 5))
    _too_big(store, monkeypatch)
    real = store._run_slice
    calls = []

    def dying(con, plan, idx, log):
        calls.append(idx)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(con, plan, idx, log)
    monkeypatch.setattr(store, "_run_slice", dying)
    with pytest.raises(KeyboardInterrupt):
        fold_in(store, "test")
    plan = store.rollup_plan()
    assert plan is not None and plan.get("scope") == ["P-C"]
    seen = _spy_slices(store, monkeypatch)
    monkeypatch.setattr(store, "_run_slice", lambda con, p, i, log: (
        seen.append(dict(p["slices"][i])), real(con, p, i, log))[1])
    fold_in(store, "test")
    assert {s["payer"] for s in seen if s["t"] == "spine"} <= {"P-C"}
    assert not store.rollups_stale() and store.rollup_plan() is None
    assert (_spine_rows(store), _dir_rows(store)) == _one_shot(store)


def test_a_scoped_build_drops_a_removed_payer_and_keeps_the_others(cfg, store, monkeypatch):
    """A forgotten file (its payer's last) plus a big new file: the scoped
    prune deletes only the removed payer's rows; every other payer stays."""
    from mrfx.foldin import fold_in
    _seed(store)
    store.rebuild_rollups()
    store.forget_file("f_P-D.json")
    _write(store, "f_P-B2.json", "P-B", _recs("P-B", 5))
    _too_big(store, monkeypatch)
    with pytest.raises(RollupScoped) as ei:
        store.update_rollups_incremental()
    assert sorted(ei.value.scope["payers"]) == ["P-B", "P-D"]
    fold_in(store, "test")
    with store.connect() as con:
        payers = {r[0] for r in con.execute("SELECT DISTINCT payer FROM rates_by_tin_tbl").fetchall()}
    assert payers == {"P-A", "P-B", "P-C"}
    assert not store.rollups_stale()
    assert (_spine_rows(store), _dir_rows(store)) == _one_shot(store)


def test_a_scope_is_dropped_when_the_spine_layout_changed(cfg, store, monkeypatch):
    """A spine whose columns are not this version's cannot be rebuilt for a
    few payers (the others would keep the old layout): the scoped request
    becomes a full build, and the result is still exact."""
    _seed(store)
    store.rebuild_rollups()
    with store.connect() as con:
        con.execute("ALTER TABLE rates_by_tin_tbl ADD COLUMN zz_old INTEGER")
    seen = _spy_slices(store, monkeypatch)
    store.rebuild_rollups(scope={"payers": ["P-A"], "cover_ts": "", "cover_names": [],
                                 "pend_gen": 0, "pending": []})
    assert {s["payer"] for s in seen if s["t"] == "spine"} == {"P-A", "P-B", "P-C", "P-D"}
    assert (_spine_rows(store), _dir_rows(store)) == _one_shot(store)


def test_scoped_is_still_a_needs_full_for_old_callers():
    assert issubclass(RollupScoped, RollupNeedsFull)


# --------------------------------------------------------------------------
# the enrichment queue reads the per-part NPI set, not every rate per batch


def test_unenriched_npis_from_the_part_set_equal_the_raw_scan(cfg, store):
    """The NPIs still to look up (NPIs in the TIN slot included, malformed
    ids excluded, the cursor honoured) equal the raw whole-store DISTINCT.
    was: that DISTINCT over every rate ran once per batch of 200 NPIs."""
    _seed(store)
    _write(store, "f_bad.json", "P-A", [dict(r, npi="12345") for r in _recs("P-A", 8, n_tins=1)])
    with store.connect() as con:
        raw = [r[0] for r in con.execute(
            "SELECT DISTINCT npi FROM (SELECT npi FROM rates UNION SELECT tin_value FROM rates "
            "WHERE tin_is_really_npi AND tin_value IS NOT NULL) "
            "WHERE regexp_full_match(npi, '[0-9]{10}') ORDER BY npi").fetchall()]
        con.execute("INSERT INTO npi_directory (npi) VALUES (?)", [raw[0]])
    assert raw and "12345" not in raw
    assert store.unenriched_npis(limit=10 ** 6) == raw[1:]
    assert store.unenriched_npis(limit=3, after=raw[4]) == raw[5:8]
    with store.connect() as con:          # the set is kept: parts scanned once
        assert con.execute("SELECT count(*) FROM rate_npi_parts").fetchone()[0] == 5


# --------------------------------------------------------------------------
# per-request NPI lookups from the kept (TIN, NPI) pairs, not every rate


def test_pairs_answer_what_the_raw_scans_answered(cfg, store):
    """The pairs file's row counts give the same modal TIN per NPI and modal
    ZIP per practice as mode() over the raw rows (ties: the smallest), and
    the same NPI sets. was: each of these scanned every rate per request
    (Medicare practice list, dossier check, packets, service lines, new
    clinics / closures)."""
    rows = []
    base = _recs("P-A", 1, n_tins=1, edge=False)[0]
    # NPI X: 3 rows under T2, 3 under T1, 1 under T3 -> T1 (tie: smallest)
    # NPI Y: 2 under T3, 1 under T1 -> T3
    for npi, tin, n in [("1999999991", "400000002", 3), ("1999999991", "400000001", 3),
                        ("1999999991", "400000003", 1), ("1999999992", "400000003", 2),
                        ("1999999992", "400000001", 1), (None, "400000001", 4)]:
        rows += [dict(base, npi=npi, tin_value=tin, negotiated_rate=50.0 + i) for i in range(n)]
    _write(store, "f_modes.json", "P-A", rows)
    pairs = store.pairs_relation()
    with store.connect() as con:
        con.execute("INSERT INTO npi_directory (npi, zip) VALUES ('1999999991', '64101'), "
                    "('1999999992', '64102-1234')")
        want = dict(con.execute("SELECT npi, mode(tin_value ORDER BY tin_value) FROM rates "
                                "WHERE npi IS NOT NULL GROUP BY npi").fetchall())
        got = dict(con.execute(
            f"SELECT npi, tin_value FROM (SELECT npi, tin_value, sum(c) AS k FROM {pairs} "
            "WHERE npi IN (SELECT unnest(?::VARCHAR[])) AND tin_value IS NOT NULL "
            "GROUP BY npi, tin_value) "
            "QUALIFY row_number() OVER (PARTITION BY npi ORDER BY k DESC, tin_value) = 1",
            [list(want)]).fetchall())
        assert got == want == {"1999999991": "400000001", "1999999992": "400000003"}
        for tins in (["400000001"], ["400000001", "400000003"], ["400000002"]):
            w = con.execute(
                "SELECT mode(lpad(substr(trim(n.zip), 1, 5), 5, '0') ORDER BY "
                "lpad(substr(trim(n.zip), 1, 5), 5, '0')) FROM rates r JOIN npi_directory n "
                "ON n.npi = r.npi WHERE r.tin_value IN (SELECT unnest(?::VARCHAR[]))", [tins]
            ).fetchone()[0]
            g = con.execute(
                f"SELECT z FROM (SELECT lpad(substr(trim(n.zip), 1, 5), 5, '0') AS z, "
                f"sum(r.c) AS k FROM {pairs} r JOIN npi_directory n ON n.npi = r.npi "
                "WHERE r.tin_value IN (SELECT unnest(?::VARCHAR[])) GROUP BY z) "
                "ORDER BY k DESC, z LIMIT 1", [tins]).fetchone()
            assert (g[0] if g else None) == w
            assert sorted(con.execute(
                f"SELECT DISTINCT npi FROM {pairs} WHERE tin_value IN "
                "(SELECT unnest(?::VARCHAR[])) AND npi IS NOT NULL", [tins]).fetchall()) == sorted(
                con.execute("SELECT DISTINCT npi FROM rates WHERE tin_value IN "
                            "(SELECT unnest(?::VARCHAR[])) AND npi IS NOT NULL", [tins]).fetchall())
