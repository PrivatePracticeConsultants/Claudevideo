"""One place that decides what a US state means.

Every state-scoped feature used to do `str(value).strip().upper()[:2]` and then
check the length. That truncates instead of validating: "MISSOURI" silently
becomes "MI" — a real, different state — so a user who types the state name out
gets Michigan's schedules, Michigan's hospitals and Michigan's peers presented
as their own. A wrong answer that looks right is the one failure this app must
never produce, so a state is now either recognised or refused.

Full names are accepted on purpose: the person using this types "Missouri".
"""
from __future__ import annotations

__all__ = ["STATE_CODES", "state_code", "state_code_or_none"]

# USPS codes: 50 states + DC + the territories that appear in NPPES/CMS files.
_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "washington dc": "DC", "washington d c": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR", "virgin islands": "VI", "us virgin islands": "VI",
    "guam": "GU", "american samoa": "AS", "northern mariana islands": "MP",
}
STATE_CODES = frozenset(_NAME_TO_CODE.values())


def state_code(value, *, field: str = "state") -> str | None:
    """Return a USPS code, or None for a blank. Raise ValueError on nonsense.

    Accepts "mo", " MO ", "Missouri". Refuses "MISSOURI"-style truncation bait,
    numbers, dicts, and codes that are not real states — none of which can be
    turned into a correct answer, so none of them may be guessed at.
    """
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, set, bool)):
        raise ValueError(f"{field} must be a two-letter state code or a state "
                         f"name, not {type(value).__name__}")
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) == 2 and text.isalpha():
        code = text.upper()
        if code not in STATE_CODES:
            raise ValueError(f"{text.upper()!r} is not a US state code")
        return code
    key = "".join(c if c.isalpha() or c.isspace() else " " for c in text.lower())
    hit = _NAME_TO_CODE.get(" ".join(key.split()))
    if hit:
        return hit
    # echo back what they typed, but never paste a whole pasted document into
    # an error message — fuzzing sent a 1 000-character "state"
    shown = text if len(text) <= 40 else text[:40] + "…"
    raise ValueError(
        f"could not read {shown!r} as a state — use the two-letter code "
        f"(for example MO) or the full name (Missouri)")


def state_code_or_none(value) -> str | None:
    """The lenient twin, for row data where one bad cell must not stop a load."""
    try:
        return state_code(value)
    except ValueError:
        return None
