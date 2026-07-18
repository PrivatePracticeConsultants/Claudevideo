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


def test_stale_spill_files_swept_on_open(tmp_path):
    # crash leftovers: DuckDB never removes pre-existing temp files itself, so
    # the store sweeps duckdb_temp* in ITS spill dir on open (lock held = ours)
    from mrfx.store import Store

    fast = tmp_path / "ssd"
    s1 = Store(tmp_path / "store", temp_dir=fast)
    dead = s1._tmp_dir / "duckdb_temp_storage-123.tmp"
    dead.write_bytes(b"x" * 1024)
    s1.close()
    s2 = Store(tmp_path / "store", temp_dir=fast)
    assert not dead.exists()
    s2.close()


def test_explicit_spill_fallback_is_flagged(tmp_path):
    # an unusable duckdb_temp_dir must be VISIBLE (serve banner reads the flag),
    # not a silent in-store fallback the user mistakes for success
    from mrfx.store import Store

    blocker = tmp_path / "afile"
    blocker.write_text("x")
    store = Store(tmp_path / "store", temp_dir=blocker / "no" / "way")
    assert store.spill_is_relocated is False
    assert store.spill_fallback_from is not None
    assert "no" in store.spill_fallback_from


def test_explorer_month_filter_rejects_latest(cfg, store):
    # 'latest' is the REPORT tabs' sentinel; the explorer month filter matches
    # file_month literally, so it must 422 instead of silently matching 0 rows
    # while the methodology sidecar claims the filter applied
    from fastapi.testclient import TestClient

    from mrfx.api import create_app

    c = TestClient(create_app(cfg, store))
    r = c.get("/api/rates?month=latest")
    assert r.status_code == 422
    assert "real month" in r.json()["detail"]
    assert c.get("/api/rates?month=2026-06").status_code == 200


def test_no_route_blocks_the_event_loop(cfg, store):
    # Every endpoint must be a sync `def` (threadpool) — an `async def` doing
    # store/compute work blocks uvicorn's single event loop, and the whole
    # server (including the dashboard's status poll) reads as "API unreachable"
    # for the duration of a heavy benchmark or a rollup's write-lock wait.
    import asyncio

    from fastapi.routing import APIRoute

    from mrfx.api import create_app

    # ONE deliberate exception: /api/debug/stacks runs on the event loop so it
    # still answers when every threadpool worker is wedged — the situation it
    # exists to diagnose. It does no store/compute work (in-memory frame walk).
    offenders = [r.path for r in create_app(cfg, store).routes
                 if isinstance(r, APIRoute) and asyncio.iscoroutinefunction(r.endpoint)
                 and r.path != "/api/debug/stacks"]
    assert offenders == [], f"async endpoints would block the event loop: {offenders}"


def test_config_paths_anchor_to_project_not_cwd(tmp_path, monkeypatch):
    # relative paths in <root>/config/mrfx.yaml must resolve against <root>,
    # NOT the terminal's current directory — running `mrfx serve` from the
    # wrong folder used to silently create a brand-new empty store there
    # (seen live as "the dashboard is completely empty after an update")
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "config" / "mrfx.yaml").write_text(
        "store_dir: data/mrfx_store\ninbox_dir: data/inbox\n"
        "enrichment:\n  mode: bulk\n  bulk_csv_path: nppes.zip\n")
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cfg = load_mrfx_config(root / "config" / "mrfx.yaml")
    assert cfg.store_dir == root / "data" / "mrfx_store"
    assert cfg.inbox_dir == root / "data" / "inbox"
    assert cfg.enrichment.bulk_csv_path == root / "nppes.zip"
    # absolute paths in the file are honored untouched
    (root / "config" / "mrfx.yaml").write_text(
        f'store_dir: "{tmp_path / "abs_store"}"\n')
    cfg = load_mrfx_config(root / "config" / "mrfx.yaml")
    assert cfg.store_dir == tmp_path / "abs_store"
    # a config NOT in a config/ folder anchors to its own directory
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / "mrfx.yaml").write_text("store_dir: data/mrfx_store\n")
    cfg = load_mrfx_config(lone / "mrfx.yaml")
    assert cfg.store_dir == lone / "data" / "mrfx_store"


def test_refresh_interval_uses_build_time_not_lock_wait():
    # a refresh that queued 40 min behind an ingest rollup did not "take"
    # 40 min — the adaptive cadence must read the store's measured BUILD time,
    # or one contended refresh silences name updates for hours
    import time as _time

    import mrfx.enrich as en

    class FakeStore:
        # simulates: long wall time (lock wait) but a fast actual build
        def rebuild_rollups(self, names_only=False):
            _time.sleep(0.05)  # stand-in for a long lock wait
            return 1.0  # the real build was fast

    en._last_dir_refresh = 0.0
    en._dir_refresh_interval = en._DIRECTORY_REFRESH_SECONDS
    assert en._maybe_refresh_directory(FakeStore(), force=True) is True
    assert en._dir_refresh_interval == en._DIRECTORY_REFRESH_SECONDS  # 5*1.0 < floor

    class SlowBuildStore:
        def rebuild_rollups(self, names_only=False):
            return 300.0  # genuinely slow build

    assert en._maybe_refresh_directory(SlowBuildStore(), force=True) is True
    assert en._dir_refresh_interval == 1500.0
    en._dir_refresh_interval = en._DIRECTORY_REFRESH_SECONDS  # reset for other tests
    en._last_dir_refresh = 0.0


def test_project_root_config_dir_case_insensitive(tmp_path):
    # Windows folders are case-insensitive: <root>/Config/mrfx.yaml must anchor
    # to <root>, exactly like <root>/config/mrfx.yaml
    root = tmp_path / "proj2"
    (root / "Config").mkdir(parents=True)
    (root / "Config" / "mrfx.yaml").write_text("store_dir: data/mrfx_store\n")
    cfg = load_mrfx_config(root / "Config" / "mrfx.yaml")
    assert cfg.store_dir == root / "data" / "mrfx_store"


def test_entity_map_edit_survives_long_open_snapshot(store):
    # While ANY long transaction (a rollup rebuild) holds an old snapshot,
    # DuckDB can't vacuum a deleted PK key, so the old DELETE-all + re-INSERT
    # in set_entity_map raised a spurious 'Duplicate key' — every entity edit
    # made during an ingest 500'd. The upsert-shaped replace must survive.
    store.set_entity_map({"222222222": "A"})
    old = store.connect()
    old.execute("BEGIN TRANSACTION")
    old.execute("SELECT count(*) FROM entity_map")
    try:
        store.set_entity_map({"222222222": "B"})                       # same key, new name
        store.set_entity_map({"222222222": "C", "333333333": "D"})     # add a key
        store.set_entity_map({"333333333": "D"})                       # remove a key
    finally:
        old.execute("ROLLBACK")
        old.close()
    assert store.entity_map() == {"333333333": "D"}


def test_debug_stacks_endpoint(cfg, store):
    import asyncio

    from fastapi.routing import APIRoute
    from fastapi.testclient import TestClient

    from mrfx.api import create_app

    app = create_app(cfg, store)
    c = TestClient(app)
    r = c.get("/api/debug/stacks")
    assert r.status_code == 200
    assert "--- thread " in r.text and "threading.py" in r.text
    # It must stay async (event loop, not threadpool): the diagnostic has to
    # answer even when a stall has consumed every threadpool worker.
    route = next(rt for rt in app.routes
                 if isinstance(rt, APIRoute) and rt.path == "/api/debug/stacks")
    assert asyncio.iscoroutinefunction(route.endpoint), (
        "debug_stacks must not depend on a threadpool worker being free")


def test_stack_recorder_writes_and_refreshes(tmp_path):
    # The flight recorder is the diagnosis channel of last resort — when the
    # server is so wedged even HTTP is dead, <store>/diagnostics/
    # stacks_latest.txt must exist, contain thread stacks, and keep
    # refreshing (its heartbeat is how a dead process is distinguished
    # from a jammed one).
    import threading
    import time

    from mrfx.cli import _start_stack_recorder

    stop = threading.Event()
    _start_stack_recorder(tmp_path, interval=0.2, stop=stop)
    try:
        path = tmp_path / "diagnostics" / "stacks_latest.txt"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not path.exists():
            time.sleep(0.05)
        assert path.exists(), "recorder never wrote its first snapshot"
        text = path.read_text(encoding="utf-8")
        assert "snapshot written" in text and "--- thread " in text
        first_mtime = path.stat().st_mtime_ns
        while time.monotonic() < deadline:
            if path.stat().st_mtime_ns != first_mtime:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("recorder never refreshed the snapshot")
    finally:
        stop.set()


def test_rollup_never_rebuilds_back_to_back_mid_grind():
    # The field failure: an unconditioned "10 files pending" trigger meant a
    # big store's hour-long rebuild ended with the next batch already
    # pending, so rebuilds ran back-to-back and extraction starved for the
    # disk ("chunks frozen, CPU busy"). Mid-grind, ONLY the adaptive
    # interval may trigger — no pending count, however large, forces it.
    from mrfx.fetch import _rollup_due

    assert not _rollup_due(500, 3, 10.0, 3600.0)      # huge backlog, interval not passed
    assert _rollup_due(1, 3, 3600.0, 3600.0)          # interval passed → rebuild
    assert _rollup_due(1, 0, 0.0, 3600.0)             # queue idle → final rebuild now
    assert not _rollup_due(0, 0, 99999.0, 1.0)        # nothing pending → never


def test_scan_inbox_rebuilds_once_per_pass(cfg, store, monkeypatch):
    # Two files dropped together must cost ONE full analytics rebuild, not
    # one per file (each is a full scan of the entire store).
    import mrfx.ingest as ingest_mod
    from tests.mrfx.conftest import drop

    drop(cfg, "innetwork_mixed.json", gz=True)
    # same fixture under a second name = a second real ingest in one pass
    import gzip
    import shutil
    src = cfg.inbox_dir / "innetwork_mixed.json.gz"
    with gzip.open(src) as fin, open(cfg.inbox_dir / "second_copy.json", "wb") as fout:
        shutil.copyfileobj(fin, fout)

    calls = []
    real = store.rebuild_rollups
    monkeypatch.setattr(store, "rebuild_rollups",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    results = ingest_mod.scan_inbox(cfg, store)
    assert [r["status"] for r in results].count("done") == 2
    assert len(calls) == 1


def _payer_part(store, fname, payer, rows):
    # rows: (tin, npi, code, month, rate)
    recs = [dict(
        payer=payer, tin_value=t, tin_type="ein", npi=n, source_file=fname,
        billing_code=c, billing_code_type="CPT", discipline="pt", is_timed=True,
        billing_class="professional", negotiated_rate=r, negotiated_type="negotiated",
        is_dollar_rate=True, modifier_set=[], service_code=["11"], file_month=m,
        last_updated_on="2026-06-01", expiration_date=None, schema_version="2.0.0",
        tin_is_really_npi=False, state="MO",
    ) for (t, n, c, m, r) in rows]
    with store.rates_part_writer(fname) as w:
        w.write_batch(recs)
    store.upsert_file(fname, payer=payer, file_type="in_network", status="done",
                      rows_emitted=len(recs))


def _rollup_snapshot(store):
    with store.connect() as con:
        # source_files is any_value() — legitimately nondeterministic for a
        # group spanning several files, so it's excluded from the comparison
        spine = con.execute(
            "SELECT * EXCLUDE (source_files) FROM rates_by_tin_tbl "
            "ORDER BY payer, tin_value, billing_code, file_month, modifier_set, "
            "billing_class, service_code_set, is_dollar_rate, tin_is_really_npi"
        ).fetchall()
        directory = con.execute(
            "SELECT * FROM tin_directory_tbl ORDER BY tin_value").fetchall()
    return spine, directory


def test_incremental_rollup_matches_full_rebuild(cfg, store):
    # Numbers correctness IS the product: the payer-slice incremental update
    # must produce BIT-IDENTICAL rollups to a full rebuild — including a
    # group that spans an old file and a new file (median over both).
    import duckdb as _duckdb  # noqa: F401

    _payer_part(store, "a1.json", "Alpha", [
        ("431111111", "1111111111", "97110", "2026-06", 50.0),
        ("431111111", "1111111112", "97110", "2026-06", 60.0),
        ("432222222", "1111111113", "97112", "2026-06", 70.0),
    ])
    store.upsert_file("a1.json", finished_at="2026-07-01 10:00:00")
    _payer_part(store, "b1.json", "Beta", [
        ("433333333", "1111111114", "97110", "2026-06", 80.0),
    ])
    store.upsert_file("b1.json", finished_at="2026-07-01 10:00:01")
    store.rebuild_rollups()
    assert not store.rollups_stale()

    # a new Alpha file: a new month, a brand-new TIN, and an overlapping
    # (same tin/code/month as a1) rate that must merge into the group median
    _payer_part(store, "a2.json", "Alpha", [
        ("431111111", "1111111111", "97110", "2026-07", 55.0),
        ("434444444", "1111111115", "97110", "2026-07", 90.0),
        ("431111111", "1111111116", "97110", "2026-06", 70.0),
    ])
    store.upsert_file("a2.json", finished_at="2026-07-01 11:00:00")
    assert store.rollups_stale()

    took = store.update_rollups_incremental()
    assert took > 0.0 and not store.rollups_stale()
    inc = _rollup_snapshot(store)

    # cross-file group: median of {50, 60, 70} = 60, 2 source files, 3 NPIs
    with store.connect() as con:
        row = con.execute(
            "SELECT negotiated_rate, source_count, npi_count FROM rates_by_tin_tbl "
            "WHERE tin_value = '431111111' AND file_month = '2026-06'").fetchone()
    assert row == (60.0, 2, 3)

    store.rebuild_rollups()
    assert inc == _rollup_snapshot(store)

    # nothing new -> the incremental update is a fast no-op
    assert store.update_rollups_incremental() == 0.0


def test_incremental_rollup_falls_back_when_preconditions_missing(cfg, store):
    # no prior full build (no schema marker / tables) -> must raise so
    # callers run the always-correct full rebuild instead
    _payer_part(store, "x.json", "Gamma", [
        ("435555555", "1111111117", "97110", "2026-06", 40.0),
    ])
    store.upsert_file("x.json", finished_at="2026-07-01 10:00:00")
    with pytest.raises(Exception):
        store.update_rollups_incremental()
    # rollup tables built by an OLDER schema version -> raise, so the
    # migration goes through the full rebuild (which re-stamps the marker)
    store.rebuild_rollups()
    _payer_part(store, "y.json", "Delta", [
        ("436666666", "1111111118", "97110", "2026-06", 45.0),
    ])
    store.upsert_file("y.json", finished_at="2026-07-02 10:00:00")
    with store.connect() as con:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_schema_version', '1')")
    with pytest.raises(Exception):
        store.update_rollups_incremental()


def test_incremental_rollup_heals_zero_row_reingest(cfg, store):
    # A file re-ingested down to 0 rows drops its parquet part — but its
    # files row leaves the rows_emitted>0 delta, so without the queued-slice
    # note the removed rows would keep being served with rollups_stale()
    # False (honesty-contract violation, found in the launch audit).
    _payer_part(store, "z1.json", "Zeta", [
        ("437777777", "1111111119", "97110", "2026-06", 42.0)])
    store.upsert_file("z1.json", finished_at="2026-07-01 10:00:00")
    store.rebuild_rollups()
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM rates_by_tin_tbl "
                           "WHERE payer = 'Zeta'").fetchone()[0] == 1
    store.drop_rates_part("z1.json")  # the short-circuit re-ingest path
    store.upsert_file("z1.json", rows_emitted=0, finished_at="2026-07-01 11:00:00")
    assert store.rollups_stale()      # queued slice = detectable staleness
    store.update_rollups_incremental()
    assert not store.rollups_stale()
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM rates_by_tin_tbl "
                           "WHERE payer = 'Zeta'").fetchone()[0] == 0
        # the ghost directory row is pruned too
        assert con.execute("SELECT count(*) FROM tin_directory_tbl "
                           "WHERE tin_value = '437777777'").fetchone()[0] == 0


def test_incremental_rollup_boundary_same_microsecond(cfg, store):
    # A done-upsert committing AFTER the marker stamp with an IDENTICAL
    # finished_at (same-microsecond race) must still be detected and rolled
    # up — the marker tie-breaks by filename, never by timestamp alone.
    _payer_part(store, "m1.json", "Mu", [
        ("438888888", "1111111120", "97110", "2026-06", 30.0)])
    store.upsert_file("m1.json", finished_at="2026-07-01 10:00:00")
    store.rebuild_rollups()
    assert not store.rollups_stale()
    _payer_part(store, "m2.json", "Nu", [
        ("439999999", "1111111121", "97110", "2026-06", 33.0)])
    store.upsert_file("m2.json", finished_at="2026-07-01 10:00:00")  # same instant
    assert store.rollups_stale()
    store.update_rollups_incremental()
    assert not store.rollups_stale()
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM rates_by_tin_tbl "
                           "WHERE payer = 'Nu'").fetchone()[0] == 1
    assert store.update_rollups_incremental() == 0.0


def test_scoped_name_refresh_matches_full_rebuild(cfg, store):
    # The enrichment name-refresh must no longer re-scan the whole store:
    # refresh_directory_incremental recomputes only TINs touched by newly
    # enriched NPIs — and its result must equal a full names rebuild.
    _payer_part(store, "n1.json", "Omega", [
        ("441111111", "1311111111", "97110", "2026-06", 55.0),
        ("442222222", "1322222222", "97110", "2026-06", 65.0),
    ])
    store.upsert_file("n1.json", finished_at="2026-07-01 10:00:00")
    store.rebuild_rollups()  # stamps directory_names_through

    store.save_npi("1311111111", org_name="Omega Physical Therapy LLC",
                   taxonomy_code="225100000X", taxonomy_desc=None,
                   city="ST LOUIS", state="MO", entity_type="NPI-2")
    took = store.refresh_directory_incremental()
    assert isinstance(took, float)
    with store.connect() as con:
        named = con.execute("SELECT display_name FROM tin_directory_tbl "
                            "WHERE tin_value = '441111111'").fetchone()[0]
        other = con.execute("SELECT display_name FROM tin_directory_tbl "
                            "WHERE tin_value = '442222222'").fetchone()[0]
    assert named == "Omega Physical Therapy LLC"
    assert other.startswith("TIN ")  # untouched TIN keeps its fallback label
    inc_rows = None
    with store.connect() as con:
        inc_rows = con.execute(
            "SELECT * FROM tin_directory_tbl ORDER BY tin_value").fetchall()
    store.rebuild_rollups(names_only=True)
    with store.connect() as con:
        full_rows = con.execute(
            "SELECT * FROM tin_directory_tbl ORDER BY tin_value").fetchall()
    assert inc_rows == full_rows


def test_rollback_provenance_guard_forces_one_full_rebuild(cfg, store):
    # The user's real history: rollups rebuilt by an OLDER app copy leave the
    # version marker intact but not the covered-names key. Reopening the
    # store must schedule one full rebuild and refuse incremental updates
    # until it runs.
    from mrfx.store import Store

    _payer_part(store, "p1.json", "Rho", [
        ("443333333", "1333333333", "97110", "2026-06", 45.0)])
    store.upsert_file("p1.json", finished_at="2026-07-01 10:00:00")
    store.rebuild_rollups()
    assert not store.rollups_stale()
    with store.connect() as con:  # simulate an old build's rebuild history
        con.execute("DELETE FROM meta WHERE key = 'rollup_covered_names'")
    reopened = Store(cfg.store_dir, 2)
    assert reopened.rollups_stale()          # needs_full queued at open
    with pytest.raises(Exception):
        reopened.update_rollups_incremental()
    reopened.rebuild_rollups()               # the one-time full pass
    assert not reopened.rollups_stale()
    assert reopened.update_rollups_incremental() == 0.0


def test_forget_recomputes_shared_tin_directory_flags(cfg, store):
    # Launch-audit HIGH regression: a TIN shared across two files, one carrying
    # a HOSPITAL NPI (excludes it from leads) and one a THERAPY NPI. Forgetting
    # the hospital file must flip has_hospital→False / is_therapy→True — the
    # incremental removal path used to leave the directory row stale
    # (rollups_stale()==False), silently mis-qualifying the lead.
    _payer_part(store, "hosp.json", "Hospital Plan", [
        ("450000001", "1400000001", "97110", "2026-06", 60.0)])
    store.upsert_file("hosp.json", finished_at="2026-07-01 10:00:00")
    _payer_part(store, "ther.json", "Therapy Plan", [
        ("450000001", "1400000002", "97110", "2026-06", 62.0)])
    store.upsert_file("ther.json", finished_at="2026-07-01 10:00:01")
    store.save_npi("1400000001", org_name="Big Hospital System",
                   taxonomy_code="282N00000X", taxonomy_desc=None,
                   city="ST LOUIS", state="MO", entity_type="NPI-2")
    store.save_npi("1400000002", org_name="Downtown PT",
                   taxonomy_code="2251C2600X", taxonomy_desc=None,
                   city="ST LOUIS", state="MO", entity_type="NPI-2")
    store.rebuild_rollups()
    with store.connect() as con:
        row = con.execute("SELECT has_hospital, is_therapy, npi_count FROM "
                          "tin_directory_tbl WHERE tin_value = '450000001'").fetchone()
    assert row == (True, False, 2)  # hospital NPI present → excluded from leads

    from mrfx.ingest import forget_file
    forget_file(cfg, store, "hosp.json")
    assert not store.rollups_stale()  # removal fully accounted for
    with store.connect() as con:
        inc = con.execute("SELECT has_hospital, is_therapy, npi_count FROM "
                          "tin_directory_tbl WHERE tin_value = '450000001'").fetchone()
    assert inc == (False, True, 1)  # now purely therapy → qualifies as a lead

    # must match a full rebuild exactly
    store.rebuild_rollups()
    with store.connect() as con:
        full = con.execute("SELECT has_hospital, is_therapy, npi_count FROM "
                           "tin_directory_tbl WHERE tin_value = '450000001'").fetchone()
    assert inc == full


def test_store_stats_and_states_cached_single_flight(cfg, store):
    # The stall fix: store_stats and available_states must serve from cache
    # within the TTL (no repeat whole-store scan on the 15s dashboard poll).
    _payer_part(store, "s.json", "Sig", [
        ("451111111", "1411111111", "97110", "2026-06", 40.0)])
    store.upsert_file("s.json", finished_at="2026-07-01 10:00:00")
    store.rebuild_rollups()
    a = store.store_stats()
    assert a["rates"] == 1 and a["payers"] == 1
    # second call within TTL returns the SAME cached dict object
    assert store.store_stats() is store._store_stats_cache[1]
    st = store.available_states()
    assert store.available_states() is store._states_cache[1]
