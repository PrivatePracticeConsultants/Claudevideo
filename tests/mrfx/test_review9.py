"""Review 9 (docs/NEXT_STEPS_PROMPT_9.md): each test reproduces a defect found
at 64a371c and fails there; the reproductions are in docs/review9_repros/."""

import gzip
import json
import logging
import os
import time

import pytest

import mrfx.store as S
from mrfx.ingest import ingest_file, scan_inbox

HDR = {"reporting_entity_name": "Acme Health", "version": "1.0", "last_updated_on": "2026-05-01"}


def grp(npi, tin):
    return {"npi": [npi], "tin": {"type": "ein", "value": tin}}


def item(code, refs=None, groups=None, rate=50.0, ntype="negotiated"):
    nr = {"negotiated_prices": [{"negotiated_type": ntype, "negotiated_rate": rate,
                                 "billing_class": "professional", "service_code": ["11"]}]}
    if refs is not None:
        nr["provider_references"] = refs
    if groups is not None:
        nr["provider_groups"] = groups
    return {"negotiation_arrangement": "ffs", "billing_code_type": "CPT",
            "billing_code": code, "negotiated_rates": [nr]}


def put(cfg, name, obj, raw=None):
    p = cfg.inbox_dir / name
    p.write_bytes(raw if raw is not None else json.dumps(obj).encode())
    return p


def q(store, sql, *a):
    with store.connect() as con:
        return con.execute(sql, list(a)).fetchall()


def big_doc(rate, n=7000, payer="Acme Health", month="2026-05-01"):
    """~2.4 MB: between 1 MiB and 15 MiB and not a whole number of MiB —
    the shape 0.7.1's floored MiB count never recognised as itself."""
    return {"reporting_entity_name": payer, "version": "1.0", "last_updated_on": month,
            "in_network": [item("97110", groups=[grp(f"1{i:09d}", f"{100000000 + i}")],
                                rate=rate + (i % 7)) for i in range(n)]}


# -- 1.1 the stored-row update -------------------------------------------------

def _age_part(store, name, ntype="fee_schedule", dollar=False, rules=2):
    part = store.rates_dir / f"{S.file_key(name)}.parquet"
    old = part.with_suffix(".old")
    with store.connect() as con:
        con.execute(f"COPY (SELECT * REPLACE ('{ntype}' AS negotiated_type, {str(dollar).lower()} "
                    f"AS is_dollar_rate) FROM read_parquet('{S.sql_path(part)}')) TO "
                    f"'{S.sql_path(old)}' (FORMAT parquet)")
        con.execute("UPDATE files SET row_rules = ? WHERE filename = ?", [rules, name])
    old.replace(part)
    return part


def _doc1(rate=50.0):
    return {**HDR, "in_network": [item("97110", groups=[grp("1234567893", "123456789")], rate=rate)]}


def test_update_never_stamps_a_part_it_could_not_match(cfg, store, monkeypatch):
    # DuckDB answering with the part's path spelled differently (letter
    # case, a prefix — as a Windows path may): 0.7.2 stamped the file 3
    # with its 'fee_schedule' rows still out of every median
    cfg.move_processed = False
    ingest_file(cfg, store, put(cfg, "acme_2026-05.json", _doc1()))
    _age_part(store, "acme_2026-05.json")
    store.close()

    real = S.Store.connect

    class Respelled:
        def __init__(self, con):
            self._c = con

        def __getattr__(self, k):
            return getattr(self._c, k)

        def __enter__(self):
            self._c.__enter__()
            return self

        def __exit__(self, *a):
            return self._c.__exit__(*a)

        def execute(self, sql, params=None):
            r = self._c.execute(sql, params) if params is not None else self._c.execute(sql)
            if "filename = true" in sql:
                rows = [(str(f).replace("/rates/", "/RATES/"), t, d) for f, t, d in r.fetchall()]

                class F:
                    def fetchall(self):
                        return rows
                return F()
            return r

    monkeypatch.setattr(S.Store, "connect", lambda self, *a, **k: Respelled(real(self, *a, **k)))
    st2 = S.Store(cfg.store_dir)
    monkeypatch.setattr(S.Store, "connect", real)
    assert q(st2, "SELECT negotiated_type, is_dollar_rate FROM rates") == [("fee schedule", True)]
    assert q(st2, "SELECT row_rules FROM files") == [(S.ROW_RULES_VERSION,)]


def test_one_bad_part_does_not_block_the_others_and_the_log_says_so(cfg, store, caplog):
    cfg.move_processed = False
    for i in range(4):
        ingest_file(cfg, store, put(cfg, f"acme{i}_2026-05.json", _doc1(40.0 + i)))
        _age_part(store, f"acme{i}_2026-05.json")
    bad = store.rates_dir / f"{S.file_key('acme2_2026-05.json')}.parquet"
    data = bytearray(bad.read_bytes())
    data[-8:-4] = b"\xff\xff\xff\x7f"          # valid PAR1 magic, broken footer length
    bad.write_bytes(bytes(data))
    store.close()
    with caplog.at_level(logging.INFO, logger="mrfx.store"):
        st2 = S.Store(cfg.store_dir)
    rules = dict(q(st2, "SELECT filename, row_rules FROM files"))
    assert rules["acme2_2026-05.json"] == 2                  # left for the next start
    assert all(v == S.ROW_RULES_VERSION for k, v in rules.items() if k != "acme2_2026-05.json")
    assert "up to date" not in caplog.text
    assert "1 file(s) could not be read" in caplog.text
    for i in (0, 1, 3):
        part = st2.rates_dir / f"{S.file_key(f'acme{i}_2026-05.json')}.parquet"
        assert q(st2, f"SELECT DISTINCT negotiated_type, is_dollar_rate FROM "
                      f"read_parquet('{S.sql_path(part)}')") == [("fee schedule", True)]


def test_type_rewrite_never_doubles_rows(cfg, store):
    # one stored spelling with BOTH dollar flags in a part: the old join on
    # the type alone matched each row twice
    cfg.move_processed = False
    doc = {**HDR, "in_network": [item("97110", groups=[grp("1234567893", "123456789")]),
                                 item("97112", groups=[grp("1234567893", "123456789")], rate=60.0)]}
    ingest_file(cfg, store, put(cfg, "acme_2026-05.json", doc))
    part = store.rates_dir / f"{S.file_key('acme_2026-05.json')}.parquet"
    old = part.with_suffix(".old")
    with store.connect() as con:
        con.execute(f"COPY (SELECT * REPLACE ('fee_schedule' AS negotiated_type, "
                    f"billing_code = '97110' AS is_dollar_rate) FROM read_parquet('{S.sql_path(part)}')) "
                    f"TO '{S.sql_path(old)}' (FORMAT parquet)")
        con.execute("UPDATE files SET row_rules = 2")
    old.replace(part)
    store.close()
    st2 = S.Store(cfg.store_dir)
    assert q(st2, "SELECT billing_code, negotiated_type, is_dollar_rate FROM rates ORDER BY 1") == [
        ("97110", "fee schedule", True), ("97112", "fee schedule", True)]


def test_update_is_batched_with_progress(cfg, store, caplog, monkeypatch):
    cfg.move_processed = False
    for i in range(5):
        ingest_file(cfg, store, put(cfg, f"acme{i}_2026-05.json", _doc1(40.0 + i)))
    with store.connect() as con:
        con.execute("UPDATE files SET row_rules = 2")
    store.close()
    monkeypatch.setattr(S, "_TYPE_SCAN_BATCH", 2)
    calls = []
    real = S.Store._migrate_types_only

    def spy(self, names, progress):
        def p(done):
            calls.append(done)
            progress(done)
        return real(self, names, p)
    monkeypatch.setattr(S.Store, "_migrate_types_only", spy)
    S.Store(cfg.store_dir)
    assert calls == [2, 4, 5]


# -- 1.2 the same file is the same file ----------------------------------------

@pytest.mark.parametrize("move", [False, True])
def test_the_same_2_3_mib_file_is_never_loaded_twice(cfg, store, move):
    cfg.move_processed = move
    body = json.dumps(big_doc(40.0)).encode()
    assert 1 << 20 < len(body) < 15 << 20 and len(body) % (1 << 20)
    put(cfg, "acme_2026-05.json", None, body)
    scan_inbox(cfg, store)
    for _ in range(3):
        if move:
            put(cfg, "acme_2026-05.json", None, body)        # dropped again
        scan_inbox(cfg, store)
    assert q(store, "SELECT count(*) FROM files") == [(1,)]
    assert q(store, "SELECT count(*) FROM rates")[0][0] == 7000


def test_the_watcher_does_not_loop_on_a_kept_file(cfg, store):
    cfg.move_processed = False
    put(cfg, "acme_2026-05.json", None, json.dumps(big_doc(40.0)).encode())
    for _ in range(4):
        scan_inbox(cfg, store)
    assert [p.name for p in cfg.inbox_dir.iterdir() if p.is_file()] == ["acme_2026-05.json"]
    assert q(store, "SELECT count(*) FROM files") == [(1,)]


def test_a_corrected_file_of_the_same_size_is_held_then_replaces(cfg, store):
    from mrfx.ingest import resolve_choice
    body = json.dumps(big_doc(40.0)).encode()
    fixed = body.replace(b'"negotiated_rate": 40.0', b'"negotiated_rate": 55.0', 1)
    assert len(fixed) == len(body) and fixed != body
    put(cfg, "acme_2026-05.json", None, body)
    scan_inbox(cfg, store)
    os.utime(cfg.processed_dir / "acme_2026-05.json", (1, 1))   # an old build kept no identity
    with store.connect() as con:
        con.execute("DELETE FROM content_fingerprints")
    put(cfg, "acme_2026-05.json", None, fixed)
    res = scan_inbox(cfg, store)
    held = [r for r in res if r["status"] == "needs_choice"]
    assert held, res                        # never ignored, never blended
    rows = q(store, "SELECT filename, status FROM files WHERE status = 'needs_choice'")
    assert len(rows) == 1
    r = resolve_choice(cfg, store, rows[0][0], "replace")
    ingest_file(cfg, store, r["path"])
    assert q(store, "SELECT count(*) FROM rates WHERE negotiated_rate = 55.0") == [(1,)]
    assert q(store, "SELECT count(*) FROM rates")[0][0] == 7000
    assert q(store, "SELECT filename FROM files") == [("acme_2026-05.json",)]


def test_keep_both_loads_the_held_file_beside(cfg, store):
    from mrfx.ingest import resolve_choice
    put(cfg, "acme_2026-05.json", _doc1(40.0))
    scan_inbox(cfg, store)
    put(cfg, "acme_2026-05.json", _doc1(60.0))
    scan_inbox(cfg, store)
    (name,) = q(store, "SELECT filename FROM files WHERE status = 'needs_choice'")[0]
    r = resolve_choice(cfg, store, name, "keep")
    ingest_file(cfg, store, r["path"])
    assert sorted(x[0] for x in q(store, "SELECT negotiated_rate FROM rates")) == [40.0, 60.0]


def test_another_month_under_the_same_name_loads_once_under_a_content_name(cfg, store):
    put(cfg, "acme.json", _doc1(40.0))
    scan_inbox(cfg, store)
    june = {**_doc1(45.0), "last_updated_on": "2026-06-01"}
    put(cfg, "acme.json", june)
    scan_inbox(cfg, store)
    put(cfg, "acme.json", june)                 # the same June file again
    scan_inbox(cfg, store)
    names = {r[0] for r in q(store, "SELECT filename FROM files")}
    assert len(names) == 2 and "acme.json" in names
    other = (names - {"acme.json"}).pop()
    assert other.startswith("acme-") and other.endswith(".json")


def test_the_same_upload_twice_while_the_first_is_loading_is_a_duplicate(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    c = TestClient(create_app(cfg, store))
    body = json.dumps(big_doc(40.0, n=2000)).encode()
    a = c.post("/api/upload", files={"file": ("acme_2026-05.json", body)}).json()
    b = c.post("/api/upload", files={"file": ("acme_2026-05.json", body)}).json()
    assert a["status"] == "queued" and b["status"] == "duplicate"
    deadline = time.monotonic() + 20
    while (store.file_status("acme_2026-05.json") or {}).get("status") != "done":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert q(store, "SELECT count(*) FROM files") == [(1,)]


def test_a_file_changed_while_it_is_read_is_not_recorded_as_loaded(cfg, store, monkeypatch):
    import mrfx.ingest as ing
    cfg.move_processed = True
    p = put(cfg, "acme_2026-05.json", _doc1(40.0))
    real = ing._note_ingest_start

    def swap(path):
        real(path)
        tmp = path.with_suffix(".new")
        tmp.write_text(json.dumps(_doc1(55.0)))
        tmp.replace(path)                    # replaced by rename mid-read (POSIX)
    monkeypatch.setattr(ing, "_note_ingest_start", swap)
    ingest_file(cfg, store, p)
    monkeypatch.setattr(ing, "_note_ingest_start", real)
    assert p.exists()                        # left for the next scan, not moved
    assert store.loaded_identity("acme_2026-05.json") is None


def test_unique_name_keeps_a_middle_suffix():
    from mrfx.ingest import unique_inbox_name

    class St:
        def file_status(self, n):
            return None
    from pathlib import Path
    d = Path("/nonexistent-dir-for-name-test")
    assert unique_inbox_name(d, "rates.json.2026-05.json", St(), "abc") == "rates.json.2026-05-abc.json"
    assert unique_inbox_name(d, "x.json.gz", St(), "abc") == "x-abc.json.gz"


# -- 1.3 a rate file uses its own month's provider list -------------------------

def _companion(month_day, a, b):
    return {**HDR, "last_updated_on": month_day, "provider_references": [
        {"provider_group_id": 1, "provider_groups": [grp(a, a[1:])]},
        {"provider_group_id": 2, "provider_groups": [grp(b, b[1:])]}]}


def test_a_rate_file_never_takes_another_months_provider_list(cfg, store):
    cfg.move_processed = False
    ingest_file(cfg, store, put(cfg, "acme_refs_2026-04.json",
                                _companion("2026-04-01", "1111111111", "1222222222")))
    may = {**HDR, "in_network": [item("97110", refs=[1, 2])]}
    out = ingest_file(cfg, store, put(cfg, "acme_rates_2026-05.json", may))
    # May's list is not there yet: missing and counted, never April's practices
    assert out["rows"] == 0 and out["ref_groups_skipped"] == 1
    r = ingest_file(cfg, store, put(cfg, "acme_refs_2026-05.json",
                                    _companion("2026-05-01", "1555555555", "1666666666")))
    assert r["requeued"] == ["acme_rates_2026-05.json"]
    assert [t for (t,) in q(store, "SELECT tin_value FROM rates ORDER BY 1")] == [
        "555555555", "666666666"]
    # April's rate file, loaded after May's list exists, keeps April's practices
    april = {**HDR, "last_updated_on": "2026-04-01", "in_network": [item("97110", refs=[1, 2])]}
    ingest_file(cfg, store, put(cfg, "acme_rates_2026-04.json", april))
    assert [t for (t,) in q(store, "SELECT tin_value FROM rates WHERE source_file = "
                                   "'acme_rates_2026-04.json' ORDER BY 1")] == ["111111111", "222222222"]


# -- 1.4 long ids are exact --------------------------------------------------------

@pytest.mark.parametrize("fast", [False, True])
def test_ids_above_2_53_stay_distinct(cfg, store, fast):
    from mrfx.parser import clean_ref_id
    assert clean_ref_id(1234567890123456789) != clean_ref_id(1234567890123456790)
    assert clean_ref_id("0012") == 12 and clean_ref_id("12.0") == 12 and clean_ref_id(12.7) is None
    assert clean_ref_id(2 ** 64) is None and clean_ref_id(9007199254740993.0) is None
    cfg.fast_scan = fast
    a, b = "1234567890123456789", "1234567890123456790"
    grp_json = '[{"npi": ["%s"], "tin": {"type": "ein", "value": "%s"}}]'
    raw = ('{"reporting_entity_name": "Acme Health", "version": "1.0", '
           '"last_updated_on": "2026-05-01", "provider_references": ['
           '{"provider_group_id": ' + a + ', "provider_groups": ' + grp_json % ("1111111111", "111111111") + '},'
           '{"provider_group_id": ' + b + ', "provider_groups": ' + grp_json % ("2222222222", "222222222") + '}],'
           '"in_network": [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT", '
           '"billing_code": "97110", "negotiated_rates": [{"provider_references": [' + a + '], '
           '"negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 50.0, '
           '"billing_class": "professional", "service_code": ["11"]}]}]}]}')
    json.loads(raw)
    p = cfg.inbox_dir / "big_ids_2026-05.json"
    p.write_text(raw)
    ingest_file(cfg, store, p)
    assert q(store, "SELECT tin_value FROM rates") == [("111111111",)]


# -- 1.5 a multi-member gzip's tail proves nothing about its middle ---------------

def test_trailer_proof_is_recorded_per_container(tmp_path):
    from mrfx.fetch import _proves, _zip_cd_in_tail, trailer_proves_for
    from mrfx.identity import hash_file
    body = json.dumps({"x": [i for i in range(200000)]}).encode()
    single = tmp_path / "s.json.gz"
    single.write_bytes(gzip.compress(body))
    multi = tmp_path / "m.json.gz"
    multi.write_bytes(gzip.compress(body[:300000]) + gzip.compress(body[300000:]))
    assert trailer_proves_for(single, hash_file(single)[1]) == 1
    assert trailer_proves_for(multi, hash_file(multi)[1]) == 0
    import zipfile
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("rates.json", body)
    data = z.read_bytes()
    assert _zip_cd_in_tail(data[-(1 << 20):], len(data))
    assert not _zip_cd_in_tail(data[-30:], len(data))
    mine = {"magic": "gz", "tail_sha": "t", "tail_bytes": 1, "win_sha": "w"}
    twin = {**mine, "trailer_proves": 1}
    assert _proves(mine, twin)
    assert not _proves(mine, {**twin, "trailer_proves": 0})
    assert not _proves(mine, {**twin, "trailer_proves": None})


def test_a_segmented_download_runs_the_early_check_when_a_twin_may_exist():
    import inspect

    import mrfx.fetch as F
    src = inspect.getsource(F)
    assert "has_twin" in src and "and not twin_possible" in src
    src2 = inspect.getsource(F.early_duplicate_check)
    assert "check.has_twin = has_twin" in src2


# -- Group 2: start-up and the Files tab ------------------------------------------

def test_the_start_up_check_reads_only_new_or_changed_parts(cfg, store, monkeypatch):
    import builtins
    cfg.move_processed = False
    for i in range(3):
        ingest_file(cfg, store, put(cfg, f"acme{i}_2026-05.json", _doc1(40.0 + i)))
    store.close()
    S.Store(cfg.store_dir).close()                  # first open records the verified parts
    opened = []
    real_open = builtins.open

    def spy(f, *a, **k):
        if str(f).endswith(".parquet"):
            opened.append(str(f))
        return real_open(f, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    S.Store(cfg.store_dir).close()
    assert opened == []                             # nothing re-read at the next start
    part = cfg.store_dir / "rates" / f"{S.file_key('acme1_2026-05.json')}.parquet"
    monkeypatch.setattr(builtins, "open", real_open)
    data = part.read_bytes()
    part.write_bytes(data[:-4] + b"XXXX")           # damaged later: still caught
    st = S.Store(cfg.store_dir)
    assert not part.exists() and (cfg.store_dir / "rates" / "corrupt" / part.name).exists()
    assert st.file_status("acme1_2026-05.json")["status"] == "failed"


def test_files_list_keeps_rows_that_need_the_user(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    for i in range(600):
        store.upsert_file(f"done{i:04d}.json", status="done", rows_emitted=1,
                          finished_at=S.FINISHED_NOW)
    store.upsert_file("BIG_needs_ok.json.gz", status="pending_confirmation", size_bytes=9_000_000_000)
    store.upsert_file("queued_one.json", status="queued")
    store.upsert_file("failed_old.json", status="failed", error="x",
                      finished_at="2020-01-01 00:00:00")
    d = TestClient(create_app(cfg, store)).get("/api/files").json()
    names = [f["filename"] for f in d["files"]]
    assert names[:3] == ["BIG_needs_ok.json.gz", "queued_one.json", "failed_old.json"]
    assert d["attention"] == 3 and d["total"] == 603 and len(names) == 503
    assert TestClient(create_app(cfg, store)).get(
        "/api/files", params={"offset": 10 ** 20}).status_code == 422


def test_freeze_diagnostics_start_before_the_store_opens():
    import inspect

    from mrfx import cli
    src = inspect.getsource(cli.cmd_serve)
    assert src.index("_start_stack_recorder(") < src.index("store = Store(")
    assert src.index('"opening the store at') < src.index("store = Store(")
    assert "the dashboard is ready" in src


# -- Group 3: ingest robustness ------------------------------------------------------

def _refs_doc(n, month_day="2026-05-01"):
    return {**HDR, "last_updated_on": month_day, "provider_references": [
        {"provider_group_id": i, "provider_groups": [grp(str(1000000000 + i), str(100000000 + i))]}
        for i in range(1, n + 1)]}


@pytest.mark.parametrize("why", ["fast_scan_off", "low_disk"])
def test_a_big_reference_file_is_stored_on_the_two_pass_path(cfg, store, monkeypatch, why):
    import mrfx.ingest as ing
    import mrfx.parser as P
    cfg.move_processed = False
    if why == "fast_scan_off":
        cfg.fast_scan = False
    else:
        monkeypatch.setattr(P, "_free_disk", lambda _p: 1 << 30)   # under the 2 GB floor
    assert ingest_file(cfg, store, put(cfg, "acme_B_2026-05.json",
                                       {**HDR, "in_network": [item("97110", refs=[1, 2])]}))["rows"] == 0
    monkeypatch.setattr(ing, "LARGE_FILE_UNCOMPRESSED_BYTES", 1)
    out = ingest_file(cfg, store, put(cfg, "acme_refs_2026-05.json", _refs_doc(20000)))
    assert out["refs"] == 20000 and out["requeued"] == ["acme_B_2026-05.json"]
    assert q(store, "SELECT file_type FROM files WHERE filename = 'acme_refs_2026-05.json'") == [
        ("provider_reference",)]


def test_a_linked_rate_file_is_downloaded_again_when_its_list_lands(cfg, store):
    cfg.move_processed = False
    uid = store.enqueue_url("https://x.example/acme_B_2026-05.json", "b")
    p = put(cfg, "acme_B_2026-05.json", {**HDR, "in_network": [item("97110", refs=[1, 2])]})
    assert ingest_file(cfg, store, p)["rows"] == 0
    store.update_url(uid, status="done", filename="acme_B_2026-05.json")
    p.unlink()                                       # the download was removed after ingest
    out = ingest_file(cfg, store, put(cfg, "acme_refs_2026-05.json", _refs_doc(2)))
    assert out["requeued"] == ["acme_B_2026-05.json"]
    row = q(store, "SELECT status, error FROM url_queue WHERE id = ?", uid)[0]
    assert row[0] == "queued" and "provider reference file arrived" in row[1]
    # another month's list re-downloads nothing
    store.update_url(uid, status="done")
    out = ingest_file(cfg, store, put(cfg, "acme_refs_2026-07.json", _refs_doc(2, "2026-07-01")))
    assert out["requeued"] == [] and q(store, "SELECT status FROM url_queue") == [("done",)]


def test_deferred_groups_spill_to_disk_and_are_all_emitted(cfg, store, monkeypatch):
    import mrfx.parser as P
    monkeypatch.setattr(P, "DEFER_MEMORY_GROUPS", 5)
    cfg.move_processed = False
    ingest_file(cfg, store, put(cfg, "acme_refs_2026-05.json", _refs_doc(60)))
    spilled = []
    real = P._DeferredSpool.drain

    def spy(self):
        spilled.append(self.spilled)
        return real(self)
    monkeypatch.setattr(P._DeferredSpool, "drain", spy)
    doc = {**HDR, "in_network": [item("97110", refs=[i]) for i in range(1, 51)]}
    assert ingest_file(cfg, store, put(cfg, "acme_B_2026-05.json", doc))["rows"] == 50
    assert spilled and spilled[0] == 50
    assert not list((cfg.store_dir / "rates").glob("*deferred*"))


def test_empty_or_remote_companion_entries_are_counted(cfg, store):
    cfg.move_processed = False
    comp = {**HDR, "provider_references": [
        {"provider_group_id": 1, "provider_groups": [grp("1111111111", "111111111")]},
        {"provider_group_id": 2, "provider_groups": []},
        {"provider_group_id": 3, "location": "https://x.example/ref3.json"}]}
    ingest_file(cfg, store, put(cfg, "acme_refs_2026-05.json", comp))
    doc = {**HDR, "in_network": [item("97110", refs=[1]), item("97112", refs=[2]),
                                 item("97140", refs=[3])]}
    out = ingest_file(cfg, store, put(cfg, "acme_B_2026-05.json", doc))
    assert out["rows"] == 1 and out["ref_groups_skipped"] == 2 and out["remote_ref_groups"] == 1
    qa = json.loads(q(store, "SELECT qa FROM files WHERE filename = 'acme_B_2026-05.json'")[0][0])
    assert qa["missing_ref_ids"] == 1


def test_a_table_split_around_in_network_is_all_the_files_own(cfg, store):
    cfg.move_processed = False
    ingest_file(cfg, store, put(cfg, "acme_refs_2026-05.json", {**HDR, "provider_references": [
        {"provider_group_id": 2, "provider_groups": [grp("8888888888", "888888888")]}]}))
    raw = ('{"reporting_entity_name": "Acme Health", "version": "1.0", "last_updated_on": "2026-05-01", '
           '"provider_references": [{"provider_group_id": 1, "provider_groups": '
           '[{"npi": ["1111111111"], "tin": {"type": "ein", "value": "111111111"}}]}], '
           '"in_network": [' + json.dumps(item("97110", refs=[2])) + '], '
           '"provider_references": [{"provider_group_id": 2, "provider_groups": '
           '[{"npi": ["2222222222"], "tin": {"type": "ein", "value": "222222222"}}]}]}')
    p = cfg.inbox_dir / "acme_B_2026-05.json"
    p.write_text(raw)
    ingest_file(cfg, store, p)
    assert q(store, "SELECT tin_value FROM rates") == [("222222222",)]


def test_a_stopped_download_keeps_no_inflight_fingerprint(cfg, store):
    uid = store.enqueue_url("https://x.example/a.json.gz", "a")
    store.update_url(uid, status="downloading")
    store.record_inflight(uid, "a.json.gz", {"size": 10, "head_bytes": 10, "head_sha": "h",
                                             "tail_bytes": 0, "tail_sha": None, "win_sha": None,
                                             "magic": "gz"})
    assert q(store, "SELECT count(*) FROM inflight_fingerprints") == [(1,)]
    store.cancel_all_downloading()
    assert q(store, "SELECT count(*) FROM inflight_fingerprints") == [(0,)]

