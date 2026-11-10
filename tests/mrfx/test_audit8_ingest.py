"""Audit 8, ingest: provider references are file-local (every parse path
agrees), a parked/doomed part from an earlier run never overrides a later
one, stored rows migrate to the negotiated_type rule, a big standalone
reference file is stored as references, malformed values fail in words."""

import json

import pytest

import mrfx.ingest as ing
import mrfx.store as S
from mrfx.ingest import ingest_file, plain_parse_error

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


def put(cfg, name, obj):
    p = cfg.inbox_dir / name
    p.write_text(json.dumps(obj))
    return p


def q(store, sql, *a):
    with store.connect() as con:
        return con.execute(sql, list(a)).fetchall()


COMPANION = {**HDR, "provider_references": [
    {"provider_group_id": 1, "provider_groups": [grp("1111111111", "111111111")]},
    {"provider_group_id": 2, "provider_groups": [grp("2222222222", "222222222")]}]}


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("big", [False, True])
def test_own_reference_table_wins_on_every_parse_path(cfg, store, monkeypatch, fast, big):
    # B defines ids 1, 2 itself, AFTER in_network; the payer's standalone
    # companion defines the same ids as other practices. Before: three of
    # the four paths attached B's rates to the companion's practices.
    cfg.fast_scan = fast
    ingest_file(cfg, store, put(cfg, "acme_refs_2026-05.json", COMPANION))
    monkeypatch.setattr(ing, "LARGE_FILE_UNCOMPRESSED_BYTES", 1 if big else 1_500_000_000)
    b = {**HDR, "in_network": [item("97110", refs=[1, 2])], "provider_references": [
        {"provider_group_id": 1, "provider_groups": [grp("3333333333", "333333333")]},
        {"provider_group_id": 2, "provider_groups": [grp("4444444444", "444444444")]}]}
    assert ingest_file(cfg, store, put(cfg, "acme_B_2026-05.json", b))["status"] == "done"
    assert q(store, "SELECT tin_value, npi FROM rates ORDER BY 1") == [
        ("333333333", "3333333333"), ("444444444", "4444444444")]


@pytest.mark.parametrize("fast", [False, True])
def test_remote_reference_is_not_filled_from_a_companion(cfg, store, fast):
    cfg.fast_scan = fast
    ingest_file(cfg, store, put(cfg, "acme_refs_2026-05.json", COMPANION))
    b = {**HDR, "provider_references": [{"provider_group_id": 1,
                                         "location": "https://x.example/ref1.json"}],
         "in_network": [item("97110", refs=[1])]}
    out = ingest_file(cfg, store, put(cfg, "acme_B_2026-05.json", b))
    assert out["rows"] == 0 and out["remote_ref_groups"] == 1
    assert q(store, "SELECT count(*) FROM rates")[0][0] == 0


def test_file_without_a_table_still_uses_the_companion(cfg, store):
    ingest_file(cfg, store, put(cfg, "acme_refs_2026-05.json", COMPANION))
    b = {**HDR, "in_network": [item("97110", refs=[1, 2])]}
    assert ingest_file(cfg, store, put(cfg, "acme_B_2026-05.json", b))["rows"] == 2
    assert [r[0] for r in q(store, "SELECT npi FROM rates ORDER BY 1")] == [
        "1111111111", "2222222222"]


def test_two_same_month_companions_disagreeing_attach_neither(cfg, store):
    # a PPO and an HMO reference table with file-local ids 1, 2: an
    # in-network file naming neither cannot be attached to either
    other = {**HDR, "provider_references": [
        {"provider_group_id": 1, "provider_groups": [grp("5555555555", "555555555")]},
        {"provider_group_id": 2, "provider_groups": [grp("6666666666", "666666666")]}]}
    ingest_file(cfg, store, put(cfg, "acme_ppo_refs_2026-05.json", COMPANION))
    ingest_file(cfg, store, put(cfg, "acme_hmo_refs_2026-05.json", other))
    out = ingest_file(cfg, store, put(cfg, "acme_rates_2026-05.json",
                                      {**HDR, "in_network": [item("97110", refs=[1, 2])]}))
    assert out["rows"] == 0 and out["ambiguous_ref_ids"] == 2
    assert out["ref_groups_skipped"] == 1
    # a byte-identical twin under another name is NOT ambiguous
    store.forget_file("acme_hmo_refs_2026-05.json")
    ingest_file(cfg, store, put(cfg, "acme_ppo_refs_copy_2026-05.json", COMPANION))
    refs = store.load_provider_refs("Acme Health")
    assert refs[1] == [("111111111", "ein", ("1111111111",))]


def test_newer_companion_vintage_wins_without_ambiguity(cfg, store):
    old = {**COMPANION, "last_updated_on": "2026-04-01", "provider_references": [
        {"provider_group_id": 1, "provider_groups": [grp("9999999999", "999999999")]}]}
    ingest_file(cfg, store, put(cfg, "acme_refs_2026-04.json", old))
    ingest_file(cfg, store, put(cfg, "acme_refs_2026-05.json", COMPANION))
    assert store.load_provider_refs("Acme Health")[1] == [("111111111", "ein", ("1111111111",))]


def _doc(rate, code="97110"):
    return {**HDR, "in_network": [item(code, groups=[grp("1234567893", "123456789")], rate=rate)]}


def test_earlier_parked_part_is_not_adopted_over_a_later_run(cfg, store, monkeypatch):
    cfg.move_processed = False
    ingest_file(cfg, store, put(cfg, "acme_2026-05.json", _doc(40.0)))
    real = S.replace_with_retry

    def held(src, dst, *a, **k):
        if str(dst).endswith(".parquet"):
            raise PermissionError("held open")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(S, "replace_with_retry", held)
    monkeypatch.setattr(store, "_retry_pending_part_ops", lambda: None)
    ingest_file(cfg, store, put(cfg, "acme_2026-05.json", _doc(50.0)))     # v1 parks
    assert json.loads(q(store, "SELECT value FROM meta WHERE key='parked_parts'")[0][0])
    monkeypatch.setattr(S, "replace_with_retry", real)
    monkeypatch.delattr(store, "_retry_pending_part_ops")
    ingest_file(cfg, store, put(cfg, "acme_2026-05.json", _doc(60.0)))     # v2 swaps in
    store.rebuild_rollups()                                                # retries pending ops
    assert q(store, "SELECT negotiated_rate FROM rates") == [(60.0,)]
    assert json.loads(q(store, "SELECT value FROM meta WHERE key='parked_parts'")[0][0]) == {}


def test_earlier_doomed_part_does_not_delete_a_later_run(cfg, store, monkeypatch):
    cfg.move_processed = False
    ingest_file(cfg, store, put(cfg, "acme_2026-05.json", _doc(40.0)))
    real = S.unlink_with_retry
    monkeypatch.setattr(S, "unlink_with_retry",
                        lambda p, attempts=4: False if str(p).endswith(".parquet") else real(p, attempts))
    monkeypatch.setattr(S.Store, "_retry_pending_part_ops", lambda self: None)
    ingest_file(cfg, store, put(cfg, "acme_2026-05.json", _doc(40.0, code="99213")))  # 0 rows: doomed
    assert "parquet" in q(store, "SELECT value FROM meta WHERE key='doomed_parts'")[0][0]
    monkeypatch.undo()
    ingest_file(cfg, store, put(cfg, "acme_2026-05.json", _doc(60.0)))
    store.rebuild_rollups()
    assert q(store, "SELECT negotiated_rate FROM rates") == [(60.0,)]
    assert q(store, "SELECT negotiated_rate FROM rates_by_tin") == [(60.0,)]


def test_stored_fee_schedule_rows_migrate_to_dollars(cfg, store):
    cfg.move_processed = False
    ingest_file(cfg, store, put(cfg, "acme_2026-05.json",
                                {**HDR, "in_network": [item("97110", groups=[grp("1234567893", "123456789")],
                                                            ntype="fee_schedule")]}))
    # rewrite the part as an older build stored it, and mark it rule 2
    path = store.rates_dir / f"{S.file_key('acme_2026-05.json')}.parquet"
    tmp = path.with_suffix(".old")
    with store.connect() as con:
        con.execute(f"COPY (SELECT * REPLACE ('fee_schedule' AS negotiated_type, false AS is_dollar_rate) "
                    f"FROM read_parquet('{S.sql_path(path)}')) TO '{S.sql_path(tmp)}' (FORMAT parquet)")
        con.execute("UPDATE files SET row_rules = 2, qa = ? WHERE filename = 'acme_2026-05.json'",
                    [json.dumps({"rows": 1, "bundled_items": 3})])
    tmp.replace(path)
    st2 = S.Store(cfg.store_dir)          # open migrates
    assert q(st2, "SELECT negotiated_type, is_dollar_rate FROM rates") == [("fee schedule", True)]
    qa = json.loads(q(st2, "SELECT qa FROM files")[0][0])
    assert "re-ingest" in qa["reingest_suggested"]
    assert q(st2, "SELECT row_rules FROM files")[0][0] == S.ROW_RULES_VERSION


@pytest.mark.parametrize("big", [False, True])
def test_big_standalone_reference_file_is_stored_as_references(cfg, store, monkeypatch, big):
    # > 1 MB: the sniff window ends inside the table, so it can't see that
    # there is no in_network — it used to be a 0-row rate file
    n = 20_000
    refs = {**HDR, "provider_references": [
        {"provider_group_id": i, "provider_groups": [grp(str(1000000000 + i), str(100000000 + i))]}
        for i in range(1, n + 1)]}
    b = {**HDR, "in_network": [item("97110", refs=[1, 2])]}
    assert ingest_file(cfg, store, put(cfg, "acme_B_2026-05.json", b))["rows"] == 0
    monkeypatch.setattr(ing, "LARGE_FILE_UNCOMPRESSED_BYTES", 1 if big else 1_500_000_000)
    p = put(cfg, "acme_refs_2026-05.json", refs)
    assert p.stat().st_size > 1 << 20
    out = ingest_file(cfg, store, p)
    assert out["status"] == "done" and out["refs"] == n
    assert q(store, "SELECT count(DISTINCT ref_id) FROM provider_refs")[0][0] == n
    assert q(store, "SELECT file_type, rows_emitted FROM files WHERE filename = ?",
             "acme_refs_2026-05.json") == [("provider_reference", n)]
    assert "acme_B_2026-05.json" in out["requeued"] or q(
        store, "SELECT rows_emitted FROM files WHERE filename='acme_B_2026-05.json'")[0][0] == 2


def test_reference_file_streams_in_batches(cfg, store, monkeypatch):
    import mrfx.parser as P
    monkeypatch.setattr(P, "REF_BATCH", 7)
    calls = []
    real = store.save_provider_refs
    monkeypatch.setattr(store, "save_provider_refs",
                        lambda *a, **k: (calls.append(len(a[3])), real(*a, **k)))
    refs = {**HDR, "provider_references": [
        {"provider_group_id": i, "provider_groups": [grp(str(1000000000 + i), str(100000000 + i))]}
        for i in range(1, 21)]}
    assert ingest_file(cfg, store, put(cfg, "acme_refs_2026-05.json", refs))["refs"] == 20
    assert calls == [7, 7, 6]
    assert q(store, "SELECT count(*) FROM provider_refs")[0][0] == 20


def test_malformed_value_fails_in_words():
    msg = plain_parse_error(ValueError("integer overflow: 12345678901234567890"))
    assert msg.startswith("the payer's file holds a number too large")
    msg = plain_parse_error(Exception("lexical error: invalid char in json text."))
    assert "not valid JSON" in msg
    assert plain_parse_error(KeyError("x")) == "KeyError: 'x'"


@pytest.mark.parametrize("big", [False, True])
def test_pooled_worker_paths_agree(cfg, store, monkeypatch, big):
    # the parse-pool worker (the server's path) — own table wins, and a big
    # standalone reference file is stored as references
    from mrfx.ingest import ParsePoolManager, set_parse_pool
    if big:
        real = ing.preflight

        def big_pf(*a, **k):
            pf = real(*a, **k)
            pf.est_uncompressed_bytes = 10 ** 12
            return pf
        monkeypatch.setattr(ing, "preflight", big_pf)
    ingest_file(cfg, store, put(cfg, "acme_refs_2026-05.json", COMPANION))
    pool = ParsePoolManager(max_workers=1)
    set_parse_pool(pool)
    try:
        b = {**HDR, "in_network": [item("97110", refs=[1, 2])], "provider_references": [
            {"provider_group_id": 1, "provider_groups": [grp("3333333333", "333333333")]},
            {"provider_group_id": 2, "provider_groups": [grp("4444444444", "444444444")]}]}
        assert ingest_file(cfg, store, put(cfg, "acme_B_2026-05.json", b))["rows"] == 2
        assert [r[0] for r in q(store, "SELECT npi FROM rates ORDER BY 1")] == [
            "3333333333", "4444444444"]
        refs = {**HDR, "provider_references": [
            {"provider_group_id": i, "provider_groups": [grp(str(1000000000 + i), str(100000000 + i))]}
            for i in range(1, 20_001)]}
        out = ingest_file(cfg, store, put(cfg, "acme_big_refs_2026-05.json", refs))
        assert out["refs"] == 20_000
        assert q(store, "SELECT file_type FROM files WHERE filename='acme_big_refs_2026-05.json'") == [
            ("provider_reference",)]
    finally:
        set_parse_pool(None)
        pool.shutdown_now()
