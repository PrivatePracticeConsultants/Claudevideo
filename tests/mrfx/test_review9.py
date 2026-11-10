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


# -- Group 4: the numbers a report sells ----------------------------------------

def _px(rate, mods=("GP",)):
    return {"negotiated_type": "negotiated", "negotiated_rate": float(rate),
            "billing_class": "professional", "service_code": ["11"],
            "billing_code_modifier": list(mods), "expiration_date": "9999-12-31"}


def _pdoc(payer, day, prices):
    """prices: {code: {tin: rate}}; each TIN's one NPI is '1' + tin."""
    items = [{"negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code": code,
              "negotiated_rates": [{"provider_groups": [{"npi": ["1" + t], "tin": {"type": "ein", "value": t}}],
                                    "negotiated_prices": [_px(r)]} for t, r in by_tin.items()]}
             for code, by_tin in prices.items()]
    return {"reporting_entity_name": payer, "version": "2.0.0", "last_updated_on": day,
            "in_network": items}


def _load(cfg, store, docs, names):
    for fname, d in docs:
        put(cfg, fname, d)
    assert all(r["status"] == "done" for r in scan_inbox(cfg, store))
    for t, (label, st) in names.items():
        store.save_npi("1" + t, label, "261QP2000X", "x", "Town", st, entity_type="NPI-2",
                       zip_code="64105")
    store.rebuild_rollups()


def _client(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    return TestClient(create_app(cfg, store))


FIVE = {f"5{i}1000001": (f"{k} REHAB", "MO") for i, k in enumerate("ABCDE", start=1)}


def test_a_pinned_report_does_not_show_a_cut_from_after_its_month(cfg, store):
    # 64a371c: the trajectory behind a pitch report "as of 2023-06" ran to
    # 2024-06 and printed the 20 % cut that happened a year later
    T = list(FIVE)
    _load(cfg, store, [(f"p_{d}.json", _pdoc("P Health", d, {"97110": {t: r for t in T}}))
                       for d, r in (("2022-06-01", 50), ("2023-06-01", 50), ("2024-06-01", 40))], FIVE)
    from mrfx.monitor import compute_payer_trajectory
    pinned = compute_payer_trajectory(store, {"state": "MO", "month": "2023-06"})["payers"][0]
    assert (pinned["last_month"], pinned["cumulative_pct"]) == ("2023-06", 0.0)
    latest = compute_payer_trajectory(store, {"state": "MO"})["payers"][0]
    assert (latest["last_month"], latest["cumulative_pct"]) == ("2024-06", -20.0)
    h = _client(cfg, store).post("/api/report/pitch", json={
        "subject": "A REHAB", "market": {"month": "2023-06", "state": "MO"}}).text
    import re
    i = h.find("In real terms")
    assert i >= 0
    blk = re.sub(r"<[^>]+>", " ", h[i:i + 2000])
    blk = blk[:blk.find("Sources")] if "Sources" in blk else blk
    assert "2023-06" in blk and "2024-06" not in blk and "-20" not in blk, blk


def test_a_cut_streak_is_measured_on_the_same_pairs_month_to_month(cfg, store):
    # 64a371c: May priced two of five practices (+12.5 % on the panel), June
    # priced all five back at their April level for three and higher for two:
    # the index fell 12.5 -> 0 and the streak said 1 cut. On the pairs priced
    # in both May and June, rates went up (C 50 -> 50, D 50 -> 55).
    A, B, C, D, E = list(FIVE)
    _load(cfg, store, [
        ("al_04.json", _pdoc("Alpha Health", "2026-04-01", {"97110": {A: 40, B: 45, C: 40, D: 50, E: 60}})),
        ("al_05.json", _pdoc("Alpha Health", "2026-05-01", {"97110": {C: 50, D: 50}})),
        ("al_06.json", _pdoc("Alpha Health", "2026-06-01", {"97110": {A: 40, B: 45, C: 50, D: 55, E: 60}})),
    ], FIVE)
    from mrfx.monitor import compute_payer_trajectory
    p = compute_payer_trajectory(store, {"state": "MO"})["payers"][0]
    assert [s["index_pct"] for s in p["series"]] == [0.0, 12.5, 0.0]
    assert p["cut_streak"] == 0


# the subject rule: one practice name in two states, a mapped two-TIN entity
S_MO, S_KS, M1, M2 = "610000001", "610000002", "620000001", "620000002"
PEERS = {f"6{i}0000001": (f"{k} REHAB", "MO") for i, k in zip(range(3, 9), "BCDEFG")}
SUBJ_NAMES = {S_MO: ("ACME REHAB", "MO"), S_KS: ("ACME REHAB", "KS"),
              M1: ("NORTH CLINIC", "MO"), M2: ("SOUTH CLINIC", "MO"), **PEERS}


def _subjects(cfg, store):
    peers = list(PEERS)
    p97110 = {t: 44 + i for i, t in enumerate(peers)}
    p97140 = {t: 30 + i for i, t in enumerate(peers)}
    p97112 = {t: 40 + i for i, t in enumerate(peers)}        # nobody named ACME prices it
    _load(cfg, store, [("al_06.json", _pdoc("Alpha Health", "2026-06-01", {
        "97110": {S_MO: 41, S_KS: 60, M1: 44, M2: 52, **p97110},
        "97140": {S_MO: 30, S_KS: 36, **p97140},
        "97112": p97112}))], SUBJ_NAMES)
    from mrfx.entities import update_entity
    update_entity(cfg, store, "BIG GROUP", add_tins=[M1, M2])
    store.rebuild_rollups()


MO = {"month": "latest", "state": "MO"}


def test_an_out_of_state_subjects_priced_codes_are_not_gaps(cfg, store):
    # 64a371c: the KS ACME was benchmarked on 97110 and 97140 in a MO market,
    # and contract gaps listed the same two codes as "not in your contract"
    _subjects(cfg, store)
    c = _client(cfg, store)
    for subj in ("ACME REHAB · KS", S_KS):
        b = c.post("/api/benchmark/market", json={"subject": subj, "market": MO}).json()
        priced = {r["billing_code"] for r in b["rows"] if r["subject_rate"] is not None}
        assert priced == {"97110", "97140"}
        g = c.post("/api/contract-gaps", json={"subject": subj, "market": MO, "min_peers": 3}).json()
        assert {x["billing_code"] for x in g["gaps"]} == {"97112"}
        size = c.post("/api/quality/size", json={"subject": subj, "market": MO})
        assert size.status_code == 200 and size.json()["subject"], size.text


def test_a_partial_name_takes_the_market_state_like_the_full_name(cfg, store):
    # 64a371c: "ACME" resolved to both states' TINs ($50.50 at p70), the full
    # name to the MO one only ($41.00 at p10)
    _subjects(cfg, store)
    from mrfx.benchmark import resolve_subject_tins, subject_tins_for_market
    for subj in ("ACME REHAB", "acme rehab", "ACME"):
        assert subject_tins_for_market(store, subj, {"state": "MO"}) == [S_MO], subj
    assert subject_tins_for_market(store, S_KS, {"state": "MO"}) == [S_KS]   # an id is what it names
    assert resolve_subject_tins(store, "ACME") == sorted(resolve_subject_tins(store, "ACME"))
    b = _client(cfg, store).post("/api/benchmark/market", json={"subject": "ACME", "market": MO}).json()
    r = next(x for x in b["rows"] if x["billing_code"] == "97110")
    assert (b["subject_tins"], r["subject_rate"]) == ([S_MO], 41.0)


def test_the_dossier_names_the_mapped_entity_and_the_practice_it_prices(cfg, store):
    # 64a371c: "Practice profile — NORTH CLINIC" over BIG GROUP's numbers, and
    # "ACME REHAB (KS, MO)" above a rate table of the MO practice only
    _subjects(cfg, store)
    c = _client(cfg, store)
    f = c.post("/api/report/dossier.json", json={"subject": "BIG GROUP", "market": MO}).json()["facts"]
    assert f["display_name"] == "BIG GROUP"
    f = c.post("/api/report/dossier.json", json={"subject": "ACME REHAB", "market": MO}).json()["facts"]
    assert "KS" not in json.dumps(f.get("states") or f.get("state") or "")


@pytest.mark.parametrize("flat", [False, True])
def test_one_practice_has_one_position_on_every_screen(cfg, store, flat):
    # nine MO practices, one payer: A is 2nd lowest on 97110 (12.5) and in the
    # exact middle on 97140 (50) -> median 31.25 -> p31. 64a371c: Negotiate and
    # the dossier said p32 (median of rounded positions), and with an all-tied
    # 97530 the roster, Negotiate and the dossier said p50, the size premium 37.5
    names = "ABCDEFGHI"
    T = {k: f"5{i}1000001" for i, k in enumerate(names, start=1)}
    prices = {"97110": {T[k]: v for k, v in zip(names, [41, 40, 42, 43, 44, 45, 46, 47, 48])},
              "97140": {T[k]: v for k, v in zip(names, [35, 31, 32, 33, 34, 36, 37, 38, 39])}}
    if flat:
        prices["97530"] = {t: 50 for t in T.values()}
    _load(cfg, store, [("p_06.json", _pdoc("P Health", "2026-06-01", prices))],
          {t: (f"{k} REHAB", "MO") for k, t in T.items()})
    c = _client(cfg, store)
    mine = lambda rows: next(x for x in rows if x["display_name"] == "A REHAB")  # noqa: E731
    lead = mine(c.post("/api/leads", json={"market": MO, "threshold_percentile": 99,
                                           "min_codes": 1}).json()["leads"])
    board = mine(c.post("/api/leaderboard", json={"market": MO, "min_codes": 1}).json()["practices"])
    roster = mine(c.post("/api/roster", json={"market": MO, "payer": "P Health",
                                              "min_codes": 1}).json()["organizations"])
    neg = c.post("/api/negotiate/compare", json={"subject": "A REHAB", "payer": "P Health",
                                                 "market": MO}).json()["summary"]["headline_percentile"]
    dossier = c.post("/api/report/dossier.json", json={"subject": "A REHAB", "market": MO}
                     ).json()["facts"]["median_percentile"]
    one_pager = c.post("/api/report/negotiation", json={"subject": "A REHAB", "market": MO}).text
    size = c.post("/api/quality/size", json={"subject": "A REHAB", "market": MO}).json()
    assert [lead["median_percentile"], board["median_percentile"], roster["median_percentile"],
            neg, dossier] == [31] * 5
    assert "You sit at roughly <b>p31</b>" in one_pager
    assert (lead["n_codes"], board["n_codes"]) == (2, 2)
    assert size["subject"]["avg_percentile"] == 31.3          # the size premium's mean, same codes


def test_the_worst_payer_is_judged_on_the_codes_its_payers_share(cfg, store):
    # Alpha: 97110 $40 (market median $48), 97140 $30 (median $32);
    # Beta: 97110 $42. 64a371c averaged each payer over its own codes —
    # Alpha (8+2)/2 = $5.00 vs Beta $6.00 -> "worst: Beta" — although Alpha
    # pays less on the only code both price
    peers = list(PEERS)
    _load(cfg, store, [
        ("al.json", _pdoc("Alpha Health", "2026-06-01", {
            "97110": {S_MO: 40, **{t: 44 + 2 * i for i, t in enumerate(peers)}},
            "97140": {S_MO: 30, **{t: 30 + i for i, t in enumerate(peers)}}})),
        ("be.json", _pdoc("Beta Health", "2026-06-01", {"97110": {S_MO: 42, peers[0]: 47}}))],
        SUBJ_NAMES)
    leads = _client(cfg, store).post("/api/leads", json={
        "market": MO, "threshold_percentile": 99, "min_codes": 1}).json()["leads"]
    a = next(x for x in leads if x["display_name"] == "ACME REHAB")
    assert (a["worst_payer"], a["worst_payer_codes"], a["worst_payer_basis"]) == (
        "Alpha Health", 1, "shared")


def _outreach_zip(c, **params):
    import csv
    import io
    import zipfile
    r = c.get("/api/export/outreach.zip", params=params)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    rows = list(csv.DictReader(io.StringIO(
        z.read(next(n for n in z.namelist() if n.endswith(".csv"))).decode("utf-8-sig"))))
    return rows, z.read(next(n for n in z.namelist() if n.endswith(".txt"))).decode()


def test_the_outreach_file_describes_itself(cfg, store):
    # 64a371c: its sidecar was the Explorer's — "one row per payer x
    # billing_code x entity x ... x month", "ALL months pooled" — above
    # columns that are one row per organization on the latest month
    _subjects(cfg, store)
    rows, meth = _outreach_zip(_client(cfg, store), grain="tin", dollar_only="1",
                               billing_class="institutional")
    assert "one row per organization" in meth.lower()
    assert "ALL months pooled" not in meth and "modifier-set x class" not in meth
    assert "do not change the numbers" in meth and "C<code>_PCTL" in meth


def test_outreach_medians_are_computed_in_the_database(cfg, store, monkeypatch):
    # 4.6: the per-organization medians come back per (organization, code)
    # for the wanted codes only — never one Python entry per (TIN, code) with
    # every TIN passed as one parameter (RSS near 6 GB on a 99M store)
    _subjects(cfg, store)
    import mrfx.benchmark as bm
    seen = []
    real = bm.practice_base_sql

    def spy(store, market, **kw):
        seen.append(kw.get("extra_params"))
        return real(store, market, **kw)
    monkeypatch.setattr(bm, "practice_base_sql", spy)
    rows, _meth = _outreach_zip(_client(cfg, store), grain="tin", dollar_only="1", state="MO")
    assert seen and not any(seen)                 # no TIN list as a parameter
    acme = next(x for x in rows if x["ENTITY_ID"] == S_MO)
    assert (acme["C97110_RATE"], acme["C97140_RATE"]) == ("41.0", "30.0")


WRONG = [{"billing_class": ["professional"]}, {"discipline": {"a": 1}}, {"city": [5]},
         {"payers": [{"x": 1}]}, {"codes": 5}, {"base_only": "maybe"},
         {"target_percentile": "high"}, {"month": ["2026-06"]}, {"pos": {"a": 1}},
         {"state": 5}, {"city": "\u0000"}]


def test_no_wrong_typed_market_reaches_the_database(cfg, store):
    # 4.7: {"billing_class": ["professional"]} answered 500 with
    # "ConversionException: ... LINE 3: ..." on a dozen routes; fuzzed here
    # over EVERY POST route the API publishes
    _subjects(cfg, store)
    c = _client(cfg, store)
    routes = [p for p, ops in c.get("/openapi.json").json()["paths"].items()
              if "post" in ops and "{" not in p]
    assert len(routes) > 40
    bad = []
    for path in routes:
        for m in WRONG:
            body = {"market": m, "subject": "ACME REHAB", "payer": "Alpha Health",
                    "tins": 5, "paths": 5, "subjects": 5, "path": 5}
            r = c.post(path, json=body)
            text = r.text
            if r.status_code >= 500 or "LINE " in text or "/tmp/" in text or "\\\\" in text:
                bad.append((path, m, r.status_code, text[:120]))
    assert not bad, bad[:8]


def test_a_private_market_key_is_never_taken_from_the_wire(cfg, store):
    # 4.7: `_plan_files: ["al_06.json"]` silently scoped Benchmark to one file
    from mrfx.benchmark import normalize_market
    m = normalize_market({"_plan_files": ["x.json"], "curated_tins": ["1"], "state": "mo",
                          "base_only": "false", "codes": "97110"})
    assert "_plan_files" not in m and "curated_tins" not in m
    assert (m["state"], m["base_only"], m["codes"]) == ("MO", False, ["97110"])


def test_long_names_and_bad_paths_are_refused_in_words(cfg, store):
    c = _client(cfg, store)
    r = c.post("/api/upload", files={"file": ("x" * 300 + ".json", b"{}")})
    assert r.status_code == 422 and str(cfg.inbox_dir) not in r.text
    for route in ("/api/utilization/import", "/api/demographics/import"):
        r = c.post(route, json={"path": "/" + "y" * 3000})
        assert r.status_code == 422 and "y" * 50 not in r.text
    r = c.get("/api/files", params={"offset": 99999999999999999999})
    assert r.status_code == 422


def test_percentages_are_never_pooled_with_dollars(cfg, store):
    # 4.9: include_non_dollar pooled an 80 (%) with $40 and printed $60.00
    _subjects(cfg, store)
    r = _client(cfg, store).post("/api/benchmark/market", json={
        "subject": "ACME REHAB", "market": {**MO, "include_non_dollar": True}})
    assert r.status_code == 422 and "cannot be pooled" in r.text


def test_printed_percentages_round_half_up_and_never_read_minus_zero():
    from mrfx.inflation import erosion_line
    from mrfx.rounding import signed_pct
    from mrfx.spark import sparkline
    assert (signed_pct(-0.025), signed_pct(1.25), signed_pct(0)) == ("+0.0%", "+1.3%", "+0.0%")
    line = erosion_line("Q Health", -0.025, -2.9, "2023-06", "2024-06",
                        {"basis": "assumed", "assumed_pct_per_year": 3})
    assert "-0.0%" not in line and "+0.0%" in line
    svg = sparkline([("2026-05", 80.0), ("2026-06", 81.0)])
    assert "(+1.3%)" in svg


def test_report_footers_state_the_market_in_words(cfg, store):
    _subjects(cfg, store)
    h = _client(cfg, store).post("/api/report/pitch", json={"subject": "ACME REHAB",
                                                            "market": MO}).text
    assert 'Market definition: {"' not in h
    assert "Market definition: each payer&#x27;s newest published month; practices in MO" in h \
        or "Market definition: each payer's newest published month; practices in MO" in h


def test_shipped_catalogs_are_found_when_the_config_lives_elsewhere(tmp_path):
    from mrfx.config import load_mrfx_config
    from mrfx.known_sources import load_known_sources
    (tmp_path / "mrfx.yaml").write_text("store_dir: s\n")
    c = load_mrfx_config(tmp_path / "mrfx.yaml")
    assert c.store_dir == tmp_path / "s"
    assert len(load_known_sources(c.known_sources_path)) > 50


def test_practice_medians_survive_a_restart_and_superseded_ones_go(cfg, store):
    # 4.6 (P3): the materialised medians were named by a random per-process
    # tag — rebuilt at every start, and five restarts left ~490 MB beside the
    # store
    _subjects(cfg, store)
    from mrfx.benchmark import compute_benchmark
    compute_benchmark(store, "ACME REHAB", MO)
    files = sorted(p.name for p in store.dir.glob("practice_medians*.parquet"))
    assert files
    again = S.Store(cfg.store_dir)
    compute_benchmark(again, "ACME REHAB", MO)
    assert sorted(p.name for p in store.dir.glob("practice_medians*.parquet")) == files
    stale = store.dir / "practice_medians_v1.deadbeef-3.parquet"
    stale.write_bytes(b"old")
    put(cfg, "be.json", _pdoc("Beta Health", "2026-06-01", {"97110": {S_MO: 42}}))
    scan_inbox(cfg, again)
    again.rebuild_rollups()
    compute_benchmark(again, "ACME REHAB", MO)
    now = sorted(p.name for p in store.dir.glob("practice_medians*.parquet"))
    assert now and now != files and stale.name not in now
