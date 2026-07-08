"""Unit tests for NPPES post-filtering / target-set mechanics (no network)."""

from src.config import TaxonomyTarget
from src.npi_resolver import TargetProvider, TargetSet, _org_matches, _to_provider

TAXONOMIES = [
    TaxonomyTarget(code_prefix="2251", search_description="Physical Therapist"),
    TaxonomyTarget(code="261QP2000X", search_description="Physical Therapy"),
]


def result(codes, name="SOME PT LLC", ein=None):
    return {
        "number": 1234567893,
        "basic": {"organization_name": name, **({"ein": ein} if ein else {})},
        "taxonomies": [{"code": c} for c in codes],
    }


def test_taxonomy_prefix_match():
    assert _org_matches(result(["2251X0800X"]), TAXONOMIES)  # orthopedic PT sub-specialty
    assert _org_matches(result(["225100000X"]), TAXONOMIES)


def test_taxonomy_exact_match():
    assert _org_matches(result(["261QP2000X"]), TAXONOMIES)


def test_non_target_taxonomy_rejected():
    # OT / SLP / chiropractor orgs surfaced by broad description queries drop out
    assert not _org_matches(result(["225X00000X", "235Z00000X", "111NR0400X"]), TAXONOMIES)


def test_unavail_ein_becomes_none():
    p = _to_provider(result(["225100000X"], ein="<UNAVAIL>"))
    assert p.tin is None
    p = _to_provider(result(["225100000X"], ein="431234567"))
    assert p.tin == "431234567"


def test_target_set_lookups():
    ts = TargetSet(
        [
            TargetProvider(npi="1111111111", tin="111111111", org_name="A"),
            TargetProvider(npi="2222222222", tin=None, org_name="B"),
        ]
    )
    assert ts.npis == {"1111111111", "2222222222"}
    assert ts.tins == {"111111111"}
    assert ts.npi_to_org["2222222222"] == "B"
