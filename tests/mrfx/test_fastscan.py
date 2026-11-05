"""The byte-level fast path (mrfx/fastscan.py + InNetworkParser.parse_fast)
must be indistinguishable from the classic ijson path: same rows, same QA
counters, same item counts, same header — on hostile shapes and at every chunk
boundary. Each case runs at tiny chunk sizes so every structural byte, escape
run and key lands on a boundary somewhere."""

import io
import json
import random

import pytest

import mrfx.fastscan as fastscan
from mrfx.fastscan import FastScanUnsupported, iter_members
from mrfx.parser import (
    InNetworkParser,
    batch_to_table,
    run_in_network_parse,
    skim_needed_ref_ids,
    skim_needed_ref_ids_fast,
)
from mrfx.store import RATES_SCHEMA
from tests.mrfx.conftest import FIXTURES
from tests.mrfx.test_messy_files import HOSTILE

CHUNKS = [1, 2, 3, 7, 64, 4096]


def _key(row):
    row = dict(row)
    row.pop("ingested_at", None)
    return json.dumps(row, sort_keys=True, default=str)


def _run(cfg, data: bytes, fast: bool, keep=None, refs=None, sink=False):
    batches = []
    p = InNetworkParser(cfg, "plan_2026-05_in-network.json", external_refs=refs,
                        keep_ref_ids=keep,
                        sink=(batches.append if sink else None))
    r = p.parse_fast(io.BytesIO(data)) if fast else p.parse(io.BytesIO(data))
    rows = [row for b in batches for row in b] if sink else r.rows
    return sorted(map(_key, rows)), r


def _assert_same(cfg, data: bytes, monkeypatch, keep=None, refs=None):
    want_rows, want = _run(cfg, data, fast=False, keep=keep, refs=refs)
    for chunk in CHUNKS:
        monkeypatch.setattr(fastscan, "CHUNK", chunk)
        got_rows, got = _run(cfg, data, fast=True, keep=keep, refs=refs)
        assert got_rows == want_rows, f"rows differ at chunk={chunk}"
        assert got.qa.to_dict() == want.qa.to_dict(), f"qa differs at chunk={chunk}"
        assert (got.items_seen, got.items_matched, got.ref_groups_skipped) == (
            want.items_seen, want.items_matched, want.ref_groups_skipped), chunk
        assert (got.payer, got.schema_version, got.last_updated_on) == (
            want.payer, want.schema_version, want.last_updated_on), chunk
    return want_rows


def _item(code, ctype="CPT", refs=None, groups=None, rate=50.0, **extra):
    nr = {"negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": rate,
                                 "billing_class": "professional", "service_code": ["11"],
                                 "billing_code_modifier": ["GP"]}]}
    if refs is not None:
        nr["provider_references"] = refs
    if groups is not None:
        nr["provider_groups"] = groups
    d = {"billing_code_type": ctype, "billing_code": code, "negotiated_rates": [nr]}
    d.update(extra)
    return d


G = [{"npi": [1234567890, "1234567891"], "tin": {"type": "ein", "value": "111111111"}}]


def test_hostile_messy_file_matches(cfg, monkeypatch):
    rows = _assert_same(cfg, json.dumps(HOSTILE).encode(), monkeypatch)
    assert rows  # the case actually emits rows


def test_repo_fixture_matches(cfg, monkeypatch):
    _assert_same(cfg, (FIXTURES / "innetwork_mixed.json").read_bytes(), monkeypatch)


def test_strings_full_of_structure_and_escapes(cfg, monkeypatch):
    nasty = 'a "quoted" {brace} [bracket] \\ back\\\\slash \\" } ] { [ "billing_code": "97110"'
    doc = {
        "reporting_entity_name": "Esc \"aped\" {Co}", "version": "1.0", "last_updated_on": "2026-05-01",
        "in_network": [
            _item("99213", name=nasty, description=nasty),
            _item("97110", name=nasty, groups=G, description="\\\\\\\""),
            {"name": nasty, "billing_code": "97140", "billing_code_type": "CPT",
             "negotiated_rates": [{"provider_groups": G, "negotiated_prices": [
                 {"negotiated_type": "negotiated", "negotiated_rate": "1,234.50",
                  "billing_code_modifier": ["59", "gp"], "service_code": [11]}]}]},
        ],
    }
    for ascii_only in (True, False):
        rows = _assert_same(cfg, json.dumps(doc, ensure_ascii=ascii_only).encode(), monkeypatch)
        assert len(rows) == 4


def test_key_order_and_nested_codes(cfg, monkeypatch):
    doc = {"in_network": [
        # billing_code LAST, after a big nested negotiated_rates
        {"billing_code_type": "CPT", "negotiated_rates": [{"provider_groups": G, "negotiated_prices": [
            {"negotiated_type": "negotiated", "negotiated_rate": 10}]}] * 30, "billing_code": "97110"},
        # nested bundled_codes carry their own billing_code: the item's OWN code decides
        {"bundled_codes": [{"billing_code": "97110", "billing_code_type": "CPT"}],
         **_item("99999", groups=G)},
        {"covered_services": [{"billing_code": "99999"}], **_item("97112", groups=G)},
        # type-first other system (counted), code-first non-target (not counted)
        {"billing_code_type": "MS-DRG", "billing_code": "97110", "negotiated_rates": []},
        {"billing_code": "12345", "billing_code_type": "RC", "negotiated_rates": []},
        # code-first target then an other type (counted)
        {"billing_code": "97110", "billing_code_type": "ICD", "negotiated_rates": []},
        # numeric code / numeric type / missing code / null code / blank type
        _item(97116, groups=G), _item("97530", ctype=5, groups=G),
        {"billing_code_type": "CPT", "negotiated_rates": [{"provider_groups": G, "negotiated_prices": []}]},
        {"billing_code": None, "billing_code_type": "CPT", "negotiated_rates": []},
        _item("97535", ctype="  ", groups=G), _item("G0283", ctype="", groups=G),
        # multi-code fields (non-target, counted), bundle arrangement (counted)
        _item("97110, 97112", groups=G), _item("97110 97140", groups=G),
        _item("97110", groups=G, negotiation_arrangement="bundle"),
        # non-object members are ignored by both paths
        [1, 2, {"billing_code": "97110"}], "stray", 5,
    ], "reporting_entity_name": "Late Header Co", "last_updated_on": "2026-04-01", "version": "9"}
    _assert_same(cfg, json.dumps(doc, indent=1).encode(), monkeypatch)
    _assert_same(cfg, json.dumps(doc).encode(), monkeypatch)


def test_references_before_after_scalar_and_missing(cfg, monkeypatch):
    refs = [{"provider_group_id": i, "provider_groups": [
        {"npi": [1000000000 + i], "tin": {"type": "ein", "value": f"{i:09d}"}}]} for i in range(12)]
    refs.append({"provider_group_id": "13.0", "provider_groups": G})
    refs.append({"provider_group_id": 14.5, "provider_groups": G})          # non-integral: dropped
    refs.append({"provider_group_id": 15, "location": "https://x/y.json"})   # remote: empty
    refs.append({"provider_groups": G, "provider_group_id": 16})             # id key last
    items = [_item("97110", refs=[1, 2, 2, 13]), _item("97140", refs=3),
             _item("97112", refs=[99, 15]), _item("97530", refs=["4", "x", 5.5, 16]),
             _item("99213", refs=[6, 7])]
    for refs_first in (True, False):
        doc = ({"provider_references": refs, "in_network": items} if refs_first
               else {"in_network": items, "provider_references": refs})
        data = json.dumps(doc).encode()
        _assert_same(cfg, data, monkeypatch)
        # two-pass: the fast skim's kept-ref set must give identical rows
        want_ids, want_n = skim_needed_ref_ids(cfg, io.BytesIO(data))
        for chunk in CHUNKS:
            monkeypatch.setattr(fastscan, "CHUNK", chunk)
            got_ids, got_n = skim_needed_ref_ids_fast(cfg, io.BytesIO(data))
            assert got_n == want_n and got_ids <= want_ids
        _assert_same(cfg, data, monkeypatch, keep=got_ids)
        _assert_same(cfg, data, monkeypatch, keep=got_ids, refs={99: [("123456789", "ein", ("1234567899",))]})


def test_whitespace_and_top_level_objects(cfg, monkeypatch):
    doc = {"meta": {"version": "WRONG-nested", "in_network": [_item("97110", groups=G)]},
           "version": "1.2", "plans": [{"plan_name": "x"}], "in_network": [_item("97110", groups=G)]}
    # (a BOM is stripped by open_stream before either parser sees the bytes)
    data = b" \n\t" + json.dumps(doc, indent=3).encode() + b"\n  "
    _assert_same(cfg, data, monkeypatch)


def test_truncated_file_fails_not_silently_succeeds(cfg):
    data = json.dumps({"in_network": [_item("97110", groups=G)] * 3}).encode()[:-30]
    with pytest.raises(ValueError):
        InNetworkParser(cfg, "t.json").parse_fast(io.BytesIO(data))


def test_non_object_top_level_declines_and_falls_back(cfg):
    import contextlib

    for data in (b"[1,2,3]", b"", b"   "):
        with pytest.raises(FastScanUnsupported):
            list(iter_members(io.BytesIO(data), lambda *a: True, None))
    # through the runner: declines up front, the classic parser takes over
    calls = []

    def opener():
        calls.append(1)
        return contextlib.nullcontext(io.BytesIO(b'[{"in_network": []}]'))

    _, r = run_in_network_parse(cfg, lambda: InNetworkParser(cfg, "t.json"), opener)
    assert len(calls) == 2 and r.qa.rows == 0


def test_random_documents_match(cfg, monkeypatch):
    """Seeded fuzz over shapes/orders/escapes — the cheap way to find the
    case nobody thought to write down."""
    rng = random.Random(1234)
    codes = ["97110", "97140", "99213", "G0283", 97112, "0001U", "97110,97112"]
    junk = ['"', "\\", "{", "}", "[", "]", ":", ",", "é", " ", " "]

    def s():
        return "".join(rng.choice(junk + list("abc")) for _ in range(rng.randint(0, 12)))

    for _ in range(25):
        refs = [{"provider_group_id": i, "provider_groups": [
            {"npi": [rng.randrange(10**9, 10**10) for _ in range(rng.randint(0, 3))],
             "tin": {"type": rng.choice(["ein", "npi", ""]), "value": str(rng.randrange(10**8, 10**10))}}]}
            for i in range(rng.randint(0, 6))]
        items = []
        for _ in range(rng.randint(0, 8)):
            it = _item(rng.choice(codes), ctype=rng.choice(["CPT", "HCPCS", "RC", None]),
                       refs=rng.sample(range(8), rng.randint(0, 3)) if rng.random() < .7 else None,
                       groups=G if rng.random() < .5 else None, name=s(), description=s())
            if rng.random() < .3:
                it["bundled_codes"] = [{"billing_code": rng.choice(codes), "x": s()}]
            keys = list(it)
            rng.shuffle(keys)
            items.append({k: it[k] for k in keys})
        parts = [("in_network", items), ("provider_references", refs),
                 ("version", s()), ("reporting_entity_name", s()), ("last_updated_on", "2026-0" + str(rng.randint(1, 9)))]
        rng.shuffle(parts)
        doc = dict(parts)
        _assert_same(cfg, json.dumps(doc, ensure_ascii=rng.random() < .5,
                                     indent=rng.choice([None, 0, 2])).encode(), monkeypatch)


def test_row_batch_table_equals_dict_rows(cfg, monkeypatch):
    """The columnar sink batch builds the exact Arrow table the old
    list-of-dicts path built."""
    import pyarrow as pa

    monkeypatch.setattr(InNetworkParser, "BATCH_ROWS", 3)
    data = json.dumps(HOSTILE).encode()
    batches = []
    p = InNetworkParser(cfg, "x.json", sink=batches.append)
    p.parse(io.BytesIO(data))
    assert batches
    for b in batches:
        want = pa.Table.from_pylist(list(b), schema=RATES_SCHEMA)
        assert batch_to_table(b, RATES_SCHEMA).equals(want)
    # and sink-mode rows == in-memory rows
    assert _run(cfg, data, fast=False, sink=True)[0] == _run(cfg, data, fast=False)[0]


def test_missing_numpy_degrades_to_classic_parser(cfg, monkeypatch):
    """An install that predates the numpy pin must keep ingesting."""
    import builtins
    import contextlib
    import sys

    real_import = builtins.__import__

    def no_numpy(name, *a, **k):
        if name == "numpy" or name.startswith("numpy."):
            raise ImportError("No module named 'numpy'")
        return real_import(name, *a, **k)

    monkeypatch.delitem(sys.modules, "mrfx.fastscan")
    monkeypatch.setattr(builtins, "__import__", no_numpy)
    data = json.dumps({"in_network": [_item("97110", groups=G)]}).encode()
    _, r = run_in_network_parse(cfg, lambda: InNetworkParser(cfg, "t.json"),
                                lambda: contextlib.nullcontext(io.BytesIO(data)))
    assert r.qa.rows == 2
