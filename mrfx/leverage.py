"""Walk-away leverage: how badly does this payer need this practice?

Every negotiation number in this app so far answers "what are you paid?" The
question that actually decides a negotiation is different: **what happens to
the payer if you leave?** A payer with eight other contracted therapy practices
within fifteen miles can shrug. A payer with one — covering forty thousand
seniors — cannot, and the practice should know that before it asks.

Nothing new is downloaded. The three ingredients have been sitting in the store
on three different tabs and were never combined:

- the payer's own ROSTER (which other practices it publishes rates for),
- the bundled Census ZCTA CENTROIDS (how far away each of them is),
- the ACS SENIOR counts (how much demand the alternatives would absorb).

HONESTY — the rails that keep this from overclaiming:

- A published rate means the payer has a CONTRACT, not that the practice is
  accepting new patients, still open, or in-network for the plan in question.
  So this measures *published alternatives*, which is an upper bound on the
  payer's real fallback: the true number of usable alternatives is smaller,
  which makes the client's leverage LARGER, not smaller. The direction of the
  error is stated because it is the direction that matters.
- It is NOT a network-adequacy finding. Adequacy is a regulatory test with
  statutory time-and-distance standards that vary by state, line of business
  and county type; this is a negotiating observation.
- Alternatives are counted at the TIN grain (one practice, however many
  clinicians) from the same directory every other geography view uses, so this
  can never disagree with the ZIP search.
- With no ACS table loaded, the practice count still answers; the
  seniors-per-alternative ratio is simply absent rather than guessed.
"""

from __future__ import annotations

import logging

from .store import Store, mask_tin

log = logging.getLogger(__name__)

LEVERAGE_NOTE = (
    "Alternatives are practices this payer PUBLISHES RATES FOR within the "
    "radius — a contract, not proof the practice is open, accepting patients, "
    "or in-network for a particular plan. The real number of usable "
    "alternatives is therefore smaller than shown, which means the leverage is "
    "GREATER than shown, not less. This is a negotiating observation, never a "
    "network-adequacy finding: adequacy is a regulatory test with statutory "
    "time-and-distance standards this app does not evaluate."
)

# How thin is thin. Deliberately coarse and stated, not tuned: the point is a
# conversation opener, and a false precision here would be worse than none.
THIN_ALTERNATIVES = 3          # at or below this, the payer's fallback is thin
CROWDED_ALTERNATIVES = 10      # at or above this, the payer can shrug


def network_leverage(store: Store, subject: str, payer: str,
                     market: dict | None = None, *, radius_miles: float = 15.0,
                     centroids_path=None) -> dict:
    """The payer's published alternatives to this practice, inside a radius."""
    from .benchmark import (BenchmarkError, _market_where, _rates_relation,
                            normalize_market, resolve_plan_scope,
                            resolve_subject_tins)
    from .catalog import therapy_taxonomy_sql
    from .medicare import haversine_miles_sql, load_centroids

    payer = str(payer or "").strip()
    if not payer:
        raise BenchmarkError(
            "pick the payer you're negotiating with — leverage is per payer, "
            "because each one has a different local network")
    try:
        radius = max(1.0, min(float(radius_miles), 250.0))
    except (TypeError, ValueError):
        raise BenchmarkError("radius must be a number of miles")

    m = resolve_plan_scope(store, normalize_market(market or {}))
    m = {**m, "payers": [payer]}
    subject_tins = resolve_subject_tins(store, subject)
    if not subject_tins:
        raise BenchmarkError(f"no practice matches {subject!r}")
    where, params = _market_where(m, bool(m.get("include_assistant")),
                                  bool(m.get("include_non_dollar")))
    rel = _rates_relation(m)
    therapy = therapy_taxonomy_sql("n.taxonomy_code", all_col="n.taxonomy_codes")

    with store.connect() as con:
        load_centroids(con, centroids_path)
        # the subject sits at the ZIP most of its own NPIs share
        home = con.execute("""
            SELECT mode(lpad(substr(trim(n.zip), 1, 5), 5, '0'))
            FROM rates r JOIN npi_directory n ON n.npi = r.npi
            WHERE r.tin_value IN (SELECT unnest(?::VARCHAR[]))
              AND n.zip IS NOT NULL AND trim(n.zip) <> ''
        """, [subject_tins]).fetchone()
        home_zip = home[0] if home else None
        if not home_zip:
            raise BenchmarkError(
                f"{subject!r} has no NPPES practice location on file, so its "
                "local market cannot be drawn — run NPI enrichment first")
        origin = con.execute("SELECT lat, lon FROM _zcta WHERE zip = ?",
                             [home_zip]).fetchone()
        if origin is None:
            raise BenchmarkError(
                f"this practice's ZIP ({home_zip}) is not in the Census ZCTA "
                "centroid list, so a radius cannot be measured from it")

        miles = haversine_miles_sql("z.lat", "z.lon", origin[0], origin[1])
        in_radius = [r[0] for r in con.execute(
            f"SELECT z.zip FROM _zcta z WHERE {miles} <= ?", [radius]).fetchall()]

        # every OTHER practice this payer publishes rates for, placed by the ZIP
        # most of its NPIs share, kept when that ZIP is inside the radius
        alts = con.execute(f"""
            WITH priced AS (
                SELECT DISTINCT t.tin_value
                FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
                WHERE {where}
                  AND t.tin_value NOT IN (SELECT unnest(?::VARCHAR[]))
            ),
            placed AS (
                SELECT r.tin_value,
                       mode(lpad(substr(trim(n.zip), 1, 5), 5, '0')) AS zip,
                       count(DISTINCT n.npi) FILTER ({therapy})      AS n_therapy,
                       count(DISTINCT n.npi)                         AS n_npis
                FROM rates r JOIN npi_directory n ON n.npi = r.npi
                WHERE r.tin_value IN (SELECT tin_value FROM priced)
                  AND n.zip IS NOT NULL AND trim(n.zip) <> ''
                GROUP BY r.tin_value
            )
            SELECT p.tin_value, p.zip, p.n_therapy, p.n_npis,
                   any_value(d.display_name) AS display_name,
                   any_value(d.is_therapy)   AS is_therapy
            FROM placed p LEFT JOIN tin_directory d USING (tin_value)
            WHERE p.zip IN (SELECT unnest(?::VARCHAR[]))
            GROUP BY p.tin_value, p.zip, p.n_therapy, p.n_npis
        """, [*params, subject_tins, in_radius]).fetchall()

        # distances for the ones we kept, so the list can be read as a map
        zips = sorted({a[1] for a in alts if a[1]})
        dist = dict(con.execute(f"""
            SELECT z.zip, {miles} FROM _zcta z
            WHERE z.zip IN (SELECT unnest(?::VARCHAR[]))
        """, [zips]).fetchall()) if zips else {}

        # ACS demand inside the same radius (absent, never guessed)
        seniors = population = None
        try:
            row = con.execute("""
                SELECT sum(population), sum(pop_65_plus) FROM zip_demographics
                WHERE zip IN (SELECT unnest(?::VARCHAR[]))
            """, [in_radius]).fetchone()
            population, seniors = (row or (None, None))
        except Exception:  # noqa: BLE001 — no ACS table loaded is normal
            population = seniors = None

    rows = []
    for tin, zipc, n_therapy, n_npis, name, is_therapy in alts:
        # therapy_only scoping already applied by _market_where when the caller
        # asked for it; is_therapy is carried so the reader can see the mix
        rows.append({
            "tin_value": mask_tin(tin),
            "display_name": name or "(name pending)",
            "zip": zipc,
            "miles": round(dist[zipc], 1) if zipc in dist else None,
            "npi_count": n_npis,
            "therapy_npis": n_therapy,
            "is_therapy_practice": bool(is_therapy),
        })
    rows.sort(key=lambda r: (r["miles"] is None, r["miles"] or 0))

    n_alt = len(rows)
    per_alt = (round(seniors / n_alt, 0)
               if seniors and n_alt else None)
    band = ("thin" if n_alt <= THIN_ALTERNATIVES
            else "crowded" if n_alt >= CROWDED_ALTERNATIVES else "moderate")

    if n_alt == 0:
        headline = (
            f"Inside {radius:g} miles of this practice, {payer} publishes rates "
            "for NO other therapy practice. If this contract ends, the payer "
            "has no published local alternative at all.")
    elif band == "thin":
        headline = (
            f"{payer} publishes rates for only {n_alt} other therapy "
            f"practice{'s' if n_alt != 1 else ''} within {radius:g} miles"
            + (f", covering {int(seniors):,} residents aged 65+ — about "
               f"{int(per_alt):,} seniors per remaining practice"
               if per_alt else "")
            + ". That is a thin fallback for the payer.")
    elif band == "crowded":
        headline = (
            f"{payer} publishes rates for {n_alt} other therapy practices "
            f"within {radius:g} miles. The payer has ready alternatives here, "
            "so leverage has to come from the rate case itself, not from scarcity.")
    else:
        headline = (
            f"{payer} publishes rates for {n_alt} other therapy practices "
            f"within {radius:g} miles"
            + (f" ({int(per_alt):,} seniors each)" if per_alt else "")
            + " — a moderate fallback.")

    return {
        "subject": subject, "payer": payer,
        "subject_tins": [mask_tin(t) for t in subject_tins],
        "home_zip": home_zip, "radius_miles": radius,
        "alternatives": rows[:200], "n_alternatives": n_alt,
        "band": band,
        "population": int(population) if population else None,
        "seniors": int(seniors) if seniors else None,
        "seniors_per_alternative": int(per_alt) if per_alt else None,
        "demographics_loaded": seniors is not None,
        "zips_in_radius": len(in_radius),
        "market": {k: v for k, v in m.items() if not k.startswith("_")},
        "headline": headline,
        "note": LEVERAGE_NOTE,
    }


def leverage_summary(store: Store, subject: str, market: dict | None = None,
                     *, radius_miles: float = 15.0) -> dict:
    """Leverage against EVERY payer the subject contracts with, thinnest first.

    The negotiating order: open with the payer that can least afford to lose
    you. Per-payer failures are isolated — one payer that cannot be placed must
    not cost the whole ranking.
    """
    from .benchmark import BenchmarkError, subject_payers

    payers = subject_payers(store, subject, market or {})
    out, skipped = [], []
    for p in payers:
        try:
            r = network_leverage(store, subject, p, market,
                                 radius_miles=radius_miles)
        except BenchmarkError as e:
            skipped.append({"payer": p, "reason": str(e)})
            continue
        except Exception as e:  # noqa: BLE001
            log.warning("leverage for %s failed: %s", p, e)
            skipped.append({"payer": p, "reason": f"could not be computed ({e})"})
            continue
        out.append({k: r[k] for k in
                    ("payer", "n_alternatives", "band", "seniors_per_alternative",
                     "headline")})
    out.sort(key=lambda r: r["n_alternatives"])
    thin = [r for r in out if r["band"] == "thin"]
    return {
        "subject": subject, "radius_miles": radius_miles,
        "payers": out, "count": len(out), "skipped": skipped,
        "headline": (
            f"Open with {thin[0]['payer']}: only {thin[0]['n_alternatives']} "
            f"other local practice{'s' if thin[0]['n_alternatives'] != 1 else ''} "
            "publish rates with them."
            if thin else
            f"No payer has a thin local network here — every one of the "
            f"{len(out)} has alternatives within {radius_miles:g} miles."
            if out else "No payers found for this practice."),
        "note": LEVERAGE_NOTE,
    }
