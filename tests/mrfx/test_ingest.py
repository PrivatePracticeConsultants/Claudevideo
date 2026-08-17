"""Ingest-core acceptance tests (spec §9)."""

import json

import duckdb
import pytest

from mrfx.ingest import ingest_file, scan_inbox
from mrfx.sniff import preflight
from tests.mrfx.conftest import FIXTURES, drop, make_fixture


def rates(store, where="1=1"):
    with store.connect() as con:
        cols = [d[0] for d in con.execute("SELECT * FROM rates LIMIT 0").description]
        return [dict(zip(cols, r)) for r in con.execute(f"SELECT * FROM rates WHERE {where}").fetchall()]


# -- preflight ----------------------------------------------------------------


def test_preflight_classifies_rate_file(cfg, store):
    p = drop(cfg, "innetwork_mixed.json", gz=True)
    pf = preflight(p, cfg, store)
    assert pf.file_type == "in_network"
    assert pf.payer == "Testco"  # payer_name_map normalization
    assert pf.schema_version == "2.0.0"
    assert pf.last_updated_on == "2026-06-01"
    assert pf.uses_provider_references is True
    # embedded reference table -> self-companioned
    assert pf.verdict == "READY"


def test_preflight_classifies_reference_and_toc(cfg, store):
    ref = drop(cfg, "provider_reference_companion.json")
    toc = drop(cfg, "toc_index.json")
    pf_ref = preflight(ref, cfg, store)
    assert pf_ref.file_type == "provider_reference"
    assert pf_ref.verdict == "NOT A RATE FILE"
    pf_toc = preflight(toc, cfg, store)
    assert pf_toc.file_type == "toc"
    assert pf_toc.verdict == "NOT A RATE FILE"
    assert "index" in " ".join(pf_toc.messages).lower()


def test_preflight_unknown(cfg, store, tmp_path):
    p = make_fixture(cfg.inbox_dir, "junk.json", {"hello": ["world"]})
    pf = preflight(p, cfg, store)
    assert pf.file_type == "unknown"
    assert pf.verdict == "UNREADABLE"


def test_preflight_needs_companion_flips_ready(cfg, store, tmp_path):
    """In-network file whose rate groups cite refs with NO embedded table."""
    data = json.loads((FIXTURES / "innetwork_mixed.json").read_text())
    del data["provider_references"]
    data["in_network"][0]["negotiated_rates"] = [
        g for g in data["in_network"][0]["negotiated_rates"] if "provider_references" in g
    ]
    p = make_fixture(cfg.inbox_dir, "refs_only.json", data)
    pf = preflight(p, cfg, store)
    assert pf.uses_provider_references is True
    assert pf.verdict == "NEEDS COMPANION"

    # companion arrives in the inbox -> READY
    drop(cfg, "provider_reference_companion.json")
    pf2 = preflight(p, cfg, store)
    assert pf2.verdict == "READY"


# -- ingest ---------------------------------------------------------------


def test_ingest_inline_and_referenced_with_skip_counter(cfg, store):
    p = drop(cfg, "innetwork_mixed.json", gz=True)
    res = ingest_file(cfg, store, p)
    assert res["status"] == "done"
    rows = rates(store)
    npis = {r["npi"] for r in rows}
    assert "2222222222" in npis          # inline provider_groups
    assert "1111111111" in npis          # embedded provider_reference 101
    assert "5555555555" not in npis      # ref 555 has no source yet
    assert res["ref_groups_skipped"] == 1  # surfaced, never silent
    assert all(r["billing_code"] != "99213" for r in rows)  # code filter
    assert all(r["payer"] == "Testco" for r in rows)
    # moved out of inbox
    assert not p.exists() and (cfg.processed_dir / p.name).exists()


def test_orphan_tmp_sweep_spares_live_writers(cfg, tmp_path):
    # a crash mid-ingest leaves a .{key}.{pid}.parquet.tmp behind: a fresh
    # Store must clean it up — but must NOT touch a temp whose owning process
    # is still alive (a second serve/CLI process writing next door)
    import os

    from mrfx.store import Store

    s1 = Store(cfg.store_dir)
    orphan = s1.rates_dir / ".deadfile.999999999.parquet.tmp"
    live = s1.rates_dir / f".livefile.{os.getpid()}.parquet.tmp"
    orphan.write_bytes(b"x")
    live.write_bytes(b"x")
    Store(cfg.store_dir)  # init runs the sweep
    assert not orphan.exists()
    assert live.exists()


def test_zip_mrf_ingests_end_to_end(cfg, store):
    # zipfile needs a SEEKABLE stream (central directory lives at the end of
    # the archive): the progress wrapper used during ingest must support seek,
    # or every zip MRF dies with a misleading "File is not a zip file"
    import zipfile

    p = cfg.inbox_dir / "innetwork_mixed.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(FIXTURES / "innetwork_mixed.json", "innetwork_mixed.json")
    res = ingest_file(cfg, store, p)
    assert res["status"] == "done"
    assert {r["npi"] for r in rates(store)} >= {"2222222222", "1111111111"}


def test_reference_file_ingest_then_requeue_resolves_skips(cfg, store):
    p = drop(cfg, "innetwork_mixed.json", gz=True)
    ingest_file(cfg, store, p)
    assert store.file_status(p.name)["ref_groups_skipped"] == 1

    ref = drop(cfg, "provider_reference_companion.json")
    res = ingest_file(cfg, store, ref)
    assert res["status"] == "done"
    assert p.name in res["requeued"]

    st = store.file_status(p.name)
    assert st["ref_groups_skipped"] == 0
    npis = {r["npi"] for r in rates(store)}
    assert {"5555555555", "5555555556"} <= npis
    ref_rows = [r for r in rates(store) if r["npi"] == "5555555555"]
    assert ref_rows[0]["negotiated_rate"] == 27.5


def test_modifiers_and_dollar_tagging(cfg, store):
    drop(cfg, "innetwork_mixed.json")
    scan_inbox(cfg, store)
    rows = rates(store)
    cq = [r for r in rows if r["billing_code_modifier"] == ["CQ"]]
    assert cq and cq[0]["negotiated_rate"] == 29.33
    pct = [r for r in rows if r["negotiated_type"] == "percentage"]
    assert pct and all(r["is_dollar_rate"] is False for r in pct)
    dollars = [r for r in rows if r["negotiated_type"] in ("negotiated", "fee schedule")]
    assert all(r["is_dollar_rate"] is True for r in dollars)


def test_v1_file_ingests_best_effort(cfg, store):
    data = json.loads((FIXTURES / "innetwork_mixed.json").read_text())
    data["version"] = "1.0.0"
    p = make_fixture(cfg.inbox_dir, "v1_file.json", data)
    res = ingest_file(cfg, store, p)
    assert res["status"] == "done"
    rows = rates(store)
    assert rows and all(r["schema_version"] == "1.0.0" for r in rows)


def test_toc_quarantined_with_friendly_message(cfg, store):
    p = drop(cfg, "toc_index.json")
    res = ingest_file(cfg, store, p)
    assert res["status"] == "quarantined"
    assert "TOC" in res["error"] or "index" in res["error"].lower()
    assert (cfg.failed_dir / p.name).exists()


def test_corrupt_file_quarantines_not_crashes(cfg, store):
    p = cfg.inbox_dir / "corrupt.json.gz"
    p.write_bytes(b"\x1f\x8b\x08\x00garbage-not-gzip")
    res = ingest_file(cfg, store, p)
    assert res["status"] in ("failed", "quarantined")
    assert store.file_status("corrupt.json.gz")["status"] in ("failed", "quarantined")


def test_confirm_over_gb_gate(cfg, store):
    cfg.confirm_over_gb = 0.000001  # 1 KB
    drop(cfg, "innetwork_mixed.json")
    results = scan_inbox(cfg, store)
    assert results[0]["status"] == "pending_confirmation"
    results = scan_inbox(cfg, store, force=True)
    assert results[0]["status"] == "done"


def test_dedup_view_counts_sources(cfg, store):
    drop(cfg, "innetwork_mixed.json")
    scan_inbox(cfg, store)
    # same rates from a second source file
    drop(cfg, "innetwork_mixed.json", rename="second_copy.json")
    scan_inbox(cfg, store)
    with store.connect() as con:
        row = con.execute(
            """
            SELECT source_count FROM rates_dedup
            WHERE npi = '2222222222' AND billing_code = '97110'
              AND modifier_set = '' AND negotiated_rate = 34.5
            """
        ).fetchone()
    assert row[0] == 2


def test_big_file_scan_skips_extraction_when_no_target_codes(cfg, store, monkeypatch):
    # two-pass path: when pass 1 finds NONE of the target codes, pass 2 is
    # skipped entirely and the file is honestly recorded as done/0 rows
    import json as _json
    import mrfx.ingest as ing

    monkeypatch.setattr(ing, "LARGE_FILE_UNCOMPRESSED_BYTES", 0)  # force two-pass
    doc = _json.loads((FIXTURES / "innetwork_mixed.json").read_text())
    for item in doc["in_network"]:
        item["billing_code"] = "99213"  # office visit — not a therapy code
    path = cfg.inbox_dir / "no_target_codes.json"
    path.write_text(_json.dumps(doc))
    result = ing.ingest_file(cfg, store, path)
    assert result["status"] == "done" and result["rows"] == 0
    assert "extraction pass skipped" in result["note"]
    st = store.file_status("no_target_codes.json")
    assert st["status"] == "done" and (st.get("rows_emitted") or 0) == 0


def test_short_circuit_reingest_drops_stale_part(cfg, store, monkeypatch):
    # a RE-ingest under the same filename whose new content short-circuits
    # (no target codes) must DROP the old version's parquet part — otherwise
    # the files table says done/0 rows while the store keeps serving the old
    # rates as current (invariant 6: re-ingest atomically replaces the part)
    import json as _json
    import mrfx.ingest as ing

    doc = _json.loads((FIXTURES / "innetwork_mixed.json").read_text())
    path = cfg.inbox_dir / "monthly.json"
    path.write_text(_json.dumps(doc))
    assert ing.ingest_file(cfg, store, path)["rows"] > 0   # v1: real therapy rows
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM rates WHERE source_file='monthly.json'").fetchone()[0] > 0

    monkeypatch.setattr(ing, "LARGE_FILE_UNCOMPRESSED_BYTES", 0)  # v2 takes the two-pass path
    for item in doc["in_network"]:
        item["billing_code"] = "99213"                     # v2: no target codes at all
    path.write_text(_json.dumps(doc))
    result = ing.ingest_file(cfg, store, path)
    assert result["rows"] == 0 and "skipped" in result["note"]
    with store.connect() as con:                            # v1's rows are GONE
        assert con.execute("SELECT count(*) FROM rates WHERE source_file='monthly.json'").fetchone()[0] == 0


def test_big_file_two_pass_still_extracts_target_codes(cfg, store, monkeypatch):
    import mrfx.ingest as ing

    monkeypatch.setattr(ing, "LARGE_FILE_UNCOMPRESSED_BYTES", 0)  # force two-pass
    p = drop(cfg, "innetwork_mixed.json")
    result = ing.ingest_file(cfg, store, p)
    assert result["status"] == "done" and result["rows"] > 0


def test_rollup_rebuild_failure_keeps_previous_tables(cfg, store, monkeypatch):
    # the partitioned rebuild must be ALL OR NOTHING: a mid-slice failure
    # (temp cap, memory) rolls back to the previous complete tables instead
    # of serving a partially-filled rollup as truth
    import mrfx.store as st

    drop(cfg, "innetwork_mixed.json", gz=True)
    scan_inbox(cfg, store)
    store.rebuild_rollups()
    with store.connect() as con:
        before = con.execute("SELECT count(*) FROM rates_by_tin_tbl").fetchone()[0]
    assert before > 0
    monkeypatch.setattr(st, "ROLLUP_PARTITION_ROWS", 1)   # force multi-slice
    monkeypatch.setattr(st, "TIN_DIRECTORY_QUERY", "SELECT * FROM no_such_table WHERE {part}")
    import pytest as _pytest

    with _pytest.raises(Exception):
        store.rebuild_rollups()
    with store.connect() as con:  # previous tables intact, not partial/empty
        assert con.execute("SELECT count(*) FROM rates_by_tin_tbl").fetchone()[0] == before
        assert con.execute("SELECT count(*) FROM tin_directory_tbl").fetchone()[0] > 0


def test_two_pass_keeps_scalar_provider_references(cfg, store, monkeypatch):
    # payers write "provider_references": 101 (scalar) as well as [101]; the
    # two-pass skim must collect both or big files silently drop those groups
    import json as _json

    import mrfx.ingest as ing

    doc = _json.loads((FIXTURES / "innetwork_mixed.json").read_text())
    for item in doc["in_network"]:
        for grp in item["negotiated_rates"]:
            refs = grp.get("provider_references")
            if isinstance(refs, list) and len(refs) == 1:
                grp["provider_references"] = refs[0]  # scalar-ify
    p = cfg.inbox_dir / "scalar_refs.json"
    p.write_text(_json.dumps(doc))
    monkeypatch.setattr(ing, "LARGE_FILE_UNCOMPRESSED_BYTES", 1)  # force two-pass
    res = ingest_file(cfg, store, p)
    assert res["status"] == "done"
    npis = {r["npi"] for r in rates(store)}
    assert "1111111111" in npis  # embedded ref 101, cited as a scalar


def test_forget_file_erases_data_and_frees_disk(cfg, store):
    # user-controlled per-file erasure: rates gone, raw copies gone, other
    # files untouched, and the file re-ingests cleanly if re-added later
    from mrfx.ingest import forget_file

    p1 = drop(cfg, "innetwork_mixed.json", gz=True)
    scan_inbox(cfg, store)
    make_fixture(cfg.inbox_dir, "keep.json",
                        json.loads((FIXTURES / "innetwork_mixed.json").read_text()))
    scan_inbox(cfg, store)
    total = len(rates(store))
    assert total > 0 and store.file_status(p1.name)

    info = forget_file(cfg, store, p1.name)
    assert info["rows"] > 0 and info["bytes"] > 0
    assert store.file_status(p1.name) is None
    remaining = rates(store)
    assert remaining and all(r["source_file"] == "keep.json" for r in remaining)
    # raw copies gone everywhere the app keeps them
    for d in (cfg.inbox_dir, cfg.processed_dir, cfg.failed_dir, cfg.downloads_dir):
        assert not (d / p1.name).exists()
    # rollups rebuilt: still serving data, none of it from the forgotten file
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM rates_by_tin").fetchone()[0] > 0
        assert con.execute(
            "SELECT count(*) FROM rates_by_tin WHERE source_files = ?", [p1.name]
        ).fetchone()[0] == 0
    # path traversal never reaches the unlink calls
    with pytest.raises(ValueError):
        forget_file(cfg, store, "../evil")
    # and the same file re-ingests cleanly afterward
    drop(cfg, "innetwork_mixed.json", gz=True)
    scan_inbox(cfg, store)
    assert len(rates(store)) == total
    assert store.file_status(p1.name)["status"] == "done"


def test_reset_clears_url_queue(cfg, store):
    store.enqueue_url("https://x.example/rates.json.gz", "https://x.example/rates.json.gz")
    store.reset()
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM url_queue").fetchone()[0] == 0
    # and the same URL re-queues cleanly after the wipe
    assert store.enqueue_url("https://x.example/rates.json.gz", "https://x.example/rates.json.gz") is not None
