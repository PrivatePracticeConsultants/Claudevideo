"""Known answers for the "practice rate" basis shared by the Code page's payer
leaderboard, the summary's by-code strip and the Benchmark (round 3, P0.4).

Payer "Delta Health", one file 2026-05, POS 11:

  97110  G1 [N1]  GP professional $50, GP institutional $100
         G2 [N2]  GP professional $55
         G3 [N3]  GP professional $60
         G4 [N4]  GP professional $65, CQ|GP professional $40 (assistant)
         NPI-in-the-TIN-slot (tin.type npi = N5)  GP professional $30
  97530  H1 [N6]  GP $70 (PT)     H2 [N7]  no modifier $65 (untagged:
                                            97530 is PT+OT -> 'unspecified')

By hand, 97110 practice rates on the Benchmark basis (professional, base
modifiers, no NPI-slot rows): G1 50, G2 55, G3 60, G4 65 (its CQ line is not
base) -> leaderboard median 57.5, n 4, p25 50+.75*5 = 53.75, p75 61.25. The
Benchmark over the same market (a subject with no rates of its own, so every
practice is a peer): p50 57.5, n_peers 4. The old leaderboard said
median(75, 55, 60, 52.5, 30) = 55 over 5 "practices".
97530 with discipline=PT: H1 70 and the untagged H2 65 -> 67.5 on both
screens (the Explorer used to drop H2 and say 70).
"""

from __future__ import annotations

import json

import pytest

G1, G2, G3, G4, H1, H2 = ("910000001", "910000002", "910000003", "910000004",
                          "910000006", "910000007")
N1, N2, N3, N4, N5, N6, N7 = (f"19100000{i:02d}" for i in range(1, 8))


def _p(rate, mods=("GP",), cls="professional"):
    return {"negotiated_type": "negotiated", "negotiated_rate": rate, "billing_class": cls,
            "service_code": ["11"], "billing_code_modifier": list(mods),
            "expiration_date": "9999-12-31"}


def _r(npis, tin, prices, ttype="ein"):
    return {"provider_groups": [{"npi": npis, "tin": {"type": ttype, "value": tin}}],
            "negotiated_prices": prices}


def _item(code, rates):
    return {"negotiation_arrangement": "ffs", "name": code, "billing_code_type": "CPT",
            "billing_code_type_version": "2026", "billing_code": code, "description": code,
            "negotiated_rates": rates}


FILE = {"reporting_entity_name": "Delta Health", "reporting_entity_type": "issuer",
        "last_updated_on": "2026-05-01", "version": "2.0.0", "provider_references": [],
        "in_network": [
            _item("97110", [
                _r([N1], G1, [_p(50.0), _p(100.0, cls="institutional")]),
                _r([N2], G2, [_p(55.0)]),
                _r([N3], G3, [_p(60.0)]),
                _r([N4], G4, [_p(65.0), _p(40.0, ("CQ", "GP"))]),
                _r([], N5, [_p(30.0)], ttype="npi"),
            ]),
            _item("97530", [_r([N6], H1, [_p(70.0)]), _r([N7], H2, [_p(65.0, ())])]),
        ]}


@pytest.fixture
def kd(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from mrfx.ingest import scan_inbox

    (cfg.inbox_dir / "delta_2026-05_in-network.json").write_text(json.dumps(FILE))
    list(scan_inbox(cfg, store))
    return TestClient(create_app(cfg, store))


def _bench(c, **market):
    r = c.post("/api/benchmark/market", json={
        "subject": "999999999", "market": {"payers": ["Delta Health"], **market}}).json()
    return {x["billing_code"]: x for x in r["rows"]}


def test_payer_leaderboard_uses_the_benchmark_basis(kd):
    d = kd.get("/api/code/97110?grain=tin").json()
    pr = d["payer_rank"][0]
    assert (pr["median_rate"], pr["n_entities"], pr["p25"], pr["p75"]) == (57.5, 4, 53.75, 61.25)
    assert "professional" in d["payer_rank_basis"] and "GP/GO/GN" in d["payer_rank_basis"]
    b = _bench(kd, codes=["97110"])["97110"]
    assert (b["p50"], b["n_peers"]) == (57.5, 4)


def test_summary_by_code_uses_the_benchmark_basis(kd):
    s = kd.get("/api/summary?grain=tin").json()
    by = {c["billing_code"]: c for c in s["by_code"]}
    assert (by["97110"]["median"], by["97110"]["entities"]) == (57.5, 4)
    assert "NPI-in-the-TIN-slot" in s["by_code_basis"]


def test_an_explicit_class_filter_wins_over_the_default(kd):
    pr = kd.get("/api/code/97110?grain=tin&billing_class=institutional").json()["payer_rank"][0]
    assert (pr["median_rate"], pr["n_entities"]) == (100.0, 1)


def test_discipline_filter_keeps_untagged_shared_code_lines(kd):
    s = kd.get("/api/summary?grain=tin&discipline=PT&cpt=97530").json()
    by = {c["billing_code"]: c for c in s["by_code"]}
    # one code: the strip itself carries the number
    assert s["median"] == 67.5 and s["entities"] == 2
    assert by["97530"]["median"] == 67.5
    assert _bench(kd, codes=["97530"], discipline="PT")["97530"]["p50"] == 67.5
