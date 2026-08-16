"""Medicaid/WC floor schedules, sparklines, and NPPES weekly incrementals.

The floor comparison is the one number in the app most likely to end a
negotiation argument, so the tests care that it is never computed from
something the app invented, and never across state lines.
"""

import csv
import io

import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.benchmark import BenchmarkError
from mrfx.floors import (FloorImportError, floor_comparison, floor_status,
                         forget_schedule, import_fee_schedule)
from mrfx.spark import series_by_code, sparkline

MARKET = {"month": "2026-06", "therapy_only": False}


def _row(tin, npi, rate, *, code="97110", month="2026-06", src="a.json"):
    return dict(payer="Aetna", tin_value=tin, tin_type="ein", npi=npi,
                source_file=src, billing_code=code, billing_code_type="CPT",
                discipline="PT", is_timed=True, billing_class="professional",
                negotiated_rate=rate, negotiated_type="negotiated",
                is_dollar_rate=True, billing_code_modifier=[], service_code=["11"],
                file_month=month, last_updated_on=f"{month}-01",
                expiration_date=None, schema_version="2.0.0",
                tin_is_really_npi=False, state="MO")


def _sched(tmp_path, rows, name="mo_medicaid.csv",
           header=("Procedure Code", "Maximum Allowable")):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    p = tmp_path / name
    p.write_text(buf.getvalue())
    return p


def _seed(store):
    with store.rates_part_writer("a.json") as w:
        w.write_batch([_row("431234567", "1417594896", 42.00),
                       _row("437654321", "1999999992", 46.00),
                       _row("431234567", "1417594896", 38.00, code="97140")])
    store.save_npis_bulk([
        dict(npi=n, org_name=f"P{i}", entity_type="NPI-2",
             taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X",
             city="StL", state="MO", address="x", zip="63103", phone=None)
        for i, n in enumerate(("1417594896", "1999999992"))])
    store.rebuild_rollups()


def test_a_schedule_loads_with_the_label_that_becomes_its_provenance(cfg, store, tmp_path):
    p = _sched(tmp_path, [["97110", "$28.50"], ["97140", "26.00"],
                          ["93306", "180.00"],          # not a therapy code
                          ["97116", "n/a"]])            # no usable rate
    res = import_fee_schedule(store, p, kind="medicaid", state="mo", year="2026")
    assert res["codes"] == 2, "only therapy codes with a real dollar amount"
    assert res["skipped_no_rate"] == 1
    assert res["state"] == "MO" and res["label"] == "MO Medicaid 2026"

    st = floor_status(store)
    assert st["loaded"] and st["schedules"][0]["kind_label"] == "Medicaid"
    assert st["schedules"][0]["min_rate"] == 26.0


def test_a_schedule_without_a_state_or_a_readable_column_is_refused(cfg, store, tmp_path):
    p = _sched(tmp_path, [["97110", "28.50"]])
    with pytest.raises(FloorImportError, match="two-letter state"):
        import_fee_schedule(store, p, kind="medicaid", state="")
    with pytest.raises(FloorImportError, match="kind must be one of"):
        import_fee_schedule(store, p, kind="mediciad", state="MO")

    junk = tmp_path / "junk.csv"
    junk.write_text("alpha,beta\n1,2\n")
    with pytest.raises(FloorImportError, match="could not find a code column"):
        import_fee_schedule(store, junk, kind="medicaid", state="MO")

    nocodes = _sched(tmp_path, [["93306", "180.00"]], name="wrong.csv")
    with pytest.raises(FloorImportError, match="no therapy codes"):
        import_fee_schedule(store, nocodes, kind="medicaid", state="MO")


def test_a_duplicated_code_keeps_the_maximum_allowable(cfg, store, tmp_path):
    """Schedules list a code more than once (facility vs non-facility, modifier
    variants). Picking arbitrarily would make the import unrepeatable."""
    p = _sched(tmp_path, [["97110", "24.00"], ["97110", "28.50"], ["97110", "21.00"]])
    import_fee_schedule(store, p, kind="medicaid", state="MO")
    with store.connect() as con:
        assert con.execute(
            "SELECT rate FROM floor_schedules WHERE code = '97110'").fetchall() \
            == [(28.5,)]


def test_the_comparison_reports_commercial_as_a_percent_of_each_floor(cfg, store, tmp_path):
    _seed(store)
    import_fee_schedule(store, _sched(tmp_path, [["97110", "28.50"], ["97140", "26.00"]]),
                        kind="medicaid", state="MO", year="2026")
    import_fee_schedule(store, _sched(tmp_path, [["97110", "72.00"], ["97140", "68.00"]],
                                      name="mo_wc.csv"),
                        kind="workers_comp", state="MO", year="2026")

    cmp = floor_comparison(store, MARKET, subject="431234567", state="MO")
    by = {r["billing_code"]: r for r in cmp["rows"]}
    a = by["97110"]
    assert a["commercial_median"] == 44.00        # median(42, 46)
    assert a["medicaid"] == 28.50 and a["workers_comp"] == 72.00
    assert a["pct_of_medicaid"] == 154.4          # 44.00 / 28.50
    assert a["pct_of_workers_comp"] == 61.1       # 44.00 / 72.00
    assert a["subject_rate"] == 42.00
    assert a["subject_pct_of_medicaid"] == 147.4  # 42.00 / 28.50
    assert "of MO Medicaid" in cmp["headline"]
    assert "workers' comp" in cmp["headline"]
    assert "managed-Medicaid plans commonly pay a percentage" in cmp["note"]


def test_a_code_the_schedule_omits_is_absent_never_zero(cfg, store, tmp_path):
    """Treating a missing schedule line as $0 would report an infinite ratio
    and make a rate look heroic."""
    _seed(store)
    import_fee_schedule(store, _sched(tmp_path, [["97110", "28.50"]]),
                        kind="medicaid", state="MO")
    cmp = floor_comparison(store, MARKET, state="MO")
    assert {r["billing_code"] for r in cmp["rows"]} == {"97110"}
    assert "97140" in cmp["codes_not_in_schedule"]


def test_a_floor_comparison_never_crosses_state_lines(cfg, store, tmp_path):
    """A fee schedule is a state document; comparing a national commercial
    median to one state's Medicaid would be an accidental apples-to-oranges."""
    _seed(store)
    import_fee_schedule(store, _sched(tmp_path, [["97110", "28.50"]]),
                        kind="medicaid", state="MO")
    with pytest.raises(BenchmarkError, match="needs a state"):
        floor_comparison(store, MARKET)
    other = floor_comparison(store, MARKET, state="KS")
    assert other["loaded"] is False
    assert "no schedule loaded for KS" in other["reason"] and "MO" in other["reason"]


def test_the_floors_api_imports_compares_and_forgets(cfg, store, tmp_path):
    _seed(store)
    c = TestClient(create_app(cfg, store))
    assert c.get("/api/floors/status").json()["loaded"] is False
    p = _sched(tmp_path, [["97110", "28.50"]])
    with open(p, "rb") as fh:
        r = c.post("/api/floors/import?kind=medicaid&state=MO&year=2026",
                   files={"file": ("mo.csv", fh, "text/csv")})
    assert r.status_code == 200 and r.json()["codes"] == 1

    cmp = c.post("/api/floors/compare",
                 json={"market": MARKET, "state": "MO"}).json()
    assert cmp["rows"][0]["pct_of_medicaid"] == 154.4

    with open(tmp_path / "bad.csv", "w") as fh:
        fh.write("a,b\n1,2\n")
    with open(tmp_path / "bad.csv", "rb") as fh:
        assert c.post("/api/floors/import?kind=medicaid&state=MO",
                      files={"file": ("bad.csv", fh, "text/csv")}).status_code == 422

    assert forget_schedule(store, "medicaid", "MO", "2026")["removed"] == 1
    assert floor_status(store)["loaded"] is False


# ------------------------------------------------------------------ sparklines

def test_a_single_point_draws_no_trend_line():
    """A lone dot styled like a trend is a claim the data cannot support."""
    assert "svg" not in sparkline([("2026-06", 40.0)])
    assert "no trend to draw" in sparkline([("2026-06", 40.0)])
    assert "svg" not in sparkline([])
    assert "svg" not in sparkline(None)


def test_a_sparkline_encodes_direction_and_names_its_months():
    down = sparkline([("2026-01", 50.0), ("2026-06", 40.0)], label="97110")
    assert "<svg" in down and "#d92d20" in down, "a fall is drawn as a fall"
    assert "2026-01 $50.00" in down and "2026-06 $40.00" in down
    assert "-20.0%" in down, "the tooltip carries the actual change"

    up = sparkline([("2026-01", 40.0), ("2026-06", 50.0)])
    assert "#12805c" in up
    flat = sparkline([("2026-01", 40.0), ("2026-06", 40.0)])
    assert "#667085" in flat, "no direction, no colour claim"


def test_series_are_read_on_the_same_basis_as_every_other_number(cfg, store):
    with store.rates_part_writer("a.json") as w:
        w.write_batch([_row("431234567", "1417594896", 40.0, month="2026-05"),
                       _row("431234567", "1417594896", 44.0, month="2026-06")])
    store.save_npis_bulk([dict(
        npi="1417594896", org_name="P", entity_type="NPI-2",
        taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X", city="StL",
        state="MO", address="x", zip="63103", phone=None)])
    store.rebuild_rollups()
    s = series_by_code(store, ["431234567"], {"therapy_only": False})
    assert s["97110"] == [("2026-05", 40.0), ("2026-06", 44.0)]
    # the series pins its own month, so a caller's bad month cannot reach SQL
    assert series_by_code(store, ["431234567"], {"month": ["not", "a", "month"]}) == s
    # cosmetic tier: an input that genuinely raises returns {} rather than
    # costing a deliverable (invariant 3)
    assert series_by_code(store, [], {}) == {}
    assert series_by_code(store, ["431234567"], "not a market") == {}
    assert series_by_code(store, ["431234567"], {"payers": 7}) == {}


def test_the_pitch_report_carries_the_trend_column_when_there_is_history(cfg, store):
    from mrfx.benchmark import compute_benchmark, render_pitch_report

    with store.rates_part_writer("a.json") as w:
        w.write_batch([_row("431234567", "1417594896", 40.0, month="2026-05"),
                       _row("431234567", "1417594896", 44.0, month="2026-06"),
                       _row("437654321", "1999999992", 50.0, month="2026-06")])
    store.save_npis_bulk([
        dict(npi=n, org_name=f"P{i}", entity_type="NPI-2",
             taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X",
             city="StL", state="MO", address="x", zip="63103", phone=None)
        for i, n in enumerate(("1417594896", "1999999992"))])
    store.rebuild_rollups()

    b = compute_benchmark(store, "431234567",
                          {"month": "2026-06", "state": "MO", "therapy_only": False})
    html = render_pitch_report(cfg, store, b)
    assert "Your trend" in html and "<svg" in html
    assert "not interpolated" in html or "is a gap, not a flat segment" in html


# ------------------------------------------------- NPPES weekly incrementals

_NPPES_HEADER = [
    "NPI", "Entity Type Code", "Provider Organization Name (Legal Business Name)",
    "Provider First Name", "Provider Last Name (Legal Name)",
    "Provider First Line Business Practice Location Address",
    "Provider Business Practice Location Address City Name",
    "Provider Business Practice Location Address State Name",
    "Provider Business Practice Location Address Postal Code",
    "Provider Business Practice Location Address Telephone Number",
    "Provider Enumeration Date", "NPI Deactivation Date", "NPI Reactivation Date",
    "Healthcare Provider Taxonomy Code_1",
]


def _nppes_csv(tmp_path, rows, name="npidata_pfile_weekly.csv"):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_NPPES_HEADER)
    for r in rows:
        w.writerow(r)
    p = tmp_path / name
    p.write_text(buf.getvalue())
    return p


def _nppes_row(npi, name, *, deact="", city="StL"):
    return [npi, "2", name, "", "", "1 Main St", city, "MO", "63103",
            "3145551212", "01/01/2015", deact, "", "261QP2000X"]


def test_a_weekly_incremental_updates_only_what_it_contains(cfg, store, tmp_path):
    """A weekly is authoritative for the NPIs it carries and says nothing about
    the ones it omits — dropping those would gut the directory."""
    from mrfx.enrich import _nppes_cache_path, apply_weekly_update

    monthly = _nppes_csv(tmp_path, [
        _nppes_row("1417594896", "Old Name Therapy"),
        _nppes_row("1999999992", "Untouched Therapy"),
    ], name="npidata_pfile_monthly.csv")
    cfg.enrichment.bulk_csv_path = monthly
    from mrfx.enrich import _write_nppes_parquet
    assert _write_nppes_parquet(cfg, store, _nppes_cache_path(store), None) == 2

    weekly = _nppes_csv(tmp_path, [
        _nppes_row("1417594896", "Renamed Therapy"),            # changed
        _nppes_row("1215555554", "Brand New Therapy"),          # new
        _nppes_row("1023456789", "Closed Therapy", deact="07/15/2026"),
    ])
    res = apply_weekly_update(cfg, store, weekly)
    assert res["rows_in_weekly"] == 3
    assert res["new_npis"] == 2 and res["updated_npis"] == 1
    assert res["deactivations_in_file"] == 1
    assert res["cache_rows_before"] == 2 and res["cache_rows_after"] == 4

    import duckdb
    con = duckdb.connect()
    names = dict(con.execute(
        f"SELECT npi, org_name FROM read_parquet('{_nppes_cache_path(store)}')"
    ).fetchall())
    con.close()
    assert names["1417594896"] == "Renamed Therapy", "the weekly wins for its own NPI"
    assert names["1999999992"] == "Untouched Therapy", "and never touches the rest"
    assert names["1215555554"] == "Brand New Therapy"


def test_a_weekly_without_a_cache_refuses_rather_than_becoming_the_directory(cfg, store, tmp_path):
    from mrfx.enrich import WeeklyUpdateError, apply_weekly_update

    weekly = _nppes_csv(tmp_path, [_nppes_row("1417594896", "Some Therapy")])
    with pytest.raises(WeeklyUpdateError, match="no local NPPES cache"):
        apply_weekly_update(cfg, store, weekly)


def test_a_wrong_file_as_a_weekly_is_refused(cfg, store, tmp_path):
    from mrfx.enrich import (WeeklyUpdateError, _nppes_cache_path,
                             _write_nppes_parquet, apply_weekly_update)

    monthly = _nppes_csv(tmp_path, [_nppes_row("1417594896", "A Therapy")],
                         name="npidata_pfile_monthly.csv")
    cfg.enrichment.bulk_csv_path = monthly
    _write_nppes_parquet(cfg, store, _nppes_cache_path(store), None)

    with pytest.raises(WeeklyUpdateError, match="no such file"):
        apply_weekly_update(cfg, store, tmp_path / "nope.zip")
    empty = _nppes_csv(tmp_path, [], name="header_only.csv")
    with pytest.raises(WeeklyUpdateError, match="no NPI rows"):
        apply_weekly_update(cfg, store, empty)
    # and the cache the refusal protected is intact
    import duckdb
    con = duckdb.connect()
    assert con.execute(
        f"SELECT count(*) FROM read_parquet('{_nppes_cache_path(store)}')"
    ).fetchone()[0] == 1
    con.close()


def test_a_non_finite_value_never_reaches_the_svg_path():
    """Found by the adversarial harness: NaN/inf rates wrote literal 'nan' into
    the SVG `d` attribute — a visibly broken picture inside a client report.
    Non-finite points are dropped; a span that overflows draws nothing."""
    nan, inf = float("nan"), float("inf")
    for pts in ([("2026-01", 40.0), ("2026-02", nan)],
                [("2026-01", nan), ("2026-02", nan)],
                [("2026-01", 40.0), ("2026-02", inf)],
                [("2026-01", -inf), ("2026-02", 40.0)],
                [("2026-01", -1e308), ("2026-02", 1e308)]):
        out = sparkline(pts)
        assert "nan" not in out.lower() or "<svg" not in out, out
        assert "inf" not in out.split("aria-label")[0].lower() or "<svg" not in out
    # three points, one poisoned: the two good ones still draw
    out = sparkline([("2026-01", 40.0), ("2026-02", nan), ("2026-03", 44.0)])
    assert "<svg" in out and "nan" not in out.lower()
