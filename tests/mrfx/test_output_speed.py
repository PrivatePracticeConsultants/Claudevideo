"""Round 4 (output speed) — the changes that are not the stored latest flag
(that is tests/mrfx/test_latest_flag.py). Each is a speed change that must not
move a number, so each test checks the fast answer against the slow one.

- /api/stats name progress: the NPIs the rates reference are kept in a side
  table rebuilt only when the rate parts change (was: two DISTINCT scans of
  every raw row each minute the dashboard was open).
- Overview: one pass over the spine into a per-request temp table, then the
  four sections (was: four whole-spine scans).
- mode(): DuckDB breaks a tie by thread arrival — 4 different answers in 8
  runs on a tie-dense table. Every mode() now orders its input.
- Exports sort on the RAW columns: the masked TIN alias made every
  SSN-pattern TIN tie, so the CSV's row order changed per download.
"""

from __future__ import annotations

import json

import pytest

NP1, NP2, NP3 = "1310000001", "1310000002", "1310000003"


def _mrf(day, rows, payer="Kappa Health"):
    return {"reporting_entity_name": payer, "reporting_entity_type": "issuer",
            "last_updated_on": day, "version": "2.0.0", "provider_references": [],
            "in_network": [{"negotiation_arrangement": "ffs", "name": "x",
                            "billing_code_type": "CPT", "billing_code_type_version": "2026",
                            "billing_code": "97110", "description": "x",
                            "negotiated_rates": [{
                                "provider_groups": [{"npi": npis, "tin": {"type": tt, "value": tin}}],
                                "negotiated_prices": [{
                                    "negotiated_type": "negotiated", "negotiated_rate": rate,
                                    "billing_class": "professional", "service_code": ["11"],
                                    "billing_code_modifier": ["GP"],
                                    "expiration_date": "9999-12-31"}]}
                                for npis, tin, tt, rate in rows]}]}


def _slow_progress(store):
    """The pre-round-4 queries, verbatim in effect."""
    with store.connect() as con:
        union = ("SELECT npi FROM rates UNION SELECT tin_value FROM rates "
                 "WHERE tin_is_really_npi AND tin_value IS NOT NULL")
        total = con.execute(f"SELECT count(*) FROM (SELECT DISTINCT npi FROM ({union}) "
                            "WHERE regexp_full_match(npi, '[0-9]{10}'))").fetchone()[0]
        enriched, named = con.execute(
            "SELECT count(*), count(*) FILTER (WHERE org_name IS NOT NULL) FROM npi_directory "
            f"WHERE regexp_full_match(npi, '[0-9]{{10}}') AND npi IN ({union})").fetchone()
    return {"total": total, "enriched": enriched, "named": named,
            "remaining": max(0, total - enriched)}


def test_name_progress_matches_the_full_scan_through_parts_changing(cfg, store):
    from mrfx.ingest import scan_inbox
    (cfg.inbox_dir / "k_2026-05_in-network.json").write_text(json.dumps(_mrf("2026-05-01", [
        ([NP1, NP2], "320000001", "ein", 40.0),
        ([], NP3, "npi", 35.0),                     # an NPI in the TIN slot counts too
        (["123"], "320000002", "ein", 41.0)])))     # malformed NPI: never counted
    list(scan_inbox(cfg, store))
    p = store.enrichment_progress(max_age_seconds=0)
    assert p == _slow_progress(store) and p["total"] == 3
    with store.connect() as con:                      # kept per part
        assert con.execute("SELECT count(DISTINCT npi) FROM rate_npi_set").fetchone()[0] == 3
        assert con.execute("SELECT count(*) FROM rate_npi_parts").fetchone()[0] == 1
    # names arriving change enriched/named WITHOUT a part changing
    store.save_npi(NP1, "KAPPA PT", "261QP2000X", "Clinic/Center PT", "Town", "MO",
                   entity_type="NPI-2")
    assert store.enrichment_progress(max_age_seconds=0) == _slow_progress(store)
    # a new part: the side table follows (new NPI counted)
    (cfg.inbox_dir / "k2_2026-05_in-network.json").write_text(json.dumps(_mrf("2026-05-01", [
        (["1310000009"], "320000003", "ein", 50.0)], payer="Lambda Health")))
    list(scan_inbox(cfg, store))
    p = store.enrichment_progress(max_age_seconds=0)
    assert p == _slow_progress(store) and p["total"] == 4
    # a removed part too
    from mrfx.ingest import forget_file
    forget_file(cfg, store, "k2_2026-05_in-network.json")
    p = store.enrichment_progress(max_age_seconds=0)
    assert p == _slow_progress(store) and p["total"] == 3


def test_header_counts_follow_every_spine_change(cfg, store):
    """store_stats' spine counts are held per data generation: after each
    ingest / removal they equal a raw recount."""
    from mrfx.ingest import forget_file, scan_inbox

    def raw():
        with store.connect() as con:
            n, t, p = con.execute("SELECT count(*), count(DISTINCT tin_value), "
                                  "count(DISTINCT payer) FROM rates").fetchone()
        return {"rates": n, "tins": t, "payers": p}
    (cfg.inbox_dir / "k_2026-05_in-network.json").write_text(json.dumps(_mrf("2026-05-01", [
        ([NP1], "320000001", "ein", 40.0), ([NP2], "320000002", "ein", 41.0)])))
    list(scan_inbox(cfg, store))
    assert store.store_stats(max_age_seconds=0) == raw() == {"rates": 2, "tins": 2, "payers": 1}
    (cfg.inbox_dir / "k2_2026-05_in-network.json").write_text(json.dumps(_mrf("2026-05-01", [
        ([NP3], "320000003", "ein", 50.0)], payer="Lambda Health")))
    list(scan_inbox(cfg, store))
    assert store.store_stats(max_age_seconds=0) == raw() == {"rates": 3, "tins": 3, "payers": 2}
    forget_file(cfg, store, "k2_2026-05_in-network.json")
    assert store.store_stats(max_age_seconds=0) == raw() == {"rates": 2, "tins": 2, "payers": 1}


def test_name_progress_never_waits_on_a_writer(cfg, store):
    from mrfx.ingest import scan_inbox
    (cfg.inbox_dir / "k_2026-05_in-network.json").write_text(json.dumps(_mrf("2026-05-01", [
        ([NP1], "320000001", "ein", 40.0)])))
    list(scan_inbox(cfg, store))
    with store.write_lock:                            # e.g. a rollup's catalog write
        assert store.enrichment_progress(max_age_seconds=0) == _slow_progress(store)


def test_overview_single_pass_hand_answer(cfg, store):
    """Three payers price the same 12 practices at $30..$41 on three codes;
    Kappa's June file supersedes its May one. By hand: 3 payers, 3 codes, 12
    practices (6 in MO), every code's median $35.50, every payer exactly at
    the market (index 1.0). The temp table is gone after the call."""
    from mrfx import overview as ov
    from mrfx.ingest import scan_inbox
    rows = [([f"13100000{i:02d}"], f"3300000{i:02d}", "ein", 30.0 + i) for i in range(12)]
    for payer, day in (("Kappa Health", "2026-05-01"), ("Kappa Health", "2026-06-01"),
                       ("Lambda Health", "2026-06-01"), ("Mu Health", "2026-06-01")):
        doc = _mrf(day, rows, payer=payer)
        if day == "2026-05-01":                       # superseded: must not count
            for r in doc["in_network"][0]["negotiated_rates"]:
                r["negotiated_prices"][0]["negotiated_rate"] = 99.0
        doc["in_network"].append({**doc["in_network"][0], "billing_code": "92507"})
        doc["in_network"].append({**doc["in_network"][0], "billing_code": "97140"})
        (cfg.inbox_dir / f"{payer.split()[0]}_{day}_in-network.json").write_text(json.dumps(doc))
    list(scan_inbox(cfg, store))
    for i in range(12):
        store.save_npi(f"13100000{i:02d}", f"PRACTICE {i}", "261QP2000X", "Clinic/Center PT",
                       "Town", "MO" if i % 2 else "KS", entity_type="NPI-2")
    store.rebuild_rollups()
    o = ov.market_overview(store)
    assert (o["payers"], o["codes"], o["practices"]) == (3, 3, 12)
    assert {c["billing_code"]: c["median"] for c in o["top_codes"]} == {
        "92507": 35.5, "97110": 35.5, "97140": 35.5}
    assert {p["payer"]: p["index"] for p in o["payer_index"]} == {
        "Kappa Health": 1.0, "Lambda Health": 1.0, "Mu Health": 1.0}
    assert ov.market_overview(store, "MO")["practices"] == 6
    with store.connect() as con:
        assert not con.execute("SELECT count(*) FROM information_schema.tables "
                               "WHERE table_name = '__overview'").fetchone()[0]


def test_mode_breaks_ties_the_same_way_every_time():
    """The class behind the practice ZIP / city / state placements: plain
    mode() on a tie returns whichever value its thread saw first."""
    import duckdb
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("CREATE TABLE t AS SELECT i % 3000 AS g, CASE WHEN (i // 3000) % 2 = 0 "
                "THEN 'b' ELSE 'a' END || (i % 3000) AS v FROM range(600000) r(i) "
                "ORDER BY random()")
    q = "SELECT md5(string_agg(m, ',' ORDER BY g)) FROM (SELECT g, {} AS m FROM t GROUP BY g)"
    assert len({con.execute(q.format("mode(v ORDER BY v)")).fetchone()[0] for _ in range(6)}) == 1


def test_no_mode_in_the_code_without_an_order():
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[2] / "mrfx"
    bad = []
    for p in root.glob("*.py"):
        for m in re.finditer(r"\bmode\(([^()]*(?:\([^()]*(?:\([^()]*\)[^()]*)*\)[^()]*)*)\)",
                             p.read_text()):
            text = p.read_text()
            line_start = text.rfind("\n", 0, m.start()) + 1
            if "#" in text[line_start:m.start()]:       # a Python comment
                continue
            if "ORDER BY" not in m.group(1) and m.group(1).strip():
                bad.append(f"{p.name}:{text[:m.start()].count(chr(10)) + 1}")
    assert not bad, f"mode() without ORDER BY (ties are random): {bad}"


@pytest.mark.parametrize("view", ["csv", "zip"])
def test_export_order_is_the_screen_order_with_ssn_pattern_tins(cfg, store, view):
    """Several SSN-pattern TINs at one rate: the export masks them all to
    'MASKED-SSN', and sorting on that alias left their order to the threads.
    The export must list them in the screen's (raw-TIN) order, every time."""
    import csv
    import io
    import zipfile

    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from mrfx.ingest import scan_inbox
    ssns = ["078051120", "078051121", "078051122", "078051123", "078051124", "078051125"]
    (cfg.inbox_dir / "k_2026-05_in-network.json").write_text(json.dumps(_mrf(
        "2026-05-01", [([f"14000000{i:02d}"], s, "ein", 40.0) for i, s in enumerate(ssns)])))
    list(scan_inbox(cfg, store))
    for i in range(len(ssns)):                       # names tell the rows apart
        store.save_npi(f"14000000{i:02d}", f"ORG {5 - i}", "261QP2000X", "Clinic/Center PT",
                       "Town", "MO", entity_type="NPI-2")
    store.rebuild_rollups()
    c = TestClient(create_app(cfg, store))
    rows = c.get("/api/rates?grain=tin&cpt=97110&limit=100").json()["rows"]
    assert [r["unit_id"] for r in rows] == ssns       # the screen: raw TIN order
    screen = [r["display_name"] for r in rows]

    def export_names():
        r = c.get(f"/api/export.{view}?grain=tin&cpt=97110")
        z = None if view == "csv" else zipfile.ZipFile(io.BytesIO(r.content))
        text = r.text if z is None else z.read(
            next(n for n in z.namelist() if n.endswith(".csv"))).decode()
        out = list(csv.DictReader(io.StringIO(text)))
        assert {row["unit_id"] for row in out} == {"MASKED-SSN"}
        return [row["display_name"] for row in out]
    for _ in range(4):
        assert export_names() == screen


def test_capped_rate_changes_keep_the_same_rows_every_time(cfg, store, monkeypatch):
    """Twelve practices all get the same +5% raise; the list is capped at 4.
    Ordered by the % alone, WHICH four came back was the threads' choice."""
    import mrfx.monitor as mon
    from mrfx.ingest import scan_inbox
    rows = [([f"15000000{i:02d}"], f"3500000{i:02d}", "ein", 40.0) for i in range(12)]
    (cfg.inbox_dir / "k_2026-05_in-network.json").write_text(json.dumps(_mrf("2026-05-01", rows)))
    (cfg.inbox_dir / "k_2026-06_in-network.json").write_text(json.dumps(_mrf(
        "2026-06-01", [(n, t, tt, 42.0) for n, t, tt, _ in rows])))
    list(scan_inbox(cfg, store))
    monkeypatch.setattr(mon, "_CHANGES_CAP", 4)
    m = {"month": "2026-06", "prev_month": "2026-05"}
    first = mon.compute_rate_changes(store, m)
    assert first["truncated"] and [r["tin_value"] for r in first["changes"]] == [
        f"3500000{i:02d}" for i in range(4)]
    for _ in range(5):
        assert mon.compute_rate_changes(store, m)["changes"] == first["changes"]


def test_a_practice_split_evenly_over_zips_sits_at_the_lowest(cfg, store):
    """Four NPIs, one per ZIP, one rate row each: a four-way tie that plain
    mode() settled by thread arrival. The helpers pick the lowest ZIP, every
    time, and agree with mode(zip ORDER BY zip)."""
    from mrfx.ingest import scan_inbox
    from mrfx.sqlfn import ZIP5, modal_zip_by_tin, modal_zip_of
    npis = ["1610000001", "1610000002", "1610000003", "1610000004"]
    (cfg.inbox_dir / "k_2026-05_in-network.json").write_text(json.dumps(_mrf("2026-05-01", [
        ([n], "360000001", "ein", 40.0) for n in npis])))
    list(scan_inbox(cfg, store))
    for n, z in zip(npis, ("64105", "35244-1234", "66101", "35565")):
        store.save_npi(n, "PT", "225100000X", "PT", "Town", "MO", entity_type="NPI-1", zip_code=z)
    with store.connect() as con:
        ref = con.execute(f"SELECT mode({ZIP5} ORDER BY {ZIP5}) FROM rates r JOIN npi_directory n "
                          "ON n.npi = r.npi WHERE r.tin_value = '360000001'").fetchone()[0]
        for _ in range(5):
            assert con.execute(f"SELECT * FROM ({modal_zip_of('SELECT unnest(?::VARCHAR[])')})",
                               [["360000001"]]).fetchone()[0] == ref == "35244"
            assert con.execute(modal_zip_by_tin("SELECT '360000001'")).fetchall() == [
                ("360000001", "35244")]
