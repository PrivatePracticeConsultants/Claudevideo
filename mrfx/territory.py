"""Three market-structure questions, below the state line.

The app already answers "what does this payer pay?" It has not answered the
questions a consultant asks when deciding where to compete, how much leverage a
payer actually has, and which referral relationship to go after next:

- **Where inside this state?** State medians hide the split that matters most
  to a client: the metro pays differently from the county seat 40 miles away.
  `local_rate_map` ranks CITIES within one state, with the same thin-cell
  suppression the state map uses.
- **How concentrated are the payers?** If one payer holds most of the
  contracted relationships in a market, that payer has leverage and a practice
  cannot credibly walk. The Herfindahl-Hirschman Index is the standard measure
  and the DOJ thresholds are widely recognised, so `payer_concentration`
  computes it — with the honesty rail that matters more than the number
  itself: this is a share of PUBLISHED PROVIDER RELATIONSHIPS IN THIS STORE,
  not a share of covered lives, and it is only as complete as the payer files
  the user has ingested.
- **Whose referrals are going to someone else?** `steal_share` finds the
  physicians who send patients to OTHER therapy practices near a client and
  little or nothing to the client. Every other lead surface in this app finds
  practices; this one finds the specific relationship to go and win.
"""

from __future__ import annotations

import logging
import re

from .catalog import code_info
from .states import state_code_or_none
from .store import Store, mask_tin

log = logging.getLogger(__name__)

# Same thin-cell rule the state rate map uses: a city with a handful of
# practices is not a market, and printing it as one invites a client to price
# against three neighbours.
MIN_CELL = 5

LOCAL_NOTE = (
    "City is the provider's NPPES practice-location city, not the geography a "
    "contract legally applies to, and a multi-site group contributes its rate "
    "to every city it operates in. Cities with fewer than "
    f"{MIN_CELL} practices are suppressed rather than shown as a market."
)

HHI_NOTE = (
    "This is concentration of PUBLISHED PROVIDER RELATIONSHIPS in your store — "
    "how the practices that have contracts are distributed across payers — NOT "
    "market share of covered lives, which no machine-readable file reports. It "
    "is therefore only as complete as the payer files you have ingested: load "
    "three payers and the index will call the market concentrated no matter "
    "what the real market looks like. Read it alongside the file coverage "
    "stated with it, and treat it as a leverage indicator, not an antitrust "
    "finding."
)

STEAL_NOTE = (
    "Shared-patient counts come from one CMS referral release and describe that "
    "period only — they are patients seen by both providers, which is a proxy "
    "for referral, not a record of one. A physician sending to another practice "
    "may have a contractual or ownership reason that no data here can see. "
    "These are the calls worth making first, not proof a relationship is "
    "winnable."
)

# DOJ/FTC Horizontal Merger Guidelines bands, used because they are the ones a
# reader may already recognise — not because a provider network is a merger.
def _hhi_band(hhi: float | None) -> str | None:
    if hhi is None:
        return None
    if hhi < 1500:
        return "unconcentrated"
    if hhi < 2500:
        return "moderately concentrated"
    return "highly concentrated"


def local_rate_map(store: Store, code: str, market: dict, *,
                   state: str | None = None, limit: int = 60) -> dict:
    """Median rate for one code by CITY within a state."""
    from .benchmark import (BenchmarkError, _market_where, _rates_relation,
                            month_label, normalize_market, resolve_plan_scope)

    code = str(code or "").strip().upper()
    if not code:
        raise BenchmarkError("pick a billing code")
    limit = max(1, min(int(limit), 500))   # a negative LIMIT is a DuckDB binder 500
    m = resolve_plan_scope(store, normalize_market({**(market or {}), "codes": [code]}))
    st = state_code_or_none(state or m.get("state")) or ""
    if not st:
        raise BenchmarkError(
            "a sub-state map needs a state — rates vary far more between states "
            "than between cities, so pooling them would bury the difference")
    m = {**m, "state": st}
    where, params = _market_where(m, bool(m.get("include_assistant")),
                                  bool(m.get("include_non_dollar")))
    rel = _rates_relation(m)
    sql = f"""
    WITH base AS (
        SELECT t.tin_value, any_value(td.cities) AS cities,
               median(t.negotiated_rate) AS rate
        FROM {rel} t JOIN tin_directory td USING (tin_value)
        WHERE {where} AND td.cities IS NOT NULL AND len(td.cities) > 0
        GROUP BY t.tin_value
    ),
    exploded AS (
        SELECT upper(trim(unnest(cities))) AS city, tin_value, rate FROM base
    )
    SELECT city,
           count(DISTINCT tin_value)          AS n_practices,
           round(median(rate), 2)             AS median_rate,
           round(quantile_cont(rate, .25), 2) AS p25,
           round(quantile_cont(rate, .75), 2) AS p75
    FROM exploded
    WHERE city <> ''
    GROUP BY city
    HAVING count(DISTINCT tin_value) >= ?
    ORDER BY median_rate DESC
    LIMIT ?
    """
    with store.connect() as con:
        cur = con.execute(sql, [*params, MIN_CELL, int(limit)])
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        ref = con.execute(f"""
            WITH base AS (
                SELECT t.tin_value, median(t.negotiated_rate) AS rate
                FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
                WHERE {where} GROUP BY t.tin_value)
            SELECT round(median(rate), 2), count(*) FROM base""", params).fetchone()
        suppressed = con.execute(f"""
            WITH base AS (
                SELECT t.tin_value, any_value(td.cities) AS cities
                FROM {rel} t JOIN tin_directory td USING (tin_value)
                WHERE {where} AND td.cities IS NOT NULL AND len(td.cities) > 0
                GROUP BY t.tin_value),
            exploded AS (SELECT upper(trim(unnest(cities))) AS city, tin_value FROM base)
            SELECT count(*) FROM (
                SELECT city FROM exploded WHERE city <> '' GROUP BY city
                HAVING count(DISTINCT tin_value) < ?)""", [*params, MIN_CELL]).fetchone()[0]

    state_median = ref[0]
    for r in rows:
        r["vs_state_pct"] = (round(100.0 * (r["median_rate"] - state_median)
                                   / state_median, 1)
                             if state_median else None)
    spread = None
    if len(rows) >= 2:
        spread = round(100.0 * (rows[0]["median_rate"] - rows[-1]["median_rate"])
                       / rows[-1]["median_rate"], 1) if rows[-1]["median_rate"] else None
    return {
        "code": code, "description": code_info(code)[0], "state": st,
        "as_of": month_label(m["month"]),
        "state_median": state_median, "state_practices": ref[1],
        "cities": rows, "count": len(rows),
        "suppressed_cities": suppressed, "min_practices": MIN_CELL,
        "spread_pct": spread,
        "headline": (
            f"Within {st}, {rows[0]['city'].title()} pays {spread:g}% more than "
            f"{rows[-1]['city'].title()} for {code}."
            if spread and len(rows) >= 2 else
            f"Not enough cities in {st} with {MIN_CELL}+ practices to compare."
            if not rows else f"{len(rows)} city market(s) in {st}."),
        "note": LOCAL_NOTE,
    }


def payer_concentration(store: Store, market: dict | None = None,
                        *, limit: int = 25) -> dict:
    """How the contracted relationships in a market divide across payers."""
    limit = max(1, min(int(limit), 500))
    from .benchmark import (_market_where, _rates_relation, month_label,
                            normalize_market, resolve_plan_scope)

    m = resolve_plan_scope(store, normalize_market(market or {}))
    where, params = _market_where(m, bool(m.get("include_assistant")),
                                  bool(m.get("include_non_dollar")))
    rel = _rates_relation(m)
    sql = f"""
        SELECT t.payer,
               count(DISTINCT t.tin_value)         AS n_practices,
               count(DISTINCT t.billing_code)      AS n_codes,
               round(median(t.negotiated_rate), 2) AS median_rate
        FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where}
        GROUP BY t.payer ORDER BY n_practices DESC
    """
    with store.connect() as con:
        cur = con.execute(sql, params)
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        # how much of the store's payer universe this market's scope covers —
        # the number that decides whether the index means anything at all
        n_files, n_payer_files = con.execute(
            "SELECT count(*), count(DISTINCT payer) FROM files "
            "WHERE status = 'done' AND file_type = 'in_network'").fetchone()

    total = sum(r["n_practices"] for r in rows)
    for r in rows:
        r["share_pct"] = round(100.0 * r["n_practices"] / total, 1) if total else None
    hhi = round(sum((r["share_pct"] or 0) ** 2 for r in rows), 0) if total else None
    top = rows[0] if rows else None
    # Fewer than ~5 payers cannot produce a meaningful index — with 3 payers the
    # minimum possible HHI is 3333, i.e. "highly concentrated" by construction.
    thin = len(rows) < 5
    return {
        "payers": rows[:limit], "n_payers": len(rows),
        "total_relationships": total,
        "hhi": hhi, "band": None if thin else _hhi_band(hhi),
        "thin": thin,
        "top_payer": top["payer"] if top else None,
        "top_share_pct": top["share_pct"] if top else None,
        "as_of": month_label(m["month"]),
        "files_ingested": n_files, "payers_with_files": n_payer_files,
        "market": {k: v for k, v in m.items() if not k.startswith("_")},
        "headline": (
            f"Only {len(rows)} payer(s) in this scope — too few for a "
            "concentration index to mean anything (with three payers the "
            "index calls every market concentrated by construction). Ingest "
            "more payers' files first."
            if thin and rows else
            f"{top['payer']} holds {top['share_pct']:g}% of the contracted "
            f"practice relationships here; the market is {_hhi_band(hhi)} "
            f"(HHI {hhi:,.0f}) across the {len(rows)} payers you have loaded."
            if rows else "No rates match this market."),
        "note": HHI_NOTE,
    }


def _add_miles(store: Store, rows: list[dict], subject_tins: list[str],
               centroids_path=None) -> None:
    """Attach `miles` from the subject's own location to each referrer.

    Mutates `rows` in place and swallows every failure: a distance column is
    an ordering aid, and a missing centroid file must never cost a consultant
    the call list itself.
    """
    if not rows:
        return
    try:
        from .medicare import haversine_miles_sql, load_centroids
        with store.connect() as con:
            load_centroids(con, centroids_path)
            origin = con.execute("""
                SELECT mode(lpad(substr(trim(n.zip), 1, 5), 5, '0'))
                FROM rates r JOIN npi_directory n ON n.npi = r.npi
                WHERE r.tin_value IN (SELECT unnest(?::VARCHAR[]))
                  AND n.zip IS NOT NULL AND trim(n.zip) <> ''
            """, [subject_tins]).fetchone()
            home = origin[0] if origin else None
            if not home:
                return
            pt = con.execute("SELECT lat, lon FROM _zcta WHERE zip = ?",
                             [home]).fetchone()
            if pt is None:
                return
            npis = [r["npi"] for r in rows if r.get("npi")]
            dist = dict(con.execute(f"""
                SELECT n.npi,
                       {haversine_miles_sql('c.lat', 'c.lon', pt[0], pt[1])} AS miles
                FROM npi_directory n
                JOIN _zcta c ON c.zip = lpad(substr(trim(n.zip), 1, 5), 5, '0')
                WHERE n.npi IN (SELECT unnest(?::VARCHAR[]))
            """, [npis]).fetchall())
        for r in rows:
            d = dist.get(r.get("npi"))
            r["miles"] = round(d, 1) if d is not None else None
            r["from_zip"] = home
    except Exception:  # noqa: BLE001 — ordering aid only
        log.debug("steal-share distances unavailable", exc_info=True)


def steal_share(store: Store, subject: str, *, radius_miles: float | None = None,
                zip_code: str | None = None, limit: int = 50,
                dataset_id: str | None = None, min_patients: int = 11,
                centroids_path=None) -> dict:
    """Referral sources sending patients to a client's COMPETITORS, not to them.

    The client's own inbound partners are subtracted, so what remains is the
    list of physicians with a demonstrated therapy referral habit and no
    meaningful relationship with this practice — the highest-value calls a
    consultant can hand a client.
    """
    from .benchmark import BenchmarkError, resolve_subject_tins
    from .medicare import active_dataset, _ensure_referral_view, taxonomy_label

    limit = max(1, min(int(limit), 500))
    min_patients = max(0, int(min_patients))
    tins = resolve_subject_tins(store, subject)
    if not tins:
        raise BenchmarkError(f"no practice matches {subject!r}")
    with store.connect() as con:
        mine = [r[0] for r in con.execute(
            "SELECT DISTINCT npi FROM rates WHERE tin_value IN "
            "(SELECT unnest(?::VARCHAR[])) AND npi IS NOT NULL", [tins]).fetchall()]
    if not mine:
        # resolve_subject_tins falls through to the raw string for an unknown
        # subject, so a typo lands here rather than above — name the likely
        # cause instead of only the symptom
        raise BenchmarkError(
            f"no NPIs are associated with {subject!r} in the rate data, so "
            "there is nothing to compare referral flow against — check the "
            "practice name, tax ID or NPI")

    active = active_dataset(store, None, dataset_id)
    if active is None:
        return {"rows": [], "count": 0, "subject": subject, "dataset": None,
                "reason": ("no CMS referral dataset is loaded — import one on "
                           "the Medicare tab first"),
                "note": STEAL_NOTE}
    label, year, ds_id = active

    with store.connect() as con:
        _ensure_referral_view(con, store)
        # competitor set: therapy practices in the same cities as the subject,
        # excluding the subject's own NPIs. City is the sharpest geography the
        # directory carries for every provider.
        cities = [r[0] for r in con.execute(
            "SELECT DISTINCT upper(trim(c)) FROM ("
            "  SELECT unnest(cities) AS c FROM tin_directory "
            "  WHERE tin_value IN (SELECT unnest(?::VARCHAR[])))"
            " WHERE c IS NOT NULL AND trim(c) <> ''", [tins]).fetchall()]
        if not cities:
            return {"rows": [], "count": 0, "subject": subject,
                    "dataset": label, "dataset_id": ds_id,
                    "reason": ("this practice has no NPPES city on file, so its "
                               "local competitor set cannot be identified — run "
                               "NPI enrichment first"),
                    "note": STEAL_NOTE}

        sql = """
        WITH mine AS (SELECT unnest(?::VARCHAR[]) AS npi),
        local_rivals AS (
            SELECT DISTINCT n.npi
            FROM npi_directory n
            WHERE upper(trim(coalesce(n.city, ''))) IN (SELECT unnest(?::VARCHAR[]))
              AND n.npi NOT IN (SELECT npi FROM mine)
        ),
        to_rivals AS (
            SELECT p.source_npi, sum(p.patients) AS patients_elsewhere,
                   count(DISTINCT p.target_npi)  AS n_rivals
            FROM referral_pairs p
            WHERE p.dataset_id = ?
              AND p.target_npi IN (SELECT npi FROM local_rivals)
            GROUP BY p.source_npi
        ),
        to_me AS (
            SELECT p.source_npi, sum(p.patients) AS patients_to_me
            FROM referral_pairs p
            WHERE p.dataset_id = ? AND p.target_npi IN (SELECT npi FROM mine)
            GROUP BY p.source_npi
        )
        SELECT r.source_npi                          AS npi,
               coalesce(n.org_name, '')              AS name,
               coalesce(n.taxonomy_code, '')         AS taxonomy,
               coalesce(n.city, '')                  AS city,
               coalesce(n.state, '')                 AS state,
               n.phone                               AS phone,
               r.patients_elsewhere,
               r.n_rivals,
               coalesce(m.patients_to_me, 0)         AS patients_to_me
        FROM to_rivals r
        LEFT JOIN to_me m USING (source_npi)
        LEFT JOIN npi_directory n ON n.npi = r.source_npi
        WHERE r.source_npi NOT IN (SELECT npi FROM mine)
          AND r.patients_elsewhere >= ?
        ORDER BY (r.patients_elsewhere - coalesce(m.patients_to_me, 0)) DESC
        LIMIT ?
        """
        cur = con.execute(sql, [mine, cities, ds_id, ds_id,
                                int(min_patients), int(limit)])
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

    # How far away each referrer actually is. A call list ordered only by volume
    # sends a client to a physician 90 miles away before the one down the road;
    # distance is what makes it a route. Cosmetic tier — no centroid means the
    # row keeps its place with miles=None, never dropped.
    _add_miles(store, rows, tins, centroids_path)

    for r in rows:
        r["specialty"] = taxonomy_label(r.get("taxonomy"))
        r["gap"] = int(r["patients_elsewhere"]) - int(r["patients_to_me"])
        # the share of this physician's local therapy volume the client is NOT
        # getting — the number that ranks a call list
        tot = int(r["patients_elsewhere"]) + int(r["patients_to_me"])
        r["share_missed_pct"] = round(100.0 * r["gap"] / tot, 1) if tot else None
        r["already_a_partner"] = int(r["patients_to_me"]) > 0

    cold = [r for r in rows if not r["already_a_partner"]]
    placed = [r for r in rows if r.get("miles") is not None]
    return {
        "rows": rows, "count": len(rows), "n_cold": len(cold),
        "subject": subject, "subject_tins": [mask_tin(t) for t in tins],
        "cities": cities, "dataset": label, "data_year": year,
        "n_with_distance": len(placed),
        "n_unplaced": len(rows) - len(placed),
        "dataset_id": ds_id, "min_patients": min_patients,
        "reason": None if rows else (
            "no local physician sends therapy patients to another practice in "
            "these cities in this release — either the practice already has the "
            "referral flow, or the release does not cover these providers"),
        "headline": (
            f"{len(cold)} physician(s) send patients to therapy practices in "
            f"{', '.join(c.title() for c in cities[:3])} and none to this one; "
            f"the largest sends {cold[0]['patients_elsewhere']:,} elsewhere."
            if cold else
            f"{len(rows)} referral source(s) send more elsewhere than here."
            if rows else "Nothing to target in this release."),
        "note": STEAL_NOTE,
    }


RADIUS_NOTE = (
    "Distance is from the centre of the ZIP you named to the centre of each "
    "practice's own ZIP (Census ZCTA centroids), so it is a ZIP-to-ZIP "
    "approximation, not a driving distance. A practice's ZIP is the NPPES "
    "practice location of its NPIs; a multi-site group is placed at the ZIP "
    "most of its NPIs share, and practices whose ZIP has no centroid are "
    "reported as unplaced rather than dropped. Bands with fewer than "
    f"{MIN_CELL} practices are suppressed rather than shown as a market."
)

# Distance bands. The question this answers is "does the same payer pay
# differently 30 miles away", so the bands are coarse enough to hold real
# practice counts and fine enough that the near band is genuinely local.
RADIUS_BANDS = ((0, 10, "0-10 miles"), (10, 25, "10-25 miles"),
                (25, 50, "25-50 miles"), (50, 100, "50-100 miles"))


def radius_rate_map(store: Store, code: str, market: dict, *,
                    zip_code: str, max_miles: float = 100.0,
                    centroids_path=None) -> dict:
    """One code's median rate by DISTANCE BAND from a ZIP.

    The sharpest form of the sub-state question: same payer, same code, 30
    miles apart, different rate. City names cannot show that — two adjacent
    suburbs are different cities and a metro's edge is 40 miles from its
    centre — so this places every practice by its own ZIP's centroid.
    """
    from .benchmark import (BenchmarkError, _market_where, _rates_relation,
                            month_label, normalize_market, resolve_plan_scope)
    from .medicare import haversine_miles_sql, load_centroids

    code = str(code or "").strip().upper()
    if not code:
        raise BenchmarkError("pick a billing code")
    zip_code = str(zip_code or "").strip()[:5]
    if not re.fullmatch(r"\d{5}", zip_code):
        raise BenchmarkError(f"{zip_code!r} is not a 5-digit ZIP code")
    try:
        max_miles = max(1.0, min(float(max_miles), 500.0))
    except (TypeError, ValueError):
        raise BenchmarkError("radius must be a number of miles")

    m = resolve_plan_scope(store, normalize_market({**(market or {}), "codes": [code]}))
    where, params = _market_where(m, bool(m.get("include_assistant")),
                                  bool(m.get("include_non_dollar")))
    rel = _rates_relation(m)

    with store.connect() as con:
        load_centroids(con, centroids_path)
        origin = con.execute("SELECT lat, lon FROM _zcta WHERE zip = ?",
                             [zip_code]).fetchone()
        if origin is None:
            raise BenchmarkError(
                f"ZIP {zip_code} is not in the Census ZCTA centroid list")
        # A practice sits at the ZIP most of its NPIs share — mode, not an
        # arbitrary pick, so a multi-site group lands where its bulk is.
        sql = f"""
        WITH per_tin AS (
            SELECT t.tin_value, median(t.negotiated_rate) AS rate
            FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
            WHERE {where}
            GROUP BY t.tin_value
        ),
        tin_zip AS (
            SELECT r.tin_value,
                   mode(lpad(substr(trim(n.zip), 1, 5), 5, '0')) AS zip
            FROM rates r JOIN npi_directory n ON n.npi = r.npi
            WHERE r.tin_value IN (SELECT tin_value FROM per_tin)
              AND n.zip IS NOT NULL AND trim(n.zip) <> ''
            GROUP BY r.tin_value
        ),
        placed AS (
            SELECT p.tin_value, p.rate, z.zip,
                   {haversine_miles_sql('c.lat', 'c.lon', origin[0], origin[1])} AS miles
            FROM per_tin p
            LEFT JOIN tin_zip z USING (tin_value)
            LEFT JOIN _zcta c ON c.zip = z.zip
        )
        SELECT tin_value, rate, zip, miles FROM placed
        """
        cur = con.execute(sql, params)
        rows = [dict(zip(["tin_value", "rate", "zip", "miles"], r))
                for r in cur.fetchall()]

    unplaced = sum(1 for r in rows if r["miles"] is None)
    inside = [r for r in rows if r["miles"] is not None and r["miles"] <= max_miles]

    # Band edges must COVER the whole radius: a practice inside max_miles that
    # falls past the last fixed edge would otherwise be counted in `inside` and
    # appear in no band — silently vanishing from the very table that is meant
    # to show it. Clip the fixed bands to the radius and extend the last one.
    edges = [(lo, hi, label) for lo, hi, label in RADIUS_BANDS if lo < max_miles]
    if not edges:
        edges = [(0.0, max_miles, f"0-{max_miles:g} miles")]
    else:
        lo, hi, label = edges[-1]
        if max_miles > hi:
            edges.append((hi, max_miles, f"{hi:g}-{max_miles:g} miles"))
        elif max_miles < hi:
            edges[-1] = (lo, max_miles, f"{lo:g}-{max_miles:g} miles")

    bands = []
    for lo, hi, label in edges:
        grp = [r for r in inside if lo <= r["miles"] < hi
               or (hi >= max_miles and r["miles"] == hi)]
        if len(grp) < MIN_CELL:
            bands.append({"band": label, "min_miles": lo, "max_miles": hi,
                          "n_practices": len(grp), "median_rate": None,
                          "thin": True})
            continue
        vals = sorted(r["rate"] for r in grp)
        mid = len(vals) // 2
        bands.append({
            "band": label, "min_miles": lo, "max_miles": hi,
            "n_practices": len(grp),
            "median_rate": round(vals[mid] if len(vals) % 2
                                 else (vals[mid - 1] + vals[mid]) / 2, 2),
            "thin": False,
        })

    usable = [b for b in bands if not b["thin"]]
    near = usable[0] if usable else None
    for b in usable:
        b["vs_nearest_pct"] = (
            round(100.0 * (b["median_rate"] - near["median_rate"])
                  / near["median_rate"], 1)
            if near and near["median_rate"] else None)
    spread = None
    if len(usable) >= 2:
        rates = [b["median_rate"] for b in usable]
        lo_r, hi_r = min(rates), max(rates)
        spread = round(100.0 * (hi_r - lo_r) / lo_r, 1) if lo_r else None

    # invariant: every placed practice inside the radius lands in exactly one
    # band. If that ever stops holding, say so rather than quietly under-reporting.
    banded = sum(b["n_practices"] for b in bands)
    return {
        "code": code, "description": code_info(code)[0],
        "zip": zip_code, "max_miles": max_miles,
        "n_banded": banded,
        "unbanded": len(inside) - banded,
        "as_of": month_label(m["month"]),
        "bands": bands, "n_placed": len(inside), "unplaced": unplaced,
        "min_practices": MIN_CELL, "spread_pct": spread,
        "headline": (
            f"Within {max_miles:g} miles of {zip_code}, {code} pays {spread:g}% "
            "more in one distance band than another — the same payers, a short "
            "drive apart."
            if spread and spread >= 1 else
            f"No meaningful distance effect for {code} within {max_miles:g} "
            f"miles of {zip_code}."
            if len(usable) >= 2 else
            f"Only {len(usable)} distance band around {zip_code} has "
            f"{MIN_CELL}+ practices — too thin to compare."),
        "note": RADIUS_NOTE,
    }
