"""Outreach export: one row per entity with org name + geography + per-code
percentile merge fields, Brevo/Excel-friendly."""

import csv
import io

import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.ingest import ingest_file
from tests.mrfx.conftest import make_fixture
from tests.mrfx.test_v4 import innetwork, item


@pytest.fixture
def outreach_store(cfg, store):
    """5 entities with a known 97110 distribution ($30..$34) + NPPES geography."""
    groups = [([f"1{i:09d}"], f"43-00000{i:02d}", "ein", [(30.0 + i, ["GP"])]) for i in range(5)]
    data = innetwork(items=[item("97110", groups), item("97140", groups[:2])])
    p = make_fixture(cfg.inbox_dir, "outreach.json", data)
    ingest_file(cfg, store, p)
    names = ["Alpha PT", "Bravo Rehab", "Charlie Therapy", "Delta Motion", "Echo Hand Center"]
    cities = ["Saint Louis", "Springfield", "Columbia", "Branson", "Fenton"]
    for i in range(5):
        store.save_npi(
            f"1{i:09d}", names[i], "261QP2000X", "Clinic/Center, Physical Therapy",
            cities[i], "MO", entity_type="NPI-2",
            address=f"{100 + i} Main St", zip_code=f"6310{i}-1234", phone=f"314-555-000{i}",
        )
    store.rebuild_rollups()
    return store


def fetch_csv(client, params=""):
    resp = client.get(f"/api/export/outreach.csv?{params}")
    assert resp.status_code == 200
    body = resp.content
    assert body.startswith(b"\xef\xbb\xbf")  # BOM for Excel/Brevo
    return list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))


def test_one_row_per_entity_with_org_and_geography(cfg, outreach_store):
    client = TestClient(create_app(cfg, outreach_store))
    rows = fetch_csv(client)
    assert len(rows) == 5
    alpha = next(r for r in rows if r["ORG_NAME"] == "Alpha PT")
    assert alpha["CITY"] == "Saint Louis" and alpha["STATE"] == "MO"
    assert alpha["ZIP"] == "63100"                 # zip5 normalization
    assert alpha["ADDRESS"] == "100 Main St"
    assert alpha["PHONE"] == "314-555-0000"
    assert alpha["WEBSITE"] == ""                  # never sourced, present for merge templates
    assert alpha["PRIMARY_DISCIPLINE"] == "PT"
    assert alpha["PAYERS"] == "Testco"
    assert alpha["MONTHS"] == "2026-06"


def test_per_code_merge_fields_and_percentiles(cfg, outreach_store):
    client = TestClient(create_app(cfg, outreach_store))
    rows = fetch_csv(client, "cpt=97110")
    alpha = next(r for r in rows if r["ORG_NAME"] == "Alpha PT")   # $30 = lowest of 5
    echo = next(r for r in rows if r["ORG_NAME"] == "Echo Hand Center")  # $34 = highest
    assert float(alpha["C97110_RATE"]) == 30.0
    assert float(alpha["C97110_MKT_MEDIAN"]) == 32.0
    assert int(alpha["C97110_PCTL"]) == 20        # 1 of 5 at-or-below
    assert float(alpha["C97110_GAP_TO_MEDIAN"]) == 2.0
    assert int(echo["C97110_PCTL"]) == 100
    # unrequested codes are absent when an explicit list is passed
    assert "C97140_RATE" not in alpha


def test_geography_filter_narrows_export(cfg, outreach_store):
    client = TestClient(create_app(cfg, outreach_store))
    rows = fetch_csv(client, "city=Springfield")
    assert [r["ORG_NAME"] for r in rows] == ["Bravo Rehab"]


def test_entity_map_grain_used_when_present(cfg, outreach_store):
    import yaml

    cfg.entity_map_path.write_text(yaml.safe_dump(
        {"entities": [{"name": "AlphaBravo Group", "tins": ["430000000", "430000001"]}]}
    ))
    client = TestClient(create_app(cfg, outreach_store))
    rows = fetch_csv(client, "grain=entity")
    names = {r["ORG_NAME"] for r in rows}
    assert "AlphaBravo Group" in names
    grouped = next(r for r in rows if r["ORG_NAME"] == "AlphaBravo Group")
    assert len(grouped["TINS"].split(";")) == 2
    assert len(rows) == 4  # 5 TINs -> 4 entities


def test_cli_outreach_writes_csv_and_methodology(cfg, outreach_store, tmp_path, monkeypatch):
    from mrfx import cli

    out = tmp_path / "contacts.csv"
    args = type("A", (), {
        "out": str(out), "payer": None, "cpt": "97110", "state": None, "city": None,
        "month": None, "discipline": None, "grain": None, "base_only": True,
    })
    monkeypatch.setattr(cli, "Store", lambda *_: outreach_store)
    assert cli.cmd_outreach(cfg, args) == 0
    rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8-sig"))))
    assert len(rows) == 5 and "C97110_PCTL" in rows[0]
    assert (tmp_path / "contacts_methodology.txt").exists()
