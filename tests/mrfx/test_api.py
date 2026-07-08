"""API acceptance tests: server-side pagination/sort, filter/CSV parity,
2M-row performance, sources registry overrides."""

import csv
import io
import time

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.ingest import scan_inbox
from tests.mrfx.conftest import drop


@pytest.fixture
def client(cfg, store):
    drop(cfg, "innetwork_mixed.json")
    drop(cfg, "provider_reference_companion.json")
    scan_inbox(cfg, store)
    return TestClient(create_app(cfg, store))


def test_rates_default_sort_and_pagination(client):
    r = client.get("/api/rates?page_size=2&page=1").json()
    assert r["total"] == 5  # 6 rows minus the percentage row (dollar_only default ON)
    assert len(r["rows"]) == 2
    rates = [row["negotiated_rate"] for row in r["rows"]]
    assert rates == sorted(rates, reverse=True)  # default: rate desc
    r2 = client.get("/api/rates?page_size=2&page=2").json()
    assert [x["npi"] for x in r2["rows"]] != [x["npi"] for x in r["rows"]]


def test_dollar_only_toggle(client):
    all_rows = client.get("/api/rates?dollar_only=0").json()
    assert all_rows["total"] == 6
    pct = [x for x in all_rows["rows"] if x["negotiated_type"] == "percentage"]
    assert pct and pct[0]["is_dollar_rate"] is False


def test_filters_modifier_and_search(client):
    base = client.get("/api/rates?modifier=base").json()
    assert all(x["modifier_set"] == "" for x in base["rows"])
    cq = client.get("/api/rates?modifier=CQ").json()
    assert cq["total"] == 1 and cq["rows"][0]["modifier_set"] == "CQ"
    byq = client.get("/api/rates?q=5555555555").json()
    assert byq["total"] == 1 and byq["rows"][0]["npi"] == "5555555555"


def test_summary_reacts_to_filters(client):
    s_all = client.get("/api/summary").json()
    s_cq = client.get("/api/summary?modifier=CQ").json()
    assert s_all["n"] == 5 and s_cq["n"] == 1
    assert s_cq["min"] == s_cq["max"] == 29.33


def test_org_and_cpt_views(client):
    org = client.get("/api/org/2222222222").json()
    assert org["rates"] and all(r["payer"] == "Testco" for r in org["rates"])
    assert org["chart"]
    cpt = client.get("/api/cpt/97110").json()
    assert cpt["description"] == "Therapeutic exercises"
    assert cpt["ranked"][0]["median_rate"] >= cpt["ranked"][-1]["median_rate"]
    assert sum(b["n"] for b in cpt["histogram"]) == 5


def test_csv_export_matches_view_exactly(client, store):
    """Spec §8.7: export = current filter state exactly."""
    params = "modifier=base&sort=negotiated_rate&dir=desc"
    view = client.get(f"/api/rates?{params}&page_size=1000").json()
    resp = client.get(f"/api/export.csv?{params}")
    assert resp.status_code == 200
    body = resp.content
    assert body.startswith(b"\xef\xbb\xbf")  # Excel-friendly BOM
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    assert len(rows) == view["total"]
    assert [f"{float(r['negotiated_rate']):.2f}" for r in rows] == [
        f"{v['negotiated_rate']:.2f}" for v in view["rows"]
    ]
    assert [r["npi"] for r in rows] == [v["npi"] for v in view["rows"]]


def test_files_view_surfaces_skips(cfg, store):
    drop(cfg, "innetwork_mixed.json")  # no companion this time
    scan_inbox(cfg, store)
    client = TestClient(create_app(cfg, store))
    files = client.get("/api/files").json()["files"]
    f = next(x for x in files if x["filename"] == "innetwork_mixed.json")
    assert f["ref_groups_skipped"] == 1
    stats = client.get("/api/stats").json()
    assert any(a["ref_groups_skipped"] == 1 for a in stats["attention"])


def test_upload_roundtrip(cfg, store, client):
    payload = (
        b'{"reporting_entity_name": "Testco Health Plans Inc", "version": "2.0.0",'
        b'"last_updated_on": "2026-06-01", "provider_references": [],'
        b'"in_network": [{"billing_code_type": "CPT", "billing_code": "97110",'
        b'"negotiated_rates": [{"provider_groups": [{"npi": [3333333333],'
        b'"tin": {"type": "ein", "value": "333333333"}}], "negotiated_prices":'
        b'[{"negotiated_type": "negotiated", "negotiated_rate": 50.0,'
        b'"billing_class": "professional", "service_code": ["11"]}]}]}]}'
    )
    r = client.post("/api/upload", files={"file": ("uploaded.json", payload, "application/json")})
    assert r.status_code == 200
    # TestClient runs background tasks synchronously after the response
    rows = client.get("/api/rates?q=3333333333").json()
    assert rows["total"] == 1 and rows["rows"][0]["negotiated_rate"] == 50.0


def test_2m_row_store_paginates_under_1s(cfg, store):
    """Spec §9: synthetic 2M-row store, server-side sort+page in <1s."""
    con = duckdb.connect()
    con.execute(
        """
        COPY (
            SELECT 'SynthPayer' || (i % 5) AS payer,
                   'file' || (i % 20) AS source_file,
                   '2.0.0' AS schema_version, '2026-06-01' AS last_updated_on,
                   lpad(CAST(1000000000 + (i % 300000) AS VARCHAR), 10, '0') AS npi,
                   CAST(NULL AS VARCHAR) AS tin,
                   '971' || lpad(CAST(i % 20 AS VARCHAR), 2, '0') AS billing_code,
                   'CPT' AS billing_code_type,
                   CASE WHEN i % 4 = 0 THEN ['CQ'] ELSE [] END AS billing_code_modifier,
                   round(random() * 200, 2) AS negotiated_rate,
                   'negotiated' AS negotiated_type, TRUE AS is_dollar_rate,
                   'professional' AS billing_class, ['11'] AS service_code,
                   '9999-12-31' AS expiration_date, '2026-07-08' AS ingested_at
            FROM generate_series(1, 2000000) t(i)
        ) TO '{path}' (FORMAT PARQUET)
        """.replace("{path}", str(store.rates_dir / "synthetic.parquet"))
    )
    store.rebuild_dedup()  # ingest does this; the fixture bypassed ingest
    client = TestClient(create_app(cfg, store))
    t0 = time.perf_counter()
    r = client.get("/api/rates?sort=negotiated_rate&dir=desc&page=37&page_size=100").json()
    elapsed = time.perf_counter() - t0
    assert r["total"] >= 1_000_000  # dedup collapses some of the 2M
    assert len(r["rows"]) == 100
    assert elapsed < 1.0, f"page took {elapsed:.2f}s"


def test_sources_registry_and_override(cfg, store, tmp_path, monkeypatch):
    cfg.registry_path = tmp_path / "payer_registry.yaml"
    cfg.registry_overrides_path = tmp_path / "registry_overrides.yaml"
    cfg.registry_path.write_text(
        """
national_payers:
  - name: UnitedHealthcare
    mrf_url: https://transparency-in-coverage.uhc.com/
    verified: true
bcbs_by_state:
  MO:
    licensee: [Anthem Blue Cross and Blue Shield (most of MO), Blue Cross and Blue Shield of Kansas City (western)]
    parent: [Elevance, Independent]
    mrf_url: https://www.anthem.com/machine-readable-file/search
    verified: true
  WA:
    licensee: [Premera Blue Cross, Regence BlueShield]
    parent: [Premera, Cambia/Regence]
    mrf_url: null
    verified: false
elevance_master_index_pattern: https://example.com/{YYYY-MM}-01_anthem_index.json.gz
"""
    )
    client = TestClient(create_app(cfg, store))
    mo = client.get("/api/sources?state=MO").json()
    assert len(mo["state"]) == 2  # multi-Blue states show ALL licensees
    assert mo["state"][0]["verified"] is True
    assert mo["state"][1]["verified"] is False  # co-licensee never inherits verification
    assert "master index" in mo["elevance_note"]

    wa = client.get("/api/sources?state=WA").json()["state"]
    assert all(not c["verified"] for c in wa)

    # paste a confirmed URL -> persisted override flips it to verified locally
    key = wa[0]["key"]
    r = client.post("/api/sources/override", json={"key": key, "mrf_url": "https://www.premera.com/mrf"})
    assert r.status_code == 200
    wa2 = client.get("/api/sources?state=WA").json()["state"]
    card = next(c for c in wa2 if c["key"] == key)
    assert card["verified"] is True and card["verified_locally"] is True
    assert cfg.registry_overrides_path.exists()
