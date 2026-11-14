"""0.8.6 audit — regressions for the defects an independent review and a
national-scale run found in the practice names / types / Negotiate work.
Each test fails on c7299d8 (before the fixes)."""
from __future__ import annotations

import threading

import pytest

from mrfx.benchmark import BenchmarkError, compute_benchmark, resolve_subject_tins
from mrfx.store import FINISHED_NOW, Store

RATE = dict(billing_code="97110", billing_code_type="CPT", discipline="PT", is_timed=True,
            billing_class="professional", negotiated_type="negotiated", is_dollar_rate=True,
            modifier_set=["GP"], service_code=["11"], file_month="2026-06",
            last_updated_on="2026-06-01", expiration_date=None, schema_version="2.0.0",
            tin_is_really_npi=False, payer="BCBS TN")
PT, MD, ORTHO = "225100000X", "207Q00000X", "207X00000X"


def _add(store: Store, fname: str, tin: str, npis: list[str], rate: float):
    recs = [dict(RATE, tin_value=tin, tin_type="ein", npi=n, negotiated_rate=rate,
                 source_file=fname, state="TN") for n in npis]
    with store.rates_part_writer(fname) as w:
        w.write_batch(recs)
    store.upsert_file(fname, payer="BCBS TN", file_type="in_network", status="done",
                      rows_emitted=len(recs), finished_at=FINISHED_NOW)


def _npi(store, npi, tax, etype="NPI-1", name="X", city="NASHVILLE"):
    store.save_npi(npi, name, tax, None, city, "TN", entity_type=etype,
                   address=f"{npi[-3:]} MAIN ST", zip_code="37203", taxonomy_codes=tax)


def test_unnamed_groups_are_never_pooled_by_their_label(store):
    """Two unrelated practices, 2 PTs each in NASHVILLE, no org name: both
    read "Unnamed group — 2 clinicians (NASHVILLE)". The label is not a
    name: the picker never offers it as one multi-TIN practice, and typing
    it asks which tax ID instead of benchmarking the two together ($45).
    was: one picker entity; resolve_subject_tins returned both TINs."""
    for n in ("1000000001", "1000000002", "2000000001", "2000000002", "3000000001"):
        _npi(store, n, PT)
    _add(store, "a.json", "111111111", ["1000000001", "1000000002"], 30.0)
    _add(store, "b.json", "222222222", ["2000000001", "2000000002"], 60.0)
    _add(store, "c.json", "333333333", ["3000000001"], 50.0)
    store.rebuild_rollups()
    assert store.refresh_practice_names() is True
    label = "Unnamed group — 2 clinicians (NASHVILLE)"
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM tin_directory WHERE display_name = ?",
                           [label]).fetchone()[0] == 2
    with pytest.raises(BenchmarkError, match="2 different practices are labelled"):
        resolve_subject_tins(store, label)
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from mrfx.config import MrfxConfig
    client = TestClient(create_app(MrfxConfig(store_dir=store.dir, cms_dac="off",
                                              cms_reference="off"), store),
                        base_url="http://localhost")
    assert label not in client.get("/api/benchmark/subjects").json()["entities"]


def test_a_clinic_folded_in_after_its_pairs_were_classified_is_reclassified(store):
    """serve classifies as soon as a new part changes the pairs — often
    before the analytics fold-in adds the TIN to the directory, so it is
    'unknown' then. The fold-in changes the directory; the next refresh
    must see that. was: the stamp ignored the directory, the clinic stayed
    'unknown' (out of the default comparison group) until a forced run."""
    for n in ("1000000001", "1000000002", "2000000001", "2000000002"):
        _npi(store, n, PT)
    _add(store, "a.json", "111111111", ["1000000001", "1000000002"], 40.0)
    store.rebuild_rollups()
    assert store.refresh_practice_names() is True
    _add(store, "b.json", "222222222", ["2000000001", "2000000002"], 40.0)
    assert store.refresh_practice_names() is True          # before the fold-in
    store.update_rollups_incremental()
    assert store.refresh_practice_names() is True          # the directory moved
    with store.connect() as con:
        assert con.execute("SELECT practice_type FROM tin_directory WHERE tin_value = "
                           "'222222222'").fetchone()[0] == "outpatient_clinic"


def test_comparison_groups_work_before_the_first_analytics_build(store):
    """A fresh store (or one just reset) has no materialised directory; every
    screen sends peer_group 'clinics' by default. was: BinderException
    ("td" has no column practice_type) -> HTTP 500."""
    _npi(store, "1000000001", PT)
    _npi(store, "2000000001", PT)
    _add(store, "a.json", "111111111", ["1000000001"], 40.0)
    _add(store, "b.json", "222222222", ["2000000001"], 44.0)
    with store.connect() as con:
        assert not con.execute("SELECT count(*) FROM information_schema.tables "
                               "WHERE table_name = 'tin_directory_tbl'").fetchone()[0]
        cols = {r[0] for r in con.execute("DESCRIBE tin_directory").fetchall()}
    assert {"practice_type", "org_clinic", "org_taxonomies", "name_source"} <= cols
    for g in ("clinics", "org", "therapy"):
        compute_benchmark(store, "111111111", {"month": "latest", "peer_group": g})


def test_street_names_are_not_cut_at_a_unit_word():
    """'1500 FLORIDA AVE' kept FLORIDA's 'FL' as a floor and became
    '37203|1500' — the same key as '1500 FLOYD ST' or a CMS hospital at
    number 1500 in the ZIP (wrongly typing a clinic 'hospital')."""
    import duckdb

    from mrfx.cmsref import addr_key_sql
    con = duckdb.connect()
    con.execute("CREATE TABLE a(addr VARCHAR)")
    cases = {"1500 FLORIDA AVE": "37203|1500 FLORIDA AVE", "1500 FLOYD ST": "37203|1500 FLOYD ST",
             "200 STERLING RD": "37203|200 STERLING RD", "10 UNITED WAY": "37203|10 UNITED WAY",
             "100 Hospital Dr. Suite 3": "37203|100 HOSPITAL DR",
             "5 Care Center Road STE 1": "37203|5 CARE CTR RD", "12 MAIN ST #4": "37203|12 MAIN ST",
             "12 Main St, Fl 2": "37203|12 MAIN ST", "9 Main St Ste. 200": "37203|9 MAIN ST"}
    con.executemany("INSERT INTO a VALUES (?)", [[k] for k in cases])
    got = dict(con.execute(f"SELECT addr, {addr_key_sql('addr', chr(39) + '37203' + chr(39))} "
                           "FROM a").fetchall())
    assert got == cases


def test_concurrent_refreshes_do_not_collide(store):
    """serve's part-sets, CMS DAC and CMS reference threads all refresh the
    names; a first run takes minutes on a big store. was: shared temp files
    (keyed on the pid) — all but one thread failed, and one could swap in
    another's rows under its own stamp."""
    for i in range(6):
        _npi(store, f"10000000{i:02d}", PT)
    _add(store, "a.json", "111111111", [f"10000000{i:02d}" for i in range(6)], 40.0)
    store.rebuild_rollups()
    results, errors = [], []

    def run():
        try:
            results.append(store.refresh_practice_names(force=True))
        except Exception as e:  # pragma: no cover
            errors.append(e)
    ts = [threading.Thread(target=run) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == [] and results == [True, True, True]
    assert not list(store.dir.glob("practice_names.*.tmp*"))
    with store.connect() as con:
        assert con.execute("SELECT value FROM meta WHERE key = 'practice_names_stamp'"
                           ).fetchone()[0] == store._names_stamp()


def test_slices_give_the_same_rows_as_one_pass(store):
    """A national store is classified in hash(tin) slices (a single pass
    needed 6.5 GB of temp disk at 15M pairs). Every aggregate is per TIN,
    so 1 slice and many slices must produce identical tables."""
    for t in range(12):
        npis = [f"{t + 10}000000{i:02d}" for i in range(3)]
        for j, n in enumerate(npis):
            _npi(store, n, PT if (t + j) % 2 else MD, city=("MEMPHIS", "NASHVILLE", "PARIS")[t % 3])
        _add(store, f"f{t}.json", f"{t + 1:09d}", npis, 30.0 + t)
    store.rebuild_rollups()
    store.refresh_practice_names(force=True)
    with store.connect() as con:
        one = con.execute("SELECT * FROM practice_names_tbl ORDER BY tin_value").fetchall()
    store.NAMES_SLICE_PAIRS = 5                       # 36 pairs -> 8 slices
    store.refresh_practice_names(force=True)
    with store.connect() as con:
        many = con.execute("SELECT * FROM practice_names_tbl ORDER BY tin_value").fetchall()
    assert len(one) == 12 and one == many


def test_a_physician_org_npi_listing_a_therapy_code_is_not_a_clinic(store):
    """An orthopedic group's org NPI registered as 207X00000X AND 225100000X
    (its therapy department) with physicians on the roster: not a therapy
    clinic, not in the "organization NPI taxonomy" group. A clinic's org NPI
    registered only as 261QP2000X still is. was: any therapy code on an org
    NPI made the practice a clinic, ahead of every other test."""
    _npi(store, "1000000001", f"{ORTHO}|{PT}", etype="NPI-2", name="BONE GROUP PC")
    _npi(store, "1000000002", ORTHO)
    _npi(store, "1000000003", ORTHO)
    _npi(store, "1000000004", PT)
    _npi(store, "2000000001", "261QP2000X", etype="NPI-2", name="RIVER PT LLC")
    _npi(store, "2000000002", PT)
    _add(store, "a.json", "111111111", ["1000000001", "1000000002", "1000000003", "1000000004"], 60.0)
    _add(store, "b.json", "222222222", ["2000000001", "2000000002"], 40.0)
    store.rebuild_rollups()
    store.refresh_practice_names()
    with store.connect() as con:
        got = dict(con.execute("SELECT tin_value, (practice_type, org_clinic) FROM tin_directory"
                               ).fetchall())
    assert got["111111111"] == ("physician_other", False)
    assert got["222222222"] == ("outpatient_clinic", True)


def test_a_failed_cms_fetch_is_retried_the_next_day(store, tmp_path, monkeypatch):
    """ensure_cmsref recorded its monthly check even when both sources
    failed, so a first-start outage meant no hospital / nursing-home /
    Medicare evidence for 30 days."""
    import mrfx.cmsref as cr

    def boom(*a, **k):
        raise RuntimeError("HTTP 503")
    monkeypatch.setattr(cr, "fetch_facilities", boom)
    monkeypatch.setattr(cr, "fetch_medicare_therapy", boom)
    got = cr.ensure_cmsref(store, tmp_path)
    assert got["facilities"].startswith("not loaded") and got["medicare"].startswith("not loaded")
    with store.connect() as con:
        assert store._meta_get(con, "cmsref_checked_at") is None


def test_medicare_rows_without_provider_type_claim_nothing(store):
    """The newest Medicare year imported without provider types (another
    year has them): the reason must not say '0 of 2 … bill as therapists in
    private practice' — that is unknown, not zero; the year WITH provider
    types is the evidence."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from mrfx.utilization import _register_view
    _npi(store, "1000000001", None)
    _npi(store, "1000000002", None)
    _add(store, "a.json", "111111111", ["1000000001", "1000000002"], 40.0)
    d = store.dir / "utilization"
    d.mkdir(exist_ok=True)
    base = {"npi": ["1000000001", "1000000002"], "billing_code": ["97110"] * 2,
            "services": [100.0, 100.0], "beneficiaries": [20, 20], "avg_allowed": [25.0, 25.0],
            "place_of_service": ["O", "O"], "state": ["TN", "TN"], "zip5": ["37203", "37203"]}
    pq.write_table(pa.table({**base, "data_year": ["2023"] * 2, "entity_code": ["I"] * 2,
                             "provider_type": ["Physical Therapist in Private Practice"] * 2}),
                   d / "2023.parquet")
    pq.write_table(pa.table({**base, "data_year": ["2024"] * 2}), d / "2024.parquet")
    with store.write_lock, store.connect() as con:
        _register_view(con, store)
    store.rebuild_rollups()
    store.refresh_practice_names()
    with store.connect() as con:
        kind, why = con.execute("SELECT practice_type, type_reason FROM tin_directory "
                                "WHERE tin_value = '111111111'").fetchone()
    assert "0 of 2" not in why
    assert "2 of 2 clinicians billing therapy to Medicare bill as therapists" in why
    assert kind == "outpatient_clinic"


def test_negotiate_panel_ignores_a_superseded_answer():
    """Compare for payer A then payer B: A's slower answer must not land
    under B's results; and a list cut at the screen's limit says so."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / "mrfx" / "web" / "app.js").read_text(encoding="utf-8")
    body = js[js.index("async function loadPaidMore("):js.index("async function runNegotiate()")]
    assert "if (seq !== state.paidMoreSeq) return;" in body
    assert "Download CSV for every one" in body
