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
