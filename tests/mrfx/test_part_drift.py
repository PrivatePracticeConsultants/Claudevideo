"""The rollups must never silently drift from the raw parts (round 3, P1.6):
a damaged part set aside, a part a locked file kept from being deleted or
swapped in, and a failed re-ingest whose old part stays live."""

from __future__ import annotations

import json

import pytest

from mrfx.store import Store, file_key


def _mrf(payer, tin, npi, rate):
    return {"reporting_entity_name": payer, "reporting_entity_type": "issuer",
            "last_updated_on": "2026-05-01", "version": "2.0.0", "provider_references": [],
            "in_network": [{"negotiation_arrangement": "ffs", "name": "x",
                            "billing_code_type": "CPT", "billing_code_type_version": "2026",
                            "billing_code": "97110", "description": "x",
                            "negotiated_rates": [{
                                "provider_groups": [{"npi": [npi], "tin": {"type": "ein", "value": tin}}],
                                "negotiated_prices": [{"negotiated_type": "negotiated",
                                                       "negotiated_rate": rate,
                                                       "billing_class": "professional",
                                                       "service_code": ["11"],
                                                       "expiration_date": "9999-12-31"}]}]}]}


@pytest.fixture
def two(cfg, store):
    """Two payers ingested and rolled up."""
    from mrfx.ingest import ingest_file
    a = cfg.inbox_dir / "alpha_2026-05.json"
    b = cfg.inbox_dir / "bravo_2026-05.json"
    a.write_text(json.dumps(_mrf("Alpha", "830000001", "1830000001", 40.0)))
    b.write_text(json.dumps(_mrf("Bravo", "830000002", "1830000002", 50.0)))
    for p in (a, b):
        assert ingest_file(cfg, store, p)["status"] == "done"
    store.update_rollups_incremental()
    return a.name, b.name


def _rolled(store):
    with store.connect() as con:
        return sorted(r[0] for r in con.execute("SELECT DISTINCT payer FROM rates_by_tin").fetchall())


def test_a_damaged_part_flips_its_file_and_leaves_the_analytics(cfg, store, two):
    a, _ = two
    part = store.rates_dir / f"{file_key(a)}.parquet"
    part.write_bytes(part.read_bytes()[:-20])            # truncated on disk
    store.close()
    st = Store(cfg.store_dir)
    f = st.file_status(a)
    assert f["status"] == "failed" and "damaged on disk" in f["error"]
    assert st.rollups_stale()                             # a full rebuild is asked for
    st.rebuild_rollups()
    assert _rolled(st) == ["Bravo"]                       # Alpha's rows no longer served
    st.close()


def test_a_part_that_could_not_be_deleted_is_retried_and_then_rerolled(cfg, store, two, monkeypatch):
    """A 0-row re-ingest (or any part removal) whose delete is blocked by a
    reader: the part stayed in the glob for good while the files row said 0
    rows. Now it is remembered and deleted at the next chance, and its payers
    re-roll so the analytics drop its rows."""
    import mrfx.store as ms
    a, _ = two
    part = store.rates_dir / f"{file_key(a)}.parquet"
    monkeypatch.setattr(ms, "unlink_with_retry", lambda p, attempts=4: False)   # held open
    store.drop_rates_part(a)
    assert part.exists()
    assert json.loads(store.meta_get("doomed_parts")) == [part.name]
    monkeypatch.undo()
    store.update_rollups_incremental()                    # the retry runs first, then the slice
    assert not part.exists() and json.loads(store.meta_get("doomed_parts")) == []
    assert _rolled(store) == ["Bravo"]


def test_a_finished_parse_whose_swap_is_blocked_is_parked_then_adopted(cfg, store, two, monkeypatch):
    import mrfx.store as ms
    from mrfx.ingest import ingest_file
    a, _ = two
    real = ms.replace_with_retry
    calls = {"n": 0}

    def locked(src, dst, attempts=12):
        if str(dst).endswith(".parquet") and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError("held open by an export")
        return real(src, dst, attempts)
    monkeypatch.setattr(ms, "replace_with_retry", locked)
    p = cfg.inbox_dir / a
    p.write_text(json.dumps(_mrf("Alpha", "830000001", "1830000001", 44.0)))   # a re-ingest: $44 now
    assert ingest_file(cfg, store, p, rebuild_rollups=False)["status"] == "done"   # not thrown away
    assert (store.rates_dir / f".{file_key(a)}.parked").exists()
    monkeypatch.undo()
    store.update_rollups_incremental()                    # adopted first, then rolled up
    assert not (store.rates_dir / f".{file_key(a)}.parked").exists()
    with store.connect() as con:
        assert con.execute("SELECT negotiated_rate FROM rates_by_tin WHERE payer = 'Alpha'"
                           ).fetchall() == [(44.0,)]


def test_a_failed_reingest_queues_the_surviving_part(cfg, store, two, monkeypatch):
    """The old part stays live while the row is 'failed' — invisible to the
    delta; its payers are queued so the analytics recompute from the raw."""
    import mrfx.ingest as mi
    a, _ = two
    store.meta_set("rollup_pending_payers", "")
    with store.connect() as con:
        con.execute("DELETE FROM meta WHERE key = 'rollup_pending_payers'")

    def boom(*a_, **k):
        raise RuntimeError("disk error mid-parse")
    monkeypatch.setattr(mi, "run_in_network_parse", boom)
    monkeypatch.setattr(mi, "run_big_file_fast", boom, raising=False)
    p = cfg.inbox_dir / a
    p.write_text(json.dumps(_mrf("Alpha", "830000001", "1830000001", 40.0)))
    assert mi.ingest_file(cfg, store, p, rebuild_rollups=False)["status"] == "failed"
    assert "Alpha" in (store.meta_get("rollup_pending_payers") or "")


def test_header_counts_from_the_spine_match_the_raw_store(cfg, store, two):
    """P2.3: the header's TIN/payer counts come from the TIN spine when it is
    current (a whole-store raw hash was 35-42 s cold at 306M rows) — and from
    the raw parts while a new file awaits its rollup, so they never lag."""
    from mrfx.ingest import ingest_file

    def raw():
        with store.connect() as con:
            return con.execute("SELECT count(*), count(DISTINCT tin_value), "
                               "count(DISTINCT payer) FROM rates").fetchone()
    s = store.store_stats(max_age_seconds=0)
    assert (s["rates"], s["tins"], s["payers"]) == raw() == (2, 2, 2)
    c = cfg.inbox_dir / "charlie_2026-05.json"
    c.write_text(json.dumps(_mrf("Charlie", "830000003", "1830000003", 60.0)))
    ingest_file(cfg, store, c, rebuild_rollups=False)
    s = store.store_stats(max_age_seconds=0)
    assert (s["rates"], s["tins"], s["payers"]) == raw() == (3, 3, 3)
