"""ONE rule for a provider_group_id / provider_references entry (review 9
item 1.4), shared by the parser, fastscan, the skim and the reference-file
parser.

Ids are integers. Going through float() — as both copies did — collapsed
every id above 2^53 into a neighbour: 1234567890123456789 and
1234567890123456790 both became 1234567890123456768, and rates attached to
the wrong practice with nothing counted. Now:
- an int stays an int (bool is not an id);
- a digit string is parsed exactly ("0012" -> 12);
- "12.0" / 12.0 count only when integral — a float only below 2^53, where
  it is exact (a float id above that has already lost its low digits);
- anything else, or outside BIGINT (the provider_refs.ref_id column), is
  None: the caller counts it as bad_ref_ids, never wraps or truncates it.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

_INT64_MIN, _INT64_MAX = -(1 << 63), (1 << 63) - 1
_FLOAT_EXACT = 1 << 53


def clean_ref_id(v) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        out = v
    elif isinstance(v, float):
        if v != v or not v.is_integer() or abs(v) >= _FLOAT_EXACT:
            return None
        out = int(v)
    else:
        t = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        t = t.strip()
        if not t:
            return None
        body = t[1:] if t[0] in "+-" else t
        if body.isdigit():
            out = int(t)
        else:
            try:
                d = Decimal(t)
            except (InvalidOperation, ValueError):
                return None
            if not d.is_finite() or d != d.to_integral_value():
                return None
            out = int(d)
    return out if _INT64_MIN <= out <= _INT64_MAX else None
