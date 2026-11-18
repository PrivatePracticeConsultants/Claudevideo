"""Known answers for the ENTITY grain (round 3, P0.3), worked out by hand and
checked through ingest -> rollups -> API, and against the Benchmark's entity
rule (median of member-TIN rates).

Payer "Gamma Health", 97110, professional, GP, POS 11. NPPES names/states:

  TIN A 810000001  NPI P1 "ACME THERAPY"  MO   June $45, July $55
  TIN B 810000002  NPI P2 "ACME THERAPY"  MO   June $40 (not in the July file)
  TIN F 810000006  NPI P6 "ACME THERAPY"  KS   June $70
  TIN D 810000004  NPI P4 "D REHAB"       MO   June $40   } entity_map: both
  TIN E 810000005  NPI P5 "E REHAB"       KS   June $60   } -> "Beta Group"
  TIN J 810000007  NPI P7 "JANE DOE" (a person, NPI-1)  MO  June $35
  TIN K 810000008  NPI P7 too (she also bills here)     MO  June $85

By hand:
  Entity "ACME THERAPY · MO" (A, B — F is a different practice in KS):
    latest: A's newest is July $55, B's newest is June $40 -> median 47.5,
            tin_count 2, rate_variants 2 (two member rates), month 2026-07
    June:   A $45, B $40 -> 42.5; both members come from the ONE June file,
            so source_count = 1 (summing members said 2)
  Entity "ACME THERAPY · KS" (F): $70 — not blended into the MO practice.
  J and K are both "named" JANE DOE (their only clinician) but are two
    practices: each TIN is its own entity ($35 and $85), never one $60.
  Entity "Beta Group" (manual map), state=MO: only member D -> $40 (filtering
    after aggregation showed the MO+KS blend $50).
  Benchmark (subject = TIN A's org "ACME THERAPY", payer Gamma, state MO,
    latest): subject rate = median(A 55, B 40) = 47.5 — equals the Explorer.
"""

from __future__ import annotations

import json

import pytest

A, B, D, E, F = "810000001", "810000002", "810000004", "810000005", "810000006"
J, K = "810000007", "810000008"
P1, P2, P4, P5, P6, P7 = ("1810000001", "1810000002", "1810000004", "1810000005",
                          "1810000006", "1810000007")


def _r(npi, tin, rate):
    return {"provider_groups": [{"npi": [npi], "tin": {"type": "ein", "value": tin}}],
            "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": rate,
                                   "billing_class": "professional", "service_code": ["11"],
                                   "billing_code_modifier": ["GP"],
                                   "expiration_date": "9999-12-31"}]}


def _file(day, rates):
    return {"reporting_entity_name": "Gamma Health", "reporting_entity_type": "issuer",
            "last_updated_on": day, "version": "2.0.0", "provider_references": [],
            "in_network": [{"negotiation_arrangement": "ffs", "name": "x",
                            "billing_code_type": "CPT", "billing_code_type_version": "2026",
                            "billing_code": "97110", "description": "x",
                            "negotiated_rates": rates}]}


JUNE = _file("2026-06-01", [_r(P1, A, 45.0), _r(P2, B, 40.0), _r(P6, F, 70.0),
                            _r(P4, D, 40.0), _r(P5, E, 60.0), _r(P7, J, 35.0),
                            _r(P7, K, 85.0)])
JULY = _file("2026-07-01", [_r(P1, A, 55.0)])


@pytest.fixture
def ke(cfg, store):
    from fastapi.testclient import TestClient

    from mrfx.api import create_app
    from mrfx.ingest import scan_inbox

    (cfg.inbox_dir / "gamma_2026-06_in-network.json").write_text(json.dumps(JUNE))
    (cfg.inbox_dir / "gamma_2026-07_in-network.json").write_text(json.dumps(JULY))
    list(scan_inbox(cfg, store))
    for npi, name, st in ((P1, "ACME THERAPY", "MO"), (P2, "ACME THERAPY", "MO"),
                          (P6, "ACME THERAPY", "KS"), (P4, "D REHAB", "MO"),
                          (P5, "E REHAB", "KS")):
        store.save_npi(npi, name, "261QP2000X", "Clinic/Center PT", "Town", st,
                       entity_type="NPI-2")
    store.save_npi(P7, "JANE DOE", "225100000X", "Physical Therapist", "Town", "MO",
                   entity_type="NPI-1")
    store.rebuild_rollups()
    client = TestClient(create_app(cfg, store))
    # after create_app: it loads config/entity_map.yaml (empty here) at start
    store.set_entity_map({D: "Beta Group", E: "Beta Group"})
    return client


def _ent(c, qs):
    rows = c.get(f"/api/rates?grain=entity&cpt=97110&limit=100{qs}").json()["rows"]
    return {r["unit_id"]: r for r in rows}


def test_latest_takes_each_member_at_its_own_newest_month(ke):
    e = _ent(ke, "&latest=1")["ACME THERAPY · MO"]
    assert (e["negotiated_rate"], e["tin_count"], e["rate_variants"], e["file_month"]) == (
        47.5, 2, 2, "2026-07")
    assert e["tin_value"] == f"{A}; {B}"
    # the Code page (latest by default) agrees
    ranked = {r["unit_id"]: r for r in ke.get("/api/code/97110?grain=entity").json()["ranked"]}
    assert ranked["ACME THERAPY · MO"]["median_rate"] == 47.5


def test_pinned_month_and_source_count(ke):
    e = _ent(ke, "&month=2026-06")["ACME THERAPY · MO"]
    assert (e["negotiated_rate"], e["tin_count"], e["source_count"]) == (42.5, 2, 1)


def test_same_name_in_another_state_is_another_entity(ke):
    ents = _ent(ke, "&latest=1")
    assert ents["ACME THERAPY · KS"]["negotiated_rate"] == 70.0
    assert ents["ACME THERAPY · KS"]["tin_value"] == F
    assert "ACME THERAPY" not in ents          # no name-only blend of MO + KS


def test_state_filter_applies_to_member_practices(ke):
    beta = _ent(ke, "&latest=1&state=MO")["Beta Group"]
    assert (beta["negotiated_rate"], beta["tin_count"], beta["tin_value"]) == (40.0, 1, D)
    both = _ent(ke, "&latest=1")["Beta Group"]
    assert (both["negotiated_rate"], both["tin_count"]) == (50.0, 2)


def test_entity_drawer_lists_exactly_the_members(ke):
    d = ke.get("/api/entity/entity/ACME THERAPY · MO").json()
    assert sorted(t["tin_value"] for t in d["tins"]) == [A, B]


def test_benchmark_entity_rate_matches_explorer(ke):
    r = ke.post("/api/benchmark/market", json={
        "subject": "ACME THERAPY",
        "market": {"payers": ["Gamma Health"], "codes": ["97110"], "state": "MO"}}).json()
    row = next(x for x in r["rows"] if x["billing_code"] == "97110")
    assert row["subject_rate"] == 47.5


def test_a_person_named_tin_is_its_own_entity(ke):
    ents = _ent(ke, "&latest=1")
    assert (ents[J]["negotiated_rate"], ents[J]["display_name"]) == (35.0, "JANE DOE")
    assert (ents[K]["negotiated_rate"], ents[K]["display_name"]) == (85.0, "JANE DOE")
    assert "JANE DOE" not in ents and "JANE DOE · MO" not in ents


def test_benchmark_subject_accepts_the_explorer_entity_key(ke):
    r = ke.post("/api/benchmark/market", json={
        "subject": "ACME THERAPY · MO",
        "market": {"payers": ["Gamma Health"], "codes": ["97110"]}}).json()
    row = next(x for x in r["rows"] if x["billing_code"] == "97110")
    assert row["subject_rate"] == 47.5        # A and B only — not F (KS, $70)


def test_a_bare_name_link_resolves_only_when_unambiguous(ke):
    # "D REHAB" exists once -> its drawer opens; "ACME THERAPY" is two practices
    # (MO and KS) -> refused with both names, never one picked silently
    assert ke.get("/api/entity/entity/ACME THERAPY").status_code == 409
    assert "ACME THERAPY · KS" in ke.get("/api/entity/entity/ACME THERAPY").json()["detail"]
    r = ke.get("/api/entity/entity/ACME THERAPY · KS")
    assert r.status_code == 200 and [t["tin_value"] for t in r.json()["tins"]] == [F]


def test_no_grain_relation_plans_a_nested_loop_join(ke, store):
    """Once practice names exist, a join written with a one-sided predicate in
    its ON clause (`ON t.tin_is_really_npi AND nn.npi = t.tin_value`) made
    DuckDB nested-loop every spine row against every named provider: the
    Explorer table took 158 s on the real 32M-row store (0.9 s unnamed), the
    Code page 10 s. The plan of every grain relation must hash-join."""
    from mrfx.api import FilterSet, rel_sql
    for grain, qp in (("tin", {"latest": "1"}), ("tin", {"cpt": "97110"}),
                      ("entity", {"latest": "1"}), ("npi", {"cpt": "97110"})):
        fs = FilterSet(qp)
        with store.connect() as con:
            plan = con.execute("EXPLAIN " + rel_sql(grain, fs), fs.params).fetchall()[0][1]
        assert "BLOCKWISE_NL_JOIN" not in plan and "CROSS_PRODUCT" not in plan, (grain, qp)
