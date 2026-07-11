"""API acceptance tests (V4): server-side pagination/sort at TIN grain,
filter/CSV parity, 2M-row performance, sources registry overrides."""

import csv
import io
import time

import duckdb
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


def test_rates_default_grain_is_tin(client):
    r = client.get("/api/rates").json()
    assert r["grain"] == "tin"
    # 4 dollar TIN-grain rows: 34.5 / CQ 29.33 (tin 222...) / 31.07 (111...) / 27.5 (555...)
    assert r["total"] == 4
    rates = [row["negotiated_rate"] for row in r["rows"]]
    assert rates == sorted(rates, reverse=True)
    ref_tin = next(x for x in r["rows"] if x["tin_value"] == "555555555")
    assert ref_tin["npi_count"] == 2  # two NPIs rolled into one entity row


def test_pagination_is_stable_under_tied_sort_keys(cfg, store):
    # Multi-month stores are tie-dense (a TIN's rate is usually unchanged
    # month over month). LIMIT/OFFSET pages are independent queries, so with
    # an incomplete tiebreaker DuckDB ordered tie-groups differently per page:
    # some rows appeared twice and others NEVER appeared, silently. The
    # tiebreaker must be a total order over the row identity.
    from mrfx.store import RATES_SCHEMA  # noqa: F401 — documents the row shape

    rows = []
    for i in range(60):
        for month in ("2026-05-01", "2026-06-01"):
            rows.append({
                "payer": "TiePayer", "source_file": f"tie_{month}.json",
                "file_month": month[:7], "schema_version": "1.0.0",
                "last_updated_on": month, "tin_value": f"6{i:08d}",
                "tin_type": "ein", "tin_is_really_npi": False,
                "npi": f"15{i:08d}", "billing_code": "97110",
                "billing_code_type": "CPT", "discipline": "PT",
                "is_timed": True, "billing_code_modifier": ["GP"],
                "negotiated_rate": 55.0,  # every row ties on the sort key
                "negotiated_type": "negotiated", "is_dollar_rate": True,
                "billing_class": "professional", "service_code": ["11"],
                "expiration_date": "2027-01-01", "ingested_at": "2026-07-11",
            })
    for month in ("2026-05-01", "2026-06-01"):
        with store.rates_part_writer(f"tie_{month}.json") as w:
            w.write_batch([r for r in rows if r["source_file"] == f"tie_{month}.json"])
    store.upsert_file("tie_2026-05-01.json", payer="TiePayer", file_type="in_network", status="done")
    store.upsert_file("tie_2026-06-01.json", payer="TiePayer", file_type="in_network", status="done")
    store.rebuild_rollups()
    client = TestClient(create_app(cfg, store))

    first = client.get("/api/rates?grain=tin&page_size=7&page=1").json()
    total = first["total"]
    assert total >= 120
    seen = []
    page = 1
    while len(seen) < total:
        r = client.get(f"/api/rates?grain=tin&page_size=7&page={page}").json()
        if not r["rows"]:
            break
        seen += [(row["unit_id"], row["file_month"], row["payer"]) for row in r["rows"]]
        page += 1
    assert len(seen) == total
    assert len(set(seen)) == total, "pages duplicated some rows and dropped others"


def test_stats_reports_enrichment_progress(client, store):
    s = client.get("/api/stats").json()
    assert "enrichment" in s and s["enrichment"]["total"] >= 1
    assert s["enrichment"]["named"] == 0        # nothing enriched yet
    assert s["enrichment_mode"] in ("api", "bulk", "off")
    # states endpoint is empty until enrichment writes a state, then reflects it
    assert client.get("/api/states").json()["states"] == []
    for npi in store.unenriched_npis():
        store.save_npi(npi, "Clinic LLC", "225100000X", "PT", "KC", "MO")
    store.rebuild_rollups()
    s2 = client.get("/api/stats").json()
    assert s2["enrichment"]["named"] == s2["enrichment"]["total"]
    assert client.get("/api/states").json()["states"] == ["MO"]


def test_payer_filter_survives_commas_in_payer_name(cfg, store):
    # Real reporting_entity_name values contain commas ("... , a Division of
    # HCSC"). The old comma-joined ?payer= split one name into two and matched
    # nothing. Repeated ?payer= params must filter to exactly that payer.
    import json as _json

    from mrfx.ingest import ingest_file

    payer = "Blue Cross and Blue Shield of Illinois, a Division of HCSC"
    doc = {
        "reporting_entity_name": payer, "reporting_entity_type": "health plan",
        "last_updated_on": "2026-06-01", "version": "1.0.0",
        "in_network": [{
            "negotiation_arrangement": "ffs", "name": "PT", "billing_code_type": "CPT",
            "billing_code_type_version": "2026", "billing_code": "97110",
            "negotiated_rates": [{
                "provider_groups": [{"npi": [1234567893], "tin": {"type": "ein", "value": "123456789"}}],
                "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 42.0,
                                       "expiration_date": "2027-01-01", "service_code": ["11"],
                                       "billing_class": "professional"}],
            }],
        }],
    }
    p = cfg.inbox_dir / "bcbsil.json"
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(doc))
    ingest_file(cfg, store, p)
    client = TestClient(create_app(cfg, store))

    # the payer name comes straight from /api/payers, exactly as stored
    payers = client.get("/api/payers").json()["payers"]
    assert payer in payers
    # repeated param with the comma-bearing name filters to it (not zero rows)
    r = client.get("/api/rates", params=[("payer", payer)]).json()
    assert r["total"] >= 1 and all(row["payer"] == payer for row in r["rows"])
    # and a genuinely absent payer still returns nothing
    assert client.get("/api/rates", params=[("payer", "No Such Payer")]).json()["total"] == 0


def test_pagination(client):
    r1 = client.get("/api/rates?page_size=2&page=1").json()
    r2 = client.get("/api/rates?page_size=2&page=2").json()
    assert len(r1["rows"]) == 2 and len(r2["rows"]) == 2
    assert r1["rows"] != r2["rows"]


def test_dollar_only_toggle(client):
    all_rows = client.get("/api/rates?dollar_only=0").json()
    assert all_rows["total"] == 5  # + the percentage row
    pct = [x for x in all_rows["rows"] if x["negotiated_type"] == "percentage"]
    assert pct and pct[0]["is_dollar_rate"] is False


def test_modifier_and_search_filters(client):
    base = client.get("/api/rates?modifier=base").json()
    assert all(x["modifier_set"] in ("", "GP", "GO", "GN") for x in base["rows"])
    cq = client.get("/api/rates?modifier=CQ").json()
    assert cq["total"] == 1 and cq["rows"][0]["modifier_set"] == "CQ"
    # custom membership: CQ absent
    no_cq = client.get("/api/rates?mod_not=CQ").json()
    assert cq["total"] + no_cq["total"] == 4
    byq = client.get("/api/rates?q=555555555").json()
    assert byq["total"] == 1


def test_summary_counts_entities_and_codes(client):
    s = client.get("/api/summary").json()
    assert s["n"] == 4 and s["entities"] == 3 and s["codes"] == 1
    s_cq = client.get("/api/summary?modifier=CQ").json()
    assert s_cq["n"] == 1 and s_cq["min"] == s_cq["max"] == 29.33


def test_npi_grain_toggle(client):
    r = client.get("/api/rates?grain=npi").json()
    assert r["grain"] == "npi"
    assert r["total"] == 5  # 555555555 splits into two NPI rows
    npis = {x["unit_id"] for x in r["rows"]}
    assert {"5555555555", "5555555556"} <= npis


def test_entity_detail_and_code_views(client):
    d = client.get("/api/entity/tin/555555555").json()
    assert d["display_name"].startswith("TIN 555555555")  # unenriched yet
    assert {n["npi"] for n in d["npis"]} == {"5555555555", "5555555556"}
    c = client.get("/api/code/97110").json()
    assert c["description"] == "Therapeutic exercises"
    assert c["timed"] is True
    assert c["ranked"][0]["median_rate"] >= c["ranked"][-1]["median_rate"]
    assert sum(b["n"] for b in c["histogram"]) == 4


def test_csv_export_matches_view_exactly(client):
    params = "modifier=base&sort=negotiated_rate&dir=desc"
    view = client.get(f"/api/rates?{params}&page_size=1000").json()
    resp = client.get(f"/api/export.csv?{params}")
    assert resp.status_code == 200
    body = resp.content
    assert body.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    assert len(rows) == view["total"]
    assert [f"{float(r['negotiated_rate']):.2f}" for r in rows] == [
        f"{v['negotiated_rate']:.2f}" for v in view["rows"]
    ]
    # provenance columns present on every row (§7A.6)
    for col in ("payer", "source_files", "file_month", "last_updated_on", "schema_version"):
        assert col in rows[0]


def test_export_zip_has_methodology_sidecar(client):
    import zipfile

    resp = client.get("/api/export.zip?modifier=base")
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    names = z.namelist()
    assert any(n.endswith("_methodology.txt") for n in names)
    method = z.read([n for n in names if n.endswith(".txt")][0]).decode()
    assert "Filters" in method and "ghost rates" in method
    assert "Rate files in the store at export time" in method  # honest source list


def test_files_view_surfaces_skips_and_qa(cfg, store):
    drop(cfg, "innetwork_mixed.json")  # no companion this time
    scan_inbox(cfg, store)
    client = TestClient(create_app(cfg, store))
    files = client.get("/api/files").json()["files"]
    f = next(x for x in files if x["filename"] == "innetwork_mixed.json")
    assert f["ref_groups_skipped"] == 1
    assert f["qa"]["non_dollar_rows"] == 1  # percentage row
    assert "outlier_rule" in f["qa"]


def test_trend_and_months(client):
    months = client.get("/api/months").json()["months"]
    assert months == ["2026-06"]
    t = client.get("/api/trend?cpt=97110").json()["rows"]
    assert t and t[0]["file_month"] == "2026-06"


def test_validation_crosscheck(client):
    r = client.post("/api/validate", json={"id": "55-5555555", "code": "97110",
                                           "expected_rate": 27.50}).json()
    assert r["match"] is True
    assert any(row["delta_vs_expected"] == 0.0 for row in r["rows"])
    r2 = client.post("/api/validate", json={"id": "555555555", "code": "97110",
                                            "expected_rate": 30.0}).json()
    assert r2["match"] is False


def test_2m_row_store_paginates_under_1s(cfg, store):
    con = duckdb.connect()
    con.execute(
        """
        COPY (
            SELECT 'SynthPayer' || (i % 5) AS payer,
                   'file' || (i % 20) AS source_file,
                   '2026-06' AS file_month,
                   '2.0.0' AS schema_version, '2026-06-01' AS last_updated_on,
                   lpad(CAST(100000000 + (i % 150000) AS VARCHAR), 9, '0') AS tin_value,
                   'ein' AS tin_type, FALSE AS tin_is_really_npi,
                   lpad(CAST(1000000000 + (i % 300000) AS VARCHAR), 10, '0') AS npi,
                   '971' || lpad(CAST(i % 20 AS VARCHAR), 2, '0') AS billing_code,
                   'CPT' AS billing_code_type, 'PT' AS discipline, TRUE AS is_timed,
                   CASE WHEN i % 4 = 0 THEN ['CQ'] ELSE [] END AS billing_code_modifier,
                   round(random() * 200, 2) AS negotiated_rate,
                   'negotiated' AS negotiated_type, TRUE AS is_dollar_rate,
                   'professional' AS billing_class, ['11'] AS service_code,
                   '9999-12-31' AS expiration_date, '2026-07-08' AS ingested_at
            FROM generate_series(1, 2000000) t(i)
        ) TO '{path}' (FORMAT PARQUET)
        """.replace("{path}", str(store.rates_dir / "synthetic.parquet"))
    )
    store.rebuild_rollups()
    client = TestClient(create_app(cfg, store))
    t0 = time.perf_counter()
    r = client.get("/api/rates?sort=negotiated_rate&dir=desc&page=37&page_size=100").json()
    elapsed = time.perf_counter() - t0
    assert r["total"] >= 100_000  # TIN-grain rollup collapses the 2M raw rows
    assert len(r["rows"]) == 100
    assert elapsed < 1.0, f"page took {elapsed:.2f}s"


def test_sources_registry_and_override(cfg, store, tmp_path):
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
    assert len(mo["state"]) == 2
    assert mo["state"][0]["verified"] is True
    assert mo["state"][1]["verified"] is False
    assert "master index" in mo["elevance_note"]

    wa = client.get("/api/sources?state=WA").json()["state"]
    key = wa[0]["key"]
    r = client.post("/api/sources/override", json={"key": key, "mrf_url": "https://www.premera.com/mrf"})
    assert r.status_code == 200
    wa2 = client.get("/api/sources?state=WA").json()["state"]
    card = next(c for c in wa2 if c["key"] == key)
    assert card["verified"] is True and card["verified_locally"] is True
    assert cfg.registry_overrides_path.exists()
