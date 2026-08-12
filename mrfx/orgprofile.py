"""One-organization bundle, built to be combined with the Medicare Order &
Referring Tracker.

The two tools know disjoint halves of the same organization:

- The tracker knows the REFERRAL and ELIGIBILITY side — who feeds the practice
  Medicare patients, whether its referrers are still eligible to order/refer,
  which practice group its therapists reassign to, its competitive position.
  It has no negotiated commercial rates; MRFs are not in its world.
- MRF Explorer knows the CONTRACT side — what each commercial payer actually
  pays this practice, per code, and how that compares to its peers and to
  Medicare. It has no referral or claims data; MRFs contain published rates
  and nothing else.

The join is the NPI: the tracker is NPI-native (Provider lookup, Batch NPI
check, Watchlist all take NPIs), while this app is TIN-grained. So the bundle
leads with a paste-ready NPI list and carries the rate profile alongside it.

Everything here is derived from the same `compute_fee_schedule` the Rate card
uses, so the numbers in the bundle and the numbers on screen cannot diverge.
"""

from __future__ import annotations

import csv
import datetime as dt
import io

from . import __version__
from .benchmark import BenchmarkError, normalize_market, resolve_subject_tins
from .catalog import code_info
from .config import MrfxConfig
from .schedule import (_methodology, compute_fee_schedule,
                       payer_scorecard, require_rate_card_content)
from .store import Store, defuse_csv, mask_tin


def _w(rows: list[list]) -> str:
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return out.getvalue()


def compute_org_profile(store: Store, subject: str, market: dict) -> dict:
    """Identity + NPIs + the full rate profile for ONE organization."""
    market = normalize_market(market)
    tins = resolve_subject_tins(store, subject)
    if not tins:
        raise BenchmarkError(f"no practice matched '{subject}'")

    fs = compute_fee_schedule(store, subject, market)
    require_rate_card_content(fs)          # refuse an empty bundle, as reports do
    sc = payer_scorecard(fs)

    with store.connect() as con:
        # The NPIs that actually bill under this practice's TIN(s) — the handle
        # the tracker works in. Includes the NPI-in-the-TIN-slot case.
        npis = [r[0] for r in con.execute(
            "SELECT DISTINCT npi FROM rates WHERE tin_value IN "
            "(SELECT unnest(?::VARCHAR[])) AND npi IS NOT NULL ORDER BY npi",
            [tins]).fetchall()]
        ident = con.execute(
            "SELECT any_value(display_name), "
            "       list_sort(list_distinct(flatten(list(states)))), "
            "       list_sort(list_distinct(flatten(list(cities)))) "
            "FROM tin_directory WHERE tin_value IN (SELECT unnest(?::VARCHAR[]))",
            [tins]).fetchone() or (None, [], [])
        # NPPES contact detail, same source the outreach export uses
        contact = con.execute(
            "SELECT mode(org_name) FILTER (org_name IS NOT NULL), "
            "       mode(address)  FILTER (address IS NOT NULL), "
            "       mode(city)     FILTER (city IS NOT NULL), "
            "       mode(state)    FILTER (state IS NOT NULL), "
            "       mode(zip)      FILTER (zip IS NOT NULL), "
            "       mode(phone)    FILTER (phone IS NOT NULL) "
            "FROM npi_directory WHERE npi IN (SELECT unnest(?::VARCHAR[]))",
            [npis or [""]]).fetchone() or (None,) * 6
        months = [r[0] for r in con.execute(
            "SELECT DISTINCT file_month FROM rates_by_tin WHERE tin_value IN "
            "(SELECT unnest(?::VARCHAR[])) ORDER BY file_month", [tins]).fetchall()]

    return {
        "subject": subject,
        "display_name": ident[0] or contact[0] or subject,
        "tins": [mask_tin(t) for t in tins],
        "npis": npis,
        "states": list(ident[1] or []),
        "cities": list(ident[2] or []),
        "address": contact[1], "city": contact[2], "state": contact[3],
        "zip": contact[4], "phone": contact[5],
        "months_present": months,
        "fee_schedule": fs,
        "scorecard": sc,
    }


def org_bundle_files(store: Store, profile: dict) -> dict[str, str]:
    """filename -> text, for the zip. Deliberately plain CSV/TXT: these are
    meant to be opened in Excel and pasted into the tracker, not parsed."""
    fs, sc = profile["fee_schedule"], profile["scorecard"]
    name = profile["display_name"]
    files: dict[str, str] = {}

    # 1. THE BRIDGE. One NPI per line, nothing else — the tracker's Provider
    #    lookup and Batch NPI check both take exactly this. No header: a header
    #    line would be scanned for 10-digit numbers like any other text.
    files["npis.txt"] = "\n".join(profile["npis"]) + ("\n" if profile["npis"] else "")

    # 3. The rate profile — one row per payer x code, the app's house basis
    rows = [["payer", "scorecard_rank", "billing_code", "description", "discipline",
             "rate", "pct_of_medicare", "payer_peer_median", "peer_practices",
             "vs_peer_median_pct"]]
    rank = {r["payer"]: r.get("rank") for r in sc["rows"]}
    for entry in fs["codes"]:
        desc, disciplines, _timed = code_info(entry["billing_code"])
        for payer in fs["payers"]:
            v = entry["rates"].get(payer)
            if not v or v["rate"] is None:
                continue
            rows.append([
                defuse_csv(payer), rank.get(payer) or "", entry["billing_code"],
                defuse_csv(desc) or "", "/".join(disciplines), v["rate"],
                "" if v["pct_medicare"] is None else int(v["pct_medicare"]),
                "" if v.get("market_median") is None else v["market_median"],
                "" if v.get("market_median") is None else (v.get("n_peers") or 0),
                "" if v.get("vs_market_pct") is None else v["vs_market_pct"],
            ])
    files["rates.csv"] = _w(rows)

    # 4. Payer scorecard — who pays this practice best
    # The scorecard ranks on % of Medicare when an MPFS anchor is loaded and on
    # % of the best payer otherwise; carry BOTH columns and name which one the
    # rank used, so the CSV can't imply a Medicare comparison that never ran.
    prows = [["rank", "payer", "codes_priced", "codes_head_to_head", "median_rate",
              "median_pct_of_medicare", "median_pct_of_best", "ranked_on"]]
    for r in sc["rows"]:
        prows.append([
            r.get("rank") or "", defuse_csv(r["payer"]), r.get("n_codes") or 0,
            r.get("n_comparable") or 0, r.get("median_rate") or "",
            "" if r.get("median_pct_medicare") is None else r["median_pct_medicare"],
            "" if r.get("median_pct_of_best") is None else r["median_pct_of_best"],
            sc.get("metric", ""),
        ])
    files["payers.csv"] = _w(prows)

    # 5. Medicare layers, when they've been imported. These are the halves the
    #    rate data cannot supply: who may order/refer, and who feeds the
    #    practice patients. Absent unless the user has imported them.
    from .medicare import (REFERRAL_CAVEAT, npi_eligibility, org_referrals,
                           recent_losses)

    elig = npi_eligibility(store, profile["npis"])
    if elig:
        erows = [["npi", "on_order_referring_list", "name",
                  "partb", "dme", "hha", "pmd", "hospice", "release"]]
        for e in elig:
            erows.append([e["npi"], "Y" if e["on_list"] else "N", defuse_csv(e["name"]),
                          *["" if e[f] is None else ("Y" if e[f] else "N")
                            for f in ("partb", "dme", "hha", "pmd", "hospice")],
                          e["release"] or ""])
        erows.append([])
        erows.append(["NOTE: therapists and organizations are NOT on the Order & "
                      "Referring roster at all, so 'N' for this practice's own NPIs "
                      "is expected and is not a finding. The roster matters for the "
                      "REFERRING physicians in referral_sources.csv."])
        files["eligibility.csv"] = _w(erows)

    for direction, fname in (("in", "referral_sources.csv"),
                             ("out", "referral_destinations.csv")):
        ref = org_referrals(store, profile["npis"], direction, limit=250)
        if not ref["rows"]:
            continue
        row_npis = [r["npi"] for r in ref["rows"]]
        by_npi = {e["npi"]: e for e in npi_eligibility(store, row_npis)}
        losses = recent_losses(store, row_npis)   # same flag the dashboard shows
        rrows = [["npi", "name", "taxonomy", "shared_patients", "transactions",
                  "avg_day_wait", "still_eligible_partb", "recent_change",
                  "dataset", "data_year"]]
        for r in ref["rows"]:
            e = by_npi.get(r["npi"]) or {}
            rrows.append([
                r["npi"], defuse_csv(r["name"]), r["taxonomy"], r["patients"],
                r["transactions"], r["avg_day_wait"] if r["avg_day_wait"] else "",
                "" if not e.get("on_list") else ("Y" if e.get("partb") else "N"),
                losses.get(r["npi"], ""),
                ref["dataset"], ref["data_year"]])
        rrows.append([])
        rrows.append([f"NOTE: {REFERRAL_CAVEAT}"])
        files[fname] = _w(rrows)

    # 2. Identity, so the bundle is self-describing away from the app
    files["profile.txt"] = "\n".join([
        f"Organization:   {name}",
        f"Subject as entered: {profile['subject']}",
        f"Tax IDs:        {', '.join(profile['tins']) or '(none)'}",
        f"NPIs:           {len(profile['npis'])}",
        f"Location:       {', '.join(x for x in (profile.get('city'), profile.get('state')) if x) or '(not resolved)'}",
        f"Address:        {profile.get('address') or '(not resolved)'}",
        f"Phone:          {profile.get('phone') or '(not resolved)'}",
        f"States seen:    {', '.join(profile['states']) or '(none)'}",
        f"Payers:         {len(fs['payers'])}",
        f"Codes priced:   {len(fs['codes'])}",
        f"Months present: {', '.join(profile['months_present']) or '(none)'}",
        f"As-of month:    {fs.get('month')}",
        "",
        "HOW TO USE THIS WITH THE ORDER & REFERRING TRACKER",
        "  1. Open npis.txt and copy the NPIs.",
        "  2. Tracker -> Provider lookup -> paste them -> Full report (one-stop)",
        "     for this practice's referral base, eligibility and competitors.",
        "     (Several NPIs pasted together are analyzed as one practice, which",
        "     is the right move here: a practice's volume is usually split",
        "     across its organization NPI and its therapists'.)",
        "  3. Tracker -> Batch NPI check on the same list confirms which of",
        "     these NPIs are currently eligible to order/refer for Medicare.",
        "  4. Read that alongside rates.csv and payers.csv here: the tracker",
        "     tells you where this practice's patients come from, this bundle",
        "     tells you what its commercial contracts pay.",
        "",
        "WHAT IS IN THIS BUNDLE",
    ] + [f"  {n}" for n in sorted(files)] + [
        "",
    ] + ([
        "The Medicare layers above were imported into this app, so this bundle",
        "already carries eligibility and referral structure. The tracker remains",
        "the richer view of both (competitors, groups, trends, maps).",
    ] if any(k in files for k in ("eligibility.csv", "referral_sources.csv",
                                  "referral_destinations.csv")) else [
        "WHAT THIS BUNDLE DOES NOT CONTAIN",
        "  Referral, claims, volume or Medicare-eligibility data — none has been",
        "  imported here. Machine-readable files publish negotiated RATES only.",
        "  Run `mrfx medicare` to add those layers, or get them from the tracker.",
    ])) + "\n"


    files["methodology.txt"] = "\n".join([
        f"Generated:      {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC "
        f"by MRF Explorer v{__version__}",
        f"Organization:   {name}",
        f"Tax IDs:        {', '.join(profile['tins'])}",
        f"NPIs exported:  {len(profile['npis'])}",
        f"Rate rows:      {max(0, len(rows) - 1)}",
        "",
        _methodology(store, fs),
        "",
        "Source: payers' Transparency-in-Coverage machine-readable files, as",
        "ingested by this app. These are PUBLISHED NEGOTIATED RATES — not paid",
        "amounts, not volumes, and not evidence that any service was rendered.",
    ] + ([
        "",
        "eligibility.csv: CMS Order & Referring roster, as imported into this",
        "app (`mrfx medicare`). Current enrollment eligibility only.",
    ] if "eligibility.csv" in files else []) + ([
        "",
        "referral_sources.csv / referral_destinations.csv: CMS or CareSet",
        "shared-patient pair data, as imported into this app. " + REFERRAL_CAVEAT,
    ] if "referral_sources.csv" in files or "referral_destinations.csv" in files
        else [
        "",
        "This bundle contains no Medicare referral or eligibility data — none",
        "has been imported. Run `mrfx medicare` to add it, or use the Order &",
        "Referring Tracker for that side.",
    ])) + "\n"
    return files
