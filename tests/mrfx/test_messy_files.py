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


def test_large_file_streams_without_accumulating_rows(cfg, store, monkeypatch):
    """A file yielding more than one batch must stream to the part without the
    parser holding all rows in memory (constant-memory guarantee, §8.1)."""
    from mrfx.parser import InNetworkParser

    monkeypatch.setattr(InNetworkParser, "BATCH_ROWS", 500)
    # one code, one price, 1,500 provider groups -> 1,500 rows across 3 batches
    groups = [
        {"npi": [1000000000 + i], "tin": {"type": "ein", "value": f"43{i:07d}"}}
        for i in range(1500)
    ]
    data = {
        "reporting_entity_name": "Big Payer", "version": "2.0.0", "last_updated_on": "2026-06-01",
        "in_network": [{
            "billing_code": "97110", "billing_code_type": "CPT",
            "negotiated_rates": [{"provider_groups": groups,
                "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 40.0,
                                       "billing_class": "professional", "service_code": ["11"]}]}],
        }],
    }
    p = make_fixture(cfg.inbox_dir, "big.json", data)
    res = ingest_file(cfg, store, p)
    assert res["status"] == "done" and res["rows"] == 1500
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM rates").fetchone()[0] == 1500
        # header stamped correctly on rows written in early batches
        assert con.execute("SELECT count(DISTINCT payer) FROM rates").fetchone()[0] == 1
        assert con.execute("SELECT DISTINCT file_month FROM rates").fetchone()[0] == "2026-06"


def test_failed_parse_leaves_no_partial_part(cfg, store):
    """The streaming writer swaps into place atomically; a mid-parse failure
    must not leave a half-written part shadowing a prior good one."""
    good = {
        "reporting_entity_name": "P", "version": "2.0.0", "last_updated_on": "2026-06-01",
        "in_network": [{"billing_code": "97110", "billing_code_type": "CPT",
            "negotiated_rates": [{"provider_groups": [{"npi": [1111111111], "tin": {"type": "ein", "value": "431111111"}}],
                "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 40.0, "billing_class": "professional"}]}]}],
    }
    p = make_fixture(cfg.inbox_dir, "good.json", good)
    assert ingest_file(cfg, store, p)["status"] == "done"
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM rates").fetchone()[0] == 1
    # truncated gzip under the same name (same part key) fails mid-stream
    same = cfg.inbox_dir / "good.json.gz"
    same.write_bytes(b"\x1f\x8b\x08\x00" + b"garbage" * 50)
    res = ingest_file(cfg, store, same)
    assert res["status"] in ("failed", "quarantined")


def test_large_file_two_pass_bounded_refs_and_progress(cfg, store, monkeypatch):
    """A file over the large-file threshold takes the two-pass path: pass 1
    learns which references the target codes cite, pass 2 keeps only those in
    memory. A million unused refs must NOT be materialized, and progress must
    reach 100%."""
    import mrfx.ingest as ingest_mod
    from mrfx.parser import InNetworkParser

    # force the large-file path on a tiny file, and tiny chunks so progress ticks
    monkeypatch.setattr(ingest_mod, "LARGE_FILE_UNCOMPRESSED_BYTES", 0)
    monkeypatch.setattr(ingest_mod, "CHUNK_COMPRESSED_BYTES", 256)

    # 1 target item citing ref 7, plus 2,000 UNUSED provider_references
    data = {
        "reporting_entity_name": "Big CO Payer", "version": "2.0.0", "last_updated_on": "2026-06-01",
        "provider_references": (
            [{"provider_group_id": i, "provider_groups": [
                {"npi": [9000000000 + i], "tin": {"type": "ein", "value": f"99{i:07d}"}}]}
             for i in range(2000)]
            + [{"provider_group_id": 7, "provider_groups": [
                {"npi": [1111111111], "tin": {"type": "ein", "value": "431111111"}}]}]
        ),
        "in_network": [{
            "billing_code": "97110", "billing_code_type": "CPT",
            "negotiated_rates": [{"provider_references": [7],
                "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 40.0,
                                       "billing_class": "professional", "service_code": ["11"]}]}],
        }],
    }
    p = make_fixture(cfg.inbox_dir, "big_co.json", data)

    seen = {}
    orig_init = InNetworkParser.__init__

    def spy(self, *a, **k):
        orig_init(self, *a, **k)
        seen["keep"] = self._keep_ref_ids
    monkeypatch.setattr(InNetworkParser, "__init__", spy)

    bar_calls = []
    res = ingest_file(cfg, store, p, progress_bar=lambda d, t, pct: bar_calls.append((d, t, pct)))
    assert res["status"] == "done" and res["rows"] == 1
    # pass 2 was told to keep ONLY ref 7 — not the 2,000 unused ones
    assert seen["keep"] == {7}
    rows = rates(store)
    assert rows[0]["npi"] == "1111111111" and rows[0]["negotiated_rate"] == 40.0
    # progress ran and finished at 100%
    assert bar_calls and bar_calls[-1][2] == 100.0
    st = store.file_status("big_co.json")
    assert st["progress"] == 100.0 and st["chunks_done"] == st["chunks_total"]


def test_skim_finds_only_target_cited_refs(cfg):
    import io
    import json as _json

    from mrfx.parser import skim_needed_ref_ids

    data = {
        "reporting_entity_name": "P", "version": "2.0.0", "last_updated_on": "2026-06-01",
        "provider_references": [{"provider_group_id": i, "provider_groups": []} for i in range(50)],
        "in_network": [
            {"billing_code": "97110", "billing_code_type": "CPT",  # target -> keep its refs
             "negotiated_rates": [{"provider_references": [3, 8], "negotiated_prices": [{"negotiated_rate": 1}]}]},
            {"billing_code": "99213", "billing_code_type": "CPT",  # NON-target -> ignore its refs
             "negotiated_rates": [{"provider_references": [40, 41], "negotiated_prices": [{"negotiated_rate": 1}]}]},
        ],
    }
    needed, target_items = skim_needed_ref_ids(cfg, io.BytesIO(_json.dumps(data).encode()))
    assert needed == {3, 8}  # 40/41 belong to a non-target code and are excluded
    assert target_items == 1  # exactly one item carried a target code


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
