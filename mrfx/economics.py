"""Per-visit economics, and a position number weighted by what's actually billed.

Two problems with the same root cause: the app reports per-CODE numbers, and a
practice owner does not think in codes.

**Per-visit value.** Nobody runs a clinic on "$44 for 97110". They run it on
"what is an Aetna visit worth?" A therapy visit is a BASKET — typically an
evaluation or a timed unit or two plus a manual-therapy or modality line — and
the practice's own Medicare claims already say which codes it bills and in what
proportion. Pricing that basket per payer turns every rate in the store into
the unit the client's P&L speaks: *"an Aetna visit pays you about $96; BCBS
about $118 — same patients, $22 a visit apart."*

**Weighted position.** The headline "you sit at p38" currently averages codes
equally, so a code the practice bills eleven times a year drags it exactly as
hard as the one it bills four thousand times. Weighting by the practice's own
volumes makes the single number a client remembers actually represent their
dollars.

HONESTY — this section is built on Medicare claims, and inherits every limit
those carry, restated wherever a number appears:

- The mix is MEDICARE fee-for-service only. A paediatric or sports practice's
  commercial mix differs, sometimes a lot. So the basket is *this practice's
  Medicare basket*, labelled as such, never "a visit".
- CMS suppresses any code under 11 beneficiaries, so small-volume codes are
  ABSENT from the mix, not zero — the basket is therefore built from the codes
  that dominate it, which is the right shape for an average but is not complete.
- Units are Medicare's service counts, not visits. A visit usually contains
  several timed units, so the basket is per-visit ONLY after dividing by an
  estimated units-per-visit, and that divisor is a user-supplied assumption
  echoed everywhere it is used. Without it, the figure is stated per-unit-mix
  rather than invented.
- Weighted position is only reported when volumes cover a real share of the
  practice's priced codes; below that it says so instead of quietly weighting a
  fragment.
"""

from __future__ import annotations

import logging

from .catalog import code_info
from .store import Store

log = logging.getLogger(__name__)

MIX_NOTE = (
    "The visit basket is built from this practice's own MEDICARE fee-for-"
    "service claims — the only per-practice code mix that exists in public "
    "data. A commercial mix can differ, and CMS suppresses codes under 11 "
    "beneficiaries, so the basket reflects the codes that dominate this "
    "practice's Medicare billing rather than every code it bills. It is priced "
    "with published negotiated rates, which are not proof of collection."
)

VISIT_NOTE = (
    "Medicare counts SERVICES (timed units), not visits. A per-visit figure "
    "therefore requires a units-per-visit divisor, which is YOUR assumption — "
    "it is echoed with every number that uses it and is never chosen for you. "
    "With no divisor supplied, figures are reported for the whole billed mix "
    "rather than scaled into a visit."
)

# Below this share of the practice's priced codes covered by volumes, a
# weighted position describes a fragment and says so rather than pretending.
MIN_WEIGHT_COVERAGE_PCT = 50.0


def visit_economics(store: Store, subject: str, market: dict | None = None, *,
                    units_per_visit: float | None = None,
                    year: str | None = None) -> dict:
    """What one payer's visit is worth to this practice, versus the others."""
    from .benchmark import BenchmarkError, resolve_subject_tins
    from .schedule import compute_fee_schedule
    from .utilization import practice_utilization

    tins = resolve_subject_tins(store, subject)
    if not tins:
        raise BenchmarkError(f"no practice matches {subject!r}")
    if units_per_visit is not None:
        try:
            units_per_visit = float(units_per_visit)
        except (TypeError, ValueError):
            raise BenchmarkError(
                "units per visit must be a number (e.g. 3 timed units a visit)")
        if not 0.5 <= units_per_visit <= 20:
            raise BenchmarkError(
                "units per visit must be between 0.5 and 20 — outside that it "
                "describes something other than a therapy visit")

    util = practice_utilization(store, tins, year)
    if not util["codes"]:
        return {
            "loaded": False, "subject": subject, "rows": [],
            "reason": (
                "no Medicare utilization is on file for this practice, so its "
                "own code mix is unknown — import a CMS Physician & Other "
                "Practitioners file (Data tab), or use the per-code rate card"),
            "note": MIX_NOTE,
        }

    mix = {c["billing_code"]: float(c["units"] or 0) for c in util["codes"]
           if (c["units"] or 0) > 0}
    total_units = sum(mix.values())
    if not total_units:
        return {"loaded": False, "subject": subject, "rows": [],
                "reason": "this practice's Medicare rows carry no service units",
                "note": MIX_NOTE}

    fs = compute_fee_schedule(store, subject, market or {})
    # rate lookup: {(payer, code): rate}
    priced: dict[tuple, float] = {}
    for entry in fs["codes"]:
        for payer, cell in (entry.get("rates") or {}).items():
            if cell.get("rate") is not None:
                priced[(payer, entry["billing_code"])] = float(cell["rate"])
    if not priced:
        return {"loaded": False, "subject": subject, "rows": [],
                "reason": ("this practice has no published rates under this "
                           "market scope, so a basket cannot be priced"),
                "note": MIX_NOTE}

    payers = sorted({p for p, _c in priced})
    rows = []
    for payer in payers:
        # Only the codes this payer actually prices contribute, and the mix is
        # RE-NORMALIZED over them: pricing a basket with a missing code at zero
        # would make a payer that simply doesn't publish that code look cheap.
        covered = {c: u for c, u in mix.items() if (payer, c) in priced}
        cov_units = sum(covered.values())
        if not cov_units:
            continue
        value_per_unit = sum(
            priced[(payer, c)] * u for c, u in covered.items()) / cov_units
        rows.append({
            "payer": payer,
            "value_per_billed_unit": round(value_per_unit, 2),
            "value_per_visit": (round(value_per_unit * units_per_visit, 2)
                                if units_per_visit else None),
            "codes_priced": len(covered),
            "codes_in_mix": len(mix),
            "mix_coverage_pct": round(100.0 * cov_units / total_units, 1),
            "annual_medicare_units": int(cov_units),
            "annual_value_at_this_rate": round(value_per_unit * cov_units, 2),
        })
    rows.sort(key=lambda r: r["value_per_billed_unit"], reverse=True)

    best, worst = (rows[0], rows[-1]) if rows else (None, None)
    spread = (round(best["value_per_billed_unit"] - worst["value_per_billed_unit"], 2)
              if best and worst and best is not worst else None)
    unit_word = "visit" if units_per_visit else "billed unit"

    def _fig(r):
        return (r["value_per_visit"] if units_per_visit
                else r["value_per_billed_unit"])

    headline = "No payer prices enough of this practice's mix to value a visit."
    if rows and spread:
        gap = (round(_fig(best) - _fig(worst), 2))
        headline = (
            f"On this practice's own Medicare mix, a {unit_word} is worth "
            f"${_fig(best):,.2f} from {best['payer']} and ${_fig(worst):,.2f} "
            f"from {worst['payer']} — ${gap:,.2f} apart for the same work.")
    elif rows:
        headline = (f"A {unit_word} on this practice's mix is worth "
                    f"${_fig(rows[0]):,.2f} from {rows[0]['payer']}.")

    top_mix = sorted(mix.items(), key=lambda kv: kv[1], reverse=True)[:12]
    return {
        "loaded": True, "subject": subject, "rows": rows, "count": len(rows),
        "units_per_visit": units_per_visit,
        "assumption_note": (
            f"ASSUMPTION: {units_per_visit:g} billed units per visit, supplied "
            "by you — not measured. Medicare counts services, not visits."
            if units_per_visit else None),
        "mix": [{"billing_code": c, "description": code_info(c)[0],
                 "annual_units": int(u),
                 "share_pct": round(100.0 * u / total_units, 1)}
                for c, u in top_mix],
        "mix_year": util.get("year"),
        "total_annual_units": int(total_units),
        "best_payer": best["payer"] if best else None,
        "worst_payer": worst["payer"] if worst else None,
        "spread_per_unit": spread,
        "market": fs.get("market"),
        "headline": headline,
        "note": MIX_NOTE + " " + VISIT_NOTE,
    }


def weighted_position(store: Store, subject: str, market: dict | None = None, *,
                      volumes: dict | None = None, year: str | None = None,
                      benchmark: dict | None = None) -> dict:
    """The subject's market position, weighted by what it actually bills.

    The unweighted headline treats a code billed eleven times a year exactly
    like one billed four thousand times. Weighting by the practice's own volumes
    makes the number represent its dollars — but only when the volumes cover
    enough of the priced codes to mean anything.
    """
    from .benchmark import BenchmarkError, clean_volumes, compute_benchmark
    from .utilization import practice_utilization

    b = benchmark or compute_benchmark(store, subject, market or {})
    rows = [r for r in b.get("rows", [])
            if r.get("subject_percentile") is not None]
    if not rows:
        return {"loaded": False, "subject": subject,
                "reason": "no code has a comparable position under this scope",
                "note": MIX_NOTE}

    src = "supplied"
    vols = clean_volumes(volumes) if volumes else {}
    if not vols:
        from .benchmark import resolve_subject_tins
        util = practice_utilization(store, resolve_subject_tins(store, subject), year)
        vols = {c["billing_code"]: float(c["units"] or 0) for c in util["codes"]
                if (c["units"] or 0) > 0}
        src = "medicare"
        if not vols:
            return {"loaded": False, "subject": subject,
                    "reason": ("no volumes supplied and no Medicare utilization "
                               "on file, so position cannot be weighted"),
                    "note": MIX_NOTE}

    weighted_rows = [r for r in rows if vols.get(r["billing_code"], 0) > 0]
    covered_codes = len(weighted_rows)
    coverage = round(100.0 * covered_codes / len(rows), 1) if rows else 0.0

    unweighted = round(sum(r["subject_percentile"] for r in rows) / len(rows), 1)
    if coverage < MIN_WEIGHT_COVERAGE_PCT or not weighted_rows:
        return {
            "loaded": False, "subject": subject,
            "unweighted_percentile": unweighted,
            "coverage_pct": coverage, "min_coverage_pct": MIN_WEIGHT_COVERAGE_PCT,
            "volume_source": src,
            "reason": (
                f"volumes cover only {coverage:g}% of this practice's "
                f"comparable codes (at least {MIN_WEIGHT_COVERAGE_PCT:g}% is "
                "needed) — weighting a fragment would misrepresent the whole"),
            "note": MIX_NOTE,
        }

    total_w = sum(vols[r["billing_code"]] for r in weighted_rows)
    weighted = round(sum(r["subject_percentile"] * vols[r["billing_code"]]
                         for r in weighted_rows) / total_w, 1)
    shift = round(weighted - unweighted, 1)
    heaviest = max(weighted_rows, key=lambda r: vols[r["billing_code"]])
    return {
        "loaded": True, "subject": subject,
        "weighted_percentile": weighted,
        "unweighted_percentile": unweighted,
        "shift": shift,
        "coverage_pct": coverage, "n_codes_weighted": covered_codes,
        "n_codes_total": len(rows),
        "volume_source": src,
        "heaviest_code": heaviest["billing_code"],
        "heaviest_code_share_pct": round(
            100.0 * vols[heaviest["billing_code"]] / total_w, 1),
        "headline": (
            f"Weighted by what this practice actually bills, its position is "
            f"p{weighted:g} — {abs(shift):g} points "
            f"{'above' if shift > 0 else 'below'} the unweighted p{unweighted:g}, "
            f"because {heaviest['billing_code']} carries "
            f"{round(100.0 * vols[heaviest['billing_code']] / total_w):g}% of the volume."
            if abs(shift) >= 1 else
            f"Weighting by volume barely moves this practice's position "
            f"(p{weighted:g} vs p{unweighted:g}) — the rate case is consistent "
            "across its whole book."),
        "note": MIX_NOTE + (
            " Volumes are the ones you supplied." if src == "supplied"
            else " Volumes come from this practice's Medicare claims (a floor)."),
    }
