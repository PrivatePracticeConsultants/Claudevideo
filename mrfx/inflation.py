"""Real-terms erosion: what a flat rate actually costs a practice.

A payer that has not moved a rate since 2023 is not "holding steady" — it is
cutting pay every year that costs rise. That sentence is the single most useful
thing a rate consultant can say in a renewal conversation, and nothing else in
this app says it: every other number here is nominal dollars.

Two ways to get it, and the difference between them is stated everywhere:

- **Index basis (factual).** A published price index converts nominal dollars
  to constant ones. CPI-U annual averages ship with the app (BLS series
  CUUR0000SA0, 1982-84=100) because they are public, stable, revised rarely,
  and the number a practice owner already recognises. They only run through the
  last year the shipped table covers; the user extends it with `mrfx inflation`
  (or the Medicare Economic Index, if they prefer the physician-practice cost
  basket) and the store keeps their values.

- **Assumption basis (explicitly an assumption).** If the covering year is not
  loaded, the caller may supply an annual rate ("assume 3%/yr"). It is echoed
  back as an assumption in every output that uses it, the way the utilization
  multiplier already is. It is never applied by default.

HONESTY:
- A missing index year produces a REFUSAL WITH A REASON, never an
  extrapolation. Guessing last year's inflation to make a slide land is exactly
  the kind of fabricated number this app exists not to produce.
- CPI-U is a consumer basket. A therapy practice's costs are mostly wages, so
  CPI understates clinic cost growth in a tight labour market and overstates it
  when energy spikes. Every result carries that caveat and names its basis.
- Erosion is computed on PUBLISHED rates, so it inherits their limits: a rate
  is not a collection.
"""

from __future__ import annotations

import datetime as dt
import logging

from .store import Store

log = logging.getLogger(__name__)


class InflationError(Exception):
    """Cannot compute a real-terms figure — say why, never approximate."""


# CPI-U, all items, U.S. city average, ANNUAL AVERAGE (BLS series CUUR0000SA0,
# 1982-84 = 100). Shipped so the feature works out of the box for any month
# range these years cover; the user adds newer years themselves, because this
# app must never invent an index value it did not read somewhere.
DEFAULT_INDEX = "CPI-U"
DEFAULT_INDEX_SOURCE = (
    "BLS CPI-U, all items, U.S. city average, annual average (1982-84=100)")
DEFAULT_VALUES: dict[int, float] = {
    2010: 218.056, 2011: 224.939, 2012: 229.594, 2013: 232.957,
    2014: 236.736, 2015: 237.017, 2016: 240.007, 2017: 245.120,
    2018: 251.107, 2019: 255.657, 2020: 258.811, 2021: 270.970,
    2022: 292.655, 2023: 304.702, 2024: 313.689,
}

BASIS_CAVEAT = (
    "Real-terms figures restate published rates in constant dollars using an "
    "annual price index. CPI-U is a CONSUMER basket: a therapy practice's costs "
    "are mostly wages, so it understates clinic cost growth when labour is tight "
    "and overstates it when energy spikes. It is a fair, citable approximation "
    "of erosion, not a measurement of this practice's costs."
)


def _year_of(month: str | None) -> int | None:
    """Year from a YYYY-MM file_month. A month we cannot read is a refusal
    upstream, never a silently-assumed year."""
    s = str(month or "").strip()
    if len(s) >= 4 and s[:4].isdigit():
        y = int(s[:4])
        if 1900 <= y <= 2200:
            return y
    return None


def load_index(store: Store) -> dict:
    """The index in force: the shipped CPI-U table plus whatever the user
    loaded, user values winning for the same year (they are newer, or a
    deliberate switch to a different basket)."""
    values = dict(DEFAULT_VALUES)
    name, source = DEFAULT_INDEX, DEFAULT_INDEX_SOURCE
    user: dict[int, float] = {}
    try:
        with store.connect() as con:
            rows = con.execute(
                "SELECT year, value, index_name, source FROM inflation_index "
                "ORDER BY year").fetchall()
    except Exception:  # noqa: BLE001 — a store predating the table
        rows = []
    for y, v, nm, src in rows:
        try:
            user[int(y)] = float(v)
        except (TypeError, ValueError):
            continue
        if nm:
            name = str(nm)
        if src:
            source = str(src)
    values.update(user)
    return {
        "index_name": name,
        "source": source if not user else f"{source}; extended locally",
        "values": values,
        "years": sorted(values),
        "first_year": min(values) if values else None,
        "last_year": max(values) if values else None,
        "user_years": sorted(user),
        "shipped_last_year": max(DEFAULT_VALUES),
    }


def save_index_values(store: Store, rows: list[tuple], *, index_name: str | None = None,
                      source: str | None = None) -> int:
    """Record user-supplied index values (year, value). Replaces a year in
    place, so correcting a typo is re-entering it."""
    payload = []
    for r in rows:
        try:
            y, v = int(r[0]), float(r[1])
        except (TypeError, ValueError, IndexError):
            raise InflationError(
                f"each row must be a year and a number, got {r!r}")
        if not 1900 <= y <= 2200:
            raise InflationError(f"{y} is not a plausible year")
        if v <= 0:
            raise InflationError(
                f"an index value must be positive, got {v} for {y}")
        payload.append([y, v, index_name or DEFAULT_INDEX,
                        source or "entered locally", dt.datetime.now(dt.timezone.utc)])
    if not payload:
        return 0
    with store.write_lock, store.connect() as con:
        con.executemany(
            "INSERT OR REPLACE INTO inflation_index "
            "(year, value, index_name, source, added_at) VALUES (?, ?, ?, ?, ?)",
            payload)
    return len(payload)


def deflator(index: dict, from_month: str | None, to_month: str | None,
             *, assume_pct_per_year: float | None = None) -> dict:
    """How much prices rose between two months' YEARS.

    Returns {factor, basis, ...} where `factor` is the multiplier a
    `from_month` dollar needs to hold constant purchasing power at
    `to_month`, or {factor: None, reason: ...} when neither basis is available.
    Never extrapolates the index past its last year.
    """
    # Validate the assumption whether or not it ends up being used: a caller
    # that sent "3%" as "soon" has a bug, and hearing about it only on the
    # stores whose index happens to fall short is the worst way to find out.
    rate = None
    if assume_pct_per_year is not None:
        try:
            rate = float(assume_pct_per_year)
        except (TypeError, ValueError):
            raise InflationError("the inflation assumption must be a number, e.g. 3")
        if not -50.0 <= rate <= 100.0:
            raise InflationError(f"{rate}%/yr is not a usable inflation assumption")
    y0, y1 = _year_of(from_month), _year_of(to_month)
    if y0 is None or y1 is None:
        return {"factor": None, "basis": None,
                "reason": "a real-terms figure needs two readable YYYY-MM months"}
    vals = index.get("values") or {}
    if y0 in vals and y1 in vals and vals[y0]:
        return {
            "factor": vals[y1] / vals[y0],
            "basis": "index",
            "index_name": index.get("index_name"),
            "source": index.get("source"),
            "from_year": y0, "to_year": y1,
            "from_value": vals[y0], "to_value": vals[y1],
            "reason": None,
        }
    missing = sorted({y for y in (y0, y1) if y not in vals})
    if rate is not None:
        return {
            "factor": (1.0 + rate / 100.0) ** (y1 - y0),
            "basis": "assumption",
            "assumed_pct_per_year": rate,
            "from_year": y0, "to_year": y1,
            "reason": None,
            "assumption_note": (
                f"ASSUMPTION: {rate:g}% price growth per year, supplied by you — "
                f"not measured. {index.get('index_name', 'The index')} has no value "
                f"for {', '.join(str(m) for m in missing)}."),
        }
    return {
        "factor": None, "basis": None, "from_year": y0, "to_year": y1,
        "missing_years": missing,
        "reason": (
            f"no {index.get('index_name', 'index')} value for "
            f"{', '.join(str(m) for m in missing)}, and none was assumed — add it "
            f"with `mrfx inflation --year {missing[0]} --value <annual average>` "
            "(BLS publishes CPI-U annual averages each January), or pass an "
            "explicit percent-per-year assumption."),
    }


def real_change_pct(nominal_pct: float | None, defl: dict) -> float | None:
    """Nominal percent change restated in constant dollars.

    (1 + nominal) / (1 + inflation) - 1 — the compounding form, not
    `nominal - inflation`: over a multi-year span the subtraction drifts, and
    it drifts in the direction that makes a cut look smaller.
    """
    if nominal_pct is None or not defl or not defl.get("factor"):
        return None
    return round(100.0 * ((1.0 + nominal_pct / 100.0) / defl["factor"] - 1.0), 1)


def erosion_line(payer: str, nominal_pct: float | None, real_pct: float | None,
                 first_month: str, last_month: str, defl: dict) -> str:
    """One sentence a consultant can read aloud in a renewal meeting."""
    if real_pct is None:
        return ""
    span = f"{first_month} to {last_month}"
    nom = f"{nominal_pct:+.1f}%" if nominal_pct is not None else "unchanged"
    basis = (f"{defl.get('index_name')}" if defl.get("basis") == "index"
             else f"an assumed {defl.get('assumed_pct_per_year'):g}%/yr")
    if real_pct < 0 and (nominal_pct or 0) >= 0:
        return (f"{payer}: {nom} in dollars over {span}, but {real_pct:+.1f}% in "
                f"real terms — flat pay is a pay cut once costs rise ({basis}).")
    if real_pct < 0:
        return (f"{payer}: {nom} in dollars over {span}, and {real_pct:+.1f}% "
                f"after inflation ({basis}) — the cut is deeper than it looks.")
    return (f"{payer}: {nom} in dollars over {span}, {real_pct:+.1f}% in real "
            f"terms ({basis}) — this payer kept ahead of costs.")


def index_status(store: Store) -> dict:
    """What the dashboard shows about the basis, without raising."""
    idx = load_index(store)
    return {
        "index_name": idx["index_name"],
        "source": idx["source"],
        "first_year": idx["first_year"],
        "last_year": idx["last_year"],
        "n_years": len(idx["values"]),
        "user_years": idx["user_years"],
        "shipped_last_year": idx["shipped_last_year"],
        "caveat": BASIS_CAVEAT,
        "note": (
            f"Years after {idx['last_year']} have no index value. Add each new "
            "year once it is published (`mrfx inflation --year YYYY --value N`, "
            "or the Changes tab) — until then, real-terms figures for that year "
            "are refused rather than estimated."),
    }
