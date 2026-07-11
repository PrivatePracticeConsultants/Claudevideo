"""Round-2 deep-audit regressions: numbers correctness + concurrency claims."""

import gzip
import json
import threading
import zipfile

from mrfx.ingest import ingest_file, scan_inbox
from mrfx.parser import InNetworkParser, parse_provider_group
from tests.mrfx.conftest import FIXTURES, drop, make_fixture


def _rates(store):
    with store.connect() as con:
        cols = [d[0] for d in con.execute("SELECT * FROM rates LIMIT 0").description]
        return [dict(zip(cols, r)) for r in con.execute("SELECT * FROM rates").fetchall()]


def _mrf(items):
    return {
        "reporting_entity_name": "Testco", "reporting_entity_type": "issuer",
        "version": "2.0.0", "last_updated_on": "2026-06-01", "in_network": items,
    }


def _item(code="97110", arrangement=None, prices=None, npi="1111111111"):
    it = {
        "billing_code": code, "billing_code_type": "CPT",
        "negotiated_rates": [{
            "provider_groups": [{"npi": [npi], "tin": {"type": "ein", "value": "431111111"}}],
            "negotiated_prices": prices or [{
                "negotiated_type": "negotiated", "negotiated_rate": 50.0,
                "billing_class": "professional", "service_code": ["11"],
            }],
        }],
    }
    if arrangement is not None:
        it["negotiation_arrangement"] = arrangement
    return it


def test_bundle_and_capitation_items_excluded(cfg, store):
    # a $500 bundle price is NOT a per-code rate — it must never enter medians
    p = make_fixture(cfg.inbox_dir, "bundle.json", _mrf([
        _item(arrangement="bundle", prices=[{
            "negotiated_type": "negotiated", "negotiated_rate": 500.0,
            "billing_class": "professional"}]),
        _item(arrangement="ffs"),
        _item(),  # missing arrangement defaults to ffs per schema
    ]))
    res = ingest_file(cfg, store, p)
    assert res["status"] == "done"
    rows = _rates(store)
    assert {r["negotiated_rate"] for r in rows} == {50.0}  # bundle price absent
    st = store.file_status("bundle.json")
    assert json.loads(st["qa"] if isinstance(st["qa"], str) else json.dumps(st["qa"]))[
        "bundled_items"] == 1


def test_modifier_and_pos_order_never_splits_the_grain(cfg, store):
    # [GP,59] and [59,GP] are ONE modifier set; publication order must not
    # split the dedup grain into two medians
    p = make_fixture(cfg.inbox_dir, "mods.json", _mrf([
        _item(prices=[
            {"negotiated_type": "negotiated", "negotiated_rate": 50.0,
             "billing_class": "professional", "billing_code_modifier": ["GP", "59"],
             "service_code": ["11", "12"]},
            {"negotiated_type": "negotiated", "negotiated_rate": 50.0,
             "billing_class": "professional", "billing_code_modifier": ["59", "GP", "GP"],
             "service_code": ["12", "11"]},
        ]),
    ]))
    ingest_file(cfg, store, p)
    with store.connect() as con:
        rows = con.execute(
            "SELECT DISTINCT array_to_string(billing_code_modifier, '|'), "
            "array_to_string(service_code, '|') FROM rates").fetchall()
    assert rows == [("59|GP", "11|12")]  # one canonical form, deduped


def test_file_month_falls_back_to_filename_date(cfg, store):
    doc = _mrf([_item()])
    del doc["last_updated_on"]
    p = make_fixture(cfg.inbox_dir, "2025-03_testco_rates.json", doc)
    ingest_file(cfg, store, p)
    assert {r["file_month"] for r in _rates(store)} == {"2025-03"}


def test_untyped_ten_digit_tin_is_flagged_npi():
    v, t, _ = parse_provider_group({"npi": ["1111111111"], "tin": {"value": "1234567893"}})
    assert (v, t) == ("1234567893", "npi")  # NPI in the TIN slot, flagged
    v, t, _ = parse_provider_group({"npi": ["1111111111"], "tin": {"value": "431111111"}})
    assert (v, t) == ("431111111", "ein")


def test_provider_refs_newest_vintage_wins(store):
    old = {1: [("431111111", "ein", ("1111111111",))]}
    new = {1: [("439999999", "ein", ("1222222222",))]}
    store.save_provider_refs("Testco", "refs_old.json", "2026-05-01", old)
    store.save_provider_refs("Testco", "refs_new.json", "2026-06-01", new)
    loaded = store.load_provider_refs("Testco")
    assert loaded == {1: [("439999999", "ein", ("1222222222",))]}  # no stale union


def test_corrupt_gz_neighbor_cannot_wedge_the_inbox(cfg, store):
    # a mid-stream deflate error raises zlib.error — it used to escape the
    # companion scan and abort EVERY scan pass forever
    good = gzip.compress(json.dumps(_mrf([_item()])).encode())
    (cfg.inbox_dir / "good.json.gz").write_bytes(good)
    (cfg.inbox_dir / "corrupt.json.gz").write_bytes(good[:40] + b"\x00" * 200)
    results = scan_inbox(cfg, store)
    by_name = {r["file"]: r["status"] for r in results}
    assert by_name["good.json.gz"] == "done"
    assert by_name["corrupt.json.gz"] == "quarantined"
    assert scan_inbox(cfg, store) is not None  # next scan runs fine too


def test_zip_reads_largest_member_not_first(cfg, store):
    p = cfg.inbox_dir / "multi.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("a_manifest.json", json.dumps({"note": "tiny manifest"}))
        z.writestr("b_rates.json", json.dumps(_mrf([_item()])))
    res = ingest_file(cfg, store, p)
    assert res["status"] == "done" and res["rows"] > 0  # the manifest would be 0 rows


def test_second_concurrent_ingest_of_same_file_bows_out(cfg, store, monkeypatch):
    import mrfx.ingest as ing

    barrier = threading.Barrier(2, timeout=10)
    results = []
    p = drop(cfg, "innetwork_mixed.json", gz=True)

    real = ing._ingest_file_locked

    def slow(cfg2, store2, path2, pf2, bar2, rb2):
        barrier.wait()  # hold the claim until the second call has been tried
        return real(cfg2, store2, path2, pf2, bar2, rb2)

    monkeypatch.setattr(ing, "_ingest_file_locked", slow)
    t = threading.Thread(target=lambda: results.append(ingest_file(cfg, store, p)))
    t.start()
    import time as _t

    for _ in range(100):  # wait until the first thread holds the claim
        if ing.ingest_in_progress(p.name):
            break
        _t.sleep(0.05)
    second = ingest_file(cfg, store, p)  # must bow out instantly, not corrupt
    barrier.wait()
    t.join(timeout=60)
    assert second["status"] == "skipped" and "already being ingested" in second["error"]
    assert results and results[0]["status"] == "done"


def test_worker_count_auto_scales_and_clamps(monkeypatch):
    import mrfx.fetch as fetch
    from mrfx.config import MrfxConfig
    monkeypatch.setattr(fetch.os, "cpu_count", lambda: 8)
    assert fetch.resolve_worker_count(MrfxConfig(parallel_ingests=0)) == 7  # auto = cores-1
    assert fetch.resolve_worker_count(MrfxConfig(parallel_ingests=3)) == 3  # pinned
    assert fetch.resolve_worker_count(MrfxConfig(parallel_ingests=99)) == 7  # clamp to cores-1
    assert fetch.resolve_worker_count(MrfxConfig(parallel_ingests=1)) == 1
    monkeypatch.setattr(fetch.os, "cpu_count", lambda: 64)
    assert fetch.resolve_worker_count(MrfxConfig(parallel_ingests=0)) == 8  # auto capped at 8
    monkeypatch.setattr(fetch.os, "cpu_count", lambda: 1)
    assert fetch.resolve_worker_count(MrfxConfig(parallel_ingests=0)) == 1  # never below 1


def test_json_backend_detector_runs():
    # must never crash, and in this venv the fast C backend is present
    from mrfx.parser import warn_if_slow_json_backend
    assert warn_if_slow_json_backend() is True


def test_file_month_memo_invalidates_on_header_change():
    # the memo must recompute when last_updated_on arrives late (header-at-EOF)
    from mrfx.parser import ParseResult
    r = ParseResult(source_file="rates.json")
    r.last_updated_on = None
    first = r.file_month  # falls back (no date in name/header)
    r.last_updated_on = "2025-03-01"
    assert r.file_month == "2025-03"  # recomputed, not the stale cached value
    r.last_updated_on = "2025-08-15"
    assert r.file_month == "2025-08"


def test_conservative_percentile_cannot_exceed_target():
    import pytest

    from mrfx.benchmark import BenchmarkError, compute_opportunity

    bench = {"target_percentile": 25, "rows": [
        {"billing_code": "97110", "description": "x", "subject_rate": 50.0, "p25": 60.0, "p40": 70.0},
    ]}
    with pytest.raises(BenchmarkError, match="must not exceed"):
        compute_opportunity(bench, {"97110": 100}, conservative_percentile=40)
