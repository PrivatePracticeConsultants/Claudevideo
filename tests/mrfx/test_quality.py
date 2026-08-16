"""Rate-quality signals: what kind of rate, how old, and does size explain it.

These all describe the DATA, not the practice, and the failure mode for each is
the same: stating a weak signal as a strong finding. So the tests check the
thin-data guards as hard as they check the arithmetic.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from mrfx.api import create_app
from mrfx.quality import (code_type_flag, payer_freshness, rate_type_mix,
                          size_premium)

MARKET = {"month": "2026-06", "therapy_only": False}


def _row(tin, npi, rate, *, payer="Aetna", code="97110", ntype="negotiated",
         src="a.json"):
    return dict(payer=payer, tin_value=tin, tin_type="ein", npi=npi,
                source_file=src, billing_code=code, billing_code_type="CPT",
                discipline="PT", is_timed=True, billing_class="professional",
                negotiated_rate=rate, negotiated_type=ntype, is_dollar_rate=True,
                billing_code_modifier=[], service_code=["11"],
                file_month="2026-06", last_updated_on="2026-06-01",
                expiration_date=None, schema_version="2.0.0",
                tin_is_really_npi=False, state="MO")


def _npis(store, pairs):
    store.save_npis_bulk([
        dict(npi=n, org_name=nm, entity_type="NPI-2", taxonomy_code="261QP2000X",
             taxonomy_codes="261QP2000X", city="StL", state="MO", address="x",
             zip="63103", phone=None) for n, nm in pairs])


def test_derived_rates_are_reported_not_silently_filtered(cfg, store):
    """Excluding them by default would change every number the user has already
    seen. The composition IS the finding."""
    rows = [_row("431234567", "1417594896", 40.0),
            _row("437654321", "1999999992", 44.0),
            _row("431111111", "1215555554", 60.0, ntype="derived"),
            _row("432222222", "1023456789", 62.0, ntype="derived")]
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    _npis(store, [("1417594896", "A"), ("1999999992", "B"),
                  ("1215555554", "C"), ("1023456789", "D")])
    store.rebuild_rollups()

    mix = rate_type_mix(store, MARKET)
    by = {r["rate_type"]: r for r in mix["rows"]}
    assert by["negotiated"]["n_rows"] == 2 and by["negotiated"]["contracted"] is True
    assert by["derived"]["n_rows"] == 2 and by["derived"]["contracted"] is False
    assert mix["derived_pct"] == 50.0 and mix["flag"] is True
    assert "not contracted amounts" in mix["headline"]
    assert mix["by_payer"][0]["pct_contracted"] == 50.0

    # and the market median still includes them — nothing changed underneath.
    # The subject (431234567, $40) is excluded from its own peer set, so the
    # peers are 44, 60, 62 and their median is 60.
    from mrfx.benchmark import compute_benchmark
    b = compute_benchmark(store, "431234567", MARKET)
    assert next(r for r in b["rows"] if r["billing_code"] == "97110")["p50"] == 60.0


def test_a_practices_own_derived_rates_change_what_the_ask_is(cfg, store):
    with store.rates_part_writer("a.json") as w:
        w.write_batch([_row("431234567", "1417594896", 40.0, ntype="derived"),
                       _row("431234567", "1417594896", 38.0, code="97140",
                            ntype="derived"),
                       _row("437654321", "1999999992", 44.0)])
    _npis(store, [("1417594896", "A"), ("1999999992", "B")])
    store.rebuild_rollups()

    f = code_type_flag(store, MARKET, "431234567")
    assert f["derived_pct"] == 100.0 and f["flag"] is True
    assert "ask for a contracted schedule" in f["headline"]
    assert {r["billing_code"] for r in f["rows"]} == {"97110", "97140"}

    clean = code_type_flag(store, MARKET, "437654321")
    assert clean["derived_pct"] == 0.0 and clean["flag"] is False


def test_freshness_separates_stale_from_unreadable_from_unknown(cfg, store):
    """A date we cannot parse must not pass as fresh — and must not be called
    stale either. It is unknown, and says so."""
    with store.rates_part_writer("a.json") as w:
        w.write_batch([_row("431234567", "1417594896", 40.0)])
    store.rebuild_rollups()
    old = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    new = (dt.date.today() - dt.timedelta(days=10)).isoformat()
    store.upsert_file("a.json", payer="Aetna", status="done",
                      file_type="in_network", last_updated_on=new, rows_emitted=1)
    store.upsert_file("b.json", payer="Sleepy Health", status="done",
                      file_type="in_network", last_updated_on=old, rows_emitted=1)
    store.upsert_file("c.json", payer="Vague Health", status="done",
                      file_type="in_network", last_updated_on="soon", rows_emitted=1)
    store.upsert_file("d.json", payer="Silent Health", status="done",
                      file_type="in_network", last_updated_on=None, rows_emitted=1)

    fr = payer_freshness(store)
    by = {r["payer"]: r for r in fr["payers"]}
    assert by["Aetna"]["stale"] is False and by["Aetna"]["age_days"] == 10
    assert by["Sleepy Health"]["stale"] is True
    assert by["Vague Health"]["stale"] is False, "unreadable is not stale"
    assert by["Vague Health"]["date_unreadable"] is True
    assert by["Silent Health"]["published"] is None
    assert fr["n_stale"] == 1 and fr["n_unknown"] == 2
    assert "Sleepy Health" in fr["headline"]


def _size_market(store):
    """Two size bands, same payer and code, with the larger practices paid more.
    Six practices per band so neither band trips the thin guard."""
    rows, npis = [], []
    npi = 1417594896
    for i in range(6):                       # solo practices, paid 40-45
        tin = f"43100000{i}"
        n = str(npi + i)
        rows.append(_row(tin, n, 40.0 + i))
        npis.append((n, f"Solo {i}"))
    for i in range(6):                       # 5-provider groups, paid 60-65
        tin = f"43200000{i}"
        for j in range(5):
            n = str(npi + 100 + i * 10 + j)
            rows.append(_row(tin, n, 60.0 + i))
            npis.append((n, f"Group {i}"))
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    _npis(store, npis)
    store.rebuild_rollups()


def test_a_real_size_premium_is_measured_within_payer_and_code(cfg, store):
    _size_market(store)
    sp = size_premium(store, MARKET)
    by = {b["band"]: b for b in sp["bands"]}
    assert by["solo (1 provider)"]["n_practices"] == 6
    assert by["group (5-14)"]["n_practices"] == 6
    assert by["solo (1 provider)"]["avg_percentile"] < \
        by["group (5-14)"]["avg_percentile"], "the larger band is paid better here"
    assert sp["spread_points"] and sp["spread_points"] > 5
    assert "percentile points above" in sp["headline"]
    # bands nobody is in are listed as thin, not omitted or invented
    assert by["large (15+)"]["thin"] is True
    assert by["large (15+)"]["avg_percentile"] is None


def test_no_size_premium_is_stated_as_an_answer_to_the_objection(cfg, store):
    """'The practices above me are bigger' is the commonest push-back on a
    benchmark. When it is false, the app has to say so plainly."""
    rows, npis = [], []
    npi = 1417594896
    for i in range(6):
        tin, n = f"43100000{i}", str(npi + i)
        rows.append(_row(tin, n, 40.0 + i))
        npis.append((n, f"Solo {i}"))
    for i in range(6):
        tin = f"43200000{i}"
        for j in range(5):
            n = str(npi + 100 + i * 10 + j)
            rows.append(_row(tin, n, 40.0 + i))   # SAME rates as the solos
            npis.append((n, f"Group {i}"))
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    _npis(store, npis)
    store.rebuild_rollups()

    sp = size_premium(store, MARKET)
    assert abs(sp["spread_points"]) < 5
    assert "No size premium" in sp["headline"]
    assert "does not explain a rate gap" in sp["headline"]


def test_a_thin_band_is_never_reported_as_a_finding(cfg, store):
    """Three practices in a band is the same sin as a two-practice 'market'."""
    rows, npis = [], []
    npi = 1417594896
    for i in range(6):
        tin, n = f"43100000{i}", str(npi + i)
        rows.append(_row(tin, n, 40.0 + i))
        npis.append((n, f"Solo {i}"))
    for i in range(2):                        # only 2 groups: below min
        tin = f"43200000{i}"
        for j in range(5):
            n = str(npi + 100 + i * 10 + j)
            rows.append(_row(tin, n, 80.0))
            npis.append((n, f"Group {i}"))
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    _npis(store, npis)
    store.rebuild_rollups()

    sp = size_premium(store, MARKET)
    by = {b["band"]: b for b in sp["bands"]}
    assert by["group (5-14)"]["thin"] is True
    assert by["group (5-14)"]["avg_percentile"] is None
    assert sp["spread_points"] is None
    assert "Not enough practices" in sp["headline"]


def test_the_subject_is_placed_against_others_its_own_size(cfg, store):
    _size_market(store)
    sp = size_premium(store, MARKET, subject="431000000")
    assert sp["subject"]["npi_count"] == 1
    assert sp["subject"]["band"] == "solo (1 provider)"
    assert sp["subject"]["vs_band"] is not None
    assert "others its size" in sp["headline"]


def test_the_endpoints_answer_and_refuse_cleanly(cfg, store):
    _size_market(store)
    c = TestClient(create_app(cfg, store))
    assert c.post("/api/quality/types", json={"market": MARKET}).json()["total_rows"] > 0
    assert c.get("/api/quality/freshness").status_code == 200
    assert c.post("/api/quality/size", json={"market": MARKET}).json()["bands"]
    assert c.post("/api/quality/subject-types",
                  json={"market": MARKET, "subject": "431000000"}
                  ).json()["derived_pct"] == 0.0
    # hostile input is a refusal, never a 500
    for body in ({"market": "everything"}, {"market": {"month": 5}},
                 {"market": MARKET, "subject": "nobody at all"}):
        assert c.post("/api/quality/size", json=body).status_code in (200, 422)
    assert c.post("/api/quality/size",
                  json={"market": MARKET, "subject": "nobody at all"}
                  ).status_code == 422
    assert c.get("/api/quality/freshness?stale_days=0").status_code == 200


def test_a_thin_subject_is_a_cant_say_not_a_bad_name(cfg, store):
    """A real practice whose every cell is too thin to rank, and a typo, both
    produce no rows. Conflating them would either hide a typo or accuse a real
    client of not existing."""
    from mrfx.benchmark import BenchmarkError

    # one practice per (payer, code) cell: nothing to rank against
    with store.rates_part_writer("a.json") as w:
        w.write_batch([_row("431234567", "1417594896", 40.0),
                       _row("437654321", "1999999992", 44.0, code="97140")])
    _npis(store, [("1417594896", "A"), ("1999999992", "B")])
    store.rebuild_rollups()

    sp = size_premium(store, MARKET, subject="431234567")
    assert sp["subject"]["avg_percentile"] is None
    assert "too thin to place it by size" in sp["subject"]["reason"]

    with pytest.raises(BenchmarkError, match="check the practice name"):
        size_premium(store, MARKET, subject="Not A Real Practice")


def test_negotiated_and_fee_schedule_are_never_collapsed(cfg, store):
    """'negotiated' and 'fee schedule' are both contracted amounts, but they say
    OPPOSITE things about whether a negotiation is worth opening. Treating them
    as one value — which the first cut of this module did — throws away the most
    actionable fact in the column."""
    from mrfx.quality import payer_posture

    rows, npis = [], []
    npi = 1417594896
    # Dealmaker: negotiated_type='negotiated' AND actually pays differently
    for i in range(6):
        rows.append(_row(f"43100000{i}", str(npi + i), 40.0 + i, payer="Dealmaker"))
        npis.append(str(npi + i))
    for i in range(6):
        rows.append(_row(f"43100000{i}", str(npi + i), 50.0 + i,
                         payer="Dealmaker", code="97140"))
    for i in range(6):
        rows.append(_row(f"43100000{i}", str(npi + i), 30.0 + i,
                         payer="Dealmaker", code="97112"))
    # Takeitorleaveit: negotiated_type='fee schedule' AND one rate for everyone
    for i in range(6):
        rows.append(_row(f"43200000{i}", str(npi + 100 + i), 44.0,
                         payer="Takeitorleaveit", ntype="fee schedule"))
        npis.append(str(npi + 100 + i))
    for i in range(6):
        rows.append(_row(f"43200000{i}", str(npi + 100 + i), 39.0,
                         payer="Takeitorleaveit", ntype="fee schedule", code="97140"))
    for i in range(6):
        rows.append(_row(f"43200000{i}", str(npi + 100 + i), 33.0,
                         payer="Takeitorleaveit", ntype="fee schedule", code="97112"))
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    _npis(store, [(n, f"C{n[-3:]}") for n in npis])
    store.rebuild_rollups()

    p = {x["payer"]: x for x in payer_posture(store, MARKET)["payers"]}
    d, t = p["Dealmaker"], p["Takeitorleaveit"]

    assert d["claimed"] == "negotiates" and d["pct_negotiated"] == 100.0
    assert t["claimed"] == "standard fee schedule" and t["pct_fee_schedule"] == 100.0
    assert d["observed"] == "prices practices differently"
    assert t["observed"] == "one rate for everyone"
    assert d["winnable"] is True, "a negotiation worth opening"
    assert t["winnable"] is False, "expect little movement"
    assert "expect little movement" in t["verdict"]

    res = payer_posture(store, MARKET)
    assert res["n_winnable"] == 1 and "Dealmaker" in res["headline"]


def test_a_payers_claim_and_its_behaviour_can_disagree(cfg, store):
    """A payer calling every rate a 'fee schedule' while paying practices
    differently IS making exceptions. Reporting only the label would hide that,
    and reporting only the spread would ignore what the payer said."""
    from mrfx.quality import payer_posture

    rows, npis = [], []
    npi = 1417594896
    for i in range(6):
        rows.append(_row(f"43100000{i}", str(npi + i), 40.0 + i * 3,
                         payer="SaysFixed", ntype="fee schedule"))
        rows.append(_row(f"43100000{i}", str(npi + i), 30.0 + i * 2,
                         payer="SaysFixed", ntype="fee schedule", code="97140"))
        rows.append(_row(f"43100000{i}", str(npi + i), 20.0 + i,
                         payer="SaysFixed", ntype="fee schedule", code="97112"))
        npis.append(str(npi + i))
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    _npis(store, [(n, f"C{n[-3:]}") for n in npis])
    store.rebuild_rollups()

    p = payer_posture(store, MARKET)["payers"][0]
    assert p["claimed"] == "standard fee schedule"
    assert p["observed"] == "prices practices differently"
    assert p["conflict"] is True
    assert "it does make exceptions" in p["verdict"]
    assert "CLAIM" in payer_posture(store, MARKET)["note"], (
        "the note has to say the label is a claim and the spread is evidence")


def test_a_payer_with_too_little_shared_data_is_not_judged(cfg, store):
    """One practice per code cannot show dispersion either way. Calling that
    'one rate for everyone' would invent the finding."""
    from mrfx.quality import payer_posture

    with store.rates_part_writer("a.json") as w:
        w.write_batch([_row("431234567", "1417594896", 40.0, payer="Tiny")])
    _npis(store, [("1417594896", "A")])
    store.rebuild_rollups()
    p = payer_posture(store, MARKET)["payers"][0]
    assert p["thin"] is True and p["observed"] is None and p["winnable"] is None
    assert "too few shared codes to check" in p["verdict"]
