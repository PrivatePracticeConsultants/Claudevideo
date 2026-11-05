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

CHUNKS = [1, 2, 3, 7, 64, 4096, 1 << 20]


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


# ---------------------------------------------------------------------------
# strictness parity: whatever the classic parser REJECTS inside a member we
# actually decode must fail the file on the fast path too — never become a row
# ---------------------------------------------------------------------------

_BAD_NUMBERS = ["NaN", "Infinity", "-Infinity", "1e400", "-1e999",
                "123456789012345678901234567890"]


@pytest.mark.parametrize("bad", _BAD_NUMBERS)
def test_invalid_numbers_in_a_target_item_fail_the_file_like_classic(cfg, bad):
    """A NaN/inf rate is not JSON; the classic parser fails the file. The
    fast path's json.loads would ACCEPT it and emit a NaN rate into a median
    — the one outcome that is never acceptable."""
    doc = json.dumps({"in_network": [_item("97110", groups=G)]}).replace('"negotiated_rate": 50.0', f'"negotiated_rate": {bad}')
    assert bad in doc
    with pytest.raises(Exception):
        InNetworkParser(cfg, "t.json").parse(io.BytesIO(doc.encode()))
    with pytest.raises(ValueError):
        InNetworkParser(cfg, "t.json").parse_fast(io.BytesIO(doc.encode()))


@pytest.mark.parametrize("bad", _BAD_NUMBERS)
def test_invalid_numbers_in_a_kept_reference_fail_the_file(cfg, bad):
    doc = json.dumps({
        "provider_references": [{"provider_group_id": 1, "provider_groups": [
            {"npi": ["1234567890"], "tin": {"type": "ein", "value": "111111111"}, "x": "PLACEHOLDER"}]}],
        "in_network": [_item("97110", refs=[1])]}).replace('"PLACEHOLDER"', bad)
    with pytest.raises(ValueError):
        InNetworkParser(cfg, "t.json").parse_fast(io.BytesIO(doc.encode()))


def test_big_numbers_that_are_valid_still_parse(cfg, monkeypatch):
    """The strict decoder only engages on a suspicious pattern — and must not
    reject the valid numbers that trip it (19-digit strings, 1e100)."""
    doc = {"in_network": [_item("97110", rate=1e100, groups=[
        {"npi": ["1234567890123456789", 1234567890], "tin": {"type": "ein", "value": "1234567890123456789"}}])]}
    _assert_same(cfg, json.dumps(doc).encode(), monkeypatch)


@pytest.mark.parametrize("tail", [b" xyz", b'{"in_network": []}', b"]", b"}", b"\x00"])
def test_bytes_after_the_document_fail_the_file(cfg, tail):
    """Trailing bytes after the closing brace mean a doubled/corrupt download
    (e.g. a resume that appended the file again). Classic fails it; so must
    the fast path, rather than quietly ingesting half a story."""
    data = json.dumps({"in_network": [_item("97110", groups=G)]}).encode() + tail
    with pytest.raises(Exception):
        InNetworkParser(cfg, "t.json").parse(io.BytesIO(data))
    for chunk in CHUNKS:
        import mrfx.fastscan as fs
        old = fs.CHUNK
        fs.CHUNK = chunk
        try:
            with pytest.raises(ValueError):
                InNetworkParser(cfg, "t.json").parse_fast(io.BytesIO(data))
        finally:
            fs.CHUNK = old


def test_trailing_whitespace_is_fine(cfg, monkeypatch):
    _assert_same(cfg, json.dumps({"in_network": [_item("97110", groups=G)]}).encode() + b" \r\n\t\n", monkeypatch)


def test_lone_surrogates_never_reach_a_row(cfg):
    """A bad \\uD800 escape decodes to a lone surrogate, which can't be stored
    as UTF-8 — the Parquet write would crash and fail the file. The classic
    parser turns it into '?'; so does the fast path, everywhere a string can
    reach a row (header, modifiers, billing class, codes)."""
    raw = ('{"reporting_entity_name": "Pay\\ud800er", "in_network": [{"billing_code": "97110", '
           '"billing_code_type": "CPT", "negotiated_rates": [{"provider_groups": '
           '[{"npi": ["1234567890"], "tin": {"type": "ein", "value": "111111111"}}], '
           '"negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 5, '
           '"billing_class": "prof\\udc00", "billing_code_modifier": ["G\\ud83dP"], '
           '"service_code": ["1\\udfff1"]}]}]}]}').encode()
    batches = []
    p = InNetworkParser(cfg, "t.json", sink=batches.append)
    r = p.parse_fast(io.BytesIO(raw))
    assert r.payer == "Pay?er"
    t = batch_to_table(batches[0], RATES_SCHEMA)          # must not raise
    row = t.to_pylist()[0]
    assert row["billing_class"] == "prof?" and row["billing_code_modifier"] == ["G?P"]
    assert row["service_code"] == ["1?1"]


def test_valid_surrogate_pairs_are_kept(cfg, monkeypatch):
    raw = '{"reporting_entity_name": "A\\ud83d\\ude00", "in_network": []}'.encode()
    _assert_same(cfg, raw, monkeypatch)
    p = InNetworkParser(cfg, "t.json")
    assert p.parse_fast(io.BytesIO(raw)).payer.endswith("\U0001F600")


# ---------------------------------------------------------------------------
# the one DELIBERATE difference: corrupt bytes inside an item we never decode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("corrupt", [
    b'"name": "\xff\xfe bad utf-8",',                 # invalid UTF-8
    b'"name": "raw\nnewline",',                        # unescaped control char
    b'"name": "x", "extra": [1, 2, ],',               # trailing comma
    b'"rate": NaN,',                                    # NaN in a non-target item
])
def test_corruption_inside_a_non_target_item_keeps_the_target_rows(cfg, corrupt):
    """The classic parser tokenizes every byte, so ONE malformed character in
    an unrelated billing code fails the whole file and loses every PT rate in
    it. The fast path never reads non-target bytes, so it keeps the valid PT
    rows — identical to the same file with the corrupt item removed. This
    pins that behavior so it can't change silently in either direction."""
    target = json.dumps(_item("97110", groups=G)).encode()
    bad = b'{"billing_code": "99213", "billing_code_type": "CPT", ' + corrupt + b' "negotiated_rates": []}'
    corrupt_doc = b'{"in_network": [' + bad + b", " + target + b"]}"
    clean_doc = b'{"in_network": [' + target + b"]}"
    with pytest.raises(Exception):
        InNetworkParser(cfg, "t.json").parse(io.BytesIO(corrupt_doc))
    got = InNetworkParser(cfg, "t.json").parse_fast(io.BytesIO(corrupt_doc))
    want = InNetworkParser(cfg, "t.json").parse(io.BytesIO(clean_doc))
    assert sorted(map(_key, got.rows)) == sorted(map(_key, want.rows)) and got.qa.rows == 2
    assert got.items_seen == 2  # the corrupt item is still counted as seen


# ---------------------------------------------------------------------------
# the production sink: batching, header-at-EOF restamp, deferred references
# ---------------------------------------------------------------------------

def _sink_rows(cfg, data, fast, keep=None):
    batches = []
    p = InNetworkParser(cfg, "plan_2026-05.json", sink=batches.append, keep_ref_ids=keep,
                        header_defaults={"payer": "Seeded", "schema_version": None, "last_updated_on": None})
    r = (p.parse_fast if fast else p.parse)(io.BytesIO(data))
    tables = [batch_to_table(b, RATES_SCHEMA) for b in batches]
    rows = [{k: v for k, v in row.items() if k != "ingested_at"} for t in tables for row in t.to_pylist()]
    return rows, r, len(batches)


@pytest.mark.parametrize("batch_rows", [1, 2, 7, 50_000])
def test_sink_mode_matches_including_batching_and_late_header(cfg, monkeypatch, batch_rows):
    """The real ingest path: rows leave in batches, and a header that arrives
    AFTER in_network re-stamps only the still-buffered rows. Both parsers must
    flush at the same points and stamp the same rows — compared row-by-row IN
    ORDER, not just as sets."""
    monkeypatch.setattr(InNetworkParser, "BATCH_ROWS", batch_rows)
    refs = [{"provider_group_id": i, "provider_groups": [
        {"npi": [1000000000 + i, 1100000000 + i], "tin": {"type": "ein", "value": f"{i:09d}"}}]}
        for i in range(6)]
    doc = {"in_network": [_item("97110", refs=[0, 1]), _item("97140", groups=G),
                          _item("99213", refs=[2]), _item("97112", refs=[3, 4, 5])],
           "provider_references": refs,           # AFTER in_network: deferred
           "reporting_entity_name": "Late Co", "last_updated_on": "2026-03-01"}
    data = json.dumps(doc).encode()
    want_rows, want, want_n = _sink_rows(cfg, data, fast=False)
    assert want_rows
    # ABSOLUTE expectation, not just "both agree": rows still buffered when
    # the late header arrives carry it; rows already flushed keep the seed
    # (a bug in the shared restamp would break both parsers identically)
    # - reference-citing items (97110/97112) DEFER until the refs table at
    #   EOF, so they are emitted after the header arrived: always "Late Co"
    # - the inline-group item (97140, 2 rows) is emitted mid-stream: it keeps
    #   the seed if a flush happened before EOF (batch <= 2), else the EOF
    #   restamp fixes it to "Late Co"
    for r in want_rows:
        late = r["billing_code"] != "97140" or batch_rows > 2
        assert r["payer"] == ("Late Co" if late else "Seeded"), (batch_rows, r["billing_code"])
        assert r["file_month"] == ("2026-03" if late else "2026-05")
    for chunk in CHUNKS:
        monkeypatch.setattr(fastscan, "CHUNK", chunk)
        got_rows, got, got_n = _sink_rows(cfg, data, fast=True)
        assert got_rows == want_rows, f"chunk={chunk}"
        assert got_n == want_n and got.qa.to_dict() == want.qa.to_dict()


def test_therapy_only_filter_counts_match(cfg, monkeypatch):
    """therapy_only_ingest drops non-therapist NPIs inside the emit loop that
    the RowBatch rewrite touched — rows AND non_therapy_dropped must match."""
    import mrfx.catalog as catalog

    therapists = {"1234567890", "2222222222"}
    monkeypatch.setattr(catalog, "therapy_npi_set", lambda _p: therapists)
    cfg.therapy_only_ingest = True
    groups = [
        {"npi": ["1234567890", "1234567891", "2222222222"], "tin": {"type": "ein", "value": "111111111"}},
        {"npi": ["9999999999"], "tin": {"type": "ein", "value": "222222222"}},     # all dropped
        {"npi": [], "tin": {"type": "ein", "value": "333333333"}},                 # EIN TIN-only: kept
        {"npi": [], "tin": {"type": "npi", "value": "2222222222"}},                # NPI-as-TIN therapist: kept
        {"npi": [], "tin": {"type": "npi", "value": "8888888888"}},                # NPI-as-TIN other: dropped
    ]
    data = json.dumps({"in_network": [_item("97110", groups=groups), _item("97140", groups=groups)]}).encode()
    want_rows, want, _ = _sink_rows(cfg, data, fast=False)
    got_rows, got, _ = _sink_rows(cfg, data, fast=True)
    assert got_rows == want_rows
    assert got.qa.to_dict() == want.qa.to_dict()
    # per item: 2 therapists + EIN TIN-only + therapist NPI-as-TIN = 4 rows;
    # dropped: 1234567891, 9999999999, 8888888888 = 3
    assert want.qa.rows == 8 and want.qa.non_therapy_dropped == 6 and want.qa.tin_only_rows == 4


# ---------------------------------------------------------------------------
# streams, big items, memory
# ---------------------------------------------------------------------------

class _ShortReads(io.RawIOBase):
    """Returns a random 1..n bytes per read() — like gzip/zip/network
    streams do — regardless of how much was asked for."""

    def __init__(self, data: bytes, seed: int, n: int):
        self._b = io.BytesIO(data)
        self._rng = random.Random(seed)
        self._n = n

    def readable(self):
        return True

    def read(self, size=-1):
        k = self._rng.randint(1, self._n)
        return self._b.read(k if size is None or size < 0 else min(size, k))


@pytest.mark.parametrize("n", [1, 5, 300])
def test_short_reads_do_not_change_anything(cfg, n):
    data = json.dumps({"provider_references": [{"provider_group_id": 1, "provider_groups": G}],
                       "in_network": [_item("97110", refs=[1], name='a "q" {b} \\'),
                                      _item("99213", groups=G), _item("97140", groups=G)],
                       "version": "1"}).encode()
    want = sorted(map(_key, InNetworkParser(cfg, "t.json").parse(io.BytesIO(data)).rows))
    for seed in range(5):
        got = InNetworkParser(cfg, "t.json").parse_fast(_ShortReads(data, seed, n))
        assert sorted(map(_key, got.rows)) == want


@pytest.mark.parametrize("code_pos", ["first", "last"])
@pytest.mark.parametrize("code", ["97110", "99213"])
def test_items_bigger_than_the_peek_limit(cfg, monkeypatch, code_pos, code):
    """An item whose own billing_code hasn't appeared within _PEEK_LIMIT bytes
    is buffered whole and decided at its end (the code-LAST layout)."""
    monkeypatch.setattr(fastscan, "_PEEK_LIMIT", 64)
    big = {"negotiated_rates": [{"provider_groups": G, "negotiated_prices": [
        {"negotiated_type": "negotiated", "negotiated_rate": float(i), "billing_class": "professional"}]}
        for i in range(40)]}
    item = ({"billing_code": code, "billing_code_type": "CPT", **big} if code_pos == "first"
            else {"billing_code_type": "CPT", **big, "billing_code": code})
    _assert_same(cfg, json.dumps({"in_network": [item, _item("97140", groups=G)]}).encode(), monkeypatch)


def test_a_huge_non_target_item_is_never_held_in_memory(cfg, monkeypatch):
    """The point of skipping: a non-target item tens of MB long (UHC ships
    single items that size) must cost a chunk of memory, not its own size."""
    import tracemalloc

    monkeypatch.setattr(fastscan, "CHUNK", 1 << 20)
    filler = json.dumps({"provider_groups": G, "negotiated_prices": [
        {"negotiated_type": "negotiated", "negotiated_rate": 1.0}]})
    n = (40 << 20) // len(filler)
    huge = ('{"billing_code": "99213", "billing_code_type": "CPT", "negotiated_rates": ['
            + ",".join([filler] * n) + "]}")
    data = ('{"in_network": [' + huge + ", " + json.dumps(_item("97110", groups=G)) + "]}").encode()
    assert len(data) > 40 << 20
    stream = io.BytesIO(data)
    tracemalloc.start()
    try:
        r = InNetworkParser(cfg, "t.json").parse_fast(stream)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert r.qa.rows == 2 and r.items_seen == 2
    assert peak < 16 << 20, f"peak {peak / 1e6:.0f} MB for a 40 MB non-target item"


def test_same_input_same_output_order(cfg):
    data = (FIXTURES / "innetwork_mixed.json").read_bytes()
    a = [_key(r) for r in InNetworkParser(cfg, "t.json").parse_fast(io.BytesIO(data)).rows]
    b = [_key(r) for r in InNetworkParser(cfg, "t.json").parse_fast(io.BytesIO(data)).rows]
    c = [_key(r) for r in InNetworkParser(cfg, "t.json").parse(io.BytesIO(data)).rows]
    assert a == b == c  # same ORDER too, not only the same set


def test_fast_scan_off_never_touches_the_fast_path(cfg, monkeypatch):
    import contextlib

    def boom(*a, **k):
        raise AssertionError("fast path used with fast_scan: false")

    monkeypatch.setattr(InNetworkParser, "parse_fast", boom)
    monkeypatch.setattr("mrfx.parser.skim_needed_ref_ids_fast", boom)
    cfg.fast_scan = False
    data = json.dumps({"in_network": [_item("97110", groups=G)]}).encode()
    _, r = run_in_network_parse(cfg, lambda: InNetworkParser(cfg, "t.json"),
                                lambda: contextlib.nullcontext(io.BytesIO(data)))
    assert r.qa.rows == 2
    from mrfx.parser import run_skim
    ids, n = run_skim(cfg, lambda: contextlib.nullcontext(io.BytesIO(data)))
    assert n == 1


# ---------------------------------------------------------------------------
# the numpy BULK skip (non-target members dropped without Python per member):
# every input whose verdict depends on cleaning, casing or key multiplicity
# must fall through to the exact per-member decision
# ---------------------------------------------------------------------------

def test_bulk_skip_never_drops_codes_that_clean_into_targets(cfg, monkeypatch):
    """clean_code strips, upper-cases and drops '.0': ' 97110 ', 'g0283',
    '97110.0' and 97110 (a number) are all TARGETS. A bulk check that only
    compares raw bytes would drop them as non-target."""
    assert "G0283" in cfg.code_set
    items = [_item(c, groups=G) for c in (" 97110 ", "97110 ", "g0283", "97110.0", 97110, "97140\t")]
    items += [_item("97112", ctype=t, groups=G) for t in ("cpt", "hcpcs", "Cpt4", " CPT", "HCPCS Level II")]
    items += [_item("99213", ctype=t, groups=G) for t in ("rc", "cdt", "ms-drg", "  ")]
    for indent in (None, 1):
        rows = _assert_same(cfg, json.dumps({"in_network": items}, indent=indent).encode(), monkeypatch)
        assert len(rows) == 2 * (6 + 5)


def test_bulk_skip_respects_duplicate_keys_and_their_qa_counts(cfg, monkeypatch):
    """Two own-level billing_code keys: the classic parser judges the FIRST to
    stream (and counts a multi-code field there). The bulk path must not
    decide on 'one of them is a plain non-target'."""
    raw = ('{"in_network": ['
           '{"billing_code": "97110, 97112", "billing_code": "99213", "negotiated_rates": []},'
           '{"billing_code": "99213", "billing_code": "97110", "billing_code_type": "CPT", "negotiated_rates": []},'
           '{"billing_code_type": "RC", "billing_code": "99213", "billing_code_type": "CPT", "negotiated_rates": []},'
           '{"billing_code": "97110", "billing_code_type": "CPT", "billing_code_type": "ICD", "negotiated_rates": []}'
           ']}').encode()
    _assert_same(cfg, raw, monkeypatch)
    r = InNetworkParser(cfg, "t.json").parse(io.BytesIO(raw))
    assert r.qa.multi_code_fields == 1 and r.qa.billing_type_other == 2


def test_bulk_reference_skip_reads_ids_like_the_classic_parser(cfg, monkeypatch):
    """provider_group_id 1e3 / 1000.0 / 1.0e3 / "1000" all mean 1000; 12.5 is
    not an id. A digits-only bulk reader would see '1' / '12' and skip a KEPT
    reference. One definition per document so nothing masks another."""
    for lit, means in (("1e3", 1000), ("1000.0", 1000), ("1.0e3", 1000), ('"1000"', 1000),
                       ("1E+3", 1000), ("12.5", None), ("7", 7), ('"007"', 7), ("0", 0)):
        raw = ('{"provider_references": [{"provider_group_id": %s, "provider_groups": '
               '[{"npi": ["1400000000"], "tin": {"type": "ein", "value": "700000000"}}]}], '
               '"in_network": [%s]}') % (lit, json.dumps(_item("97110", refs=[1000, 7, 0, 1, 12])))
        for keep in (None, {1000}, {7}, {0}, {1}, {12}):
            rows = _assert_same(cfg, raw.encode(), monkeypatch, keep=keep)
            kept = means is not None and (keep is None or means in keep)
            assert bool(rows) == kept, (lit, keep)


def test_bulk_other_code_systems_count_exactly_like_classic(cfg, monkeypatch):
    """Hospital-heavy files are mostly MS-DRG / HIPPS / RC items. They are
    skipped in bulk, but billing_type_other must still count exactly what the
    classic parser counts: type first -> counted; code first non-target ->
    not counted; code first TARGET -> counted; code absent -> counted."""
    def it(*pairs):
        return "{" + ", ".join(f'"{k}": {json.dumps(v)}' for k, v in pairs) + ', "negotiated_rates": []}'
    members = []
    for t in ("MS-DRG", "HIPPS", "RC", "ms-drg", "APR-DRG", "ICD-10", "CSTM-ALL", "CDT"):
        members += [
            it(("billing_code_type", t), ("billing_code", "470")),      # type first: counted
            it(("billing_code", "470"), ("billing_code_type", t)),      # non-target first: not
            it(("billing_code", "97110"), ("billing_code_type", t)),    # target first: counted
            it(("billing_code_type", t)),                               # no code: counted
            it(("billing_code", 470), ("billing_code_type", t)),        # numeric code: exact path
            it(("billing_code_type", t), ("billing_code", 97110)),
            it(("billing_code", 97110), ("billing_code_type", t)),      # numeric TARGET first: counted
            it(("billing\\u005fcode", "470"), ("billing_code_type", t)),  # escaped key, non-target: not
            it(("billing\\u005fcode", "97110"), ("billing_code_type", t)),  # escaped key, target: counted
        ]
    for prefix in ("CPT", "HCPCS"):
        members.append(it(("billing_code_type", prefix + "-X"), ("billing_code", "470")))
    raw = ('{"in_network": [' + ", ".join(members) + "]}").encode()
    _assert_same(cfg, raw, monkeypatch)
    r = InNetworkParser(cfg, "t.json").parse(io.BytesIO(raw))
    # per system: counted = type-first(1) + target-first(1) + no-code(1)
    # + type-first numeric (1) + numeric target first (1) + escaped target (1)
    assert r.qa.billing_type_other == 8 * 6
    pretty = json.dumps(json.loads(raw), indent=2).encode()
    _assert_same(cfg, pretty, monkeypatch)


# ---------------------------------------------------------------------------
# real-shape fixtures: slices cut from real payer files (Nov 2026), member
# bytes VERBATIM so each generator's whitespace, key order, quoting and id
# styles are exercised; identifiers anonymized (same-length look-alike digits,
# placeholder business names). Golden counts are the classic parser's output.
# ---------------------------------------------------------------------------

REAL = {
    # name: (rows, items_seen, items_matched, ref_groups_skipped, billing_type_other, payer)
    "real_bcbs_wy_compact": (327984, 12, 4, 0, 2, "Blue Cross Blue Shield of Wyoming"),
    "real_floridablue_npi_tins": (4, 12, 4, 0, 2, "Florida Blue"),
    "real_moda_compact": (11306, 10, 4, 1, 0, "Moda"),
    "real_tufts_pretty_jackson": (42063, 10, 4, 0, 0, "Tufts Health Public Plans, Inc."),
    "real_uhc_strings_refid_last": (12, 12, 4, 0, 2, "HEALTHSCOPE BENEFITS"),
}


def _row_digest(rows) -> tuple[int, int]:
    """Order-independent digest of a row multiset (cheap for 300k rows)."""
    import hashlib

    acc = 0
    for r in rows:
        h = hashlib.blake2b(_key(r).encode(), digest_size=8).digest()
        acc = (acc + int.from_bytes(h, "little")) % (1 << 64)
    return len(rows), acc


@pytest.mark.parametrize("name", sorted(REAL))
def test_real_vendor_shapes_match_classic_and_golden(cfg, monkeypatch, name):
    import gzip

    data = gzip.decompress((FIXTURES / "real" / f"{name}.json.gz").read_bytes())
    rows, items, matched, skipped, type_other, payer = REAL[name]
    want = InNetworkParser(cfg, f"{name}.json").parse(io.BytesIO(data))
    # absolute anchors: a regression in code BOTH paths share shows up here
    assert (want.qa.rows, want.items_seen, want.items_matched, want.ref_groups_skipped,
            want.qa.billing_type_other, want.payer) == (rows, items, matched, skipped, type_other, payer)
    want_digest = _row_digest(want.rows)
    small = len(data) < 100_000
    for chunk in ([7, 64, 4096, 1 << 20] if small else [4093, 1 << 16, 1 << 20]):
        monkeypatch.setattr(fastscan, "CHUNK", chunk)
        got = InNetworkParser(cfg, f"{name}.json").parse_fast(io.BytesIO(data))
        assert got.qa.to_dict() == want.qa.to_dict(), chunk
        assert (got.items_seen, got.items_matched, got.ref_groups_skipped, got.payer) == (
            want.items_seen, want.items_matched, want.ref_groups_skipped, want.payer)
        assert _row_digest(got.rows) == want_digest, chunk
    # the two-pass path: fast skim's kept set, then extraction
    monkeypatch.setattr(fastscan, "CHUNK", 1 << 16)
    ids_c, n_c = skim_needed_ref_ids(cfg, io.BytesIO(data))
    ids_f, n_f = skim_needed_ref_ids_fast(cfg, io.BytesIO(data))
    assert n_f == n_c == matched and ids_f <= ids_c
    got = InNetworkParser(cfg, f"{name}.json", keep_ref_ids=ids_f).parse_fast(io.BytesIO(data))
    assert _row_digest(got.rows) == want_digest


# ---------------------------------------------------------------------------
# big files: ONE full read + a short read of the reference table
# ---------------------------------------------------------------------------

class _Rows(list):
    """A sink that keeps rows as dicts (production sinks see the same
    batches; sink mode is where the late-header restamp happens)."""

    def __call__(self, batch):
        self.extend(batch)


def _classic_two_pass(cfg, data, refs=None):
    keep, n = skim_needed_ref_ids(cfg, io.BytesIO(data))
    rows = _Rows()
    p = InNetworkParser(cfg, "big_2026-05.json", external_refs=refs, keep_ref_ids=keep, sink=rows)
    r = p.parse(io.BytesIO(data))
    r.rows = rows
    return r, n


class _CountingOpen:
    """open_fn for run_big_file_fast that records bytes read per open."""

    def __init__(self, data: bytes):
        self.data = data
        self.reads: list[int] = []

    def __call__(self):
        import contextlib

        outer = self
        outer.reads.append(0)

        class S(io.BytesIO):
            def read(self, n=-1):
                b = super().read(n)
                outer.reads[-1] += len(b)
                return b
        return contextlib.nullcontext(S(outer.data))


def _big_fast(cfg, data, tmp_path, refs=None):
    from mrfx.parser import run_big_file_fast

    rows = _Rows()
    p = InNetworkParser(cfg, "big_2026-05.json", external_refs=refs, sink=rows)
    opener = _CountingOpen(data)
    spool = tmp_path / ".k.1.spool.tmp"
    r, n = run_big_file_fast(cfg, p, opener, spool)
    assert not spool.exists(), "spool must be deleted"
    r.rows = rows
    return r, n, opener.reads


def _layouts():
    refs = [{"provider_group_id": i, "provider_groups": [
        {"npi": [1500000000 + i, 1600000000 + i], "tin": {"type": "ein", "value": f"8{i:08d}"}}]}
        for i in range(40)]
    items = [_item("97110", refs=[1, 2, 39]), _item("99213", refs=list(range(30))),
             _item("97140", groups=G), _item("97112", refs=[5, 77]),
             {"billing_code_type": "MS-DRG", "billing_code": "470", "negotiated_rates": []},
             _item("97530", refs=7)]
    hdr = {"reporting_entity_name": "Big Co", "version": "1.0", "last_updated_on": "2026-05-01"}
    return {
        "refs_first": {**hdr, "provider_references": refs, "in_network": items},
        "refs_after": {**hdr, "in_network": items, "provider_references": refs},
        "header_last": {"provider_references": refs, "in_network": items, **hdr},
        "no_refs": {**hdr, "in_network": [_item("97110", groups=G), _item("99213", groups=G)]},
        "no_targets": {**hdr, "provider_references": refs, "in_network": [_item("99213", refs=[1])]},
    }


@pytest.mark.parametrize("layout", sorted(_layouts()))
def test_big_file_single_read_matches_classic_two_pass(cfg, tmp_path, monkeypatch, layout):
    data = json.dumps(_layouts()[layout]).encode()
    want, want_n = _classic_two_pass(cfg, data)
    for chunk in (7, 4096, 1 << 20):
        monkeypatch.setattr(fastscan, "CHUNK", chunk)
        got, n, reads = _big_fast(cfg, data, tmp_path)
        assert n == want_n
        assert sorted(map(_key, got.rows)) == sorted(map(_key, want.rows)), (layout, chunk)
        assert got.qa.to_dict() == want.qa.to_dict(), (layout, chunk)
        assert (got.items_seen, got.items_matched, got.ref_groups_skipped) == (
            want.items_seen, want.items_matched, want.ref_groups_skipped)
        assert (got.payer, got.schema_version, got.last_updated_on) == (
            want.payer, want.schema_version, want.last_updated_on)
        # ONE full read; a second, SHORT read only when references came first
        assert reads[0] == len(data)
        needs_refs = layout in ("refs_first", "header_last")
        assert len(reads) == (2 if needs_refs else 1), reads
        if needs_refs and chunk * 4 < len(data):
            # pass 2 stopped once the reference table closed — it never
            # re-read in_network (at most one chunk past where it starts)
            assert reads[1] <= data.index(b'"in_network"') + chunk, reads


def test_big_file_single_read_on_real_vendor_shapes(cfg, tmp_path, monkeypatch):
    import gzip

    for name in sorted(REAL):
        data = gzip.decompress((FIXTURES / "real" / f"{name}.json.gz").read_bytes())
        want, want_n = _classic_two_pass(cfg, data)
        monkeypatch.setattr(fastscan, "CHUNK", 1 << 16)
        got, n, reads = _big_fast(cfg, data, tmp_path)
        assert n == want_n
        assert got.qa.to_dict() == want.qa.to_dict(), name
        assert _row_digest(got.rows) == _row_digest(want.rows), name
        assert got.items_seen == want.items_seen and got.payer == want.payer


def test_big_file_uses_companion_references(cfg, tmp_path):
    """References from a standalone companion file (external_refs) resolve
    the same way on the single-read path."""
    ext = {9: [("123456789", "ein", ("1234567890",))]}
    data = json.dumps({"in_network": [_item("97110", refs=[9]), _item("97140", refs=[10])]}).encode()
    want, _ = _classic_two_pass(cfg, data, refs=ext)
    got, _, _ = _big_fast(cfg, data, tmp_path, refs=ext)
    assert sorted(map(_key, got.rows)) == sorted(map(_key, want.rows)) and got.qa.rows == 1
    assert got.ref_groups_skipped == want.ref_groups_skipped == 1


def test_big_file_spool_is_removed_on_failure(cfg, tmp_path):
    from mrfx.parser import run_big_file_fast

    data = json.dumps({"in_network": [_item("97110", groups=G)] * 3}).encode()[:-20]
    spool = tmp_path / ".k.1.spool.tmp"
    with pytest.raises(ValueError):
        run_big_file_fast(cfg, InNetworkParser(cfg, "t.json"), _CountingOpen(data), spool)
    assert not spool.exists()


def test_orphaned_spool_from_a_dead_process_is_swept(cfg):
    from mrfx.store import Store

    st = Store(cfg.store_dir)
    dead = st.rates_dir / ".somefile.999999.spool.tmp"   # pid 999999: not alive
    dead.write_bytes(b"x" * 100)
    live = st.rates_dir / f".other.{__import__('os').getpid()}.spool.tmp"
    live.write_bytes(b"y")
    st._sweep_orphan_tmps()
    assert not dead.exists() and live.exists()


def test_prefetch_reader_errors_and_early_close():
    """The background reader: an exception in read() (e.g. the orphaned-worker
    SystemExit from the progress callback) surfaces in the consumer; closing
    the generator early stops the thread."""
    import threading

    from mrfx.fastscan import _prefetch

    class Boom(io.BytesIO):
        def __init__(self):
            super().__init__(b"x" * 10_000)
            self.n = 0

        def read(self, n=-1):
            self.n += 1
            if self.n == 3:
                raise SystemExit("parent process died")
            return super().read(n)

    g = _prefetch(Boom(), 100)
    with pytest.raises(SystemExit):
        for _ in g:
            pass
    before = threading.active_count()
    g2 = _prefetch(io.BytesIO(b"y" * 10_000_000), 1000)
    next(g2)
    g2.close()
    import time
    deadline = time.monotonic() + 5
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert threading.active_count() <= before
    assert b"".join(_prefetch(io.BytesIO(b"abc" * 1000), 7)) == b"abc" * 1000


# ---------------------------------------------------------------------------
# found by an independent review of the fast path (each reproduced first)
# ---------------------------------------------------------------------------

def test_duplicate_top_level_arrays_make_the_single_read_decline(cfg, tmp_path):
    """Duplicate keys ({"in_network":[..],"provider_references":[..],
    "in_network":[..]}) are valid JSON; the classic parser's order-dependent
    reference resolution can't be replayed from a spool, so the single read
    DECLINES (before any row) and ingest falls back."""
    from mrfx.fastscan import FastScanUnsupported
    from mrfx.parser import run_big_file_fast

    ref = lambda i: '{"provider_group_id": %d, "provider_groups": [{"npi": ["%d"], "tin": {"type": "ein", "value": "11111111%d"}}]}' % (i, 1200000000 + i, i)  # noqa: E731
    for raw in (
        '{"in_network": [%s], "provider_references": [%s, %s], "in_network": [%s]}'
        % (json.dumps(_item("97110", refs=[1])), ref(1), ref(2), json.dumps(_item("97140", refs=[2]))),
        '{"provider_references": [%s], "provider_references": [%s], "in_network": [%s]}'
        % (ref(1), ref(2), json.dumps(_item("97110", refs=[1, 2]))),
    ):
        rows = _Rows()
        p = InNetworkParser(cfg, "dup.json", sink=rows)
        with pytest.raises(FastScanUnsupported):
            run_big_file_fast(cfg, p, _CountingOpen(raw.encode()), tmp_path / ".d.1.spool.tmp")
        assert rows == [] and not (tmp_path / ".d.1.spool.tmp").exists()


def test_duplicate_arrays_ingest_like_classic_through_the_fallback(tmp_path, monkeypatch):
    import mrfx.ingest as ingest_mod
    from mrfx.config import MrfxConfig
    from mrfx.ingest import ingest_file
    from mrfx.store import Store

    monkeypatch.setattr(ingest_mod, "LARGE_FILE_UNCOMPRESSED_BYTES", 1)
    ref = lambda i: '{"provider_group_id": %d, "provider_groups": [{"npi": ["%d"], "tin": {"type": "ein", "value": "11111111%d"}}]}' % (i, 1200000000 + i, i)  # noqa: E731
    raw = ('{"in_network": [%s], "provider_references": [%s, %s], "in_network": [%s]}'
           % (json.dumps(_item("97110", refs=[1])), ref(1), ref(2), json.dumps(_item("97140", refs=[2]))))
    out = {}
    for fast in (False, True):
        root = tmp_path / str(fast)
        c = MrfxConfig(inbox_dir=root / "i", processed_dir=root / "p", failed_dir=root / "f",
                       store_dir=root / "s", downloads_dir=root / "d",
                       entity_map_path=root / "e.yaml", fast_scan=fast)
        c.ensure_dirs()
        st = Store(c.store_dir)
        f = c.inbox_dir / "dup_2026-01.json"
        f.write_text(raw)
        assert ingest_file(c, st, f)["status"] == "done"
        with st.connect() as con:
            out[fast] = sorted(con.execute("SELECT billing_code, npi FROM rates").fetchall())
    assert out[True] == out[False] and len(out[True]) == 2


@pytest.mark.parametrize("rid", [9007199254740993, 12345678901234567, 123456789012345678])
def test_reference_ids_beyond_float_precision_match_classic(cfg, tmp_path, monkeypatch, rid):
    """clean_ref_id goes through float(): ids past 2**53 are ROUNDED on the
    classic path (and in the cited set). The bulk skip compared the exact
    integer and dropped the reference — chunk-size dependent."""
    for refs_first in (True, False):
        refs = [{"provider_group_id": rid, "provider_groups": G}]
        items = [_item("97110", refs=[rid])]
        doc = {"provider_references": refs, "in_network": items} if refs_first else {
            "in_network": items, "provider_references": refs}
        data = json.dumps(doc).encode()
        keep, _ = skim_needed_ref_ids(cfg, io.BytesIO(data))
        _assert_same(cfg, data, monkeypatch, keep=keep)
        want, _ = _classic_two_pass(cfg, data)
        for chunk in (7, 1 << 20):
            monkeypatch.setattr(fastscan, "CHUNK", chunk)
            got, _, _ = _big_fast(cfg, data, tmp_path)
            assert sorted(map(_key, got.rows)) == sorted(map(_key, want.rows)) and len(want.rows) == 2


@pytest.mark.parametrize("ids", [('"abc"', "1"), ("1", "5"), ("5", "1"), ("1", '"x"')])
def test_duplicate_provider_group_id_last_one_wins_like_classic(cfg, tmp_path, monkeypatch, ids):
    raw = ('{"provider_references": [{"provider_group_id": %s, "provider_group_id": %s, '
           '"provider_groups": [{"npi": ["1234567890"], "tin": {"type": "ein", "value": "111111111"}}]}], '
           '"in_network": [%s, %s]}') % (ids[0], ids[1], json.dumps(_item("97110", refs=[1])),
                                         json.dumps(_item("97140", refs=[5])))
    data = raw.encode()
    keep, _ = skim_needed_ref_ids(cfg, io.BytesIO(data))
    _assert_same(cfg, data, monkeypatch)
    _assert_same(cfg, data, monkeypatch, keep=keep)
    want, _ = _classic_two_pass(cfg, data)
    got, _, _ = _big_fast(cfg, data, tmp_path)
    assert sorted(map(_key, got.rows)) == sorted(map(_key, want.rows))


def test_escaped_reference_key_and_escaped_header_key(cfg, tmp_path, monkeypatch):
    B = "\\"
    raw = ('{"reporting' + B + 'u005fentity_name": "Real Payer", "provider_references": '
           '[{"provider' + B + 'u005fgroup_id": 1, "provider_groups": [{"npi": ["1234567890"], '
           '"tin": {"type": "ein", "value": "111111111"}}]}], "in_network": [%s]}'
           ) % json.dumps(_item("97110", refs=[1]))
    data = raw.encode()
    rows = _assert_same(cfg, data, monkeypatch)
    assert len(rows) == 1
    assert InNetworkParser(cfg, "t.json").parse_fast(io.BytesIO(data)).payer == "Real Payer"
    keep, _ = skim_needed_ref_ids(cfg, io.BytesIO(data))
    _assert_same(cfg, data, monkeypatch, keep=keep)


@pytest.mark.parametrize("gap", [600, 5000, 3 << 20])
def test_any_amount_of_whitespace_between_key_and_array(cfg, monkeypatch, gap):
    """The array's key was found in a fixed 512-byte lookback: 600 spaces
    before '[' dropped the whole in_network array (file still 'done')."""
    item = json.dumps(_item("97110", groups=G))
    for raw in ('{"in_network"' + " " * gap + ":[" + item + "]}",
                '{"in_network":' + "\n" * gap + "[" + item + "]}",
                '{"in_network"' + " " * gap + ":" + "\t" * gap + "[" + item + "]}"):
        rows = _assert_same(cfg, raw.encode(), monkeypatch) if gap < (1 << 20) else None
        if rows is not None:
            assert len(rows) == 2
        for chunk in (4096, 1 << 20):
            monkeypatch.setattr(fastscan, "CHUNK", chunk)
            got = InNetworkParser(cfg, "t.json").parse_fast(io.BytesIO(raw.encode()))
            assert got.qa.rows == 2 and got.items_seen == 1


def test_gzip_bytes_under_a_json_name_are_sized_from_the_trailer(tmp_path):
    """A gzip-encoded download is stored as sent, so a '.json' name can hold
    gzip bytes; the estimate must still see the real size (big-file routing
    keeps the reference table out of memory)."""
    import gzip

    from mrfx.sniff import _estimate_uncompressed

    body = b'{"in_network": [' + b",".join([b'{"billing_code": "99213"}'] * 100_000) + b"]}"
    f = tmp_path / "x_rates.json"
    f.write_bytes(gzip.compress(body))
    assert _estimate_uncompressed(f, f.stat().st_size) == len(body)
    plain = tmp_path / "y.json"
    plain.write_bytes(body)
    assert _estimate_uncompressed(plain, len(body)) == len(body)


def test_remote_location_references_are_counted_identically(cfg, tmp_path, monkeypatch):
    """A reference entry carrying only a `location` URL is not downloaded.
    The groups that depend on it are skipped AND counted as remote, the same
    on every path, so the Files tab can say why (not "missing companion")."""
    refs = [{"provider_group_id": 1, "location": "https://payer.example/refs/1.json"},
            {"provider_group_id": 2, "provider_groups": G},
            {"provider_group_id": 3, "location": "https://payer.example/refs/3.json"}]
    items = [_item("97110", refs=[1]),          # remote only        -> skipped, remote
             _item("97140", refs=[1, 2]),       # remote + resolvable -> emitted
             _item("97112", refs=[3, 99]),      # remote + missing   -> skipped, remote
             _item("97530", refs=[99])]         # missing only       -> skipped, not remote
    for doc in ({"provider_references": refs, "in_network": items},
                {"in_network": items, "provider_references": refs}):
        data = json.dumps(doc).encode()
        _assert_same(cfg, data, monkeypatch)
        keep, _ = skim_needed_ref_ids(cfg, io.BytesIO(data))
        _assert_same(cfg, data, monkeypatch, keep=keep)
        want, _ = _classic_two_pass(cfg, data)
        got, _, _ = _big_fast(cfg, data, tmp_path)
        assert got.qa.to_dict() == want.qa.to_dict()
        assert want.ref_groups_skipped == 3 and want.qa.remote_ref_groups == 2
        assert got.ref_groups_skipped == 3 and got.qa.remote_ref_groups == 2


@pytest.mark.parametrize("enc", ["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", "utf-16", "utf-32"])
def test_utf16_and_utf32_files_fail_fast_with_a_plain_message(cfg, enc):
    """Both parsers reject non-UTF-8 JSON. The fast path used to scan the
    WHOLE file and then blame a "corrupted or doubled download" (no BOM), or
    decline so the classic parser re-read it (BOM). Fail at once, plainly."""
    data = json.dumps({"in_network": [_item("97110", groups=G)]}).encode(enc)
    with pytest.raises(Exception):
        InNetworkParser(cfg, "t.json").parse(io.BytesIO(data))

    class Counting(io.BytesIO):
        n = 0

        def read(self, k=-1):
            b = super().read(k)
            Counting.n += len(b)
            return b
    with pytest.raises(ValueError, match="not UTF-8"):
        InNetworkParser(cfg, "t.json").parse_fast(Counting(data))


def test_a_stray_nul_is_not_blamed_on_utf16(cfg):
    raw = json.dumps({"in_network": [_item("97110", groups=G, name="a\u0000b")]}).encode()
    raw = raw.replace(b"\\u0000", b"\x00")
    with pytest.raises(Exception) as e:
        InNetworkParser(cfg, "t.json").parse_fast(io.BytesIO(raw))
    assert "UTF-8" not in str(e.value)


def test_late_header_contract(cfg, tmp_path, monkeypatch):
    """CONTRACT (AI_HANDOFF Step 3): when the header comes AFTER in_network,
    - streaming paths (classic parse, parse_fast) stamp rows already FLUSHED
      before the header arrived with the preflight seed, and later rows with
      the real header (there is no going back to rewrite a flushed batch);
    - the big-file single read emits every row after the full read, so EVERY
      row carries the real header.
    Both stamp the real header on every row whenever all rows fit one batch."""
    monkeypatch.setattr(InNetworkParser, "BATCH_ROWS", 1)
    doc = {"in_network": [_item("97110", groups=G), _item("97140", groups=G)],
           "reporting_entity_name": "Late Payer", "last_updated_on": "2026-03-01"}
    data = json.dumps(doc).encode()
    seed = {"payer": "Seeded", "schema_version": None, "last_updated_on": None}
    for streaming in ("parse", "parse_fast"):
        rows = _Rows()
        p = InNetworkParser(cfg, "x.json", sink=rows, header_defaults=seed)
        getattr(p, streaming)(io.BytesIO(data))
        assert {r["payer"] for r in rows} == {"Seeded"}, streaming   # all flushed early
    from mrfx.parser import run_big_file_fast
    rows = _Rows()
    p = InNetworkParser(cfg, "x.json", sink=rows, header_defaults=seed)
    run_big_file_fast(cfg, p, _CountingOpen(data), tmp_path / ".x.1.spool.tmp")
    assert {r["payer"] for r in rows} == {"Late Payer"}
    assert {r["file_month"] for r in rows} == {"2026-03"}


def test_row_batch_without_numpy_builds_the_same_table(cfg, monkeypatch):
    """RowBatch serves the CLASSIC path too, so an install predating the
    numpy pin must still write rates: the pure-Python index expansion has to
    build the identical table."""
    import builtins

    import pyarrow as pa

    monkeypatch.setattr(InNetworkParser, "BATCH_ROWS", 7)
    batches = []
    InNetworkParser(cfg, "x.json", sink=batches.append).parse(io.BytesIO(json.dumps(HOSTILE).encode()))
    data = json.dumps({"provider_references": [{"provider_group_id": i, "provider_groups": [
        {"npi": [1800000000 + i, 1900000000 + i], "tin": {"type": "ein", "value": f"5{i:08d}"}},
        {"npi": [], "tin": {"type": "ein", "value": f"6{i:08d}"}}]} for i in range(5)],
        "in_network": [_item("97110", refs=[0, 1, 2]), _item("97140", refs=[3, 4], groups=G)]}).encode()
    InNetworkParser(cfg, "y.json", sink=batches.append).parse(io.BytesIO(data))
    with_np = [batch_to_table(b, RATES_SCHEMA) for b in batches]
    real_import = builtins.__import__

    def no_numpy(name, *a, **k):
        if name == "numpy" or name.startswith("numpy."):
            raise ImportError("No module named 'numpy'")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_numpy)
    without = [batch_to_table(b, RATES_SCHEMA) for b in batches]
    assert len(with_np) > 2
    for a, b, batch in zip(with_np, without, batches):
        assert a.equals(b)
        assert a.equals(pa.Table.from_pylist(list(batch), schema=RATES_SCHEMA))
