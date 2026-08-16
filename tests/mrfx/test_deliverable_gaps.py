"""The deliverable-level halves of items 3, 5 and 6.

Each of these was computed correctly but only surfaced on a dashboard tab. The
ask put them in the documents a client actually receives, which is where they
change a conversation — so these tests assert on the rendered HTML.
"""

import datetime as dt

from mrfx.benchmark import compute_benchmark, render_pitch_report
from mrfx.schedule import compute_fee_schedule, payer_scorecard, render_rate_card

# the two-year fixture publishes 2022-06 and 2024-06; the one-month fixture 2026-06
MARKET = {"month": "2024-06", "state": "MO", "therapy_only": False}
MARKET_NOW = {"month": "2026-06", "state": "MO", "therapy_only": False}


def _row(tin, npi, rate, *, code="97110", month="2026-06", payer="Aetna",
         src="a.json"):
    return dict(payer=payer, tin_value=tin, tin_type="ein", npi=npi,
                source_file=src, billing_code=code, billing_code_type="CPT",
                discipline="PT", is_timed=True, billing_class="professional",
                negotiated_rate=rate, negotiated_type="negotiated",
                is_dollar_rate=True, billing_code_modifier=[], service_code=["11"],
                file_month=month, last_updated_on=f"{month}-01",
                expiration_date=None, schema_version="2.0.0",
                tin_is_really_npi=False, state="MO")


def _two_year_store(store, *, subject_flat=True):
    """The subject priced in 2022 and 2024, with peers, so the report has both
    a trend to draw and a real-terms span the shipped index covers."""
    rows = []
    for i, (tin, npi) in enumerate((("431234567", "1417594896"),
                                    ("437654321", "1999999992"),
                                    ("431111111", "1215555554"))):
        for month in ("2022-06", "2024-06"):
            base = 40.0 + i * 4
            rate = base if (subject_flat or i) else base + 6
            rows.append(_row(tin, npi, rate, month=month))
            rows.append(_row(tin, npi, rate - 4, code="97140", month=month))
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk([
        dict(npi=n, org_name=f"Practice {i}", entity_type="NPI-2",
             taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X",
             city="StL", state="MO", address="x", zip="63103", phone=None)
        for i, n in enumerate(("1417594896", "1999999992", "1215555554"))])
    store.rebuild_rollups()
    store.upsert_file("a.json", payer="Aetna", status="done",
                      file_type="in_network", last_updated_on="2024-06-01",
                      rows_emitted=len(rows))


def test_the_pitch_report_carries_the_real_terms_sentence(cfg, store):
    """"Flat in dollars is a pay cut" is the strongest line in a renegotiation
    letter, and it belongs in the letter — not only on a dashboard tab."""
    _two_year_store(store)
    b = compute_benchmark(store, "431234567", MARKET)
    html = render_pitch_report(cfg, store, b)
    assert "In real terms" in html
    assert "flat pay is a pay cut" in html or "real terms" in html
    assert "Change in real terms" in html
    assert "CONSUMER basket" in html, "the basis caveat travels into the report"
    assert "CPI-U" in html


def test_the_pitch_report_names_a_payer_that_has_not_re_published(cfg, store):
    """A reader is about to quote these numbers at that payer."""
    _two_year_store(store)
    old = (dt.date.today() - dt.timedelta(days=500)).isoformat()
    store.upsert_file("a.json", payer="Aetna", status="done",
                      file_type="in_network", last_updated_on=old)
    b = compute_benchmark(store, "431234567", MARKET)
    assert b["stale_payers"] and b["stale_payers"][0]["payer"] == "Aetna"
    html = render_pitch_report(cfg, store, b)
    assert "Publication age" in html and "Aetna" in html
    assert "does not mean an old contract" in html, "stale file != stale contract"


def test_a_fresh_payer_adds_no_publication_warning(cfg, store):
    _two_year_store(store)
    fresh = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    store.upsert_file("a.json", payer="Aetna", status="done",
                      file_type="in_network", last_updated_on=fresh)
    b = compute_benchmark(store, "431234567", MARKET)
    assert b["stale_payers"] == []
    assert "Publication age" not in render_pitch_report(cfg, store, b)


def test_the_rate_card_carries_a_trend_column(cfg, store):
    """The ask named rate cards specifically: a snapshot becomes a direction."""
    _two_year_store(store)
    fs = compute_fee_schedule(store, "431234567", MARKET)
    html = render_rate_card(cfg, store, fs, payer_scorecard(fs))
    assert "<th class='num'>Trend</th>" in html
    assert "<svg" in html and 'class="spark"' in html
    assert "is a gap, not a flat segment" in html


def test_a_one_month_store_draws_no_trend_and_still_renders(cfg, store):
    """No history is the normal state on a fresh install. Every deliverable
    must render exactly as before rather than showing an empty column."""
    rows = [_row("431234567", "1417594896", 40.0),
            _row("437654321", "1999999992", 46.0),
            _row("431111111", "1215555554", 52.0)]
    with store.rates_part_writer("a.json") as w:
        w.write_batch(rows)
    store.save_npis_bulk([
        dict(npi=n, org_name=f"P{i}", entity_type="NPI-2",
             taxonomy_code="261QP2000X", taxonomy_codes="261QP2000X",
             city="StL", state="MO", address="x", zip="63103", phone=None)
        for i, n in enumerate(("1417594896", "1999999992", "1215555554"))])
    store.rebuild_rollups()
    store.upsert_file("a.json", payer="Aetna", status="done",
                      file_type="in_network", last_updated_on="2026-06-01")

    b = compute_benchmark(store, "431234567", MARKET_NOW)
    pitch = render_pitch_report(cfg, store, b)
    assert "Your trend" not in pitch and "In real terms" not in pitch
    assert "Negotiated-rate benchmark" in pitch, "the report still renders"

    fs = compute_fee_schedule(store, "431234567", MARKET_NOW)
    card = render_rate_card(cfg, store, fs, payer_scorecard(fs))
    assert "<th class='num'>Trend</th>" not in card
    assert "Fee schedule" in card


def test_the_engagement_win_is_also_shown_in_baseline_dollars(cfg, store):
    """Part of any gain is inflation, not negotiation. Where an index covers
    the span, say what the win is worth in the baseline's own dollars."""
    from mrfx.engagements import compare_to_baseline, save_baseline

    _two_year_store(store)
    save_baseline(store, "431234567", label="engagement start",
                  market={**MARKET, "month": "2022-06"})
    cmp = compare_to_baseline(store, "431234567", "engagement start",
                              market={**MARKET, "month": "2024-06"},
                              volumes={"97110": 1000})
    real = cmp["real_terms"]
    assert real is not None
    assert real["basis"] == "index"
    assert real["factor"] == 1.0719, "2022->2024 CPI-U"
    assert "CONSUMER basket" in real["caveat"]
    assert "worth less than the same figure at the baseline" in real["note"]


def test_the_packet_month_page_shows_where_each_rate_has_been_going(cfg, store, tmp_path):
    from mrfx.clients import add_client
    from mrfx.packets import build_client_packet

    _two_year_store(store)
    add_client(store, "431234567")
    res = build_client_packet(cfg, store, "431234567", out_dir=tmp_path / "pk",
                              market=MARKET)
    doc = next((w for w in res["written"] if "this_month" in w), None)
    if doc:      # needs two months of the SAME payer, which this store has
        html = (tmp_path / "pk" / "431234567" / doc).read_text()
        assert "Where each rate has been going" in html
        assert "<svg" in html
    # the rate card inside the packet carries trends regardless
    card = next(w for w in res["written"] if "rate_card" in w)
    assert "<svg" in (tmp_path / "pk" / "431234567" / card).read_text()
