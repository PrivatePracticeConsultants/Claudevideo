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
