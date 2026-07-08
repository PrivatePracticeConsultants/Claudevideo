"""Acceptance tests for the streaming extractor (spec §7):
(a) inline provider_groups AND provider_references are both captured
(b) non-target NPIs are filtered out
(c) modifiers survive extraction
(d) gzip streaming works
plus: provider_references appearing AFTER in_network still resolve (deferred path).
"""

import gzip
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from src.extractor import Extractor
from src.toc import SourceFile
from src.writer import RECORD_SCHEMA, RecordWriter
from tests.conftest import FIXTURES

FIXTURE = FIXTURES / "in_network_small.json"


def run_extraction(cfg, targets, source_path: str) -> tuple[list[dict], dict]:
    writer = RecordWriter(cfg.paths.out_dir, run_month="2026-06")
    ex = Extractor(cfg=cfg, targets=targets, client=None, writer=writer)
    source = SourceFile(payer="blue_kc", url=str(source_path), plan_names=["TEST PLAN"], plan_ids=["P1"])
    result = ex.extract_source(source)
    rows = []
    for part in Path(cfg.paths.out_dir).rglob("*.parquet"):
        table = pq.ParquetFile(part).read()
        assert table.schema.equals(RECORD_SCHEMA)
        # payer comes back as a hive partition column on dataset reads
        assert "payer=blue_kc" in str(part)
        rows.extend(table.to_pylist())
    return rows, result


def test_inline_and_referenced_providers_both_captured(cfg, targets):
    rows, result = run_extraction(cfg, targets, FIXTURE)
    assert result["status"] == "done"
    by_npi = {r["npi"] for r in rows}
    # 2222222222 arrives via inline provider_groups, 1111111111 via provider_references
    assert "2222222222" in by_npi
    assert "1111111111" in by_npi
    ref_rows = [r for r in rows if r["npi"] == "1111111111"]
    assert ref_rows and ref_rows[0]["negotiated_rate"] == 31.07
    assert ref_rows[0]["negotiated_type"] == "fee schedule"
    assert ref_rows[0]["service_code"] == ["11", "12"]


def test_non_target_npi_filtered_out(cfg, targets):
    rows, _ = run_extraction(cfg, targets, FIXTURE)
    npis = {r["npi"] for r in rows}
    assert "8888888888" not in npis  # non-target inline group
    assert "9999999999" not in npis  # non-target referenced group
    # and the non-target billing code never appears
    assert all(r["billing_code"] != "99213" for r in rows)


def test_modifiers_survive(cfg, targets):
    rows, _ = run_extraction(cfg, targets, FIXTURE)
    mods = {(r["billing_code"], r["billing_code_modifier"], r["negotiated_rate"]) for r in rows}
    assert ("97110", "CQ", 29.33) in mods       # modifier row kept distinct
    assert ("97110", "", 34.5) in mods           # base row kept distinct
    assert ("G0283", "", 12.75) in mods          # HCPCS target code captured


def test_gzip_streaming(cfg, targets, tmp_path):
    gz_path = tmp_path / "in_network_small.json.gz"
    gz_path.write_bytes(gzip.compress(FIXTURE.read_bytes()))
    rows, result = run_extraction(cfg, targets, str(gz_path))
    assert result["status"] == "done"
    assert {r["npi"] for r in rows} == {"1111111111", "2222222222"}
    assert result["rows"] == len(rows) > 0


def test_provider_references_after_in_network_are_deferred_and_resolved(cfg, targets, tmp_path):
    data = json.loads(FIXTURE.read_text())
    refs = data.pop("provider_references")
    reordered = dict(data)  # in_network now streams before provider_references
    reordered["provider_references"] = refs
    path = tmp_path / "refs_last.json"
    path.write_text(json.dumps(reordered))
    rows, result = run_extraction(cfg, targets, str(path))
    assert result["status"] == "done"
    assert "1111111111" in {r["npi"] for r in rows}
    assert "9999999999" not in {r["npi"] for r in rows}


def test_schema_v1_skipped(cfg, targets, tmp_path):
    data = json.loads(FIXTURE.read_text())
    data["version"] = "1.0.0"
    path = tmp_path / "v1.json"
    path.write_text(json.dumps(data))
    rows, result = run_extraction(cfg, targets, str(path))
    assert result["status"] == "skipped_schema"
    assert rows == []


def test_checkpoint_skips_completed_files(cfg, targets):
    writer = RecordWriter(cfg.paths.out_dir, run_month="2026-06")
    ex = Extractor(cfg=cfg, targets=targets, client=None, writer=writer)
    source = SourceFile(payer="blue_kc", url=str(FIXTURE))
    assert ex.extract_source(source)["status"] == "done"
    summary = ex.extract_all([source])
    assert summary["skipped_checkpoint"] == 1
    assert summary["done"] == 0


def test_tin_secondary_match(cfg, targets, tmp_path):
    """A group whose NPIs are unknown still matches when its TIN is targeted."""
    data = json.loads(FIXTURE.read_text())
    data["in_network"][0]["negotiated_rates"] = [
        {
            "provider_groups": [
                {"npi": [7777777777], "tin": {"type": "ein", "value": "222222222"}}
            ],
            "negotiated_prices": [
                {
                    "negotiated_type": "negotiated",
                    "negotiated_rate": 40.0,
                    "service_code": ["11"],
                    "billing_class": "professional",
                }
            ],
        }
    ]
    del data["in_network"][1:]
    path = tmp_path / "tin_match.json"
    path.write_text(json.dumps(data))
    rows, _ = run_extraction(cfg, targets, str(path))
    assert len(rows) == 1
    assert rows[0]["npi"] == "7777777777"
    assert rows[0]["tin"] == "222222222"
    assert rows[0]["org_name"] == "Inline PT Group"
