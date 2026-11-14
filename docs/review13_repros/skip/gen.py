"""Byte generators for the skip audit: rate documents with incompressible
filler (so a gzip is several MB), stored-level gzip (level 0: a one-byte
change keeps the size), multi-member gzip, zips, and helpers to place a
change OUTSIDE the early check's sampled windows."""
import io
import json
import random
import zipfile
import zlib

CODES = ("97110", "97140", "97530", "97112")


def doc(n_items=400, payer="Acme Health", day="2026-05-01", rate=40.0, code="97110",
        seed=1, filler=2000, tin0=430000000):
    rnd = random.Random(seed)
    items = []
    for i in range(n_items):
        items.append({
            "negotiation_arrangement": "ffs", "name": code, "billing_code_type": "CPT",
            "billing_code_type_version": "2026", "billing_code": code,
            "description": "".join(rnd.choice("0123456789abcdef") for _ in range(filler)),
            "negotiated_rates": [{"provider_groups": [{"npi": [1000000000 + i],
                                                      "tin": {"type": "ein", "value": str(tin0 + i)}}],
                                  "negotiated_prices": [{"negotiated_type": "negotiated",
                                                         "negotiated_rate": rate,
                                                         "billing_class": "professional",
                                                         "service_code": ["11"],
                                                         "billing_code_modifier": ["GP"],
                                                         "expiration_date": "9999-12-31"}]}]})
    d = {"reporting_entity_name": payer, "reporting_entity_type": "issuer", "last_updated_on": day,
         "version": "2.0.0", "provider_references": [], "in_network": items}
    return json.dumps(d).encode()


def gz0(raw: bytes) -> bytes:
    """gzip with STORED deflate blocks (level 0): size = raw + ~5 B / 64 KB + 18."""
    c = zlib.compressobj(0, zlib.DEFLATED, 31)
    return c.compress(raw) + c.flush()


def gzn(raw: bytes, level=6) -> bytes:
    c = zlib.compressobj(level, zlib.DEFLATED, 31)
    return c.compress(raw) + c.flush()


def change_rate_once(raw: bytes, at_least: int, old=b'"negotiated_rate": 40.0', new=b'"negotiated_rate": 47.0') -> tuple[bytes, int]:
    """Replace ONE rate value (same length) at the first occurrence past
    `at_least` bytes. Returns (new_raw, offset)."""
    i = raw.find(old, at_least)
    if i < 0:
        raise ValueError("no rate past that offset")
    return raw[:i] + new + raw[i + len(old):], i


def diff_ranges(a: bytes, b: bytes) -> list[tuple[int, int]]:
    """[start, end) byte ranges where a and b differ (same length assumed)."""
    out = []
    i, n = 0, min(len(a), len(b))
    while i < n:
        if a[i] != b[i]:
            j = i
            while j < n and a[j] != b[j]:
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    if len(a) != len(b):
        out.append((n, max(len(a), len(b))))
    return out


def windows(size: int, head: int, tail: int = 1 << 20, n=8, w=64 << 10):
    """The early check's sampled byte ranges for a file of `size` bytes."""
    rs = [(0, min(size, head)), (max(0, size - tail), size)]
    if size > w:
        for i in range(n):
            off = min(max(0, size * (i + 1) // (n + 1) - w // 2), size - w)
            rs.append((off, off + w))
    return rs


def overlaps(ranges, sampled) -> list:
    return [(r, s) for r in ranges for s in sampled if r[0] < s[1] and s[0] < r[1]]


def zip_of(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()
