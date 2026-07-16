"""Regression tests for the class-based hardening sweep: formula injection in
exports, SSN-TIN masking depth, user-editable-file crash tolerance."""

import json

import pytest

from mrfx.api import FilterSet, export_select, methodology_text, _mask_tin_sql
from mrfx.config import ConfigFileError, load_mrfx_config
from mrfx.entities import load_entity_map, save_entity_map
from mrfx.registry import Registry


def _write_rates(store, rows_spec):
    rows = [dict(
        payer=p, tin_value=t, tin_type="ein", npi=n, source_file="f.json",
        billing_code="97110", billing_code_type="CPT", discipline="pt", is_timed=True,
        billing_class="professional", negotiated_rate=50.0, negotiated_type="negotiated",
        is_dollar_rate=True, modifier_set=[], service_code=["11"], file_month="2026-06",
        last_updated_on="2026-06-01", expiration_date=None, schema_version="2.0.0",
        tin_is_really_npi=False, state=None,
    ) for p, t, n in rows_spec]
    with store.rates_part_writer("f.json") as w:
        w.write_batch(rows)
    store.rebuild_rollups()


def test_export_defuses_formulas_and_masks_ssn_per_element(store):
    _write_rates(store, [
        ('=HYPERLINK("http://evil")', "431111111", "1111111111"),  # formula payer
        ("Testco", "070000000", "1222222222"),                     # SSN-prefix TIN
    ])
    sql, params = export_select("tin", FilterSet({}), "negotiated_rate", "desc")
    with store.connect() as con:
        cols = [c[0] for c in con.execute(sql, params).description]
        recs = [dict(zip(cols, r)) for r in con.execute(sql, params).fetchall()]
    assert any(r["payer"] == "'=HYPERLINK(\"http://evil\")" for r in recs)  # quote-prefixed
    assert any(r["tin_value"] == "MASKED-SSN" for r in recs)
    # the no-NPPES-name fallback label must not leak the raw SSN-pattern TIN
    assert any(r["display_name"] == "TIN MASKED-SSN" for r in recs)
    # an SSN-pattern TIN hiding SECOND in a joined list is still masked
    with store.connect() as con:
        joined = con.execute(
            "SELECT " + _mask_tin_sql("v") + " FROM (SELECT '431111111; 070000000' AS v)"
        ).fetchone()[0]
    assert joined == "431111111; MASKED-SSN"


def test_methodology_lists_only_rate_files_and_says_so(cfg, store):
    store.upsert_file("rates.json", payer="Testco", file_type="in_network", status="done")
    store.upsert_file("refs.json", payer="Testco", file_type="provider_reference", status="done")
    text = methodology_text(cfg, store, "tin", FilterSet({}), "negotiated_rate", "desc", "test")
    assert "rates.json" in text and "refs.json" not in text
    assert "NOT necessarily all" in text  # no false provenance claim
    assert "one row per payer x" in text  # grain sentence includes payer


def test_bad_mrfx_yaml_raises_friendly_error(tmp_path):
    p = tmp_path / "mrfx.yaml"
    p.write_text("port: [unclosed")
    with pytest.raises(ConfigFileError):
        load_mrfx_config(p)
    p.write_text('port: "abc"')
    with pytest.raises(ConfigFileError, match="port"):
        load_mrfx_config(p)
    p.write_text("- just\n- a list\n")
    with pytest.raises(ConfigFileError, match="mapping"):
        load_mrfx_config(p)
    p.write_text("port: 99999")
    with pytest.raises(ConfigFileError, match="port"):
        load_mrfx_config(p)
    p.write_text("enrichment:\n  mode: bulck\n")
    with pytest.raises(ConfigFileError, match="mode"):
        load_mrfx_config(p)


def test_cli_reports_config_problem_without_traceback(tmp_path, capsys):
    from mrfx.cli import main

    p = tmp_path / "mrfx.yaml"
    p.write_text("port: [unclosed")
    assert main(["--config", str(p), "status"]) == 1
    assert "config problem" in capsys.readouterr().err


def test_entity_map_tolerates_every_bad_shape(tmp_path):
    p = tmp_path / "entity_map.yaml"
    # per-entry garbage: strings, scalar tins, unusable tins — all skipped or
    # coerced sanely; a scalar tin is ONE tin, never iterated character-wise
    p.write_text(json.dumps({"entities": [
        "just a string",
        {"name": "Acme", "tins": 431111111},
        {"name": "Beta", "tins": "43-2222222"},
        {"name": "Gamma", "tins": {"nested": True}},
        {"name": "Delta", "tins": ["433333333"]},
        {"tins": ["434444444"]},  # nameless -> skipped
    ]}))
    m = load_entity_map(p)
    assert m == {"431111111": "Acme", "432222222": "Beta", "433333333": "Delta"}
    # entities as a dict (not list) degrades to empty, not AttributeError
    p.write_text('entities:\n  oops: 1\n')
    assert load_entity_map(p) == {}
    # atomic save round-trips
    save_entity_map(p, {"431111111": "Acme"})
    assert load_entity_map(p) == {"431111111": "Acme"}
    assert not p.with_name(p.name + ".tmp").exists()


def test_registry_tolerates_per_entry_garbage(cfg, tmp_path):
    reg_p = tmp_path / "payer_registry.yaml"
    reg_p.write_text(json.dumps({
        "national_payers": ["not a dict", {"name": "Aetna", "mrf_url": "https://x", "verified": True}],
        "bcbs_by_state": {"MO": "not a dict either", "KS": {"licensee": "BCBS KS", "verified": True}},
    }))
    ov_p = tmp_path / "registry_overrides.yaml"
    ov_p.write_text("entries:\n- a\n- list\n")  # broken shape
    cfg.registry_path = reg_p
    cfg.registry_overrides_path = ov_p
    reg = Registry(cfg)
    assert [c["name"] for c in reg.national()] == ["Aetna"]
    assert reg.state("MO") == []          # malformed entry ignored, no 500
    assert reg.state("KS")[0]["name"] == "BCBS KS"
    assert sorted(reg.states()) == ["KS", "MO"]
    # saving an override heals the broken entries shape instead of TypeError
    reg.save_override("state:KS:BCBS KS", "https://example.com/toc.json")
    assert Registry(cfg).state("KS")[0]["verified_locally"] is True


def test_crash_recovery_flips_stuck_rows(cfg, store):
    # kill -9 leaves 'processing' files and mid-flight url rows; both must
    # recover at startup or they are skipped/ignored forever
    store.upsert_file("stuck.json", payer="X", file_type="in_network", status="processing")
    a = store.enqueue_url("https://x.example/a.json.gz", "ka")
    b = store.enqueue_url("https://x.example/b.json.gz", "kb")
    store.update_url(a, status="downloading")
    store.update_url(b, status="ingesting")
    assert store.recover_stuck_files() == 1
    st = store.file_status("stuck.json")
    assert st["status"] == "failed" and "re-ingest" in st["error"]
    assert store.recover_stuck_urls() == 2
    assert all(r["status"] == "queued" for r in store.list_urls())


def test_forget_api_guards_and_flow(cfg, store):
    # HTTP forget path: 404 unknown, 409 while processing, success payload,
    # and the done url row flips to skipped with a retry hint
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from mrfx.ingest import scan_inbox
    from tests.mrfx.conftest import drop

    client = TestClient(create_app(cfg, store))
    assert client.delete("/api/files/nope.json").status_code == 404

    p = drop(cfg, "innetwork_mixed.json", gz=True)
    scan_inbox(cfg, store)
    uid = store.enqueue_url("https://x.example/rates.json.gz", "kr")
    store.update_url(uid, status="done", filename=p.name, content_sha="ab" * 32)

    store.upsert_file(p.name, status="processing")
    assert client.delete(f"/api/files/{p.name}").status_code == 409  # mid-parse guard
    store.upsert_file(p.name, status="done")

    r = client.delete(f"/api/files/{p.name}")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "forgotten" and d["rows"] > 0 and d["bytes"] > 0
    assert store.file_status(p.name) is None
    (row,) = [u for u in store.list_urls() if u["id"] == uid]
    assert row["status"] == "skipped" and "retry" in row["error"]


def test_cosmetic_failures_never_fail_real_work(cfg, store, monkeypatch):
    # progress writes and derived QA stats decorate the real work — a store
    # briefly locked by an external reader must not fail a download, a
    # multi-hour parse, or flip a durable done-file to failed
    import mrfx.ingest as ing
    from mrfx.ingest import ingest_file, qa_report
    from tests.mrfx.conftest import drop

    def boom(*a, **k):
        raise RuntimeError("store briefly locked by an external reader")

    # 1. progress writers are best-effort by contract
    monkeypatch.setattr(store, "connect", boom)
    store.update_progress("x.json", 50.0)          # must not raise
    store.url_progress(1, 100, 200)                # must not raise
    monkeypatch.undo()

    # 2. QA aggregates degrade instead of failing the finished file
    monkeypatch.setattr(ing, "_qa_aggregates", boom)
    qa = qa_report(store, {"rows": 5, "non_dollar_rows": 0}, "x.json")
    assert qa["rows"] == 5 and any("unaffected" in m for m in qa["messages"])
    p = drop(cfg, "innetwork_mixed.json", gz=True)
    res = ingest_file(cfg, store, p)
    assert res["status"] == "done" and res["rows"] > 0  # done despite QA outage


def test_progress_bar_that_throws_never_fails_the_ingest(cfg, store, monkeypatch):
    # a display bar is cosmetic — if it raises (or, as in the regression, the
    # progress code UnboundLocalErrors), the parse must still complete. Forces
    # the chunked large-file path so the progress callback actually fires.
    import mrfx.ingest as ing
    from mrfx.ingest import ingest_file
    from tests.mrfx.conftest import drop

    monkeypatch.setattr(ing, "LARGE_FILE_UNCOMPRESSED_BYTES", 1)   # force two-pass
    monkeypatch.setattr(ing, "CHUNK_COMPRESSED_BYTES", 64)          # many progress ticks
    calls = {"n": 0}

    def boom(done, total, pct):
        calls["n"] += 1
        raise RuntimeError("terminal detached / bar exploded")

    p = drop(cfg, "innetwork_mixed.json", gz=True)
    res = ingest_file(cfg, store, p, progress_bar=boom)
    assert res["status"] == "done" and res["rows"] > 0  # parse survived the bad bar
    assert calls["n"] >= 1  # the bar really did fire (and throw)


def test_orphaned_parse_worker_exits_at_chunk_boundary(cfg, tmp_path, monkeypatch):
    # found live: killing the server left its parser workers re-parented to
    # init, burning 85% CPU on multi-GB parses nobody would ever collect.
    # A worker whose parent changed must abandon the parse at the next
    # chunk boundary instead of finishing it for nobody.
    import shutil

    import mrfx.ingest as ing
    from tests.mrfx.conftest import FIXTURES

    monkeypatch.setattr(ing, "CHUNK_COMPRESSED_BYTES", 512)  # tiny chunks
    calls = {"n": 0}

    def fake_getppid():
        calls["n"] += 1
        return 111 if calls["n"] == 1 else 222  # parent "dies" after entry

    monkeypatch.setattr(ing.os, "getppid", fake_getppid)
    p = tmp_path / "rates.json"
    shutil.copy(FIXTURES / "innetwork_mixed.json", p)
    with pytest.raises(SystemExit, match="parent process died"):
        ing._parse_worker(cfg, str(p), p.name, {}, {}, 0, p.stat().st_size,
                          str(tmp_path / "out.parquet.tmp"), str(tmp_path / "prog.json"))


def test_prefetch_reuses_kept_download_never_redownloads(cfg, store, tmp_path):
    # a revived duplicate's kept bytes must be used as-is: re-downloading
    # could hit an expired signed URL (terminal 403) and destroy the only copy
    import gzip
    import json as _json

    from mrfx.fetch import add_urls, fetch_url_record, filename_for, _meta_path
    from tests.mrfx.conftest import FIXTURES

    url = "https://dead.example/expired_rates.json.gz"  # unreachable on purpose
    add_urls(store, [url])
    dest = cfg.downloads_dir / filename_for(url)
    dest.write_bytes(gzip.compress((FIXTURES / "innetwork_mixed.json").read_bytes()))
    _meta_path(dest).write_text(_json.dumps({"sha": "cafe" * 16, "final_url": url}))
    rec = store.next_queued_url()
    assert fetch_url_record(cfg, store, rec) is True
    row = next(r for r in store.list_urls() if r["url"] == url)
    assert row["status"] == "fetched" and row["content_sha"] == "cafe" * 16
    assert dest.exists()  # bytes untouched


def test_successful_ingest_stays_done_when_file_move_fails(cfg, store, monkeypatch):
    # Windows antivirus/indexer can lock the just-written source file for a
    # moment; the post-success move into processed/ must NOT be able to flip an
    # already-'done' ingest to 'failed' and scare the user about good data.
    import mrfx.ingest as ing
    from mrfx.ingest import ingest_file
    from tests.mrfx.conftest import drop

    def boom(*a, **k):
        raise PermissionError("[WinError 32] file is in use by another process")

    monkeypatch.setattr(ing.shutil, "move", boom)
    p = drop(cfg, "innetwork_mixed.json")
    res = ingest_file(cfg, store, p)
    assert res["status"] == "done" and res["rows"] > 0        # ingest succeeded
    assert store.file_status(p.name)["status"] == "done"      # NOT flipped to failed


class _FakeChromium:
    """Stand-in for pw.chromium: raise the missing-browser error until Chromium
    is 'downloaded', then hand back a sentinel browser."""
    def __init__(self):
        self.installed = False
        self.launch_calls = 0

    def launch(self, **kwargs):
        self.launch_calls += 1
        if not self.installed:
            raise RuntimeError("Executable doesn't exist at /root/.cache/ms-playwright/…")
        return f"browser(launch#{self.launch_calls})"


def _reset_auto_install(monkeypatch):
    import mrfx.render as render
    monkeypatch.setattr(render, "_auto_install_attempted", False)
    monkeypatch.setattr(render, "_auto_install_enabled", True)
    monkeypatch.setattr(render, "_find_chromium", lambda: None)  # nothing on disk
    monkeypatch.delenv("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", raising=False)
    return render


def test_missing_chromium_is_downloaded_then_launch_retried(monkeypatch):
    # the layperson never ran `playwright install chromium`; a JS portal render
    # must self-heal by downloading the browser once and retrying, not drop the
    # row to a manual command.
    render = _reset_auto_install(monkeypatch)
    chromium = _FakeChromium()
    ran = {"cmd": None}

    class _Proc:
        returncode = 0
        stdout = stderr = ""

    def fake_run(cmd, **kw):
        ran["cmd"] = cmd
        chromium.installed = True  # the download succeeds
        return _Proc()

    monkeypatch.setattr(render.subprocess, "run", fake_run)

    class _PW:
        pass
    pw = _PW()
    pw.chromium = chromium

    browser = render._launch(pw)
    assert browser == "browser(launch#2)"          # first launch failed, retry won
    assert "install" in ran["cmd"] and "chromium" in ran["cmd"]
    assert chromium.launch_calls == 2


def test_missing_chromium_download_disabled_raises_actionable(monkeypatch):
    # when auto-install is turned off (or the managed env forbids re-fetching),
    # the caller must still get the actionable RenderBrowserMissing so the queue
    # row can show the one-line manual fix.
    render = _reset_auto_install(monkeypatch)
    monkeypatch.setattr(render, "_auto_install_enabled", False)
    ran = {"called": False}
    monkeypatch.setattr(render.subprocess, "run",
                        lambda *a, **k: ran.__setitem__("called", True))

    class _PW:
        pass
    pw = _PW()
    pw.chromium = _FakeChromium()  # never 'installs'
    with pytest.raises(render.RenderBrowserMissing):
        render._launch(pw)
    assert ran["called"] is False  # no download attempted when disabled


def test_chromium_auto_download_attempted_only_once(monkeypatch):
    # a failed download (offline) must not re-block every later render on a
    # ~10-minute install — the attempt is one-shot per process.
    render = _reset_auto_install(monkeypatch)
    calls = {"n": 0}

    class _Proc:
        returncode = 1  # download fails every time
        stdout = stderr = "network unreachable"

    def fake_run(cmd, **kw):
        calls["n"] += 1
        return _Proc()

    monkeypatch.setattr(render.subprocess, "run", fake_run)
    assert render._auto_install_chromium() is False
    assert render._auto_install_chromium() is False  # second call is a no-op
    assert calls["n"] == 1  # subprocess ran exactly once


def test_duckdb_temp_dir_override_honored(tmp_path):
    # A user on a slow HDD points the spill at a fast SSD; the store must use
    # a per-store subfolder UNDER that directory (two stores configured with
    # the same duckdb_temp_dir must never share one spill dir — DuckDB spill
    # file names are per-instance counters, not unique across instances).
    from mrfx.store import Store

    fast = tmp_path / "ssd_spill"
    store = Store(tmp_path / "store", temp_dir=fast)
    assert store._tmp_dir.parent == fast
    assert store._tmp_dir.name.startswith("spill-")
    assert store._tmp_dir.is_dir()
    # and it actually feeds DuckDB's temp_directory pragma
    with store.connect() as con:
        got = con.execute("SELECT current_setting('temp_directory')").fetchone()[0]
    assert str(store._tmp_dir) in got
    # a DIFFERENT store sharing the same duckdb_temp_dir gets its own subfolder
    other = Store(tmp_path / "store2", temp_dir=fast)
    assert other._tmp_dir != store._tmp_dir
    # the SAME store reopened maps back to the same subfolder (stable tag)
    again = Store(tmp_path / "store", temp_dir=fast)
    assert again._tmp_dir == store._tmp_dir


def test_duckdb_temp_dir_empty_string_means_unset(tmp_path, monkeypatch):
    # `duckdb_temp_dir: ""` in the YAML must mean "not set" — Path('') is the
    # process CWD and spill would silently land wherever the app was launched.
    from mrfx.config import MrfxConfig

    cfg = MrfxConfig(duckdb_temp_dir="")
    assert cfg.duckdb_temp_dir is None
    cfg = MrfxConfig(duckdb_temp_dir="   ")
    assert cfg.duckdb_temp_dir is None
    cfg = MrfxConfig(duckdb_temp_dir=str(tmp_path / "x"))
    assert cfg.duckdb_temp_dir == tmp_path / "x"


def test_duckdb_temp_dir_bad_path_falls_back(tmp_path):
    # An unusable override (e.g. a drive letter that doesn't exist) must NOT
    # stop the store from opening — it falls back to the in-store spill folder.
    from mrfx.store import Store

    store_dir = tmp_path / "store"
    # a path whose PARENT is a regular file can't be mkdir'd -> OSError
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    store = Store(store_dir, temp_dir=blocker / "cannot" / "exist")
    assert store._tmp_dir == store_dir / "duckdb_tmp"
    assert store._tmp_dir.is_dir()


def test_duckdb_temp_dir_default_is_in_store(tmp_path):
    from mrfx.store import Store

    store = Store(tmp_path / "store")
    assert store._tmp_dir == tmp_path / "store" / "duckdb_tmp"
    assert store.spill_is_relocated is False
    assert store.spill_is_auto is False


def test_parse_media_output_tolerant():
    from mrfx.store import _parse_media_output

    got = _parse_media_output("C=SSD\nE=HDD\n\ngarbage line\nD:=Unspecified\n=oops\nX=")
    assert got == {"C": "SSD", "E": "HDD", "D": "Unspecified"}
    assert _parse_media_output("") == {}


def test_choose_spill_drive_only_hdd_to_ssd():
    from mrfx.store import _choose_spill_drive

    big = lambda dl: 500 * 10**9  # noqa: E731 — plenty of room everywhere
    # store on an HDD (E), a roomy SSD (C) present -> relocate to C
    assert _choose_spill_drive("E", {"E": "HDD", "C": "SSD"}, big) == "C"
    # store already on an SSD -> never relocate
    assert _choose_spill_drive("C", {"C": "SSD", "E": "HDD"}, big) is None
    # store on HDD but NO ssd available -> keep in-store
    assert _choose_spill_drive("E", {"E": "HDD", "F": "HDD"}, big) is None
    # store drive type unknown/unspecified -> don't touch (conservative)
    assert _choose_spill_drive("E", {"E": "Unspecified", "C": "SSD"}, big) is None
    # prefer the ROOMIEST ssd
    free = {"C": 30 * 10**9, "D": 800 * 10**9}
    assert _choose_spill_drive("E", {"E": "HDD", "C": "SSD", "D": "SSD"},
                               lambda dl: free[dl]) == "D"
    # an ssd that's too full is skipped
    assert _choose_spill_drive("E", {"E": "HDD", "C": "SSD"},
                               lambda dl: 5 * 10**9) is None


def test_choose_spill_drive_case_insensitive():
    from mrfx.store import _choose_spill_drive

    assert _choose_spill_drive("e", {"E": "hdd", "C": "ssd"},
                               lambda dl: 500 * 10**9) == "C"


def test_store_uses_auto_selected_spill(tmp_path, monkeypatch):
    # Simulate the Windows auto-detect returning a fast SSD base: the store must
    # spill into a per-store subfolder there and flag it as auto-selected.
    import mrfx.store as store_mod
    from mrfx.store import Store

    fast = tmp_path / "ssd"
    monkeypatch.setattr(store_mod, "_auto_spill_base", lambda d: fast)
    store = Store(tmp_path / "store", auto_spill=True)
    assert store._tmp_dir.parent == fast
    assert store._tmp_dir.name.startswith("spill-")
    assert store.spill_is_relocated is True
    assert store.spill_is_auto is True
    with store.connect() as con:
        got = con.execute("SELECT current_setting('temp_directory')").fetchone()[0]
    assert str(store._tmp_dir) in got


def test_explicit_temp_dir_beats_auto(tmp_path, monkeypatch):
    # An explicit duckdb_temp_dir wins over auto-detection, and is NOT flagged
    # as auto (so the banner says "from your config").
    import mrfx.store as store_mod
    from mrfx.store import Store

    auto = tmp_path / "auto_ssd"
    explicit = tmp_path / "my_ssd"
    monkeypatch.setattr(store_mod, "_auto_spill_base", lambda d: auto)
    store = Store(tmp_path / "store", temp_dir=explicit, auto_spill=True)
    assert store._tmp_dir.parent == explicit
    assert store.spill_is_relocated is True
    assert store.spill_is_auto is False


def test_auto_spill_never_fatal(tmp_path, monkeypatch):
    # If auto-detection itself raises, the store still opens (in-store spill).
    import mrfx.store as store_mod
    from mrfx.store import Store

    def boom(_):
        raise RuntimeError("wmi exploded")

    monkeypatch.setattr(store_mod, "_auto_spill_base", boom)
    store = Store(tmp_path / "store", auto_spill=True)
    assert store._tmp_dir == tmp_path / "store" / "duckdb_tmp"
    assert store.spill_is_relocated is False
