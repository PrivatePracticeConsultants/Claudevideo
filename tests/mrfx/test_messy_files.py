"""Robustness tests: real payer files arrive with shuffled key order, missing
fields, extra fields, and loose types. Parsing is best-effort with cleaning;
nothing is fabricated and every salvage decision is QA-counted."""

import json

import pytest

from mrfx.ingest import ingest_file
from mrfx.parser import as_list, clean_code, clean_digits, clean_rate
from tests.mrfx.conftest import make_fixture


def rates(store):
    with store.connect() as con:
        cols = [d[0] for d in con.execute("SELECT * FROM rates LIMIT 0").description]
        return [dict(zip(cols, r)) for r in con.execute("SELECT * FROM rates").fetchall()]


HOSTILE = {
    # payload FIRST — every header field appears at the END of the file
    "in_network": [
        {
            "billing_code": 97110,                       # numeric, not string
            "billing_code_type": "cpt",                  # lowercase
            "extra_field": {"nested": [1, 2, 3]},        # unknown extras everywhere
            "negotiated_rates": [
                {
                    "provider_groups": [
                        {"npi": ["1111111111 ", 1222222222, 1222222222],  # dirty + duplicate
                         "tin": {"type": "EIN", "value": "43-111 1111"},   # dirty EIN
                         "junk": True}
                    ],
                    "negotiated_prices": [
                        {"negotiated_type": "Negotiated", "negotiated_rate": "34.50",  # string rate
                         "service_code": "11",             # scalar, not array
                         "billing_class": "Professional",
                         "billing_code_modifier": "gp"},   # scalar + lowercase
                        {"negotiated_type": "negotiated",  # MISSING negotiated_rate
                         "service_code": ["11"], "billing_class": "professional"},
                    ],
                },
                {"provider_references": 900,               # single int, not array
                 "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 30.0,
                                        "billing_class": "professional"}]},
                {"provider_references": ["not-a-number"],  # junk ref id
                 "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 31.0,
                                        "billing_class": "professional"}]},
            ],
        },
        {   # MISSING billing_code_type; tin missing its type
            "billing_code": "97140",
            "negotiated_rates": [
                {"provider_groups": [{"npi": [3333333333], "tin": {"value": "432222222"}}],
                 "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 29.5,
                                        "billing_class": "professional"}]}
            ],
        },
    ],
    # provider_references AFTER in_network
    "provider_references": [
        {"provider_group_id": 900,
         "provider_groups": [{"npi": [9999999999], "tin": {"type": "ein", "value": "439999999"}}]}
    ],
    # header at EOF
    "reporting_entity_name": "Messy Payer Inc",
    "version": "2.0.0",
    "last_updated_on": "2026-06-01",
}


@pytest.fixture
def hostile_store(cfg, store):
    p = make_fixture(cfg.inbox_dir, "hostile.json", HOSTILE)
    res = ingest_file(cfg, store, p)
    assert res["status"] == "done"
    return store


def test_header_at_eof_stamped_on_every_row(hostile_store):
    rows = rates(hostile_store)
    assert rows, "hostile file must still produce rows"
    assert all(r["payer"] == "Messy Payer Inc" for r in rows)
    assert all(r["file_month"] == "2026-06" for r in rows)
    assert all(r["last_updated_on"] == "2026-06-01" for r in rows)


def test_numeric_code_and_loose_types_cleaned(hostile_store):
    rows = rates(hostile_store)
    gp = next(r for r in rows if r["negotiated_rate"] == 34.5 and r["npi"] == "1111111111")
    assert gp["billing_code"] == "97110"          # numeric 97110 matched the set
    assert gp["billing_code_modifier"] == ["GP"]  # scalar+lowercase modifier normalized
    assert gp["service_code"] == ["11"]           # scalar service_code wrapped
    assert gp["billing_class"] == "professional"  # case normalized
    assert gp["tin_value"] == "431111111"         # dashes/spaces stripped
    # duplicate NPI within a group deduped
    assert sum(1 for r in rows if r["negotiated_rate"] == 34.5) == 2


def test_refs_after_payload_and_scalar_ref_resolve(hostile_store):
    rows = rates(hostile_store)
    ref_row = next(r for r in rows if r["npi"] == "9999999999")
    assert ref_row["negotiated_rate"] == 30.0
    assert ref_row["tin_value"] == "439999999"


def test_missing_fields_never_fabricated(hostile_store, store):
    rows = rates(hostile_store)
    # the price with no negotiated_rate was skipped, not written as $0.00
    assert all(r["negotiated_rate"] > 0 for r in rows)
    qa = json.loads(store.file_status("hostile.json")["qa"])
    assert qa["unparseable_rates"] == 1
    assert qa["bad_ref_ids"] == 1
    assert qa["missing_code_type"] == 1  # 97140 accepted best-effort
    m97140 = next(r for r in rows if r["billing_code"] == "97140")
    assert m97140["billing_code_type"] == "CPT"   # inferred from code shape
    assert m97140["tin_type"] == "ein"            # 9-digit value, missing type


def test_cleaning_helpers():
    assert clean_code(97110) == "97110"
    assert clean_code(97110.0) == "97110"
    assert clean_code(" g0283 ") == "G0283"
    assert clean_rate("$34.50") == 34.5
    assert clean_rate("34.50 USD") == 34.5
    assert clean_rate("n/a") is None
    assert clean_rate(None) is None
    assert clean_digits("43-111 1111") == "431111111"
    assert clean_digits(1234567890.0) == "1234567890"
    assert as_list("x") == ["x"] and as_list(None) == [] and as_list([1]) == [1]


def test_completely_valueless_item_is_survivable(cfg, store):
    data = {
        "reporting_entity_name": "Weird Payer", "version": "2.0.0", "last_updated_on": "2026-06-01",
        "in_network": [
            {"billing_code": "97110", "billing_code_type": "CPT",
             "negotiated_rates": [
                 {"provider_groups": "not-a-list", "negotiated_prices": [{"negotiated_rate": "abc"}]},
                 {"negotiated_prices": None},
                 "not-even-a-dict",
             ]},
        ],
    }
    p = make_fixture(cfg.inbox_dir, "valueless.json", data)
    res = ingest_file(cfg, store, p)
    assert res["status"] == "done"
    assert res["rows"] == 0  # nothing salvageable, nothing invented
