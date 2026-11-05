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
    return json.loads(b'"' + raw + b'"')


def _value(m, str_group: int = 1, num_group: int = 2):
    """A regex-captured JSON scalar as the Python value ijson would hand the
    event loop: str for a string, int/float for a number."""
    if m.group(str_group) is not None:
        return _unescape(m.group(str_group))
    try:
        return json.loads(m.group(num_group))
    except ValueError:
        return m.group(num_group).decode()


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
    want_item: Callable[[str | None, str | None, str, bool], bool | None],
    want_ref: Callable[[object], bool] | None,
    on_header: Callable[[str, str], None] | None = None,
    counts: dict | None = None,
    chunk_size: int | None = None,
) -> Iterator[tuple[str, bytes | None]]:
    """Yield ('in_network' | 'provider_references', member_bytes) for every
    top-level member the predicates keep, and (array_name, None) when a
    top-level array closes. `counts` (if given) tallies ALL members per array.

    want_item(code, code_type, order, final) is called as soon as either of
    the member's own billing_code / billing_code_type keys is visible (the
    other may be None if it hasn't streamed yet; `order` says which came
    first): True keep, False skip, None undecided-yet. At the member's end
    (final=True) it must return a bool. want_ref(provider_group_id_raw) likewise for refs;
    None means "skip every reference member".
    """
    depth = 0           # structural depth BEFORE the next byte
    in_str = False      # inside a string BEFORE the next byte
    esc_carry = 0       # trailing backslash run length from the previous chunk
    tail = b""          # last bytes of previous chunk (for key-before-'[' lookback)
    zone_buf = b""      # header-zone bytes not yet scanned (spans chunks)
    cur_array: str | None = None
    member: _Member | None = None
    seen_top = False
    offset = 0
    if counts is None:
        counts = {}
    if chunk_size is None:
        chunk_size = CHUNK  # read at call time so tests can shrink it

    while True:
        buf = stream.read(chunk_size)
        if not buf:
            break
        if not seen_top:
            stripped = buf.lstrip(b"\xef\xbb\xbf \t\r\n")
            if not stripped:
                offset += len(buf)
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

        # only depth-1/2 transitions matter: array open/close at 1<->2,
        # member open/close at 2<->3
        sel = np.minimum(depth_before, depth_after) <= 2
        pos_list = struct_idx[sel].tolist()
        db_list = depth_before[sel].tolist()
        da_list = depth_after[sel].tolist()

        cursor = 0  # bytes of buf before `cursor` are already attributed
        # depth-1 "zone" = top-level scalar keys (the header) between arrays
        zone = 0 if depth == 1 else None
        for pos, db, da in zip(pos_list, db_list, da_list):
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
                # the key may straddle any number of chunks: `tail` rolls
                look = (tail + buf[max(0, pos - 512):pos])[-512:]
                if buf[pos] != _LBRACK:
                    cur_array = None   # a top-level OBJECT value: no members
                    continue
                m = _KEY_BEFORE_RE.search(look)
                cur_array = _unescape(m.group(1)) if m else None
            elif db == 2 and da == 1:
                if cur_array is not None:
                    yield cur_array, None   # array-closed marker
                cur_array = None
                zone = pos + 1
            elif db == 2 and da == 3:  # member opens
                if cur_array is None:
                    # depth 2->3 under a top-level OBJECT value (e.g. a nested
                    # header object) — not an array member; ignore it
                    continue
                if buf[pos] != _LBRACE:
                    # a non-object member (an array in in_network[]): the
                    # ijson path never sees an `.item` map here and ignores
                    # it, so do the same — and don't count it as an item
                    member = _Member("__non_object__")
                    member.decided, member.keep = True, False
                elif cur_array == "provider_references" and want_ref is None:
                    member = _Member(cur_array)
                    member.decided, member.keep = True, False
                else:
                    member = _Member(cur_array)
                    if cur_array not in ("in_network", "provider_references"):
                        member.decided, member.keep = True, False
                cursor = pos
            elif db == 3 and da == 2:  # member closes
                if member is None:
                    continue
                counts[member.array] = counts.get(member.array, 0) + 1
                piece = buf[cursor:pos + 1]
                done = _feed(member, piece, want_item, want_ref, final=True)
                if done is not None:
                    yield member.array, done
                member = None
                cursor = pos + 1
        if member is not None:
            _feed(member, buf[cursor:], want_item, want_ref, final=False)
        if zone is not None and on_header is not None:
            # carry the open header zone; scanned once when it closes. Real
            # headers are bytes long — cap a pathological zone, keeping a
            # tail so a key split at the cut is still seen whole
            zone_buf += buf[zone:]
            if len(zone_buf) > _ZONE_CAP:
                _headers(zone_buf, on_header)
                zone_buf = zone_buf[-512:]

        if len(struct_idx):
            depth = int(depth_after[-1])
        in_str = bool((n_quotes + start_parity) & 1)
        tail = (tail + buf[-512:])[-512:]
        offset += len(buf)
        del a, q_idx, v, s_idx, outside
    if not seen_top:
        raise FastScanUnsupported("empty file")
    if depth != 0 or in_str:
        # NOT FastScanUnsupported: rows may already be emitted, and the ijson
        # path would fail this file too — fail it the same way
        raise ValueError("file ended mid-structure — truncated download or not valid JSON")


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


def _find_own(regex, data: bytes, depth=None, final: bool = True):
    """First match of `regex` whose key sits at the member's OWN level
    (depth 1 inside the member: nested bundled_codes/covered_services carry
    their own billing_code keys). Returns (match, depth_array)."""
    for m in regex.finditer(data):
        if not final and m.end() >= len(data):
            # a NUMBER cut at the chunk edge ("97" of 97110) matches too —
            # never judge a value until something follows it
            return None, depth
        prefix = data[:m.start()]
        # common case, decided with C-speed byte counts: the member's own
        # '{' is the ONLY structural byte before the key, so (whatever the
        # strings hold) the key is at depth 1
        if (prefix.count(b"{") == 1 and b"[" not in prefix
                and b"}" not in prefix and b"]" not in prefix):
            return m, depth
        if _depth_before(data[:m.start() + 1])[-1] == 1:
            return m, depth
    return None, depth


def _decide_item(data: bytes, want_item, final: bool):
    m, depth = _find_own(_CODE_RE, data, final=final)
    t, depth = _find_own(_TYPE_RE, data, depth, final=final)
    if m is None and t is None and not final:
        return None
    code = ctype = None
    if m is not None:
        code = _value(m)
    if t is not None:
        ctype = _value(t)
    # key ORDER matters for mirroring the ijson path's QA counters: it
    # checks whichever of the two keys streams first
    order = "type_first" if (t is not None and (m is None or t.start() < m.start())) else "code_first"
    return want_item(code, ctype, order, final)


def _decide_ref(data: bytes, want_ref, final: bool):
    m, _ = _find_own(_REFID_RE, data, final=final)
    if m is None:
        return False if final else None
    return bool(want_ref(_value(m)))


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
