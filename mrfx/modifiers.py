"""What the `billing_code_modifier` field means — ONE definition, used by the
parser (both paths), the one-time fix of rows already in a store, the Explorer's
"base modifiers only" filter, the Code page checkbox and every Benchmark-family
report.

Measured on real payer files (round 3, 32M-rate store, 9 payers):

* BCBS Alabama publishes PLACE-OF-SERVICE codes in the modifier field —
  `["21","31","22","24"]` (inpatient, SNF, outpatient hospital, ASC) on one
  price and `["32","12","11","10"]` (nursing facility, home, office,
  telehealth-home) on another, with `service_code: ["01"]`. Read as
  modifiers, all 13.5M of its rows failed the base filter (0 of 6,190 practices
  billing 97110 appeared under "base only"). Rule: a modifier list whose every
  element is a 2-digit number AND which contains at least one number that is
  NOT a valid numeric CPT modifier is a place-of-service list: it is moved to
  `service_code` (union with whatever the payer put there — nothing published
  is dropped) and the modifier list becomes empty.
* BCBS North Dakota writes two modifiers as ONE element: `["52,53"]`. Rule:
  every element is split on commas, semicolons and whitespace.
* First Choice Health prices every therapy line twice — bare, and under
  `["26","TC","RR","NU","UE"]` — with DIFFERENT rates (97110: bare 28–67,
  listed-modifier 60/61/80; identical on only 49 of 3,752 lines). Those are
  real, different published numbers for a modifier combination, so they stay
  as published (not merged into base); the base rule below excludes them.

Valid 2-digit modifiers are left alone even if they look like POS codes
(BCBS ND/WY `["22"]`, `["53"]`, `["55"]` sit beside a real POS list in
`service_code`): only a list containing a number that cannot be a modifier is
re-read as places of service.
"""

from __future__ import annotations

import re
from itertools import combinations

# A change to the normalization below changes what a stored row looks like:
# bump store.ROW_RULES_VERSION so parts written under the older rule are
# rewritten once at store open (Store._migrate_row_rules) — a pure function of
# the stored columns.

# Level I CPT numeric modifiers in use (AMA CPT Appendix A; 21 was deleted in
# 2013, 09950-style 5-digit forms are obsolete). Anything 2-digit NOT in here
# cannot be a modifier.
VALID_NUMERIC_MODIFIERS = frozenset(
    "22 23 24 25 26 27 32 33 47 50 51 52 53 54 55 56 57 58 59 62 63 66 73 74 "
    "76 77 78 79 80 81 82 90 91 92 93 95 96 97 99".split())

_SPLIT = re.compile(r"[,;\s]+")
_TWO_DIGITS = re.compile(r"[0-9]{2}")

# Discipline modifiers say WHO provided the service (PT/OT/SLP plan of care);
# they do not change the price. Assistant modifiers (CQ/CO) do (Medicare pays
# 85%). Everything else (KX, 59, X{EPSU}, 52, 22, 26/TC, ...) is excluded from
# "base" — some payers price them differently and blending would invent a rate.
DISCIPLINE = ("GN", "GO", "GP")
ASSISTANT = ("CO", "CQ")

BASE_RULE_TEXT = ("base modifiers only = no modifier, or only the therapy "
                  "discipline modifiers GP/GO/GN")
BASE_ASSISTANT_RULE_TEXT = ("base modifiers + assistant = no modifier, or only "
                            "GP/GO/GN and the assistant modifiers CQ/CO")

# Stated in every export's methodology sidecar: what ingest does to the fields.
MODIFIER_INGEST_NOTE = (
    "Modifier and place-of-service fields: modifier lists are sorted and "
    "de-duplicated, and an element holding several modifiers (\"52,53\") is "
    "split into each. A modifier list made only of 2-digit numbers that "
    "includes a number which cannot be a CPT modifier (e.g. 11, 21, 31) is the "
    "payer's place-of-service list published in the wrong field: it is moved "
    "to place of service (added to any codes the payer put there) and the row "
    "counts as having no modifier. Each file's data-quality line counts the "
    "rows affected.")


def _sets(mods: tuple[str, ...]) -> list[str]:
    """Every non-empty modifier_set string (sorted, '|'-joined — the stored
    form) built only from `mods`."""
    mods = tuple(sorted(mods))
    return ["|".join(c) for n in range(1, len(mods) + 1) for c in combinations(mods, n)]


_BASE_SETS = _sets(DISCIPLINE)
_BASE_ASSISTANT_SETS = _sets(DISCIPLINE + ASSISTANT)


def base_sql(col: str = "modifier_set", include_assistant: bool = False) -> str:
    """SQL predicate on a stored modifier_set column (sorted, '|'-joined, ''
    when none). An exact IN list — modifier sets are sorted+deduped at ingest,
    so `GO|GP` is the only spelling of that pair."""
    sets = _BASE_ASSISTANT_SETS if include_assistant else _BASE_SETS
    return f"({col} = '' OR {col} IN ({', '.join(repr(s) for s in sets)}))"


def split_modifiers(raw) -> list[str]:
    """Sorted, de-duplicated, upper-case modifier list. A list element holding
    several modifiers (`"52,53"`, `"GP; 59"`) is split; blanks are dropped.
    [GP,59] and [59,GP] are the same set — publication order must not split the
    dedup grain into two medians."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out = set()
    for m in items:
        s = str(m).strip().upper()
        if not s:
            continue
        for part in _SPLIT.split(s):
            if part:
                out.add(part)
    return sorted(out)


def is_pos_list(mods: list[str]) -> bool:
    """True when a modifier list is really a place-of-service list: all
    2-digit numbers, at least one of which is not a valid modifier."""
    return (bool(mods) and all(_TWO_DIGITS.fullmatch(m) for m in mods)
            and any(m not in VALID_NUMERIC_MODIFIERS for m in mods))


def normalize(raw_mods: list, pos: list[str]) -> tuple[list[str], list[str], bool, bool]:
    """(modifiers, service_codes, was_split, pos_moved) for one price.

    `raw_mods` is the price's billing_code_modifier as a list (parser.as_list);
    `pos` its already-cleaned, sorted, de-duplicated service_code list. Pure
    and idempotent — normalize(*normalize(m, p)[:2]) changes nothing — which
    is what lets rows already in a store be fixed in place exactly as a
    re-ingest would write them."""
    mods = split_modifiers(raw_mods)
    was_split = any(_SPLIT.search(str(m).strip()) for m in raw_mods or ())
    if is_pos_list(mods):
        return [], sorted(set(pos) | set(mods)), was_split, True
    return mods, pos, was_split, False
