"""The two CMS layers MRFs cannot supply: order/refer eligibility, and
shared-patient (referral-structure) pairs."""

import time

import pytest

from mrfx.medicare import (MedicareImportError, import_orf_roster,
                           import_shared_patients, medicare_status,
                           npi_eligibility, org_referrals)

MINE = ["1417594896", "1234567893"]
DOCS = ["1901234561", "1811223340", "1722334459"]


def _seed_rates(store):
    rows = [dict(
        payer=p, tin_value="431234567", tin_type="ein", npi=npi, source_file="m.json",
        billing_code=c, billing_code_type="CPT", discipline="PT", is_timed=True,
        billing_class="professional", negotiated_rate=55.0, negotiated_type="negotiated",
        is_dollar_rate=True, billing_code_modifier=[], service_code=["11"],
        file_month="2026-06", last_updated_on="2026-06-01", expiration_date=None,
        schema_version="2.0.0", tin_is_really_npi=False, state=None)
        for npi in MINE for p in ("Aetna", "BCBS") for c in ("97110", "92507")]
    with store.rates_part_writer("m.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk(
        [dict(npi=n, org_name="Gateway Therapy", entity_type="NPI-2",
              taxonomy_code="261QP2000X", city="StL", state="MO", address="1 Main",
              zip="63101", phone=None) for n in MINE] +
        [dict(npi=d, org_name=f"Dr Ortho {i}", entity_type="NPI-1",
              taxonomy_code="207X00000X", city="StL", state="MO", address="2 Oak",
              zip="63101", phone=None) for i, d in enumerate(DOCS)])
    store.rebuild_rollups()


def _orf(tmp_path, name="OrderReferring_2026-07-17.csv"):
    p = tmp_path / name
    p.write_text("NPI,LAST_NAME,FIRST_NAME,PARTB,DME,HHA,PMD,HOSPICE\n"
                 + "".join(f"{d},DOC{i},ANNA,Y,{'Y' if i == 0 else 'N'},N,N,N\n"
                           for i, d in enumerate(DOCS)))
    return p


def test_orf_roster_loads_and_refuses_a_wrong_layout(store, tmp_path):
    r = import_orf_roster(store, _orf(tmp_path))
    assert r["providers"] == len(DOCS)
    assert r["release"] == "2026-07-17"          # parsed from the tracker's filename

    flags = {e["npi"]: e for e in npi_eligibility(store, DOCS)}
    assert all(flags[d]["on_list"] and flags[d]["partb"] for d in DOCS)
    assert flags[DOCS[0]]["dme"] is True and flags[DOCS[1]]["dme"] is False

    # A practice's OWN NPIs are absent from this roster by design (it lists
    # order/refer-eligible providers; therapists and orgs are not on it). That
    # must read as "not on the list", never as an error.
    assert all(e["on_list"] is False for e in npi_eligibility(store, MINE))

    bad = tmp_path / "wrong.csv"
    bad.write_text("NPI,NAME,FLAG\n1234567893,x,Y\n")
    with pytest.raises(MedicareImportError, match="Order & Referring layout"):
        import_orf_roster(store, bad)


def test_both_pair_formats_map_their_columns_correctly(store, tmp_path):
    """The two deliveries put the shared-PATIENT count in DIFFERENT positions:
    field 4 in the headerless CMS file, field 3 in Hop Teaming. Reading them
    positionally as if they matched would silently report transaction counts
    as patients."""
    _seed_rates(store)

    cms = tmp_path / "cms_2015.csv"           # npi1,npi2,pair,bene,sameday
    cms.write_text("".join(f"{d},{MINE[0]},{300 + i},{40 + i},3\n"
                           for i, d in enumerate(DOCS))
                   + "5555555555,6666666666,900,90,0\n")   # unrelated pair
    r = import_shared_patients(store, cms)
    assert r["format"] == "cms-shared-patient" and r["year"] == "2015"
    # the unrelated pair touches none of this store's NPIs and is dropped
    assert r["pairs"] == len(DOCS)

    hop = tmp_path / "docgraph_2022.csv"      # header; patients in field 3
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   + "".join(f"{d},{MINE[1]},{70 + i},{500 + i},12.5,3.1\n"
                             for i, d in enumerate(DOCS)))
    r2 = import_shared_patients(store, hop)
    assert r2["format"] == "hop-teaming" and r2["year"] == "2022"

    # CMS: patients must be the BENE column (40..42), not the pair column (300+)
    cms_rows = {x["npi"]: x for x in
                org_referrals(store, MINE, "in", year="2015")["rows"]}
    assert {cms_rows[d]["patients"] for d in DOCS} == {40, 41, 42}
    # Hop: patients must be patient_count (70..72), not transaction_count (500+)
    hop_rows = {x["npi"]: x for x in
                org_referrals(store, MINE, "in", year="2022")["rows"]}
    assert {hop_rows[d]["patients"] for d in DOCS} == {70, 71, 72}
    assert {hop_rows[d]["transactions"] for d in DOCS} == {500, 501, 502}


def test_vintages_are_never_blended(store, tmp_path):
    """Different releases measure different things over different windows — the
    free CMS file is Jan-Sep 2015, a Hop Teaming year is a full calendar year
    with a different attribution method. Adding them produces a number that
    describes no period at all. (Caught in testing: a source's inbound total
    was silently CMS + Hop.)"""
    _seed_rates(store)
    cms = tmp_path / "cms_2015.csv"
    cms.write_text(f"{DOCS[0]},{MINE[0]},300,40,3\n")
    hop = tmp_path / "docgraph_2022.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   f"{DOCS[0]},{MINE[0]},70,500,12.5,3.1\n")
    import_shared_patients(store, cms)
    import_shared_patients(store, hop)

    res = org_referrals(store, MINE, "in")        # no year: newest wins
    assert res["data_year"] == "2022" and res["dataset"] == "DocGraph Hop Teaming"
    assert [r["patients"] for r in res["rows"]] == [70], "must not be 40+70=110"
    assert res["caveat"] and "NOT a referral record" in res["caveat"]

    pinned = org_referrals(store, MINE, "in", year="2015")
    assert [r["patients"] for r in pinned["rows"]] == [40]
    with pytest.raises(MedicareImportError, match="no referral data loaded for 1999"):
        org_referrals(store, MINE, "in", year="1999")

    st = medicare_status(store)
    assert {d["year"] for d in st["referrals"]} == {"2015", "2022"}


def _fake_tracker(root, releases=("2026-08-01",), datasets=("hop_teaming_2022.csv",),
                  meta=None):
    """A stand-in for the tracker's data folder, laid out exactly as its source
    does: snapshots/, referral-map/, dataset-meta.json, state.json."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text('{"ReleaseDate": "%s"}' % (releases[0] if releases else ""))
    snaps = root / "snapshots"
    snaps.mkdir(exist_ok=True)
    for rel in releases:
        (snaps / f"OrderReferring_{rel}.csv").write_text(
            "NPI,LAST_NAME,FIRST_NAME,PARTB,DME,HHA,PMD,HOSPICE\n"
            + "".join(f"{d},DOC{i},ANNA,Y,N,N,N,N\n" for i, d in enumerate(DOCS)))
    rm = root / "referral-map"
    rm.mkdir(exist_ok=True)
    for name in datasets:
        if name.startswith("hop_"):
            (rm / name).write_text(
                "from_npi,to_npi,patient_count,transaction_count,"
                "average_day_wait,std_day_wait\n"
                + "".join(f"{d},{MINE[0]},{70 + i},{500 + i},12.5,3.1\n"
                          for i, d in enumerate(DOCS)))
        else:                                    # pspp_<year>_days<n>.txt
            (rm / name).write_text("".join(f"{d},{MINE[0]},{300 + i},{40 + i},3\n"
                                           for i, d in enumerate(DOCS)))
    if meta:
        (rm / "dataset-meta.json").write_text(meta)
    return root


def test_tracker_discovery_finds_what_is_on_disk(store, tmp_path, monkeypatch):
    """The merge's premise: the files that join the two tools are already on
    the user's disk, so the app must find them without being told."""
    from mrfx.tracker import discover, find_data_dir

    _seed_rates(store)
    root = _fake_tracker(tmp_path / "OrderReferringTracker",
                         releases=("2026-08-01", "2026-07-17"),
                         datasets=("hop_teaming_2022.csv", "pspp_2015_days180.txt"))

    # found via the tracker's own environment variable, with no config at all
    monkeypatch.setenv("ORF_DATA_DIR", str(root))
    assert find_data_dir() == root
    monkeypatch.delenv("ORF_DATA_DIR")
    # and via LOCALAPPDATA, the normal Windows install
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert find_data_dir() == root
    monkeypatch.delenv("LOCALAPPDATA")

    d = discover(store, root)
    assert d["found"] and d["data_dir"] == str(root)
    # newest roster first; both listed, neither imported yet
    assert [s["release"] for s in d["snapshots"]] == ["2026-08-01", "2026-07-17"]
    assert not any(s["imported"] for s in d["snapshots"])
    assert {x["dataset_id"] for x in d["datasets"]} == {
        "hop-teaming_2022", "cms-shared-patient_2015_180d"}
    # ONLY the newest roster is offered — importing an older snapshot would
    # move "who may order/refer today" backwards
    elig = [p for p in d["pending"] if p["kind"] == "eligibility"]
    assert len(elig) == 1 and "2026-08-01" in elig[0]["label"]
    # the licensed dataset is flagged so the UI can carry the licence warning
    hop = [p for p in d["pending"] if p.get("dataset_id") == "hop-teaming_2022"][0]
    assert hop["non_commercial"] is True

    # after importing, those items stop being pending — the card goes quiet
    import_orf_roster(store, elig[0]["path"])
    import_shared_patients(store, hop["path"], year="2022")
    d2 = discover(store, root)
    assert d2["snapshots"][0]["imported"] is True
    assert not [p for p in d2["pending"] if p["kind"] == "eligibility"]
    assert [p["dataset_id"] for p in d2["pending"]] == ["cms-shared-patient_2015_180d"]

    # a folder that is not the tracker's is not "connected"
    assert find_data_dir(tmp_path / "nope") is None
    assert discover(store, tmp_path / "nope")["found"] is False


def test_missing_tracker_is_a_clear_answer_not_a_crash(store, tmp_path, monkeypatch):
    from mrfx.tracker import discover

    monkeypatch.delenv("ORF_DATA_DIR", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    d = discover(store, None)
    assert d["found"] is False and d["data_dir"] is None
    assert d["searched"], "must say where it looked, so the user can fix it"
    assert d["pending"] == [] and d["snapshots"] == [] and d["datasets"] == []


def test_same_year_at_two_windows_is_two_datasets(store, tmp_path):
    """CMS publishes one year at several windows (pspp_2015_days30 …
    days180): different files, different counts, the SAME year. Keyed by year
    alone the second import would overwrite the first and the app would call
    whatever landed last '2015'."""
    _seed_rates(store)
    d30 = tmp_path / "pspp_2015_days30.txt"
    d30.write_text(f"{DOCS[0]},{MINE[0]},100,11,3\n")
    d180 = tmp_path / "pspp_2015_days180.txt"
    d180.write_text(f"{DOCS[0]},{MINE[0]},900,99,3\n")
    r30 = import_shared_patients(store, d30)
    r180 = import_shared_patients(store, d180)
    assert r30["interval"] == "30" and r180["interval"] == "180"
    assert r30["dataset_id"] != r180["dataset_id"]

    st = medicare_status(store)
    assert len(st["referrals"]) == 2, "two windows must not collapse into one"
    assert {d["dataset_id"] for d in st["referrals"]} == {
        "cms-shared-patient_2015_30d", "cms-shared-patient_2015_180d"}

    # pinning by year alone is now ambiguous, and saying so beats picking one
    with pytest.raises(MedicareImportError, match="more than one dataset"):
        org_referrals(store, MINE, "in", year="2015")
    got = org_referrals(store, MINE, "in", dataset_id="cms-shared-patient_2015_30d")
    assert [r["patients"] for r in got["rows"]] == [11]
    assert "30-day" in got["dataset"]
    got180 = org_referrals(store, MINE, "in", dataset_id="cms-shared-patient_2015_180d")
    assert [r["patients"] for r in got180["rows"]] == [99]


def test_dashboard_imports_from_the_tracker_without_stopping_the_server(
        cfg, store, tmp_path, monkeypatch):
    """The friction the merge removes. The CLI must refuse while the server
    owns the database; the SERVER importing its own store is the one process
    that may — so the user never stops anything."""
    from fastapi.testclient import TestClient
    from mrfx.api import create_app

    _seed_rates(store)
    root = _fake_tracker(tmp_path / "OrderReferringTracker")
    monkeypatch.setattr(cfg, "tracker_dir", root, raising=False)
    client = TestClient(create_app(cfg, store))

    d = client.get("/api/medicare/tracker").json()
    assert d["found"] and len(d["pending"]) == 2 and d["job"]["state"] == "idle"

    r = client.post("/api/medicare/tracker/import", json={})
    assert r.status_code == 200 and len(r.json()["started"]) == 2
    for _ in range(200):                       # the import runs on a thread
        job = client.get("/api/medicare/tracker").json()["job"]
        if job["state"] == "done":
            break
        time.sleep(0.05)
    assert job["state"] == "done", job
    assert all(x["ok"] for x in job["done"]), job["done"]

    st = client.get("/api/medicare/status").json()
    assert st["eligibility"]["providers"] == len(DOCS)
    assert [x["dataset_id"] for x in st["referrals"]] == ["hop-teaming_2022"]
    # and the tab now has nothing left to offer
    assert client.get("/api/medicare/tracker").json()["pending"] == []

    # a path the discovery did not just offer is refused — the request must not
    # be able to point the reader at an arbitrary file
    secret = tmp_path / "secret.csv"
    secret.write_text("NPI,LAST_NAME,FIRST_NAME,PARTB,DME,HHA,PMD,HOSPICE\n")
    r = client.post("/api/medicare/tracker/import", json={"paths": [str(secret)]})
    assert r.status_code == 422


def test_upgrade_from_pre_dataset_id_store_self_heals(store, tmp_path):
    """Views persist in DuckDB's catalog: a store that imported referral data
    under the pre-dataset_id build still carries the OLD view definition after
    upgrading. That view answers a bare probe happily while every real query
    dies on the missing column — and the not-loaded catches would read that as
    'no referral data', silently hiding data the user already imported."""
    from mrfx.store import sql_path

    _seed_rates(store)
    hop = tmp_path / "docgraph_2022.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   f"{DOCS[0]},{MINE[0]},70,500,12.5,3.1\n")
    import_shared_patients(store, hop)

    # regress the catalog to the OLD view definition (no dataset_id column),
    # exactly what an upgraded store wakes up with
    glob = sql_path(store.dir / "referrals" / "*.parquet")
    with store.write_lock, store.connect() as con:
        con.execute("CREATE OR REPLACE VIEW referral_pairs AS "
                    "SELECT source_npi, target_npi, patients, transactions, "
                    f"same_day, avg_day_wait, source_label, data_year "
                    f"FROM read_parquet('{glob}', union_by_name = true)")

    st = medicare_status(store)
    assert st["referrals"] and st["referrals"][0]["dataset_id"] == "hop-teaming_2022", \
        "an upgraded store must heal its stale view, not report no data"
    res = org_referrals(store, MINE, "in")
    assert [r["patients"] for r in res["rows"]] == [70]


def test_legacy_windowless_parquet_reads_and_is_superseded(store, tmp_path):
    """A parquet written by the pre-interval build has no dataset_id column
    and no window in its name. It must (a) still read, with a synthesized
    vintage id, and (b) be REPLACED when the same format+year is re-imported
    with a window — under the old build every window collided into that one
    file, so it IS a prior import of the same delivery, and keeping both
    would list the same data as two vintages."""
    from mrfx.store import sql_path

    _seed_rates(store)
    legacy = store.dir / "referrals" / "cms-shared-patient_2015.parquet"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    with store.write_lock, store.connect() as con:
        con.execute(f"""
            COPY (SELECT '{DOCS[0]}' AS source_npi, '{MINE[0]}' AS target_npi,
                         40::BIGINT AS patients, 300::BIGINT AS transactions,
                         3::BIGINT AS same_day, CAST(NULL AS DOUBLE) AS avg_day_wait,
                         'CMS shared-patient' AS source_label, '2015' AS data_year)
            TO '{sql_path(legacy)}' (FORMAT PARQUET)
        """)
        from mrfx.medicare import _register_referral_view
        _register_referral_view(con, store)

    st = medicare_status(store)
    assert st["referrals"][0]["dataset_id"] == "CMS shared-patient_2015"  # synthesized
    assert org_referrals(store, MINE, "in")["rows"][0]["patients"] == 40

    # re-import the same year WITH its window: the legacy file is superseded
    d180 = tmp_path / "pspp_2015_days180.txt"
    d180.write_text(f"{DOCS[0]},{MINE[0]},900,99,3\n")
    import_shared_patients(store, d180)
    assert not legacy.exists(), "the window-less duplicate must be removed"
    st = medicare_status(store)
    assert [d["dataset_id"] for d in st["referrals"]] == ["cms-shared-patient_2015_180d"]
    assert org_referrals(store, MINE, "in")["rows"][0]["patients"] == 99


def test_discovery_never_offers_an_older_roster(store, tmp_path):
    """The roster is 'who may order/refer TODAY'. If the loaded snapshot is
    newer than the tracker's newest (imported by hand from a fresher
    download), offering the tracker's would move eligibility BACKWARDS."""
    from mrfx.tracker import discover

    _seed_rates(store)
    root = _fake_tracker(tmp_path / "OrderReferringTracker",
                         releases=("2026-08-01",), datasets=())
    import_orf_roster(store, _orf(tmp_path, "OrderReferring_2026-08-10.csv"))

    d = discover(store, root)
    assert [p for p in d["pending"] if p["kind"] == "eligibility"] == [], \
        "an older roster than the loaded one must never be offered"
    # and the older snapshot is honestly listed as not-imported, just not work
    assert d["snapshots"][0]["imported"] is False


def test_failed_import_start_releases_the_job_slot(cfg, store, tmp_path, monkeypatch):
    """The busy-check and the 'running' reservation are one atomic step, so a
    request that then fails (nothing to import) must RELEASE the slot — else
    the button is stuck 'running' forever with no import alive."""
    from fastapi.testclient import TestClient
    from mrfx.api import create_app

    _seed_rates(store)
    root = _fake_tracker(tmp_path / "OrderReferringTracker")
    monkeypatch.setattr(cfg, "tracker_dir", root, raising=False)
    client = TestClient(create_app(cfg, store))

    r = client.post("/api/medicare/tracker/import", json={"paths": ["/no/such"]})
    assert r.status_code == 422
    assert client.get("/api/medicare/tracker").json()["job"]["state"] == "idle", \
        "a refused start must not leave the job stuck 'running'"
    # and a real start still works right after
    assert client.post("/api/medicare/tracker/import", json={}).status_code == 200


def test_startup_autoimport_takes_the_roster_and_only_the_roster(
        cfg, store, tmp_path, monkeypatch):
    """tracker_auto_import: ON, a newer roster is picked up at startup with no
    click — but referral datasets are NEVER auto-imported (GB-sized,
    licence-encumbered): they stay a deliberate act."""
    from fastapi.testclient import TestClient
    from mrfx.api import create_app

    _seed_rates(store)
    root = _fake_tracker(tmp_path / "OrderReferringTracker")   # roster + hop file
    monkeypatch.setattr(cfg, "tracker_dir", root, raising=False)
    monkeypatch.setattr(cfg, "tracker_auto_import", True, raising=False)

    # the context manager runs the startup hooks — that IS the code path
    with TestClient(create_app(cfg, store)) as client:
        for _ in range(200):
            st = client.get("/api/medicare/status").json()
            if st["eligibility"]:
                break
            time.sleep(0.05)
        assert st["eligibility"] and st["eligibility"]["providers"] == len(DOCS), \
            "auto-import must load the newer roster at startup"
        assert st["referrals"] == [], "referral data must NEVER auto-import"
        d = client.get("/api/medicare/tracker").json()
        assert [p["kind"] for p in d["pending"]] == ["referrals"], \
            "the referral file stays offered as a deliberate click"


def test_nothing_is_excluded_silently(store, tmp_path):
    """The three quiet ways referral data could vanish or understate, each of
    which must instead be kept or SAID:

    - padded NPIs: every downstream join is an exact string match, so one
      space of padding would silently drop every pair;
    - a 0-kept import: honest data, but it must come back with a warning, not
      read as a quiet success;
    - the row limit: totals must be exact regardless of it, so every consumer
      can say 'top N of M' instead of implying the rows are the whole story."""
    _seed_rates(store)

    # padded values in a Hop delivery still match and import
    hop = tmp_path / "docgraph_2022.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   + "".join(f" {d} , {MINE[0]} ,{70 + i},{500 + i},12.5,3.1\n"
                             for i, d in enumerate(DOCS)))
    r = import_shared_patients(store, hop)
    assert r["pairs"] == len(DOCS), "padded NPIs must be trimmed, not dropped"
    assert r["warning"] is None
    rows = org_referrals(store, MINE, "in")["rows"]
    assert {x["npi"] for x in rows} == set(DOCS)
    assert all(x["name"].startswith("Dr Ortho") for x in rows), \
        "trimmed NPIs must join to the NPPES directory"

    # totals are exact regardless of the row limit
    ref = org_referrals(store, MINE, "in", limit=2)
    assert len(ref["rows"]) == 2
    assert ref["total_partners"] == len(DOCS)
    assert ref["total_patients"] == sum(70 + i for i in range(len(DOCS)))

    # a file whose pairs touch nothing imports as an honest 0 WITH a warning
    alien = tmp_path / "alien_2019.csv"
    alien.write_text("from_npi,to_npi,patient_count,transaction_count,"
                     "average_day_wait,std_day_wait\n"
                     "9999999991,9999999992,50,100,10.0,2.0\n")
    r0 = import_shared_patients(store, alien)
    assert r0["pairs"] == 0
    assert r0["warning"] and "matches nothing" in r0["warning"]


def test_bundle_says_when_a_referral_list_is_truncated(cfg, store, tmp_path):
    """A client deliverable that is a top-N must say so in the file itself."""
    from mrfx.orgprofile import compute_org_profile, org_bundle_files

    _seed_rates(store)
    # more partners than we can name without a big fixture: monkey-level check
    # via a small limit is not possible through the bundle (fixed 500), so
    # assert the honest inverse instead: NOT truncated -> no truncation note.
    hop = tmp_path / "docgraph_2022.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   + "".join(f"{d},{MINE[0]},{70 + i},{500 + i},12.5,3.1\n"
                             for i, d in enumerate(DOCS)))
    import_shared_patients(store, hop)
    files = org_bundle_files(store, compute_org_profile(
        store, "431234567", {"month": "2026-06"}))
    src = files["referral_sources.csv"]
    assert "NOTE: truncated" not in src
    assert "NOT a referral record" in src           # the caveat still travels


def test_real_world_file_encodings_import_cleanly(store, tmp_path):
    """Files that came through Windows: UTF-8 BOM, CRLF line endings, quoted
    fields. All three at once must import with nothing dropped — these are
    cosmetic encodings, not different data."""
    _seed_rates(store)

    orf = tmp_path / "OrderReferring_2026-08-01.csv"
    body = ("NPI,LAST_NAME,FIRST_NAME,PARTB,DME,HHA,PMD,HOSPICE\r\n"
            + "".join(f'"{d}","DOC{i}","ANNA","Y","N","N","N","N"\r\n'
                      for i, d in enumerate(DOCS)))
    orf.write_bytes(b"\xef\xbb\xbf" + body.encode())          # BOM + CRLF + quotes
    r = import_orf_roster(store, orf)
    assert r["providers"] == len(DOCS)
    flags = {e["npi"]: e for e in npi_eligibility(store, DOCS)}
    assert all(flags[d]["on_list"] and flags[d]["partb"] for d in DOCS)

    hop = tmp_path / "hop_teaming_2022.csv"
    hop_body = ("from_npi,to_npi,patient_count,transaction_count,"
                "average_day_wait,std_day_wait\r\n"
                + "".join(f'"{d}","{MINE[0]}",{70 + i},{500 + i},12.5,3.1\r\n'
                          for i, d in enumerate(DOCS)))
    hop.write_bytes(b"\xef\xbb\xbf" + hop_body.encode())
    r2 = import_shared_patients(store, hop)
    assert r2["pairs"] == len(DOCS), "BOM/CRLF/quoted pairs must all be kept"
    assert {x["npi"] for x in org_referrals(store, MINE, "in")["rows"]} == set(DOCS)


def test_garbage_files_are_refused_never_crash_never_import(store, tmp_path):
    """Fuzz the two importers with byte garbage: every case must raise the
    plain-language MedicareImportError — never a traceback class, never a
    partial import."""
    _seed_rates(store)
    cases = [
        b"",                                          # empty
        b"\x00\x01\x02\xff" * 300,                    # binary junk
        b"\xef\xbb\xbf\r\n\r\n",                      # BOM + blank lines only
        "col_a,col_b\n1,2\n".encode(),                # wrong shape
        ("NPI,LAST_NAME\n123,x\n").encode(),          # truncated header
        b"PK\x03\x04not-actually-a-zip",              # zip magic, not a csv
    ]
    for i, blob in enumerate(cases):
        f = tmp_path / f"garbage_{i}.csv"
        f.write_bytes(blob)
        with pytest.raises(MedicareImportError):
            import_orf_roster(store, f)
        with pytest.raises(MedicareImportError):
            import_shared_patients(store, f)
    st = medicare_status(store)
    assert st["eligibility"] is None and st["referrals"] == [], \
        "no garbage case may leave anything imported"


def test_concurrent_requests_do_not_500(cfg, store, tmp_path):
    """Actually run the mixed read/write load the dashboard produces — status
    polls, org lookups, drawer glances, an import — from parallel threads.
    This is the empirical check for the catalog write-write conflict class:
    code reading says the read paths no longer write, so prove it."""
    import threading

    from fastapi.testclient import TestClient
    from mrfx.api import create_app

    _seed_rates(store)
    import_orf_roster(store, _orf(tmp_path))
    hop = tmp_path / "docgraph_2022.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   + "".join(f"{d},{MINE[0]},{70 + i},{500 + i},12.5,3.1\n"
                             for i, d in enumerate(DOCS)))
    import_shared_patients(store, hop)
    client = TestClient(create_app(cfg, store), raise_server_exceptions=False)

    failures: list[str] = []
    barrier = threading.Barrier(8)

    def worker(kind: str) -> None:
        barrier.wait()   # maximal overlap
        for _ in range(15):
            if kind == "status":
                r = client.get("/api/medicare/status")
            elif kind == "org":
                r = client.post("/api/medicare/org", json={"subject": "431234567"})
            elif kind == "glance":
                r = client.get("/api/entity/tin/431234567")
            else:
                r = client.post("/api/medicare/eligibility",
                                json={"text": " ".join(DOCS)})
            if r.status_code >= 500:
                failures.append(f"{kind}: {r.status_code} {r.text[:120]}")

    threads = [threading.Thread(target=worker, args=(k,))
               for k in ("status", "org", "glance", "batch") * 2]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not failures, failures[:5]


def test_kill_between_promote_and_register_is_recovered(store, tmp_path):
    """Crash-resume (invariant 6) for the referral import: the parquet was
    promoted but the process died before anything else. The view's glob is
    evaluated per query, so the data must simply appear; and a stale .tmp
    from a kill mid-COPY must be swept by the next import, not accrete."""
    _seed_rates(store)
    hop = tmp_path / "docgraph_2022.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   f"{DOCS[0]},{MINE[0]},70,500,12.5,3.1\n")
    import_shared_patients(store, hop)

    # simulate the kill artifacts: a promoted parquet appears via the glob
    # (already covered by the import above) and a dead partial sits beside it
    stale = store.dir / "referrals" / "hop-teaming_2019.parquet.tmp"
    stale.write_bytes(b"partial garbage from a killed COPY")
    assert org_referrals(store, MINE, "in")["rows"], "data must stay queryable"

    cms = tmp_path / "pspp_2015_days180.txt"
    cms.write_text(f"{DOCS[0]},{MINE[0]},300,40,3\n")
    import_shared_patients(store, cms)
    assert not stale.exists(), "the next import must sweep dead partials"
    # and the garbage tmp never leaked into the view
    assert {d["dataset_id"] for d in medicare_status(store)["referrals"]} == {
        "hop-teaming_2022", "cms-shared-patient_2015_180d"}


def test_import_inputs_are_validated_not_trusted(store, tmp_path):
    """`year` and `label` are embedded in a COPY statement (DuckDB cannot
    parameterize it) and `year` also names the output parquet — so a hostile
    or fat-fingered value must be refused/sanitized, never interpolated."""
    _seed_rates(store)
    hop = tmp_path / "hop.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   f"{DOCS[0]},{MINE[0]},70,500,12.5,3.1\n")
    for bad_year in ("2022'; DROP TABLE rates; --", "../../evil", "20222", "abc"):
        with pytest.raises(MedicareImportError, match="4-digit year"):
            import_shared_patients(store, hop, year=bad_year)
    # label: quotes and path characters are stripped, import still works
    r = import_shared_patients(store, hop, year="2022",
                               label="Evil' ); DROP--/../lbl")
    assert "'" not in r["label"] and "/" not in r["label"]
    assert org_referrals(store, MINE, "in", year="2022")["rows"]


def test_npi_check_digit():
    """NPIs carry an ISO-7812 Luhn check digit over the 80840 prefix; the
    batch check uses it to keep phone numbers out of the results."""
    from mrfx.medicare import is_valid_npi

    assert is_valid_npi("1234567893")            # CMS's canonical example
    assert all(is_valid_npi(n) for n in MINE + DOCS)
    assert not is_valid_npi("1234567890")        # wrong check digit
    assert not is_valid_npi("3145551008")        # Luhn-valid but no 1/2 prefix
    assert not is_valid_npi("141759489")         # 9 digits
    assert not is_valid_npi("14175948960")       # 11 digits


def test_roster_change_tracking_and_staleness(cfg, store, tmp_path):
    """Between two snapshots the actionable movement is who LOST order/refer
    standing — those NPIs must be flagged by name on referral rows. And a
    months-old roster must call itself stale rather than quietly answering
    eligibility from the past."""
    import datetime as dt

    from fastapi.testclient import TestClient
    from mrfx.api import create_app
    from mrfx.medicare import recent_losses

    _seed_rates(store)
    r1 = import_orf_roster(store, _orf(tmp_path))
    assert r1["diff"] is None                      # first import: nothing to diff
    st = medicare_status(store)
    assert "last_change" not in st["eligibility"]
    # age computed from the release date in the tracker's filename
    assert st["eligibility"]["age_days"] == (
        dt.date.today() - dt.date(2026, 7, 17)).days

    # v2: DOCS[0] unchanged, DOCS[1] loses Part B, DOCS[2] drops off, one new
    v2 = tmp_path / "OrderReferring_2026-08-10.csv"
    v2.write_text("NPI,LAST_NAME,FIRST_NAME,PARTB,DME,HHA,PMD,HOSPICE\n"
                  f"{DOCS[0]},DOC0,ANNA,Y,Y,N,N,N\n"
                  f"{DOCS[1]},DOC1,ANNA,N,N,N,N,N\n"
                  "1590000000,NEWDOC,SAM,Y,N,N,N,N\n")
    r2 = import_orf_roster(store, v2)
    assert r2["diff"] == {"prev_release": "2026-07-17", "added": 1, "removed": 1,
                          "partb_lost": 1, "partb_gained": 0}
    lc = medicare_status(store)["eligibility"]["last_change"]
    assert lc["partb_lost"] == 1 and lc["prev_release"] == "2026-07-17"
    assert recent_losses(store, DOCS) == {DOCS[1]: "lost_partb",
                                          DOCS[2]: "removed"}

    # re-importing the SAME release must not erase the recorded change with
    # an all-zero diff
    r3 = import_orf_roster(store, v2)
    assert r3["diff"] is None
    assert medicare_status(store)["eligibility"]["last_change"]["partb_lost"] == 1

    # the flag lands on the dashboard's referral rows
    hop = tmp_path / "docgraph_2022.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   + "".join(f"{d},{MINE[0]},{70 + i},{500 + i},12.5,3.1\n"
                             for i, d in enumerate(DOCS)))
    import_shared_patients(store, hop)
    client = TestClient(create_app(cfg, store))
    rows = {r["npi"]: r for r in client.post(
        "/api/medicare/org",
        json={"subject": "431234567"}).json()["referrals_in"]["rows"]}
    assert rows[DOCS[1]]["recent_change"] == "lost_partb"
    assert rows[DOCS[2]]["recent_change"] == "removed"
    assert rows[DOCS[0]]["recent_change"] is None


def test_failed_imports_leave_previous_data_untouched(store, tmp_path):
    """The tracker's contract, kept here: a validation failure keeps your
    existing snapshot untouched. Two ways an import can die AFTER passing the
    up-front checks — and neither may destroy what was already loaded:

    - roster: header OK but the body is unreadable. Connections are autocommit,
      so an unwrapped DELETE + INSERT committed the DELETE alone — the bad file
      WIPED the roster it was meant to replace.
    - referrals: COPY dies partway. Written straight to the *.parquet the view
      globs, the corrupt file blinded the WHOLE referral_pairs view, prior
      good datasets included."""
    _seed_rates(store)
    import_orf_roster(store, _orf(tmp_path))
    good_hop = tmp_path / "docgraph_2022.csv"
    good_hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                        "average_day_wait,std_day_wait\n"
                        f"{DOCS[0]},{MINE[0]},70,500,12.5,3.1\n")
    import_shared_patients(store, good_hop)

    # roster: right header, ragged body (e.g. a truncated re-download)
    bad = tmp_path / "OrderReferring_2026-08-01.csv"
    bad.write_text("NPI,LAST_NAME,FIRST_NAME,PARTB,DME,HHA,PMD,HOSPICE\n"
                   "1901234561,DOC,ANNA,Y,N,N,N,N,EXTRA,COLUMNS,HERE\n")
    with pytest.raises(MedicareImportError, match="untouched"):
        import_orf_roster(store, bad)
    st = medicare_status(store)
    assert st["eligibility"]["providers"] == len(DOCS), \
        "failed roster load must not wipe the loaded snapshot"
    assert st["eligibility"]["release"] == "2026-07-17"      # still the OLD release

    # referrals: header says Hop, body goes ragged mid-file
    bad_hop = tmp_path / "hop_2023.csv"
    bad_hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                       "average_day_wait,std_day_wait\n"
                       f"{DOCS[0]},{MINE[0]},70,500,12.5,3.1\n"
                       "9999999999,8888888888,1,2\n")       # 4 fields — corrupt
    with pytest.raises(MedicareImportError, match="untouched"):
        import_shared_patients(store, bad_hop)
    st = medicare_status(store)
    assert [d["year"] for d in st["referrals"]] == ["2022"], \
        "failed referral import must not blind the prior datasets"
    assert org_referrals(store, MINE, "in")["rows"], "2022 pairs still answer"
    ref_dir = store.dir / "referrals"
    assert not list(ref_dir.glob("*.tmp")) and not list(ref_dir.glob("*2023*")), \
        "no partial/corrupt parquet may be left behind"


def test_medicare_api_and_dashboard_tab(cfg, store, tmp_path):
    """The dashboard tab's three endpoints: status, one-practice lookup (with
    eligibility joined onto every referral row), and the batch NPI check."""
    from fastapi.testclient import TestClient
    from mrfx.api import create_app

    _seed_rates(store)
    client = TestClient(create_app(cfg, store))

    # Nothing imported: status says so, the practice lookup still answers with
    # its NPIs (the UI shows import instructions), and the batch check is a
    # clear refusal rather than an empty table.
    assert client.get("/api/medicare/status").json() == {
        "eligibility": None, "referrals": []}
    d = client.post("/api/medicare/org", json={"subject": "431234567"}).json()
    assert set(d["npis"]) == set(MINE)
    assert d["eligibility"] == [] and d["referrals_in"]["rows"] == []
    r = client.post("/api/medicare/eligibility", json={"text": MINE[0]})
    assert r.status_code == 422 and "no Order & Referring roster" in r.json()["detail"]

    import_orf_roster(store, _orf(tmp_path))
    hop = tmp_path / "docgraph_2022.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   + "".join(f"{d},{MINE[0]},{70 + i},{500 + i},12.5,3.1\n"
                             for i, d in enumerate(DOCS)))
    import_shared_patients(store, hop)

    st = client.get("/api/medicare/status").json()
    assert st["eligibility"]["providers"] == len(DOCS)
    assert st["referrals"] == [{"label": "DocGraph Hop Teaming", "year": "2022",
                                "pairs": len(DOCS), "dataset_id": "hop-teaming_2022"}]

    d = client.post("/api/medicare/org", json={"subject": "431234567"}).json()
    rows = d["referrals_in"]["rows"]
    assert [x["npi"] for x in rows] == [DOCS[2], DOCS[1], DOCS[0]]  # by patients desc
    # the payoff on one row: volume + current Part B eligibility + a readable specialty
    assert all(x["on_orf"] and x["partb"] for x in rows)
    assert rows[0]["specialty"] == "Orthopedic surgery"           # 207X00000X
    assert d["referrals_in"]["data_year"] == "2022"
    # the practice's own NPIs read as not-on-list (expected for therapists/orgs)
    assert all(e["on_list"] is False for e in d["eligibility"])

    # an unloaded year and an unknown practice are refusals, not empty screens
    r = client.post("/api/medicare/org", json={"subject": "431234567", "year": "1999"})
    assert r.status_code == 422 and "no referral data loaded for 1999" in r.json()["detail"]
    r = client.post("/api/medicare/org", json={"subject": "No Such Clinic LLC"})
    assert r.status_code == 422 and "no practice with NPIs" in r.json()["detail"]

    # batch check: NPIs are pulled out of any pasted text, deduplicated, and
    # 10-digit numbers that fail the NPI check digit (phone numbers) are
    # skipped and counted rather than reported as "not on the list"
    r = client.post("/api/medicare/eligibility",
                    json={"text": f"call {DOCS[0]} at 3145551000 and {MINE[0]};"
                                  f" also {DOCS[0]} again"})
    d = r.json()
    assert d["checked"] == 2 and d["on_list"] == 1 and d["ignored_non_npi"] == 1
    by = {x["npi"]: x for x in d["rows"]}
    assert by[DOCS[0]]["on_list"] is True and by[MINE[0]]["on_list"] is False
    r = client.post("/api/medicare/eligibility", json={"text": "no npis here"})
    assert r.status_code == 422
    r = client.post("/api/medicare/eligibility", json={"text": "3145551000"})
    assert r.status_code == 422 and "phone numbers" in r.json()["detail"]


def test_bundle_gains_the_medicare_layers_and_says_so(cfg, store, tmp_path):
    """With the layers imported the bundle must carry them AND stop claiming it
    contains no eligibility/referral data — that disclaimer is true only while
    they are absent."""
    from mrfx.orgprofile import compute_org_profile, org_bundle_files

    _seed_rates(store)
    before = org_bundle_files(store, compute_org_profile(
        store, "431234567", {"month": "2026-06"}))
    assert "eligibility.csv" not in before
    assert "WHAT THIS BUNDLE DOES NOT CONTAIN" in before["profile.txt"]

    import_orf_roster(store, _orf(tmp_path))
    hop = tmp_path / "docgraph_2022.csv"
    hop.write_text("from_npi,to_npi,patient_count,transaction_count,"
                   "average_day_wait,std_day_wait\n"
                   + "".join(f"{d},{MINE[0]},{70 + i},{500 + i},12.5,3.1\n"
                             for i, d in enumerate(DOCS)))
    import_shared_patients(store, hop)

    after = org_bundle_files(store, compute_org_profile(
        store, "431234567", {"month": "2026-06"}))
    assert {"eligibility.csv", "referral_sources.csv"} <= set(after)
    # the payoff: each referring physician carries BOTH volume and whether they
    # are still eligible to refer
    src = after["referral_sources.csv"]
    assert "still_eligible_partb" in src and "Dr Ortho 0" in src
    assert src.count(",Y,") >= 1
    assert "NOT a referral record" in src            # caveat travels with the data
    # and the disclaimer flipped rather than lying
    assert "WHAT THIS BUNDLE DOES NOT CONTAIN" not in after["profile.txt"]
    assert "already carries eligibility and referral structure" in after["profile.txt"]
