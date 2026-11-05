"""End-to-end: the whole ingest pipeline with fast_scan on vs off must leave
IDENTICAL stores — raw rates, the dedup/by-TIN rollups the dashboard reads, and
the files table (status, row counts, QA). Covers the in-process path, the
worker-pool path (parse in a child process), the big-file two-pass path, and
.json / .json.gz / .zip inputs, plus a standalone provider-reference companion.
"""

import gzip
import json
import shutil
import zipfile
from pathlib import Path

import pytest

import mrfx.ingest as ingest_mod
from mrfx.config import MrfxConfig
from mrfx.ingest import ParsePoolManager, ingest_file, set_parse_pool
from mrfx.store import Store
from tests.mrfx.conftest import FIXTURES
from tests.mrfx.test_messy_files import HOSTILE

G = [{"npi": [1234567890, "1234567891"], "tin": {"type": "ein", "value": "111111111"}}]


def _item(code, refs=None, groups=None, rate=50.0, **extra):
    nr = {"negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": rate,
                                 "billing_class": "professional", "service_code": ["11"],
                                 "billing_code_modifier": ["GP"]}]}
    if refs is not None:
        nr["provider_references"] = refs
    if groups is not None:
        nr["provider_groups"] = groups
    return {"billing_code_type": "CPT", "billing_code": code, "negotiated_rates": [nr], **extra}


def _corpus() -> list[tuple[str, bytes]]:
    """(filename, bytes) in ingest order. The companion refs file goes first,
    as scan_inbox orders it."""
    refs_after = {
        "reporting_entity_name": "Refs After Health", "version": "1.0.0",
        "last_updated_on": "2026-04-01",
        "in_network": [_item("97110", refs=[1, 2]), _item("97140", refs=[3]),
                       _item("99213", refs=[1]), _item("97112", refs=[1, 99])],
        "provider_references": [{"provider_group_id": i, "provider_groups": [
            {"npi": [1300000000 + i], "tin": {"type": "ein", "value": f"5{i:08d}"}}]} for i in (1, 2, 3)],
    }
    escapes = {
        "reporting_entity_name": 'Escape "Quote" {Co}', "version": "1.1", "last_updated_on": "2026-02-01",
        "in_network": [_item("97530", groups=G, name='x \\" } ] { ['),
                       _item("99999", groups=G, name="\\\\\\"),
                       _item("G0283", groups=G, description="é ü 😀")],
    }
    mixed = (FIXTURES / "innetwork_mixed.json").read_bytes()
    zbuf = __import__("io").BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", "{}")                         # smaller decoy member
        z.writestr("rates_2026-02.json", json.dumps(escapes))
    return [
        ("provider_reference_companion.json", (FIXTURES / "provider_reference_companion.json").read_bytes()),
        ("innetwork_mixed.json", mixed),
        ("hostile_2026-06.json.gz", gzip.compress(json.dumps(HOSTILE).encode())),
        ("refs_after_2026-04.json", json.dumps(refs_after, indent=2).encode()),
        ("escapes_2026-02.zip", zbuf.getvalue()),
    ]


def _make_cfg(root, fast: bool) -> MrfxConfig:
    c = MrfxConfig(
        inbox_dir=root / "inbox", processed_dir=root / "processed", failed_dir=root / "failed",
        store_dir=root / "store", downloads_dir=root / "downloads",
        entity_map_path=root / "entity_map.yaml", payer_name_map={"Testco Health": "Testco"},
        fast_scan=fast,
    )
    c.ensure_dirs()
    return c


def _snapshot(store: Store) -> dict:
    snap = {}
    with store.connect() as con:
        cols = [d[0] for d in con.execute("SELECT * FROM rates LIMIT 0").description if d[0] != "ingested_at"]
        snap["rates"] = sorted(map(repr, con.execute(f"SELECT {', '.join(cols)} FROM rates").fetchall()))
        for view in ("rates_dedup", "rates_by_tin"):
            snap[view] = sorted(map(repr, con.execute(f"SELECT * FROM {view}").fetchall()))
        snap["files"] = sorted(map(repr, con.execute(
            "SELECT filename, payer, status, schema_version, last_updated_on, rows_emitted, "
            "ref_groups_skipped, error, qa FROM files").fetchall()))
    return snap


def _ingest_all(tmp_path, fast: bool) -> dict:
    cfg = _make_cfg(tmp_path / ("fast" if fast else "classic"), fast)
    store = Store(cfg.store_dir)
    for name, data in _corpus():
        p = cfg.inbox_dir / name
        p.write_bytes(data)
        ingest_file(cfg, store, p)
    return _snapshot(store)


def _assert_stores_match(tmp_path):
    want = _ingest_all(tmp_path, fast=False)
    got = _ingest_all(tmp_path, fast=True)
    # guard against a vacuous pass: every in-network file must emit rows
    assert len(want["files"]) == 5 and all("'done'" in f for f in want["files"]), want["files"]
    per_file = {}
    for r in want["rates"]:
        name = next(n for n, _ in _corpus() if f"'{n.replace('.zip', '')}" in r or f"'{n}'" in r)
        per_file[name] = per_file.get(name, 0) + 1
    assert len(per_file) == 4 and all(v > 0 for v in per_file.values()), per_file
    for k in want:
        assert got[k] == want[k], f"{k} differs between fast_scan on and off"


def test_in_process_ingest_leaves_identical_stores(tmp_path):
    _assert_stores_match(tmp_path)


def test_two_pass_big_file_path_leaves_identical_stores(tmp_path, monkeypatch):
    """Force every file down the big-file path (skim pass + kept-refs
    extraction) — the path every multi-GB payer file takes."""
    monkeypatch.setattr(ingest_mod, "LARGE_FILE_UNCOMPRESSED_BYTES", 1)
    _assert_stores_match(tmp_path)


@pytest.mark.parametrize("two_pass", [False, True])
def test_worker_pool_ingest_leaves_identical_stores(tmp_path, monkeypatch, two_pass):
    """The queue worker parses in a CHILD process (cfg pickled across, the
    columnar RowBatch sink in the worker). Same store either way."""
    if two_pass:
        # the worker decides two-pass from `est` vs this constant in ITS own
        # import — monkeypatch can't reach a spawned child, so make the
        # estimate huge instead (the parent passes it in)
        real = ingest_mod.preflight

        def big_pf(*a, **k):
            pf = real(*a, **k)
            pf.est_uncompressed_bytes = 10 ** 12
            return pf
        monkeypatch.setattr(ingest_mod, "preflight", big_pf)
        monkeypatch.setattr(ingest_mod, "LARGE_FILE_UNCOMPRESSED_BYTES", 1)
    pool = ParsePoolManager(max_workers=1)
    set_parse_pool(pool)
    try:
        _assert_stores_match(tmp_path)
    finally:
        set_parse_pool(None)
        pool.shutdown_now()


def test_a_file_both_parsers_reject_fails_the_same_way(tmp_path):
    """Truncated download: both paths mark the file failed, write no rows,
    and leave no part behind."""
    for fast in (False, True):
        cfg = _make_cfg(tmp_path / str(fast), fast)
        store = Store(cfg.store_dir)
        p = cfg.inbox_dir / "trunc_2026-01.json"
        p.write_bytes(json.dumps({"in_network": [_item("97110", groups=G)] * 5}).encode()[:-40])
        res = ingest_file(cfg, store, p)
        assert res["status"] == "failed"
        with store.connect() as con:
            assert con.execute("SELECT count(*) FROM rates").fetchone()[0] == 0
        assert not list(store.rates_dir.glob("*.parquet"))


def test_nan_rate_fails_the_file_on_both_paths(tmp_path):
    for fast in (False, True):
        cfg = _make_cfg(tmp_path / str(fast), fast)
        store = Store(cfg.store_dir)
        p = cfg.inbox_dir / "nan_2026-01.json"
        p.write_text(json.dumps({"in_network": [_item("97110", groups=G)]}).replace("50.0", "NaN"))
        assert ingest_file(cfg, store, p)["status"] == "failed"
        with store.connect() as con:
            assert con.execute("SELECT count(*) FROM rates").fetchone()[0] == 0


def test_fastcheck_reports_identical_and_catches_a_planted_difference(tmp_path, monkeypatch):
    """The user-facing checker must be able to say DIFFERENT — a checker that
    can only say IDENTICAL proves nothing."""
    from mrfx.fastcheck import compare_parsers, format_report
    from mrfx.parser import InNetworkParser

    cfg = _make_cfg(tmp_path, True)
    p = tmp_path / "mixed.json.gz"
    p.write_bytes(gzip.compress(json.dumps(HOSTILE).encode()))
    for two_pass in (False, True):
        rep = compare_parsers(cfg, p, two_pass=two_pass)
        assert rep["identical"], rep["differences"]
        assert rep["classic"]["rows"] > 0 and "IDENTICAL" in format_report(rep, 0.001)

    # sabotage ONLY the fast path (only it calls _want_item): drop 97140
    from mrfx.parser import clean_code

    real = InNetworkParser._want_item

    def drop_97140(self, events, final):
        if any(k == "code" and clean_code(v) == "97140" for k, v in events):
            return False
        return real(self, events, final)
    monkeypatch.setattr(InNetworkParser, "_want_item", drop_97140)
    rep = compare_parsers(cfg, p, two_pass=False)
    assert not rep["identical"]
    assert rep["rows_only_in_classic"] > 0 and rep["rows_only_in_fast"] == 0
    assert "DIFFERENT" in format_report(rep)


def test_fastcheck_cli_exit_codes(tmp_path, monkeypatch, capsys):
    from mrfx import cli

    good = tmp_path / "good_2026-01.json"
    good.write_text(json.dumps(HOSTILE))
    cfg = _make_cfg(tmp_path / "c", True)
    args = type("A", (), {"paths": [str(good)], "two_pass": "auto"})
    assert cli.cmd_fastcheck(cfg, args) == 0
    assert "IDENTICAL" in capsys.readouterr().out
    args.paths = [str(tmp_path / "missing.json")]
    assert cli.cmd_fastcheck(cfg, args) == 1


def test_companion_reference_file_is_untouched_by_fast_scan(tmp_path):
    """Standalone provider-reference files keep their own parser; their refs
    feed in-network files parsed by the fast path."""
    for fast in (False, True):
        cfg = _make_cfg(tmp_path / str(fast), fast)
        store = Store(cfg.store_dir)
        for n in ("provider_reference_companion.json", "innetwork_mixed.json"):
            shutil.copy(FIXTURES / n, cfg.inbox_dir / n)
            ingest_file(cfg, store, cfg.inbox_dir / n)
        with store.connect() as con:
            skipped = con.execute("SELECT sum(ref_groups_skipped) FROM files").fetchone()[0]
        assert not skipped  # every cited reference resolved through the companion


def test_size_estimate_never_under_reads_a_high_ratio_file(tmp_path):
    """Real MRFs compress 3x-60x. A fixed 18x guess under-estimated a
    high-ratio file, which can route a genuinely big file to the single-pass
    path (whole reference table in RAM). The recorded size raises it."""
    from mrfx.sniff import _estimate_uncompressed

    body = b'{"in_network": [' + b",".join([b'{"billing_code": "99213"}'] * 200_000) + b"]}"
    gz = tmp_path / "x.json.gz"
    gz.write_bytes(gzip.compress(body))
    ratio = len(body) / gz.stat().st_size
    assert ratio > 18                      # the case the old guess got wrong
    assert _estimate_uncompressed(gz, gz.stat().st_size) == len(body)
    zp = tmp_path / "x.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("x.json", body)
    assert _estimate_uncompressed(zp, zp.stat().st_size) >= len(body)
    # never LOWER than the old guess (low-ratio / >4 GiB wrap stays safe)
    low = tmp_path / "low.json.gz"
    low.write_bytes(gzip.compress(bytes(range(256)) * 10_000, compresslevel=1))
    assert _estimate_uncompressed(low, low.stat().st_size) >= 18 * low.stat().st_size
    bad = tmp_path / "bad.json.gz"
    bad.write_bytes(b"\x1f\x8b")
    assert _estimate_uncompressed(bad, 2) == 36


def test_background_parquet_writer_writes_in_order_and_surfaces_errors(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from mrfx.store import BackgroundParquetWriter

    schema = pa.schema([pa.field("x", pa.int64())])
    w = BackgroundParquetWriter(tmp_path / "a.parquet", schema, "zstd")
    for i in range(50):
        w.write_table(pa.table({"x": list(range(i * 10, i * 10 + 10))}, schema=schema))
    w.close()
    w.close()  # idempotent
    assert pq.read_table(tmp_path / "a.parquet").column("x").to_pylist() == list(range(500))

    bad = BackgroundParquetWriter(tmp_path / "b.parquet", schema, "zstd")
    bad.write_table(pa.table({"y": ["wrong schema"]}))   # fails inside the thread
    with pytest.raises(Exception):
        for _ in range(10):
            bad.write_table(pa.table({"x": [1]}, schema=schema))
        bad.close()


def test_a_failed_write_fails_the_part_and_leaves_nothing_behind(cfg, store, monkeypatch):
    """A disk-full style error raised by the writer thread must fail the
    file exactly like a parse error: no part, no temp, files row failed."""
    import mrfx.store as store_mod

    real = store_mod.BackgroundParquetWriter.write_table
    calls = {"n": 0}

    def boom(self, table):
        calls["n"] += 1
        if calls["n"] >= 2:
            self._err = OSError(28, "No space left on device")
        return real(self, table)
    monkeypatch.setattr(store_mod.BackgroundParquetWriter, "write_table", boom)
    monkeypatch.setattr("mrfx.parser.InNetworkParser.BATCH_ROWS", 1)
    p = cfg.inbox_dir / "big_2026-01.json"
    p.write_text(json.dumps({"in_network": [_item("97110", groups=G)] * 20}))
    res = ingest_file(cfg, store, p)
    assert res["status"] == "failed" and "No space left" in res.get("error", "")
    assert not list(store.rates_dir.glob("*.parquet")) and not list(store.rates_dir.glob(".*.tmp"))


def _spool_spy(monkeypatch):
    """Record every .spool.tmp the code opens for writing."""
    import builtins

    opened = []
    real_open = builtins.open

    def spy(file, mode="r", *a, **k):
        if str(file).endswith(".spool.tmp") and "w" in mode:
            opened.append(str(file))
        return real_open(file, mode, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    return opened


def _ingest_corpus_with(tmp_path, fast: bool, **cfg_kw) -> dict:
    cfg = _make_cfg(tmp_path / ("fast" if fast else "classic"), fast)
    for k, v in cfg_kw.items():
        if k == "all_codes":
            cfg.codes.all_codes = v
        else:
            setattr(cfg, k, v)
    store = Store(cfg.store_dir)
    for name, data in _corpus():
        p = cfg.inbox_dir / name
        p.write_bytes(data)
        res = ingest_file(cfg, store, p)
        assert res["status"] == "done", res
    return _snapshot(store)


def test_all_codes_big_files_never_spool_and_ingest_identically(tmp_path, monkeypatch):
    """All-codes mode makes EVERY item a target: the single-read spool would
    be the whole in_network section written as scratch (~200 GB for a 200 GB
    file). It must decline up front and run the two-pass path instead."""
    monkeypatch.setattr(ingest_mod, "LARGE_FILE_UNCOMPRESSED_BYTES", 1)
    opened = _spool_spy(monkeypatch)
    want = _ingest_corpus_with(tmp_path, False, all_codes=True)
    got = _ingest_corpus_with(tmp_path, True, all_codes=True)
    assert opened == [], opened
    assert len(want["rates"]) > len(_ingest_corpus_with(tmp_path / "pt", False)["rates"])  # all-codes keeps more
    for k in want:
        assert got[k] == want[k], k


@pytest.mark.parametrize("when", ["up_front", "mid_spool"])
def test_low_disk_while_spooling_falls_back_and_still_finishes(tmp_path, monkeypatch, when):
    """Disk running low must not fail the file or fill the drive: the single
    read declines (no row was emitted yet) and the two-pass path, which needs
    no scratch, finishes it with identical results."""
    import mrfx.parser as parser_mod

    monkeypatch.setattr(ingest_mod, "LARGE_FILE_UNCOMPRESSED_BYTES", 1)
    calls = {"n": 0}

    def fake_free(path):
        calls["n"] += 1
        if when == "up_front" or calls["n"] > 1:
            return 1 << 20            # 1 MB free
        return 1 << 40
    monkeypatch.setattr(parser_mod, "_free_disk", fake_free)
    monkeypatch.setattr(parser_mod, "SPOOL_DISK_CHECK_BYTES", 1)
    want = _ingest_corpus_with(tmp_path, False)
    skims = []
    real_skim = ingest_mod.run_skim
    monkeypatch.setattr(ingest_mod, "run_skim", lambda *a, **k: skims.append(1) or real_skim(*a, **k))
    got = _ingest_corpus_with(tmp_path, True)
    assert calls["n"] >= 1
    assert len(skims) == 4, skims     # every in-network file fell back to two-pass
    for k in want:
        assert got[k] == want[k], k
    leftovers = list((tmp_path / "fast" / "store").rglob("*.spool.tmp"))
    assert leftovers == []


def test_single_read_progress_phases_are_monotonic_and_move_through_replay():
    from mrfx.ingest import SingleReadProgress

    seen = []
    sp = SingleReadProgress(1000, seen.append)
    for pos in range(0, 1001, 50):
        sp.feed(pos)
        if pos == 300:
            sp.refs_closed()             # the reference table ended 30% in
    assert 79.0 <= seen[-1] <= 80.0
    sp.second_pass()
    for pos in range(0, 301, 30):        # re-read stops at the table's end
        sp.feed(pos)
    assert 84.0 <= seen[-1] <= 85.0
    for done in range(0, 101, 10):
        sp.replay(done, 100)
    assert seen[-1] == 99.0
    assert seen == sorted(seen) and len(set(seen)) == len(seen)
    sp.feed(0)                           # a late callback can't move it backwards
    assert sp.pct == 99.0


def _unthrottled(fn):
    """set_percent without its half-percent throttle (a test file is tiny)."""
    def wrapper(self, pct):
        self._last_pct = -1.0
        self._last_chunk = -2
        return fn(self, pct)
    return wrapper


@pytest.mark.parametrize("pooled", [False, True])
def test_big_file_progress_is_honest_end_to_end(tmp_path, monkeypatch, pooled):
    """The single read was tracked as two equal passes: the bar sat at ~50%
    for the whole run, then jumped to 100%. Now it moves through scan ->
    reference re-read -> replay, never backwards, and ends at 100."""
    monkeypatch.setattr(ingest_mod, "LARGE_FILE_UNCOMPRESSED_BYTES", 1)
    import mrfx.ingest as im

    import mrfx.parser as parser_mod

    monkeypatch.setattr(im.SingleReadProgress, "STEP", 0.0)
    monkeypatch.setattr(parser_mod, "REPLAY_TICK_BYTES", 1)
    monkeypatch.setattr(im._ProgressTracker, "set_percent", _unthrottled(im._ProgressTracker.set_percent))
    cfg = _make_cfg(tmp_path, True)
    store = Store(cfg.store_dir)
    refs = [{"provider_group_id": i, "provider_groups": [
        {"npi": [1700000000 + i], "tin": {"type": "ein", "value": f"6{i:08d}"}}]} for i in range(3000)]
    items = [_item("97110", refs=[i, i + 1]) for i in range(0, 600, 3)]
    items += [_item("99213", refs=[5]) for _ in range(3000)]
    p = cfg.inbox_dir / "big_2026-01.json.gz"
    p.write_bytes(gzip.compress(json.dumps({"provider_references": refs, "in_network": items}).encode(),
                                compresslevel=1))
    seen = []
    real_update = store.update_progress

    def spy(name, pct, *a, **k):
        seen.append(pct)
        return real_update(name, pct, *a, **k)
    monkeypatch.setattr(store, "update_progress", spy)
    if pooled:
        pool = ParsePoolManager(max_workers=1)
        set_parse_pool(pool)
    try:
        res = ingest_file(cfg, store, p)
    finally:
        if pooled:
            set_parse_pool(None)
            pool.shutdown_now()
    assert res["status"] == "done" and res["rows"] > 0
    assert seen and seen[-1] == 100.0
    before_finish = seen[:-1]
    assert before_finish == sorted(before_finish), before_finish
    if not pooled:   # the pooled parent only samples the worker every 2 s
        assert any(85.0 < x < 100.0 for x in before_finish), before_finish   # replay phase seen
        assert max(before_finish) > 80.0


def test_stall_log_label_handles_every_progress_shape():
    """The parent's 15-minute stall warning formats the last progress it saw;
    that value became a (chunks, pct) tuple and once compared a tuple to 0."""
    from mrfx.ingest import _chunk_label

    assert _chunk_label(-1) == "0"
    assert _chunk_label((-1, None)) == "0"
    assert _chunk_label((7, 42.5)) == "7"
    assert _chunk_label((0, 0.0)) == "0"


@pytest.mark.parametrize("pooled", [False, True])
def test_ingest_reports_remote_reference_skips(tmp_path, pooled):
    refs = [{"provider_group_id": 1, "location": "https://payer.example/refs/1.json"},
            {"provider_group_id": 2, "provider_groups": G}]
    doc = {"provider_references": refs,
           "in_network": [_item("97110", refs=[1]), _item("97140", refs=[2])]}
    cfg = _make_cfg(tmp_path, True)
    store = Store(cfg.store_dir)
    p = cfg.inbox_dir / "remote_2026-01.json"
    p.write_text(json.dumps(doc))
    if pooled:
        pool = ParsePoolManager(max_workers=1)
        set_parse_pool(pool)
    try:
        res = ingest_file(cfg, store, p)
    finally:
        if pooled:
            set_parse_pool(None)
            pool.shutdown_now()
    assert res["status"] == "done" and res["ref_groups_skipped"] == 1
    assert res["remote_ref_groups"] == 1
    with store.connect() as con:
        qa = json.loads(con.execute("SELECT qa FROM files WHERE filename = ?", [p.name]).fetchone()[0])
    assert qa["remote_ref_groups"] == 1


def test_parent_removes_a_spool_its_worker_left_behind(tmp_path):
    """A worker killed mid-spool (OOM, crash) can't delete its spool, and the
    spool is named with the PARENT's live pid so the orphan sweep keeps it.
    The parent must remove it whatever happened in the worker."""
    import os

    from mrfx.ingest import spool_path_for
    from mrfx.store import file_key

    cfg = _make_cfg(tmp_path, True)
    store = Store(cfg.store_dir)
    p = cfg.inbox_dir / "x_2026-01.json"
    p.write_text(json.dumps({"in_network": [_item("97110", groups=G)]}))
    tmp_out = store.rates_dir / f".{file_key(p.name)}.{os.getpid()}.parquet.tmp"
    left = spool_path_for(tmp_out)
    left.write_bytes(b"x" * 1000)           # what a killed worker leaves
    pool = ParsePoolManager(max_workers=1)
    set_parse_pool(pool)
    try:
        assert ingest_file(cfg, store, p)["status"] == "done"
    finally:
        set_parse_pool(None)
        pool.shutdown_now()
    assert not left.exists()


def test_fastcheck_catches_a_bug_in_the_spool_replay(tmp_path, monkeypatch):
    """fastcheck runs the production single-read path for big files; a bug
    that loses an item between spool and replay must show as DIFFERENT."""
    import mrfx.parser as parser_mod
    from mrfx.fastcheck import compare_parsers

    cfg = _make_cfg(tmp_path, True)
    p = tmp_path / "big.json.gz"
    p.write_bytes(gzip.compress(json.dumps(HOSTILE).encode()))
    assert compare_parsers(cfg, p, two_pass=True)["identical"]

    real = parser_mod.InNetworkParser._handle_item
    seen = {"n": 0}

    def drop_second(self, item):
        if getattr(self, "_refs_complete", False):       # only during replay
            seen["n"] += 1
            if seen["n"] == 2:
                return None
        return real(self, item)
    monkeypatch.setattr(parser_mod.InNetworkParser, "_handle_item", drop_second)
    real_big = parser_mod.run_big_file_fast

    def big_then_count(*a, **k):
        seen["n"] = 0
        return real_big(*a, **k)
    monkeypatch.setattr(parser_mod, "run_big_file_fast", big_then_count)
    rep = compare_parsers(cfg, p, two_pass=True)
    assert not rep["identical"] and rep["rows_only_in_classic"] > 0, rep


def test_kept_reference_strings_are_shared_not_copied(tmp_path):
    """A kept reference table repeats each NPI across many groups (Aetna:
    7.4M slots, 2.3M distinct). One object per distinct value cut it 33%;
    values must be unchanged, and the store loader must share too (its dict
    is pickled to every worker, and pickle preserves the sharing)."""
    import pickle

    from mrfx.parser import InNetworkParser, share_strings

    raw = [("111111111", "ein", ("1234567890", "1234567891")),
           ("111111111", "ein", ("1234567890",))]
    copies = [(str(int(t)), ty, tuple(str(int(n)) for n in ns)) for t, ty, ns in raw]
    assert copies[0][2][0] is not copies[1][2][0]
    shared = share_strings(copies, {})
    assert shared == raw
    assert shared[0][2][0] is shared[1][2][0] and shared[0][0] is shared[1][0]

    cfg = _make_cfg(tmp_path, True)
    p = InNetworkParser(cfg, "x.json")
    p._add_ref({"provider_group_id": 1, "provider_groups": [{"npi": [1234567890], "tin": {"type": "ein", "value": "111111111"}}]})
    p._add_ref({"provider_group_id": 2, "provider_groups": [{"npi": [1234567890], "tin": {"type": "ein", "value": "111111111"}}]})
    a, b = p.result.embedded_refs[1][0], p.result.embedded_refs[2][0]
    assert a == b and a[2][0] is b[2][0] and a[0] is b[0]

    store = Store(cfg.store_dir)
    store.save_provider_refs("P", "refs.json", "2026-01-01",
                             {i: [("111111111", "ein", ("1234567890", f"12345678{i:02d}"))] for i in range(50)})
    loaded = store.load_provider_refs("P")
    firsts = [g[0][2][0] for g in loaded.values()]
    assert all(x is firsts[0] for x in firsts)
    unshared = {k: [(str(int(t)), ty, tuple(str(int(n)) for n in ns)) for t, ty, ns in v] for k, v in loaded.items()}
    assert unshared == loaded and len(pickle.dumps(loaded)) < len(pickle.dumps(unshared))


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc (Linux)")
def test_a_zombie_process_counts_as_dead_for_the_orphan_sweep():
    """signal 0 succeeds on a zombie (exited, not reaped). Seen in a kill
    trial: a killed app's temp files survived a restart because its pid was
    a zombie. A zombie writes nothing — its temps are safe to sweep."""
    import os
    import subprocess
    import time

    from mrfx.store import _pid_alive

    assert _pid_alive(os.getpid())
    child = subprocess.Popen(["true"])
    deadline = time.monotonic() + 10
    state = b""
    while time.monotonic() < deadline:
        state = Path(f"/proc/{child.pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]
        if state == b"Z":
            break
        time.sleep(0.01)
    assert state == b"Z"
    try:
        assert not _pid_alive(child.pid)
    finally:
        child.wait()
    assert not _pid_alive(child.pid)


def test_a_worker_whose_parent_died_removes_its_own_temp_files(tmp_path, monkeypatch):
    """An orphaned worker exits (SystemExit from its liveness check); it used
    to leave its .parquet.tmp / spool / progress files — named with the DEAD
    parent's pid — for the next start's sweep. It now removes them on the way
    out. Deterministic: the exit fires once rows AND a progress file exist
    (the real check runs on the read-ahead thread, so its timing can't be
    pinned in a test)."""
    import time

    import mrfx.parser as parser_mod
    import mrfx.store as store_mod
    from mrfx.ingest import _parse_worker, spool_path_for

    monkeypatch.setattr(ingest_mod, "CHUNK_COMPRESSED_BYTES", 4096)
    monkeypatch.setattr(parser_mod.InNetworkParser, "BATCH_ROWS", 5)
    cfg = _make_cfg(tmp_path, True)
    p = tmp_path / "f_2026-01.json.gz"
    p.write_bytes(gzip.compress(json.dumps({"in_network": [_item("97110", groups=G)] * 400}).encode()))
    tmp_out = tmp_path / ".k.4242.parquet.tmp"
    progress = str(tmp_path / ".k.4242.progress")
    real = store_mod.BackgroundParquetWriter.write_table
    seen = {}

    def write_then_parent_dies(self, table):
        real(self, table)
        deadline = time.monotonic() + 5
        while not tmp_out.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        seen["tmp"], seen["progress"] = tmp_out.exists(), Path(progress).exists()
        raise SystemExit("parent process died — abandoning orphaned parse")
    monkeypatch.setattr(store_mod.BackgroundParquetWriter, "write_table", write_then_parent_dies)
    with pytest.raises(SystemExit):
        _parse_worker(cfg, str(p), p.name, {}, {}, 0, p.stat().st_size, str(tmp_out), progress)
    assert seen == {"tmp": True, "progress": True}, seen          # not vacuous
    leftovers = [x for x in (tmp_out, spool_path_for(tmp_out), Path(progress), Path(progress + ".tmp")) if x.exists()]
    assert leftovers == []
