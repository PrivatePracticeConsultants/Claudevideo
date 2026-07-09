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


def test_big_file_two_pass_still_extracts_target_codes(cfg, store, monkeypatch):
    import mrfx.ingest as ing

    monkeypatch.setattr(ing, "LARGE_FILE_UNCOMPRESSED_BYTES", 0)  # force two-pass
    p = drop(cfg, "innetwork_mixed.json")
    result = ing.ingest_file(cfg, store, p)
    assert result["status"] == "done" and result["rows"] > 0
