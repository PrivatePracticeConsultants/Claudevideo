"""A state is recognised or refused — never truncated into a different one.

The bug this pins: every state-scoped feature did `str(v).strip().upper()[:2]`
and then checked the length, so "MISSOURI" passed as "MI". With Michigan data
loaded, a user asking for Missouri would have been shown Michigan's numbers
under a Missouri heading — a wrong answer that looks right.
"""
import pytest

from mrfx.benchmark import BenchmarkError, normalize_market
from mrfx.states import STATE_CODES, state_code, state_code_or_none


def test_a_two_letter_code_passes_through():
    assert state_code("MO") == "MO"
    assert state_code(" mo ") == "MO"


def test_a_full_state_name_resolves_to_its_own_code_not_its_first_two_letters():
    # the whole point: Missouri is MO, and must never come back MI
    assert state_code("Missouri") == "MO"
    assert state_code("missouri") == "MO"
    assert state_code("Michigan") == "MI"
    assert state_code("Missouri") != state_code("Michigan")


def test_other_names_whose_first_two_letters_are_a_different_state():
    # each of these truncated to a REAL but wrong state under the old rule
    assert state_code("Indiana") == "IN"      # "IN" happens to be right
    assert state_code("Maryland") == "MD"     # "MA" = Massachusetts
    assert state_code("Alaska") == "AK"       # "AL" = Alabama
    assert state_code("Mississippi") == "MS"  # "MI" = Michigan
    assert state_code("Virginia") == "VA"     # "VI" = Virgin Islands


def test_blank_is_none_not_an_error():
    assert state_code(None) is None
    assert state_code("") is None
    assert state_code("   ") is None


def test_a_code_that_is_not_a_state_is_refused():
    with pytest.raises(ValueError):
        state_code("ZZ")
    with pytest.raises(ValueError):
        state_code("QQ")


def test_junk_is_refused_rather_than_sliced_into_a_sentence():
    # a dict used to stringify and slice to "{'", which was then narrated back
    # to the user as "no schedule loaded for {'"
    for junk in ({"a": 1}, ["MO"], 12345, ("MO",)):
        with pytest.raises(ValueError):
            state_code(junk)


def test_the_lenient_twin_never_raises_for_row_data():
    assert state_code_or_none({"a": 1}) is None
    assert state_code_or_none("ZZ") is None
    assert state_code_or_none("Missouri") == "MO"


def test_every_mapped_name_lands_on_a_real_usps_code():
    assert state_code("District of Columbia") == "DC"
    assert "DC" in STATE_CODES
    assert len(STATE_CODES) == 56  # 50 states + DC + 5 territories


def test_normalize_market_normalises_the_state_box():
    assert normalize_market({"state": "Missouri"})["state"] == "MO"
    assert normalize_market({"state": " mo "})["state"] == "MO"
    assert normalize_market({"state": ""})["state"] is None
    assert "state" not in normalize_market({})


def test_normalize_market_refuses_a_state_it_cannot_read():
    with pytest.raises(BenchmarkError):
        normalize_market({"state": "Missour"})
    with pytest.raises(BenchmarkError):
        normalize_market({"state": {"a": 1}})


def test_a_skipped_packet_section_reads_as_a_clause_not_a_sentence():
    """The Clients tab joins skipped sections with "; ".

    A refusal message that ends in a full stop rendered as ".;" mid-list, e.g.
    "…pass market.allow_national=true.; Contract gaps: …".
    """
    from mrfx.packets import _reason

    assert _reason(ValueError("pass market.allow_national=true.")) == \
        "pass market.allow_national=true"
    # an ellipsis is meaningful — never strip it back to two dots
    assert _reason(ValueError("looking for codes 97110, 97140, …")).endswith("…")
    assert _reason(ValueError("no rows...")) == "no rows..."
    # newlines would break the one-line list too
    assert _reason(ValueError("two\nlines")) == "two lines"
    assert _reason(ValueError("")) == "no reason given"

    joined = "; ".join(
        f"{s}: {_reason(ValueError(m))}" for s, m in
        (("Rate card", "no codes priced this month."),
         ("Contract gaps", "no contract gaps found — nothing to chase")))
    assert ".;" not in joined


def test_a_huge_pasted_value_does_not_become_a_huge_error_message():
    with pytest.raises(ValueError) as e:
        state_code("Z" * 1000)
    assert len(str(e.value)) < 160
    assert "…" in str(e.value)
