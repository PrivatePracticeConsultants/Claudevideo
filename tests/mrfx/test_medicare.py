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
