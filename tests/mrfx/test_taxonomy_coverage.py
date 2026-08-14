"""Every therapy-filtered search must count a therapy taxonomy in ANY NPPES
slot — not just the primary one.

The bug this pins was measured on the live federal registry: 9% of real therapy
providers carry their therapy code in a SECONDARY slot (including a clinic
named "APEX PHYSICAL THERAPY, LLC" whose primary is the generic 174400000X
'Specialist'). Fixing the classifier is only half the job — the fix has to
reach every query that filters on it, so this drives them ALL against a store
whose therapy practice is only identifiable from a secondary slot.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app

# primary is the GENERIC 'Specialist' code; the therapy code sits second —
# exactly the real APEX PHYSICAL THERAPY, LLC row from the live registry
SECONDARY_ONLY = {"taxonomy_code": "174400000X",
                  "taxonomy_codes": "261QM1300X|261QP2000X|261QX0100X|174400000X"}
PLAIN_THERAPY = {"taxonomy_code": "261QP2000X",
                 "taxonomy_codes": "261QP2000X"}
NOT_THERAPY = {"taxonomy_code": "207X00000X",
               "taxonomy_codes": "207X00000X|208100000X"}

SECOND_TIN, SECOND_NPI = "431234567", "1417594896"
PLAIN_TIN, PLAIN_NPI = "437654321", "1999999992"
OTHER_TIN, OTHER_NPI = "434440001", "1876543219"


def _row(**kw):
    base = dict(payer="Aetna", tin_value=SECOND_TIN, tin_type="ein", npi=SECOND_NPI,
                source_file="s.json", billing_code="97110", billing_code_type="CPT",
                discipline="PT", is_timed=True, billing_class="professional",
                negotiated_rate=30.0, negotiated_type="negotiated",
                is_dollar_rate=True, billing_code_modifier=[], service_code=["11"],
                file_month="2026-06", last_updated_on="2026-06-01",
                expiration_date=None, schema_version="2.0.0",
                tin_is_really_npi=False, state="MO")
    base.update(kw)
    return base


@pytest.fixture
def seeded(store):
    rows = []
    for tin, npi, rate in ((SECOND_TIN, SECOND_NPI, 30.0),
                           (PLAIN_TIN, PLAIN_NPI, 34.0),
                           (OTHER_TIN, OTHER_NPI, 38.0)):
        for payer in ("Aetna", "BCBS"):
            for code in ("97110", "97140"):
                rows.append(_row(tin_value=tin, npi=npi, payer=payer,
                                 billing_code=code, negotiated_rate=rate))
    with store.rates_part_writer("s.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk([
        dict(npi=SECOND_NPI, org_name="Apex Physical Therapy LLC", entity_type="NPI-2",
             city="Saint Louis", state="MO", address="1 Main", zip="63103",
             phone=None, **SECONDARY_ONLY),
        dict(npi=PLAIN_NPI, org_name="Riverbend Rehab", entity_type="NPI-2",
             city="Saint Louis", state="MO", address="2 Oak", zip="63104",
             phone=None, **PLAIN_THERAPY),
        dict(npi=OTHER_NPI, org_name="Ortho Surgery Group", entity_type="NPI-2",
             city="Saint Louis", state="MO", address="3 Elm", zip="63103",
             phone=None, **NOT_THERAPY),
    ])
    store.rebuild_rollups()
    return store


def test_the_directory_flags_it_as_a_therapy_practice(seeded):
    """tin_directory.is_therapy is MATERIALIZED and feeds most therapy filters,
    so if it misses here, everything downstream misses."""
    with seeded.connect() as con:
        flags = dict(con.execute(
            "SELECT tin_value, is_therapy FROM tin_directory").fetchall())
    assert flags[SECOND_TIN] is True, "secondary-slot therapy practice"
    assert flags[PLAIN_TIN] is True
    assert flags[OTHER_TIN] is False, "an ortho surgery group is still not therapy"


def test_every_therapy_filtered_search_includes_it(cfg, seeded):
    """Drive each therapy_only search path and require the secondary-slot
    practice to appear in all of them."""
    client = TestClient(create_app(cfg, seeded))

    # 1. the rates table's "therapy practices only" filter
    r = client.get("/api/rates?grain=tin&therapy_only=true&limit=100").json()
    names = {x.get("display_name") for x in r["rows"]}
    assert "Apex Physical Therapy LLC" in names, names
    assert "Ortho Surgery Group" not in names

    # 2. benchmark peer market (therapy_only in the market basis)
    r = client.post("/api/benchmark/market", json={
        "subject": PLAIN_TIN,
        "market": {"month": "latest", "therapy_only": True}}).json()
    peers = sum(x.get("n_peers") or 0 for x in r["rows"])
    assert peers, "the secondary-slot practice must be available as a peer"

    # 3. leads (underpaid practices) — therapy_only on by default
    r = client.post("/api/leads", json={"market": {"month": "latest"},
                                        "min_codes": 1, "limit": 50}).json()
    assert any(x.get("display_name") == "Apex Physical Therapy LLC"
               for x in r["leads"]), [x.get("display_name") for x in r["leads"]]

    # 4. market leaderboard
    r = client.post("/api/leaderboard", json={
        "market": {"month": "latest", "therapy_only": True},
        "min_codes": 1, "limit": 50}).json()
    assert any(p.get("display_name") == "Apex Physical Therapy LLC"
               for p in r.get("practices", []))

    # 5. market sizing counts it as a practice in the radius
    r = client.post("/api/market/sizing", json={"zip": "63103",
                                                "radius_miles": 25}).json()
    assert r["practices"] >= 2, r

    # 6. new-to-network's therapy filter (no second month here: the honest
    #    reason path must still not be an error)
    r = client.post("/api/contracts/new-to-network",
                    json={"market": {"month": "latest"}, "therapy_only": True})
    assert r.status_code == 200

    # 7. payer roster, therapy-only
    r = client.post("/api/roster", json={"payer": "Aetna", "therapy_only": True,
                                         "min_codes": 1, "limit": 50}).json()
    assert any(x.get("display_name") == "Apex Physical Therapy LLC"
               for x in r["organizations"]), [x.get("display_name") for x in r["organizations"]]


def test_new_clinic_feed_counts_a_secondary_slot(cfg, seeded, tmp_path):
    """The NPPES feed reads the bulk cache directly rather than the directory,
    so it needs its own proof."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from mrfx.enrich import _NPPES_CACHE_COLS
    from mrfx.nppes import new_enumerations

    recent = (dt.date.today() - dt.timedelta(days=20)).strftime("%m/%d/%Y")
    rows = [{"npi": "1730153216", "org_name": "Brand New Apex PT",
             "entity_type": "NPI-2", "city": "Saint Louis", "state": "MO",
             "zip": "63103", "address": "9 New St", "phone": None,
             "enumeration_date": recent, **SECONDARY_ONLY},
            {"npi": "1053308064", "org_name": "New Cardiology",
             "entity_type": "NPI-2", "city": "Saint Louis", "state": "MO",
             "zip": "63103", "address": "7 Heart St", "phone": None,
             "enumeration_date": recent, **NOT_THERAPY}]
    pq.write_table(
        pa.table({c: [r.get(c) for r in rows] for c in _NPPES_CACHE_COLS},
                 schema=pa.schema([(c, pa.string()) for c in _NPPES_CACHE_COLS])),
        str(seeded.dir / "nppes_cache.parquet"))

    got = {r["org_name"] for r in new_enumerations(seeded, days=90)["rows"]}
    assert "Brand New Apex PT" in got, got
    assert "New Cardiology" not in got


def test_an_older_cache_without_the_column_still_works(seeded, tmp_path):
    """A cache built before the column existed must degrade to primary-only —
    the OLD behaviour — never a binder error that blanks the feed."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from mrfx.nppes import new_enumerations

    old_cols = ("npi", "org_name", "entity_type", "taxonomy_code", "city",
                "state", "address", "zip", "phone", "enumeration_date")
    recent = (dt.date.today() - dt.timedelta(days=20)).strftime("%m/%d/%Y")
    rows = [{"npi": "1730153216", "org_name": "Plain PT Clinic",
             "entity_type": "NPI-2", "taxonomy_code": "261QP2000X",
             "city": "StL", "state": "MO", "address": "9 New St",
             "zip": "63103", "phone": None, "enumeration_date": recent}]
    pq.write_table(
        pa.table({c: [r.get(c) for r in rows] for c in old_cols},
                 schema=pa.schema([(c, pa.string()) for c in old_cols])),
        str(seeded.dir / "nppes_cache.parquet"))

    got = {r["org_name"] for r in new_enumerations(seeded, days=90)["rows"]}
    assert got == {"Plain PT Clinic"}
