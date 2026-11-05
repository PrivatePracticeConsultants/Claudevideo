"""Byte-level item splitter for in-network MRFs: the fast path past ijson.

Why this exists: the ijson C tokenizer still hands EVERY token back to a
Python loop, so any event-based parse has a floor of ~25 s per uncompressed GB
before it does any work at all — and >95% of an in-network file is billing
codes (and provider references) we are about to throw away. This module finds
the boundaries of the top-level `in_network[]` and `provider_references[]`
members with vectorised numpy over raw bytes (string-aware, escape-aware), peeks
at each item's own `billing_code` / `billing_code_type` / `provider_group_id`
with a regex, and only hands TARGET members to `json.loads`. Non-target members
are never tokenized in Python and never materialized.

Memory: bounded by the chunk size plus the largest TARGET item (the ijson path
also materializes target items whole, via ObjectBuilder), so the peak-memory
envelope is unchanged. Non-target items are dropped the moment their code is
known; an item whose code key comes late is buffered only until that key.

Correctness: the produced item dicts are fed to the SAME `_handle_item` /
`_add_ref` code as the ijson path, so every cleaning/QA rule is shared. The
equivalence (identical row multiset) is pinned by tests/mrfx/test_fastscan.py.
Anything the splitter cannot vouch for (not a JSON object at the top, a
top-level shape it does not recognise) raises `FastScanUnsupported` BEFORE any
row is emitted, and the caller falls back to the ijson path.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable, Iterator

import numpy as np

CHUNK = 8 * 1024 * 1024  # numpy temporaries are ~10x this; 8 MB keeps them small

_QUOTE, _BSLASH = 0x22, 0x5C
_LBRACE, _RBRACE, _LBRACK, _RBRACK = 0x7B, 0x7D, 0x5B, 0x5D

# a member's OWN keys sit at depth 3 (doc { =1, array [ =2, member { =3)
_CODE_RE = re.compile(rb'"billing_code"\s*:\s*(?:"((?:[^"\\]|\\.)*)"|(-?[0-9][0-9.eE+-]*))')
_TYPE_RE = re.compile(rb'"billing_code_type"\s*:\s*(?:"((?:[^"\\]|\\.)*)"|(-?[0-9][0-9.eE+-]*))')
_REFID_RE = re.compile(rb'"provider_group_id"\s*:\s*(?:"((?:[^"\\]|\\.)*)"|(-?[0-9][0-9.eE+-]*))')
_HEADER_RE = re.compile(rb'"(reporting_entity_name|version|last_updated_on)"\s*:\s*"((?:[^"\\]|\\.)*)"')
_KEY_BEFORE_RE = re.compile(rb'"((?:[^"\\]|\\.)*)"\s*:\s*$')


_STRUCT_LUT = np.zeros(256, dtype=np.int8)
_STRUCT_LUT[[_LBRACE, _LBRACK]] = 1
_STRUCT_LUT[[_RBRACE, _RBRACK]] = -1


def _has_backslash(buf: bytes) -> bool:
    return buf.find(b"\\") != -1


def _drop_escaped(a: np.ndarray, q_idx: np.ndarray, esc_carry: int):
    """Remove escaped quotes (odd backslash run before them) from the quote
    positions; return them plus the chunk's trailing backslash run. Only
    quotes directly after a backslash are examined (vectorised pre-filter)."""
    n = len(a)
    if len(q_idx):
        prev = np.where(q_idx > 0, a[np.maximum(q_idx - 1, 0)], 0)
        cand = q_idx[prev == _BSLASH].tolist()
        if esc_carry and q_idx[0] == 0:
            cand.insert(0, 0)
        bad = []
        for qi in cand:
            j = qi - 1
            while j >= 0 and a[j] == _BSLASH:
                j -= 1
            run = (qi - 1 - j) + (esc_carry if j < 0 else 0)
            if run & 1:
                bad.append(qi)
        if bad:
            q_idx = np.setdiff1d(q_idx, np.asarray(bad, dtype=q_idx.dtype), assume_unique=True)
    k = n - 1
    while k >= 0 and a[k] == _BSLASH:
        k -= 1
    run = n - 1 - k
    return q_idx, run + (esc_carry if k < 0 else 0)


class FastScanUnsupported(Exception):
    """The splitter can't vouch for this file's shape — use the ijson path."""


def _unescape(raw: bytes) -> str:
    if b"\\" not in raw:
        # the common case: no escapes — a plain UTF-8 decode is the same
        # string json would produce (control chars are rejected the same way)
        v = raw.decode("utf-8")
        if _CTRL_RE.search(raw):
            raise ValueError("invalid control character inside a JSON string")
        return v
    v = json.loads(b'"' + raw + b'"')
    return _fix_surrogates(v) if _SURROGATE_RE.search(raw) else v


# --- decoding a kept member: the classic (yajl) path REJECTS these, so the
# fast path must too — a NaN/inf rate or an overflowing int must fail the file,
# never reach a row (a NaN in a median is a silent wrong number). The checks
# only run when a cheap regex says the member could contain one.
_SUSPECT_RE = re.compile(rb"NaN|Infinity|[0-9]{19}|[eE][+-]?[0-9]{3}")
_SURROGATE_RE = re.compile(rb"\\u[dD][89abcdefABCDEF]")
_INT64_MIN, _INT64_MAX = -(2 ** 63), 2 ** 63 - 1
_CTRL_RE = re.compile(rb"[\x00-\x1f]")
_INT_RE = re.compile(rb"-?(?:0|[1-9][0-9]*)")


def _reject_constant(name: str):
    raise ValueError(f"invalid JSON number {name!r} (NaN/Infinity are not JSON)")


def _finite_float(text: str) -> float:
    v = float(text)
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"numeric overflow: {text[:40]}")
    return v


def _int64(text: str) -> int:
    v = int(text)
    if not _INT64_MIN <= v <= _INT64_MAX:
        raise ValueError(f"integer overflow: {text[:40]}")
    return v


def _fix_surrogates(v):
    """Lone UTF-16 surrogates (from a bad \\uD800 escape) can't be stored as
    UTF-8 and would crash the Parquet write; replace them with '?' (what the
    classic parser does for the common case)."""
    if isinstance(v, str):
        # json.loads already joins VALID pairs, so any surrogate left is lone
        return "".join("?" if "\ud800" <= ch <= "\udfff" else ch for ch in v)
    if isinstance(v, list):
        return [_fix_surrogates(x) for x in v]
    if isinstance(v, dict):
        return {_fix_surrogates(k): _fix_surrogates(x) for k, x in v.items()}
    return v


def decode_member(data: bytes):
    """json.loads for one kept member, held to the classic parser's rules."""
    if _SUSPECT_RE.search(data):
        v = json.loads(data, parse_constant=_reject_constant,
                       parse_float=_finite_float, parse_int=_int64)
    else:
        v = json.loads(data)
    return _fix_surrogates(v) if _SURROGATE_RE.search(data) else v


def _value(m, str_group: int = 1, num_group: int = 2):
    """A regex-captured JSON scalar as the Python value ijson would hand the
    event loop: str for a string, int/float for a number."""
    if m.group(str_group) is not None:
        return _unescape(m.group(str_group))
    raw = m.group(num_group)
    try:
        if _INT_RE.fullmatch(raw):
            return int(raw)
        return json.loads(raw)
    except ValueError:
        return raw.decode()


class _Member:
    """A top-level array member being collected."""

    __slots__ = ("array", "parts", "decided", "keep", "peek")

    def __init__(self, array: str):
        self.array = array          # 'in_network' | 'provider_references' | other
        self.parts: list[bytes] = []
        self.decided = False        # keep/skip known?
        self.keep = False
        self.peek = b""             # bytes seen so far while undecided


def iter_members(
    stream,
    want_item: Callable[[list, bool], bool | None],
    want_ref: Callable[[object], bool] | None,
    on_header: Callable[[str, str], None] | None = None,
    counts: dict | None = None,
    chunk_size: int | None = None,
    target_codes: set | None = None,
    ref_keep: set | None = None,
    on_type_other: Callable[[int], None] | None = None,
) -> Iterator[tuple[str, bytes | None]]:
    """Yield ('in_network' | 'provider_references', member_bytes) for every
    top-level member the predicates keep, and (array_name, None) when a
    top-level array closes. `counts` (if given) tallies ALL members per array.

    want_item(events, final) gets the member's own billing_code /
    billing_code_type keys seen so far, in stream order, as
    [("code"|"type", value), ...]: True keep, False skip, None undecided-yet.
    At the member's end (final=True) it must return a bool. want_ref(provider_group_id_raw)
    likewise for refs; None means "skip every reference member".

    target_codes / ref_keep are OPTIONAL accelerators that must agree with
    the predicates: with them, members whose verdict is provably "skip, no
    side effects" (a plain non-target code with a plain CPT/HCPCS type or none;
    a plain reference id outside the kept set; a plain other-code-system type)
    are dropped in bulk with numpy, never reaching Python one by one; the one
    QA side effect bulk skips can have (billing_type_other) is reported via
    on_type_other(n). Everything else still goes through the predicates.
    """
    depth = 0           # structural depth BEFORE the next byte
    in_str = False      # inside a string BEFORE the next byte
    esc_carry = 0       # trailing backslash run length from the previous chunk
    tail = b""          # last bytes of previous chunk (for key-before-'[' lookback)
    zone_buf = b""      # header-zone bytes not yet scanned (spans chunks)
    cur_array: str | None = None
    member: _Member | None = None
    seen_top = False
    doc_closed = False
    trailing_garbage = False
    if counts is None:
        counts = {}
    if chunk_size is None:
        chunk_size = CHUNK  # read at call time so tests can shrink it
    bulk = _Bulk(target_codes, ref_keep)

    while True:
        buf = stream.read(chunk_size)
        if not buf:
            break
        if not seen_top:
            stripped = buf.lstrip(b"\xef\xbb\xbf \t\r\n")
            if not stripped:
                continue
            if stripped[:1] != b"{":
                raise FastScanUnsupported("top level is not a JSON object")
            seen_top = True
        a = np.frombuffer(buf, dtype=np.uint8)
        q_idx = np.flatnonzero(a == _QUOTE)
        if esc_carry or _has_backslash(buf):
            q_idx, esc_carry = _drop_escaped(a, q_idx, esc_carry)
        # structural bytes via one table lookup, then string-awareness by
        # counting quotes before each one (sparse: no per-byte cumsum)
        v = _STRUCT_LUT[a]
        s_idx = np.flatnonzero(v)
        start_parity = 1 if in_str else 0
        outside = ((np.searchsorted(q_idx, s_idx) + start_parity) & 1) == 0
        struct_idx = s_idx[outside]
        delta = v[struct_idx].astype(np.int64)
        depth_after = depth + np.cumsum(delta)
        depth_before = depth_after - delta
        n_quotes = len(q_idx)

        # member opens/closes (2<->3) are handled in bulk per segment; only
        # the rare transitions touching depth <= 1 (array/document open and
        # close) are walked one by one
        opens = struct_idx[(depth_before == 2) & (depth_after == 3)]
        closes = struct_idx[(depth_before == 3) & (depth_after == 2)]
        top = np.minimum(depth_before, depth_after) <= 1
        t_pos = struct_idx[top].tolist()
        t_db = depth_before[top].tolist()
        t_da = depth_after[top].tolist()
        ck = _ChunkKeys(bulk, a, q_idx, start_parity, struct_idx, depth_after, depth)
        ck.on_type_other = on_type_other

        cursor = 0   # where the open (partial) member's unread bytes start
        seg_lo = 0
        # depth-1 "zone" = top-level scalar keys (the header) between arrays
        zone = 0 if depth == 1 else None
        for pos, db, da in zip(t_pos + [len(buf)], t_db + [None], t_da + [None]):
            # ---- members in [seg_lo, pos): all belong to cur_array ----
            o = opens[np.searchsorted(opens, seg_lo):np.searchsorted(opens, pos)]
            c = closes[np.searchsorted(closes, seg_lo):np.searchsorted(closes, pos)]
            if member is not None and len(c):
                # the member left open by an earlier chunk closes here
                end = int(c[0])
                counts[member.array] = counts.get(member.array, 0) + 1
                done = _feed(member, buf[cursor:end + 1], want_item, want_ref, final=True)
                if done is not None:
                    yield member.array, done
                member = None
                c = c[1:]
            if member is None and cur_array is not None:
                n_whole = len(c)
                if n_whole:
                    yield from _whole_members(cur_array, o[:n_whole], c, a, buf, ck,
                                              want_item, want_ref, counts)
                if len(o) > n_whole:  # one member opens and runs past the chunk
                    st = int(o[n_whole])
                    member = _new_member(cur_array, buf[st], want_ref)
                    cursor = st
            if db is None:
                break
            # ---- the depth <= 1 transition itself ----
            if db == 0 and da == 1:
                zone = pos + 1
            elif db == 1 and da == 0:
                if zone is not None and on_header is not None:
                    _headers(zone_buf + buf[zone:pos], on_header)
                zone, zone_buf = None, b""
            elif db == 1 and da == 2:  # a top-level array (or object) value opens
                if zone is not None and on_header is not None:
                    _headers(zone_buf + buf[zone:pos], on_header)
                zone, zone_buf = None, b""
                if buf[pos] != _LBRACK:
                    cur_array = None   # a top-level OBJECT value: no members
                else:
                    # the key may straddle any number of chunks: `tail` rolls
                    look = (tail + buf[max(0, pos - 512):pos])[-512:]
                    m = _KEY_BEFORE_RE.search(look)
                    cur_array = _unescape(m.group(1)) if m else None
            elif db == 2 and da == 1:
                if cur_array is not None:
                    yield cur_array, None   # array-closed marker
                cur_array = None
                zone = pos + 1
            seg_lo = pos + 1
        if member is not None:
            _feed(member, buf[cursor:], want_item, want_ref, final=False)
            cursor = 0
        if zone is not None and on_header is not None:
            # carry the open header zone; scanned once when it closes. Real
            # headers are bytes long — cap a pathological zone, keeping a
            # tail so a key split at the cut is still seen whole
            zone_buf += buf[zone:]
            if len(zone_buf) > _ZONE_CAP:
                _headers(zone_buf, on_header)
                zone_buf = zone_buf[-512:]
        if not trailing_garbage:
            # bytes after the document closed (depth back to 0) must be
            # whitespace — the classic parser fails on "trailing garbage"
            ends = np.flatnonzero(depth_after == 0)
            after = buf[int(struct_idx[ends[0]]) + 1:] if len(ends) else (buf if doc_closed else b"")
            if after.strip(b" \t\r\n"):
                trailing_garbage = True
            if len(ends):
                doc_closed = True
        if len(struct_idx):
            depth = int(depth_after[-1])
        in_str = bool((n_quotes + start_parity) & 1)
        tail = (tail + buf[-512:])[-512:]
        del a, q_idx, v, s_idx, outside, ck
    if not seen_top:
        raise FastScanUnsupported("empty file")
    if trailing_garbage:
        raise ValueError("unexpected bytes after the end of the JSON document — "
                         "a corrupted or doubled download")
    if depth != 0 or in_str:
        # NOT FastScanUnsupported: rows may already be emitted, and the ijson
        # path would fail this file too — fail it the same way
        raise ValueError("file ended mid-structure — truncated download or not valid JSON")


def _new_member(cur_array: str, first_byte: int, want_ref) -> _Member:
    if first_byte != _LBRACE:
        # a non-object member (an array in in_network[]): the ijson path never
        # sees an `.item` map here and ignores it — and doesn't count it
        m = _Member("__non_object__")
        m.decided, m.keep = True, False
        return m
    m = _Member(cur_array)
    if cur_array not in ("in_network", "provider_references") or (
            cur_array == "provider_references" and want_ref is None):
        m.decided, m.keep = True, False
    return m


def _whole_members(cur_array, o, c, a, buf, ck, want_item, want_ref, counts):
    """Members opening at o[i] and closing at c[i], all inside this chunk."""
    is_obj = a[o] == _LBRACE
    n_obj = int(is_obj.sum())
    if n_obj:
        counts[cur_array] = counts.get(cur_array, 0) + n_obj
    if cur_array == "in_network":
        check, n_type_other = ck.items_needing_a_look(o, c)
        check &= is_obj
        if n_type_other and ck.on_type_other is not None:
            ck.on_type_other(n_type_other)
    elif cur_array == "provider_references" and want_ref is not None:
        check = ck.refs_needing_a_look(o, c) & is_obj
    else:
        return
    for st, en in zip(o[check].tolist(), c[check].tolist()):
        data = buf[st:en + 1]
        if cur_array == "in_network":
            keep = _decide_item(data, want_item, final=True)
        else:
            keep = _decide_ref(data, want_ref, final=True)
        if keep:
            yield cur_array, data


_WS_LUT = np.zeros(256, dtype=bool)
_WS_LUT[[0x20, 0x09, 0x0A, 0x0D]] = True
_ALNUM_LUT = np.zeros(256, dtype=bool)
_ALNUM_LUT[np.frombuffer(b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", np.uint8)] = True
_TYPE_LUT = _ALNUM_LUT.copy()
_TYPE_LUT[np.frombuffer(b"-_./", np.uint8)] = True
_DIGIT_LUT = np.zeros(256, dtype=bool)
_DIGIT_LUT[np.frombuffer(b"0123456789", np.uint8)] = True
_UPPER_LUT = np.arange(256, dtype=np.uint8)
_UPPER_LUT[ord("a"):ord("z") + 1] -= 32
# bytes that may legally follow a JSON number
_NUM_END_LUT = np.zeros(256, dtype=bool)
_NUM_END_LUT[np.frombuffer(b",}] \t\r\n", np.uint8)] = True


class _Bulk:
    """Per-file constants for the bulk skip decisions."""

    def __init__(self, target_codes, ref_keep):
        self.codes = None
        self.width = 8
        if target_codes is not None:
            codes = [str(x).upper().encode() for x in target_codes]
            self.width = max([8] + [len(x) for x in codes])
            self.codes = np.array(codes, dtype=f"S{self.width}")
        self.refs = None
        if ref_keep is not None:
            self.refs = np.array(sorted(r for r in ref_keep if _INT64_MIN <= r <= _INT64_MAX),
                                 dtype=np.int64)


class _ChunkKeys:
    """Positions and plain values of every member-own-level key in a chunk,
    found with numpy at the chunk's quote positions (no Python per key).
    Computed lazily, once per chunk."""

    def __init__(self, bulk: _Bulk, a, q_idx, start_parity, struct_idx, depth_after, depth0):
        self.bulk, self.a, self.n = bulk, a, len(a)
        self.q_idx = q_idx
        self.struct_idx, self.depth_after, self.depth0 = struct_idx, depth_after, depth0
        self.start_parity = start_parity
        self._open_q = None
        self._code = self._type = self._ref = None
        self._bs = None

    @property
    def open_q(self):
        # opening quotes only (an even number of real quotes before them)
        if self._open_q is None:
            par = (np.arange(len(self.q_idx)) + self.start_parity) & 1
            self._open_q = self.q_idx[par == 0]
        return self._open_q

    def _keys(self, lit: bytes):
        """Opening-quote positions of the key `lit` (e.g. b'"billing_code"')
        at a member's own level (document depth 3), and where each value
        starts (-1 if the key/colon layout isn't a simple one)."""
        a, n = self.a, self.n
        cand = self.open_q
        cand = cand[cand + len(lit) + 1 < n]
        # cheap pre-filter on two distinctive bytes, then the full literal
        ok = (a[cand + 1] == lit[1]) & (a[cand + len(lit) - 1] == _QUOTE)
        cand = cand[ok]
        ok = np.ones(len(cand), dtype=bool)
        for j, ch in enumerate(lit):
            ok &= a[cand + j] == ch
        p = cand[ok]
        if len(p):
            k = np.searchsorted(self.struct_idx, p)
            d = np.where(k > 0, self.depth_after[np.maximum(k - 1, 0)], self.depth0)
            p = p[d == 3]
        # value start: skip <=2 whitespace, ':', <=4 whitespace
        j = p + len(lit)
        for _ in range(2):
            j = j + _WS_LUT[a[np.minimum(j, n - 1)]]
        colon = (j < n) & (a[np.minimum(j, n - 1)] == ord(":"))
        j = j + 1
        for _ in range(4):
            j = j + _WS_LUT[a[np.minimum(j, n - 1)]]
        vs = np.where(colon & (j < n), j, -1)
        return p, vs

    def _string_values(self, vs, width: int, charset=_ALNUM_LUT):
        """For values starting at vs: (is_plain, upper-cased padded bytes)
        where plain = a quoted string of 0..width chars from `charset`
        ([A-Za-z0-9] by default)."""
        a, n = self.a, self.n
        if not len(vs):
            return np.zeros(0, bool), np.zeros(0, dtype=f"S{width}")
        vs0 = np.maximum(vs, 0)
        is_str = (vs >= 0) & (a[vs0] == _QUOTE)
        # closing quote = next real quote after the opening one
        qi = np.searchsorted(self.q_idx, vs0, side="right")
        endq = np.where(qi < len(self.q_idx), self.q_idx[np.minimum(qi, len(self.q_idx) - 1)], n)
        length = endq - vs0 - 1
        plain = is_str & (endq < n) & (length >= 0) & (length <= width)
        cols = (vs0 + 1)[:, None] + np.arange(width)[None, :]
        mat = a[np.minimum(cols, n - 1)]
        inside = np.arange(width)[None, :] < length[:, None]
        plain &= np.all(charset[mat] | ~inside, axis=1)
        up = np.where(inside, _UPPER_LUT[mat], 0).astype(np.uint8)
        return plain, np.ascontiguousarray(up).view(f"S{width}").ravel()

    @staticmethod
    def _per_member(p, o, c):
        """Member index for each key position (or -1)."""
        mi = np.searchsorted(o, p, side="right") - 1
        valid = (mi >= 0) & (p < c[np.maximum(mi, 0)])
        return np.where(valid, mi, -1)

    def _has_backslash(self, o, c):
        if self._bs is None:
            self._bs = np.flatnonzero(self.a == _BSLASH)
        if not len(self._bs):
            return np.zeros(len(o), dtype=bool)
        return np.searchsorted(self._bs, c) > np.searchsorted(self._bs, o)

    def items_needing_a_look(self, o, c):
        """(need, n_type_other): need[i] is True unless member i is provably
        a skip whose only side effect is known —
        * exactly one own-level billing_code, a plain string NOT in the
          target set, and no own-level billing_code_type or exactly one plain
          CPT*/HCPCS*/empty one: skipped, no QA effect; or
        * exactly one own-level billing_code_type, plain and of ANOTHER code
          system (MS-DRG, HIPPS, RC...), and the code plain or absent: always
          skipped, and the classic parser counts it in billing_type_other iff
          the type streams first, the code is absent, or the code is a target
          (it checks a target code, keeps going, then meets the type).
        Members with any backslash are always looked at (escaped keys)."""
        b = self.bulk
        m = len(o)
        if b.codes is None or not m:
            return np.ones(m, dtype=bool), 0
        if self._code is None:
            p, vs = self._keys(b'"billing_code"')
            plain, up = self._string_values(vs, b.width)
            target = plain & np.isin(up, b.codes)
            tp, tvs = self._keys(b'"billing_code_type"')
            tplain, tup = self._string_values(tvs, 16, _TYPE_LUT)
            fam_ok = ((tup == b"") | np.char.startswith(tup, b"CPT")
                      | np.char.startswith(tup, b"HCPCS"))
            self._code = (p, plain, target)
            self._type = (tp, tplain & fam_ok, tplain & ~fam_ok)
        p, plain, target = self._code
        tp, fine, other = self._type
        mi = self._per_member(p, o, c)
        sel = mi >= 0
        mis = mi[sel]
        n_code = np.bincount(mis, minlength=m)
        n_plain = np.bincount(mis, weights=plain[sel], minlength=m)
        n_target = np.bincount(mis, weights=target[sel], minlength=m)
        code_pos = np.full(m, -1, dtype=np.int64)
        code_pos[mis] = p[sel]
        tmi = self._per_member(tp, o, c)
        tsel = tmi >= 0
        tmis = tmi[tsel]
        n_type = np.bincount(tmis, minlength=m)
        n_fine = np.bincount(tmis, weights=fine[tsel], minlength=m)
        n_other = np.bincount(tmis, weights=other[tsel], minlength=m)
        type_pos = np.full(m, -1, dtype=np.int64)
        type_pos[tmis] = tp[tsel]
        clean = ~self._has_backslash(o, c)
        skip_fine = ((n_code == 1) & (n_plain == 1) & (n_target == 0)
                     & ((n_type == 0) | ((n_type == 1) & (n_fine == 1))))
        skip_other = ((n_type == 1) & (n_other == 1)
                      & ((n_code == 0) | ((n_code == 1) & (n_plain == 1))))
        skippable = clean & (skip_fine | skip_other)
        counted = skippable & skip_other & (
            (n_code == 0) | (type_pos < code_pos) | (n_target == 1))
        return ~skippable, int(counted.sum())

    def refs_needing_a_look(self, o, c):
        """Bool per reference member: True unless exactly one own-level
        provider_group_id whose plain integer value is NOT kept."""
        b = self.bulk
        if b.refs is None or not len(o):
            return np.ones(len(o), dtype=bool)
        if self._ref is None:
            a, n = self.a, self.n
            p, vs = self._keys(b'"provider_group_id"')
            width = 18                      # int64-safe digit count
            vs0 = np.maximum(vs, 0)
            cols = vs0[:, None] + np.arange(width + 1)[None, :]
            mat = a[np.minimum(cols, n - 1)]
            dig = _DIGIT_LUT[mat] & (cols < n)
            ln = np.argmin(dig, axis=1)     # digits before the first non-digit
            rows = np.arange(len(vs))
            term = mat[rows, ln] if len(vs) else np.zeros(0, np.uint8)
            term_ok = _NUM_END_LUT[term] & (cols[rows, ln] < n) if len(vs) else np.zeros(0, bool)
            plain = (vs >= 0) & (ln >= 1) & term_ok & ~((ln > 1) & (mat[:, 0] == ord("0")))
            pw = 10 ** np.arange(width - 1, -1, -1, dtype=np.int64)
            digits = np.where(np.arange(width)[None, :] < ln[:, None],
                              mat[:, :width].astype(np.int64) - 48, 0)
            val = (digits * pw[None, :]).sum(axis=1) // (10 ** (width - ln.astype(np.int64)))
            self._ref = (p, plain & ~np.isin(val, b.refs))
        p, skip = self._ref
        m = len(o)
        mi = self._per_member(p, o, c)
        sel = mi >= 0
        n_id = np.bincount(mi[sel], minlength=m)
        n_skip = np.bincount(mi[sel], weights=skip[sel], minlength=m)
        return ~((n_id == 1) & (n_skip == 1) & ~self._has_backslash(o, c))


def _headers(chunk: bytes, on_header) -> None:
    for m in _HEADER_RE.finditer(chunk):
        on_header(m.group(1).decode(), _unescape(m.group(2)))


def _depth_before(data: bytes) -> np.ndarray:
    """Structural depth before every byte of a member's bytes (string- and
    escape-aware). Only built on a regex hit, once per peek."""
    a = np.frombuffer(data, dtype=np.uint8)
    quote = a == _QUOTE
    bs = a == _BSLASH
    cand = (np.flatnonzero(quote[1:] & bs[:-1]) + 1).tolist() if len(a) > 1 else []
    if cand:
        quote = quote.copy()
        for qi in cand:
            j = qi - 1
            while j >= 0 and a[j] == _BSLASH:
                j -= 1
            if (qi - 1 - j) & 1:
                quote[qi] = False
    qcum = np.cumsum(quote, dtype=np.int64)
    inside = ((qcum - quote) & 1).astype(bool) | quote
    d = np.zeros(len(a), dtype=np.int64)
    d[~inside & ((a == _LBRACE) | (a == _LBRACK))] = 1
    d[~inside & ((a == _RBRACE) | (a == _RBRACK))] = -1
    after = np.cumsum(d)
    return after - d


def _own_keys(regex, data: bytes, final: bool):
    """All own-level matches of `regex` in member bytes, in order, and
    whether the scan is complete (a non-final peek stops at a match that
    touches the end — its value may be cut)."""
    out = []
    for m in regex.finditer(data):
        if not final and m.end() >= len(data):
            return out, False
        st = m.start()
        if (data.find(b"{", 1, st) == -1 and data.find(b"[", 0, st) == -1
                and data.find(b"}", 0, st) == -1 and data.find(b"]", 0, st) == -1):
            out.append(m)        # nothing structural before it: own level
        elif _depth_before(data[:st + 1])[-1] == 1:
            out.append(m)
    return out, True


# a KEY spelled with a \\u escape ("billing\\u005fcode" IS billing_code to a
# JSON parser) — the literal-key regexes can't see it
_ESCAPED_KEY_RE = re.compile(rb'"(?:[^"\\]|\\[^u])*\\u(?:[^"\\]|\\.)*"\s*:')


def _replay_with_ijson(data: bytes) -> list:
    """The member's own-level code/type events exactly as the classic ijson
    loop sees them (keys unescaped by the same tokenizer)."""
    import ijson

    out = []
    for prefix, event, value in ijson.parse(io.BytesIO(data), use_float=True):
        if event in ("string", "number") and prefix in ("billing_code", "billing_code_type"):
            out.append(("code" if prefix == "billing_code" else "type", value))
    return out


def _decide_item(data: bytes, want_item, final: bool):
    """Replay the member's own billing_code / billing_code_type keys in
    stream order — every one of them, as the classic event loop sees them
    (duplicate keys included) — and ask want_item for a verdict."""
    if _ESCAPED_KEY_RE.search(data):
        # rare: let the classic tokenizer itself read the keys. A partial
        # peek waits for the member's end — an escaped key BEFORE whichever
        # key would decide is always complete in the peek, so it is seen here
        return want_item(_replay_with_ijson(data), True) if final else None
    codes, ok1 = _own_keys(_CODE_RE, data, final)
    types, ok2 = _own_keys(_TYPE_RE, data, final)
    events = sorted([(m.start(), "code", m) for m in codes] + [(m.start(), "type", m) for m in types],
                    key=lambda e: e[0])
    if not final and not events:
        return None
    return want_item([(kind, _value(m)) for _, kind, m in events], final)


def _decide_ref(data: bytes, want_ref, final: bool):
    refs, _ = _own_keys(_REFID_RE, data, final)
    if not refs:
        return False if final else None
    return bool(want_ref(_value(refs[0])))


# an undecided member's peek buffer is re-searched on every chunk; cap how
# much we re-scan so a code-at-the-end item costs O(n), not O(n^2)
_PEEK_LIMIT = 4 * 1024 * 1024
_ZONE_CAP = 1024 * 1024


def _feed(member: _Member, piece: bytes, want_item, want_ref, final: bool):
    if member.decided:
        if member.keep:
            member.parts.append(piece)
            return b"".join(member.parts) if final else None
        return None
    member.parts.append(piece)
    if len(member.peek) < _PEEK_LIMIT or final:
        member.peek = b"".join(member.parts) if final or len(member.parts) > 1 else piece
        if member.array == "in_network":
            verdict = _decide_item(member.peek, want_item, final)
        else:
            verdict = _decide_ref(member.peek, want_ref, final)
    else:
        verdict = None
    if final and verdict is None:
        verdict = False
    if verdict is None:
        return None
    member.decided, member.keep = True, bool(verdict)
    member.peek = b""
    if not member.keep:
        member.parts = []
        return None
    return b"".join(member.parts) if final else None
