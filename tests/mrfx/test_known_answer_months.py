"""Each payer's own months — never the store's newest month (round 6, P0.1/P0.2).

The real store had one small payer publish a 76-row file dated a month ahead
of every other payer. Everything that took `max(file_month)` of the whole store
as "this month" then asked every other payer about a month it never published:
all five client packets came out empty, new-to-network listed 8 payers as
compared while comparing none, and the digest's rate changes saw one payer.

The fixture is the Leads known-answer store (Rho Health May + June, Sigma
Health June) plus that shape: Tiny Health publishes ONE file, dated 2026-07,
pricing one practice nobody else knows (T). By hand:
  - A's rates come from Rho 2026-06 and Sigma 2026-06 — never "2026-07".
  - Rho is the only payer with two files: compared 2026-05 -> 2026-06.
    Sigma and Tiny have one file each: not compared.
  - Rate changes, each payer's newest vs its previous: B's 97110 at Rho
    $20 -> $40 (+100%), the only matched line that moved.
  - New to network (Rho, 2026-05 -> 2026-06): E only, as before; Tiny's T is
    NOT "new" (Tiny has no earlier book to be new against).
  - New to network on the house basis: a newcomer whose only June line is
    institutional, or a $0.01 placeholder, is not a newly contracted rate.
"""

from __future__ import annotations

import json

import pytest

from tests.mrfx.test_known_answer_leads import TINS, leads_app  # noqa: F401  (fixture)

TINY = "590000001"


def _doc(payer, day, items):
    return {"reporting_entity_name": payer, "reporting_entity_type": "issuer",
            "last_updated_on": day, "version": "2.0.0", "provider_references": [],
            "in_network": [{
                "negotiation_arrangement": "ffs", "name": code, "billing_code_type": "CPT",
                "billing_code_type_version": "2026", "billing_code": code, "description": code,
                "negotiated_rates": [{"provider_groups": [{"npi": [npi], "tin": {"type": "ein", "value": tin}}],
                                      "negotiated_prices": [{
                                          "negotiated_type": "negotiated", "negotiated_rate": rate,
                                          "billing_class": cls, "service_code": ["11"],
                                          "billing_code_modifier": ["GP"],
                                          "expiration_date": "9999-12-31"}]}]}
                for code, tin, npi, rate, cls in items]}


@pytest.fixture
def ahead(cfg, store, leads_app):  # noqa: F811
    """The Leads store + a tiny payer one month ahead + two June newcomers at
    Rho that are not newly contracted RATES: institutional-only and a
    $0.01 placeholder."""
    from mrfx.ingest import scan_inbox
    for name, doc in (
            ("tiny_2026-07_in-network.json",
             _doc("Tiny Health", "2026-07-01", [("97110", TINY, "1590000001", 55.0, "professional")])),
            ("rho_extra_2026-06_in-network.json",
             _doc("Rho Health", "2026-06-01", [
                 ("97110", "580000001", "1580000001", 900.0, "institutional"),
                 ("97110", "580000002", "1580000002", 0.01, "professional")]))):
        (cfg.inbox_dir / name).write_text(json.dumps(doc))
    assert all(r["status"] == "done" for r in scan_inbox(cfg, store))
    store.rebuild_rollups()
    with store.connect() as con:
        assert con.execute("SELECT max(file_month) FROM rates_by_tin").fetchone()[0] == "2026-07"
    return leads_app


def test_rate_changes_per_payer(ahead):
    d = ahead.post("/api/changes", json={"market": {"month": "latest"}}).json()
    assert d["per_payer"] is True
    assert d["compared"] == [{"payer": "Rho Health", "new_month": "2026-06", "prev_month": "2026-05"}]
    assert {c["payer"] for c in d["not_compared"]} == {"Sigma Health", "Tiny Health"}
    got = [(c["display_name"], c["billing_code"], c["old_rate"], c["new_rate"], c["pct_change"],
            c["prev_month"], c["new_month"]) for c in d["changes"]]
    assert got == [("B REHAB", "97110", 20.0, 40.0, 100.0, "2026-05", "2026-06")]
    # 2026-07 picked: only Tiny published then, and Tiny has no earlier file —
    # nothing can be compared, and the answer says so instead of "No changes"
    # (review 10 item 2.4)
    r = ahead.post("/api/changes", json={"market": {"month": "2026-07"}})
    assert r.status_code == 422 and "nothing was compared" in r.json()["detail"]
    csv = ahead.post("/api/changes.csv", json={"market": {"month": "latest"}}).text
    assert "Rho Health 2026-05 -> 2026-06" in csv and ",2026-05,2026-06" in csv


def test_the_digest_sees_every_payer_s_own_months(ahead):
    for t in ("A", "B"):
        ahead.post("/api/clients", json={"subject": TINS[t]})
    d = ahead.get("/api/clients/digest").json()
    assert d["month"] == "latest" and d["can_diff"] is True
    by = {c["subject"]: c for c in d["clients"]}
    b = by[TINS["B"]]["changes"]
    assert (b["n"], b["biggest_pct"], b["biggest_payer"], b["biggest_span"]) == (
        1, 100.0, "Rho Health", "2026-05 → 2026-06")
    assert b["span"] == "Rho Health 2026-05 → 2026-06"
    a = by[TINS["A"]]["changes"]
    assert a["n"] == 0 and a["span"] == "Rho Health 2026-05 → 2026-06"
    assert by[TINS["A"]]["display_name"] == "A REHAB"


def test_packets_are_full_and_say_which_month_each_payer_is(cfg, store, ahead, tmp_path):
    from mrfx.packets import build_all_packets
    for t in ("A", "B"):
        ahead.post("/api/clients", json={"subject": TINS[t]})
    res = build_all_packets(cfg, store, tmp_path / "pk")
    assert res["month"] == "2026-06", "the clients' newest month, not the store's 2026-07"
    by = {p["subject"]: p for p in res["packets"]}
    a = by[TINS["A"]]
    assert "1_rate_card.html" in a["written"] and "3_this_month.html" in a["written"]
    assert a["vintage"] == [{"payer": "Rho Health", "month": "2026-06"},
                            {"payer": "Sigma Health", "month": "2026-06"}]
    idx = (tmp_path / "pk" / "2026-06" / TINS["A"] / "index.html").read_text()
    assert "Rho Health 2026-06, Sigma Health 2026-06" in idx and "2026-07" not in idx
    card = (tmp_path / "pk" / "2026-06" / TINS["A"] / "1_rate_card.html").read_text()
    assert "$30.00" in card and "$34.00" in card            # Rho and Sigma 97110
    month = (tmp_path / "pk" / "2026-06" / TINS["B"] / "3_this_month.html").read_text()
    assert "between each payer" in month and "Rho Health 2026-05 → 2026-06" in month


def test_new_to_network_per_payer_on_the_house_basis(ahead):
    d = ahead.post("/api/contracts/new-to-network",
                   json={"market": {"month": "latest"}, "zip": None, "radius_miles": 25}).json()
    assert d["month"] == "latest"
    assert d["compared_months"] == [{"payer": "Rho Health", "month": "2026-06", "prev_month": "2026-05"}]
    got = [(r["practice"], r["payer"], r["prev_month"], r["month"], r["codes"], r["median_rate"])
           for r in d["rows"]]
    # E (3 codes at 70/65/80) only: the institutional $900 newcomer and the $0.01
    # placeholder newcomer are not newly contracted RATES; Tiny's T has no
    # earlier Tiny book to be new against
    assert got == [("E REHAB", "Rho Health", "2026-05", "2026-06", 3, 70.0)]


def test_a_pinned_month_compares_only_payers_that_published_in_it(ahead):
    d = ahead.post("/api/contracts/new-to-network",
                   json={"market": {"month": "2026-07"}}).json()
    # only Tiny published in 2026-07, and it has no earlier book: nothing to
    # compare, said plainly — never "compared: Rho, Sigma" while comparing none
    assert d["compared"] == [] and d["rows"] == [] and d["reason"]
    d6 = ahead.post("/api/contracts/new-to-network",
                    json={"market": {"month": "2026-06"}}).json()
    assert [(r["practice"], r["payer"]) for r in d6["rows"]] == [("E REHAB", "Rho Health")]
    assert d6["compared"] == [["Rho Health", "2026-05"]]


def test_engagement_labels_use_the_practice_s_own_month(ahead):
    r = ahead.post("/api/engagements/baseline", json={"subject": "A REHAB", "market": {"month": "latest"}})
    assert r.status_code == 200, r.text
    assert r.json()["month"] == "2026-06"
