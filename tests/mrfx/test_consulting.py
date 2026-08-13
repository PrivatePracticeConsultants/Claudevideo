"""The consulting workflow: renewals, underpayment checks, win-tracking,
new-to-network, monthly packets, monthly auto-refresh."""

import datetime as dt

import pytest

from mrfx.benchmark import BenchmarkError

GW = "431234567"
GW_N = ["1417594896", "1234567893"]
PEERS = [("437654321", "1999999992"), ("434440001", "1876543219"),
         ("434440002", "1765432196"), ("434440003", "1654321987"),
         ("434440004", "1543219876")]
NEWBIE = ("438889990", "1432198765")


def _row(**kw):
    base = dict(payer="Aetna", tin_value=GW, tin_type="ein", npi=GW_N[0],
                source_file="s.json", billing_code="97110", billing_code_type="CPT",
                discipline="PT", is_timed=True, billing_class="professional",
                negotiated_rate=30.0, negotiated_type="negotiated",
                is_dollar_rate=True, billing_code_modifier=[], service_code=["11"],
                file_month="2026-06", last_updated_on="2026-06-01",
                expiration_date=None, schema_version="2.0.0",
                tin_is_really_npi=False, state=None)
    base.update(kw)
    return base


def _seed(store, *, months=("2026-05", "2026-06"), with_expiry=False,
          newbie_month=None, bump_month="2026-06"):
    """Gateway + 5 peers across two months. Optionally: real expiration dates,
    and a practice that first appears in the newer month (new to network)."""
    soon = (dt.date.today() + dt.timedelta(days=45)).isoformat()
    rows = []
    for month in months:
        members = [(GW, GW_N[0]), (GW, GW_N[1]), *PEERS]
        if newbie_month and month == newbie_month:
            members.append(NEWBIE)
        for tin, npi in members:
            mult = 1.0 if tin == GW else 1.1
            for payer in ("Aetna", "BCBS"):
                for code, base in (("97110", 30.0), ("97140", 28.0)):
                    rate = base * mult * (1.05 if payer == "BCBS" else 1.0)
                    # Gateway's Aetna 97110 rises 10% in bump_month — named
                    # explicitly so seeding one month can't accidentally apply it
                    if (tin, payer, code) == (GW, "Aetna", "97110") and month == bump_month:
                        rate *= 1.10
                    rows.append(_row(
                        payer=payer, tin_value=tin, npi=npi, billing_code=code,
                        negotiated_rate=round(rate, 2), file_month=month,
                        source_file=f"s_{month}.json",
                        last_updated_on=f"{month}-01",
                        expiration_date=(soon if with_expiry and payer == "Aetna"
                                         else ("9999-12-31" if with_expiry else None))))
    for month in months:
        with store.rates_part_writer(f"s_{month}.json") as w:
            w.write_batch([r for r in rows if r["file_month"] == month])
    directory = [dict(npi=n, org_name="Gateway Therapy", entity_type="NPI-2",
                      taxonomy_code="261QP2000X", city="StL", state="MO",
                      address="1 Main", zip="63103", phone=None) for n in GW_N]
    directory += [dict(npi=n, org_name=f"Peer {i}", entity_type="NPI-2",
                       taxonomy_code="261QP2000X", city="StL", state="MO",
                       address="2 Oak", zip="63103", phone=None)
                  for i, (_t, n) in enumerate(PEERS)]
    directory.append(dict(npi=NEWBIE[1], org_name="Brand New PT", entity_type="NPI-2",
                          taxonomy_code="261QP2000X", city="StL", state="MO",
                          address="3 Elm", zip="63103", phone=None))
    store.save_npis_bulk(directory)
    store.rebuild_rollups()


# ------------------------------------------------------------- renewals --

def test_renewal_radar_states_its_coverage_and_ignores_placeholders(store):
    """Payers publish expiration_date inconsistently. A 9999-12-31 placeholder
    is NOT a renewal, and a radar that counted it would invent a schedule."""
    from mrfx.contracts import expiration_coverage, renewal_radar

    _seed(store, with_expiry=True)
    cov = expiration_coverage(store, {"month": "latest"})
    # half the rows (Aetna) carry a real date; BCBS carries 9999-12-31
    assert 0 < cov["with_expiry"] < cov["rows"]
    assert 40 <= cov["pct"] <= 60, cov
    assert "9999" in cov["note"] or "placeholder" in cov["note"]

    rad = renewal_radar(store, [GW], {"month": "latest"}, within_days=365)
    payers = {r["payer"] for r in rad["rows"]}
    assert payers == {"Aetna"}, "the 9999 placeholder must never appear as a renewal"
    assert rad["rows"][0]["days"] <= 60
    assert rad["coverage"]["pct"] == cov["pct"], "the radar carries its own coverage"

    # a store with NO usable dates returns an empty radar WITH the explanation,
    # never a silent blank
    _seed(store, months=("2026-07",))          # no expiry dates at all
    rad2 = renewal_radar(store, None, {"month": "2026-07"})
    assert rad2["rows"] == [] and rad2["coverage"]["with_expiry"] == 0


# -------------------------------------------------------- underpayments --

def test_underpayment_check_flags_only_real_shortfalls(store):
    from mrfx.remits import check_underpayments, parse_remit, underpayment_csv

    _seed(store, months=("2026-06",), bump_month=None)
    # Gateway/Aetna published: 97110 = 30.00, 97140 = 28.00 (no bump here)
    text = ("code,payer,allowed_amount,units\n"
            "97110,Aetna,28.50,1\n"        # short by 1.50
            "97140,Aetna,28.00,1\n"        # exactly right
            "97110,Aetna,60.00,2\n"        # 2 units, exactly right
            "97110,Aetna,55.00,2\n"        # 2 units, short by 5.00
            "99999,Aetna,10.00,1\n"        # not priced -> unmatched, not "fine"
            "garbage line without numbers\n")
    r = check_underpayments(store, GW, text, {"month": "2026-06"})
    short = {(x["billing_code"], x["units"]): x["shortfall"] for x in r["flagged"]}
    assert short == {("97110", 1.0): 1.5, ("97110", 2.0): 5.0}
    assert r["summary"]["total_shortfall"] == 6.5
    assert len(r["ok"]) == 2
    assert [x["billing_code"] for x in r["unmatched"]] == ["99999"]
    assert r["problems"], "an unreadable line is REPORTED, never dropped silently"
    assert "MPPR" in r["caveat"] and "deductible" in r["caveat"]

    # the paid-vs-allowed distinction is stated, because it is the most common
    # misreading (patient responsibility makes every line look short)
    paid = check_underpayments(store, GW, text, {"month": "2026-06"}, basis="paid")
    assert "deductible" in paid["summary"]["basis_note"]

    csv_text = underpayment_csv(r)
    assert "UNDER" in csv_text and "unmatched" in csv_text and "NOTE:" in csv_text

    # loose format with no header row still parses
    rows, _ = parse_remit("97110, Aetna, 28.50\n97140, Aetna, 26.00")
    assert [x["billing_code"] for x in rows] == ["97110", "97140"]
    with pytest.raises(BenchmarkError):
        check_underpayments(store, GW, "   ", {"month": "2026-06"})
    with pytest.raises(BenchmarkError, match="no published rates"):
        check_underpayments(store, "Nobody At All", text, {"month": "2026-06"})


# --------------------------------------------------------- win tracking --

def test_baseline_is_frozen_and_the_comparison_is_honest(store):
    """The baseline must be a STORED snapshot: recomputing it from today's data
    would let a later ingest quietly rewrite history and inflate the win."""
    from mrfx.engagements import (compare_to_baseline, list_baselines,
                                  save_baseline)

    _seed(store, months=("2026-05",), bump_month=None)
    saved = save_baseline(store, GW, {"month": "2026-05"})
    assert saved["codes"] == 2 and saved["replaced"] is False
    assert [b["subject"] for b in list_baselines(store)] == [GW]

    # now the newer month lands, with Gateway's Aetna 97110 up 10%.
    # The benchmark's subject rate is the entity rate ACROSS payers, so the
    # hand-checkable numbers are the medians of (Aetna, BCBS):
    #   2026-05: median(30.00, 31.50) = 30.75
    #   2026-06: median(33.00, 31.50) = 32.25   (Aetna bumped 10%)
    _seed(store, months=("2026-05", "2026-06"), bump_month="2026-06")
    cmp = compare_to_baseline(store, GW, volumes={"97110": 1000})
    by = {r["billing_code"]: r for r in cmp["rows"]}
    assert by["97110"]["before"] == 30.75
    assert by["97110"]["after"] == 32.25
    assert by["97110"]["delta"] == 1.5 and by["97110"]["delta_pct"] == 4.9
    assert by["97110"]["annual_value"] == 1500.0
    assert cmp["summary"]["total_annual_value"] == 1500.0
    assert cmp["summary"]["n_improved"] == 1
    # market context travels alongside, so a rising tide can't be sold as a win
    assert by["97110"]["market_before"] is not None
    assert "part of any gain is the market" in cmp["note"]

    with pytest.raises(BenchmarkError, match="no baseline"):
        compare_to_baseline(store, GW, label="nope")
    again = save_baseline(store, GW, {"month": "2026-06"})
    assert again["replaced"] is True


# ------------------------------------------------------ new to network --

def test_new_to_network_finds_only_genuinely_new_practices(store):
    from mrfx.contracts import new_to_network

    _seed(store, months=("2026-05", "2026-06"), newbie_month="2026-06")
    d = new_to_network(store, {"month": "2026-06"})
    names = {r["practice"] for r in d["rows"]}
    assert names == {"Brand New PT"}, "only the practice absent last month"
    assert all(r["prev_month"] == "2026-05" for r in d["rows"])
    assert "published book this month" in d["note"]

    # ZIP+radius clips it, and a far-away ZIP excludes it
    near = new_to_network(store, {"month": "2026-06"}, zip_code="63103", radius_miles=25)
    assert {r["practice"] for r in near["rows"]} == {"Brand New PT"}
    far = new_to_network(store, {"month": "2026-06"}, zip_code="99801", radius_miles=25)
    assert far["rows"] == []

    # one month only: an honest reason, not an empty table implying "none new"
    from mrfx.store import Store as _S
    single = new_to_network(store, {"month": "2026-05"})
    assert single["rows"] == [] and single["reason"]


def test_new_to_network_keeps_unidentified_practices(store):
    """A newly signed practice whose clinicians are not yet in the NPI
    directory must be KEPT and flagged, never silently dropped by the
    therapy filter — same rule as the Medicare leaderboard."""
    from mrfx.contracts import new_to_network

    _seed(store, months=("2026-05", "2026-06"), newbie_month="2026-06")
    with store.write_lock, store.connect() as con:
        con.execute("DELETE FROM npi_directory WHERE npi = ?", [NEWBIE[1]])
    d = new_to_network(store, {"month": "2026-06"}, therapy_only=True)
    assert len(d["rows"]) >= 1, "unenriched newbie must not vanish"
    row = d["rows"][0]
    assert row["unidentified"] is True
    assert "kept and flagged" in d["note"]

    # a prev_month no payer published in must not report the whole book as new
    empty = new_to_network(store, {"month": "2026-06"}, prev_month="2020-01")
    assert empty["rows"] == [] and "2020-01" in (empty["reason"] or "")


def test_remit_parser_survives_hostile_headers(store):
    """Real remit exports carry paid_date, denial_reason_code, unit_price…
    columns. Substring-matching those as the amount/code/units column would
    silently score garbage (a date '2026-01-05' reads as $2026)."""
    from mrfx.remits import parse_remit

    text = ("claim_id,paid_date,denial_reason_code,cpt,unit_price,units,paid_amount\n"
            "C1,2026-01-05,CO45,97110,15.00,2,57.00\n"
            "C2,2026-01-06,,97140,28.00,1,28.00\n")
    rows, problems = parse_remit(text)
    assert not problems
    assert [r["billing_code"] for r in rows] == ["97110", "97140"]
    assert [r["amount"] for r in rows] == [57.0, 28.0], \
        "amount must come from paid_amount, never paid_date"
    assert [r["units"] for r in rows] == [2.0, 1.0], \
        "units must come from the units column, never unit_price"


def test_baseline_labels_carry_a_concrete_month(store):
    """A baseline saved on the default 'latest' basis must label itself with
    the store's real newest month — 'as of latest' is meaningless the day
    after it is written."""
    from mrfx.engagements import compare_to_baseline, save_baseline

    _seed(store, months=("2026-05", "2026-06"))
    saved = save_baseline(store, GW, {"month": "latest"}, label="ui default")
    assert saved["month"] == "2026-06", saved
    cmp = compare_to_baseline(store, GW, label="ui default")
    assert cmp["baseline_month"] == "2026-06"
    assert cmp["current_month"] == "2026-06", "never the literal 'latest'"

    with pytest.raises(BenchmarkError, match="volumes"):
        compare_to_baseline(store, GW, label="ui default",
                            volumes={"97110": "lots"})


# ----------------------------------------------------------- packets --

def test_packets_are_written_per_client_and_omissions_explained(cfg, store, tmp_path):
    """Fault isolation: a client that no longer resolves must not cost the
    others their packets, and every omitted section says why."""
    from mrfx.clients import add_client
    from mrfx.packets import build_all_packets

    _seed(store, months=("2026-05", "2026-06"))
    add_client(store, GW)
    add_client(store, "Ghost Practice LLC")

    res = build_all_packets(cfg, store, tmp_path / "packets")
    assert res["clients"] == 2 and res["month"] == "2026-06"
    by = {p["subject"]: p for p in res["packets"]}
    gw = by[GW]
    assert "1_rate_card.html" in gw["written"]
    assert (tmp_path / "packets" / "2026-06" / "index.html").is_file()
    idx = (tmp_path / "packets" / "2026-06" / f"{GW}" / "index.html").read_text()
    assert "monthly packet" in idx and "2026-06" in idx
    card = (tmp_path / "packets" / "2026-06" / f"{GW}" / "1_rate_card.html").read_text()
    assert "METHODOLOGY" in card.upper()

    ghost = by["Ghost Practice LLC"]
    assert ghost["written"] == [] or "index.html" not in ghost["written"]
    assert ghost["skipped"], "an unresolvable client explains itself"
    ghost_idx = (tmp_path / "packets" / "2026-06" / "Ghost_Practice_LLC" /
                 "index.html").read_text()
    assert "Not included this month" in ghost_idx

    from mrfx.clients import remove_client
    remove_client(store, GW)
    remove_client(store, "Ghost Practice LLC")
    with pytest.raises(BenchmarkError, match="no clients saved"):
        build_all_packets(cfg, store, tmp_path / "p2")


# ------------------------------------------------- monthly auto-refresh --

def test_monthly_refresh_runs_once_per_month(cfg, store, monkeypatch, tmp_path):
    """Re-queueing on every boot would thrash the queue and the disk; the
    'already ran this month' marker is DURABLE (store meta), not in-memory."""
    import threading

    from mrfx import cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_something_owns_the_port", lambda *a, **k: False,
                        raising=False)
    import mrfx.fetch as fetch
    monkeypatch.setattr(fetch, "add_urls",
                        lambda st, urls: calls.append(list(urls)) or
                        {"added": len(urls), "skipped": 0, "invalid": 0})
    import mrfx.known_sources as ks
    monkeypatch.setattr(ks, "load_known_sources",
                        lambda p, today=None: [{"url": "https://x/toc.json",
                                                "queueable": True}])

    stop = threading.Event()

    def run_once():
        stop.clear()
        t = threading.Thread(target=cli._monthly_refresh_loop, args=(cfg, store, stop))
        t.start()
        # the loop queues immediately, then waits an hour — stop it right after
        for _ in range(200):
            if calls or store.meta_get("monthly_refresh_last"):
                break
            import time
            time.sleep(0.02)
        stop.set()
        t.join(timeout=10)

    run_once()
    assert len(calls) == 1, "first run of the month queues"
    assert store.meta_get("monthly_refresh_last") == dt.date.today().strftime("%Y-%m")
    run_once()
    assert len(calls) == 1, "a restart in the same month must NOT re-queue"

    store.meta_set("monthly_refresh_last", "2020-01")   # pretend a month passed
    run_once()
    assert len(calls) == 2, "a new month queues again"
