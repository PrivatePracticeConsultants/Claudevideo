"""Two documents that make the whole store sellable.

**The prospect dossier.** Every growth signal in this app lives on a different
tab: below-market rates on Leads, size on Medicare, referral sources under
Medicare again, market room under Leads, payer weakness under Negotiate. A cold
call needs them on ONE page: *here is what I already know about your practice,
and here is the money on the table.* That page is the outreach weapon this data
has been building toward, and it is pure recombination — no new computation, no
new dataset.

**The market report.** The same trick one level up: a metro-level story
(rates, payer concentration, geography, openings and closures, demographics) as
one branded document a consultant can sell BEFORE having a client in that
market. Every section already computes; nothing here invents a number.

HONESTY. Both documents are assembled from other modules' results and carry
those modules' caveats verbatim — a caveat that gets dropped in transit is the
easiest way for an honest number to become a dishonest claim. Every section is
fault-isolated: a section that cannot be built is LISTED as omitted with its
reason, because a branded page with a silently missing section reads as a
complete picture when it is not.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import statistics

from . import __version__
from .config import MrfxConfig
from .sqlfn import modal_zip_of
from .store import Store

log = logging.getLogger(__name__)

DOSSIER_NOTE = (
    "Everything in this dossier comes from public data: payers' published "
    "machine-readable rate files, the NPPES provider registry, CMS Medicare "
    "claims summaries, and the Census. Nothing was obtained from the practice. "
    "Published rates are not proof of collection, Medicare volumes are a floor "
    "(fee-for-service only, small codes suppressed), and shared-patient counts "
    "are a proxy for referral rather than a record of one."
)


def _doc(cfg: MrfxConfig, title: str, body: str, footer: str = "") -> str:
    from .benchmark import _brand_header
    from .spark import SPARK_CSS
    e = html.escape
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{e(title)}</title>
<style>
 body {{ font: 13px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
        color: #101828; max-width: 940px; margin: 32px auto; padding: 0 24px; }}
 h1 {{ font-size: 21px; margin-bottom: 2px; }}
 h2 {{ font-size: 15px; margin-top: 26px; border-bottom: 1px solid #eaecf0;
      padding-bottom: 4px; }}
 .brand {{ color: #475467; font-size: 12px; text-transform: uppercase;
          letter-spacing: .06em; }}
 .brandbar {{ display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }}
 .brandbar .brand {{ margin: 0; }} .logo {{ max-height: 40px; max-width: 200px; }}
 .meta {{ color: #475467; margin-bottom: 16px; }}
 .lede {{ font-size: 14px; background: #f4f7fb; border-left: 3px solid #2a78d6;
         padding: 10px 14px; margin: 14px 0; }}
 table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums;
         margin-top: 6px; }}
 th, td {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid #eaecf0;
          vertical-align: top; }}
 th {{ font-size: 11px; color: #667085; text-transform: uppercase;
      letter-spacing: .04em; }}
 .num {{ text-align: right; }} .sub {{ color: #667085; font-size: 11.5px; }}
 .note {{ color: #475467; font-size: 11.5px; margin-top: 6px; }}
 .omitted {{ color: #7a4a00; background: #fbe9d0; border: 1px solid #d99a3a;
            padding: 8px 12px; font-size: 12px; margin-top: 14px; }}
{SPARK_CSS}
 footer {{ margin-top: 34px; border-top: 1px solid #d0d5dd; padding-top: 12px;
          color: #475467; font-size: 11px; white-space: pre-wrap; }}
 @media print {{ body {{ margin: 0; }} }}
</style></head><body>
{_brand_header(cfg)}
<h1>{e(title)}</h1>
{body}
<footer>{e(footer)}</footer>
</body></html>"""


def _omitted_html(skipped: list[str]) -> str:
    if not skipped:
        return ""
    items = "".join(f"<li>{html.escape(s)}</li>" for s in skipped)
    return ("<div class='omitted'><b>Not included, and why:</b>"
            f"<ul>{items}</ul>"
            "A section is omitted when the data cannot support it. It is listed "
            "rather than dropped, so this page is never read as a complete "
            "picture when it is not.</div>")


def build_prospect_dossier(cfg: MrfxConfig, store: Store, subject: str,
                           market: dict | None = None, *,
                           radius_miles: float = 25.0) -> dict:
    """One page about a practice you have never spoken to."""
    from .benchmark import BenchmarkError, resolve_subject_tins
    e = html.escape
    market = dict(market or {})
    tins = resolve_subject_tins(store, subject)
    if not tins:
        raise BenchmarkError(f"no practice matches {subject!r}")
    # resolve_subject_tins falls through to the RAW STRING for an unknown
    # subject, so a typo would otherwise assemble a branded page with every
    # section omitted — which is worse than a refusal, because a user might
    # send it. Confirm the practice is really in the store first.
    with store.connect() as con:
        known = con.execute(
            "SELECT 1 FROM rates WHERE tin_value IN (SELECT unnest(?::VARCHAR[])) "
            "LIMIT 1", [tins]).fetchone()
    if not known:
        raise BenchmarkError(
            f"no practice matches {subject!r} in this store — check the "
            "practice name, tax ID or NPI. A dossier is a document you send, "
            "so it is refused rather than rendered empty.")

    # GEOGRAPHY (round 6 P3). Every other client document is scoped to one
    # state (require_geographic_scope); the dossier pooled every loaded state
    # into one "market", so a Missouri practice was ranked against Florida
    # rates. With no state chosen it is now scoped to the practice's OWN main
    # state and says so. A deliberate national comparison (allow_national)
    # still works, under the same banner the other reports print; a practice
    # with no known state gets its rate sections omitted with the reason.
    from .benchmark import anchor_state, require_geographic_scope
    # leverage keeps the caller's market: its geography is the RADIUS, and a
    # payer's alternatives across a state line (Kansas City) are real ones
    radius_market = dict(market)
    scope_note = ""
    if not market.get("state"):
        try:
            scope_note = require_geographic_scope(market, "prospect dossier")
        except BenchmarkError:
            home_state = anchor_state(store, {}, tins)
            if home_state:
                market["state"] = home_state
                scope_note = (
                    f"Rates are compared within {home_state}, this practice's main "
                    "state (no state was chosen, and reimbursement varies by state).")

    parts: list[str] = []
    skipped: list[str] = []
    facts: dict = {"subject": subject, "state": market.get("state")}

    def section(name: str, fn):
        try:
            out = fn()
            if out:
                parts.append(out)
        except BenchmarkError as e_:
            skipped.append(f"{name}: {e_}")
        except Exception as e_:  # noqa: BLE001 — never lose the page
            log.warning("dossier %s / %s failed: %s", subject, name, e_)
            skipped.append(f"{name}: could not be built ({e_})")

    # 1. who they are
    def _identity():
        with store.connect() as con:
            # one name, the same on every call (the member TIN with the most
            # NPIs, ties to the lowest tax ID) and the practice's member-TIN
            # NPI total — any_value() mixed one TIN's name with another's count
            row = con.execute(
                "SELECT first(display_name ORDER BY npi_count DESC NULLS LAST, tin_value) "
                "       FILTER (display_name IS NOT NULL), sum(npi_count), "
                "list_sort(list_distinct(flatten(list(cities)))), "
                "list_sort(list_distinct(flatten(list(states)))) FROM tin_directory "
                "WHERE tin_value IN (SELECT unnest(?::VARCHAR[]))", [tins]).fetchone()
        name, npis, cities, states = row or (None, None, None, None)
        facts["display_name"] = name or subject
        facts["npi_count"] = npis
        where = ", ".join(list(cities or [])[:3]) + \
            (f" ({', '.join(list(states or [])[:3])})" if states else "")
        return (f"<div class='lede'><b>{e(str(name or subject))}</b> — "
                f"{npis or 0} provider NPI(s){', ' + e(where) if where.strip() else ''}. "
                "Everything below is from public data; nothing came from the practice."
                "</div>")

    # 2. what they bill (size)
    def _size():
        from .utilization import practice_utilization
        u = practice_utilization(store, tins)
        if not u["codes"]:
            raise BenchmarkError(
                "no Medicare utilization on file for this practice")
        top = sorted(u["codes"], key=lambda c: c["units"] or 0, reverse=True)[:8]
        facts["medicare_units"] = sum(int(c["units"] or 0) for c in u["codes"])
        rows = "".join(
            f"<tr><td>{e(c['billing_code'])}<div class='sub'>"
            f"{e(c.get('description') or '')}</div></td>"
            f"<td class='num'>{int(c['units'] or 0):,}</td></tr>" for c in top)
        return ("<h2>How much therapy they bill</h2>"
                f"<p>About <b>{facts['medicare_units']:,}</b> Medicare services a "
                f"year ({e(str(u.get('year') or ''))}), led by:</p>"
                "<table><thead><tr><th>Code</th>"
                "<th class='num'>Annual services</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>"
                f"<p class='note'>{e(u.get('note') or '')}</p>")

    # 3. where their rates sit
    def _rates():
        from .benchmark import compute_benchmark
        require_geographic_scope(market, "prospect dossier")
        b = compute_benchmark(store, subject, market)
        rows = [r for r in b.get("rows", [])
                if r.get("subject_percentile") is not None]
        if not rows:
            raise BenchmarkError("no comparable published rates under this scope")
        low = sorted(rows, key=lambda r: (r["subject_percentile"], r["billing_code"]))[:8]
        # the MEDIAN of the per-code positions — the one headline rule the
        # Leads list and the per-payer view use. This was a mean stored under
        # this name, so one outlier code swung the dossier but not the list.
        facts["median_percentile"] = round(
            statistics.median(r["subject_percentile"] for r in rows), 1)
        body = "".join(
            f"<tr><td>{e(r['billing_code'])}</td>"
            f"<td class='num'>${(r['subject_rate'] or 0):,.2f}</td>"
            f"<td class='num'>${(r['p50'] or 0):,.2f}</td>"
            f"<td class='num'>p{r['subject_percentile']:.0f}</td>"
            # the peer count travels with every position (the rate card's rule):
            # p10 of two practices is not p10 of a market
            f"<td class='num'>{int(r.get('n_peers') or 0):,}</td></tr>"
            for r in low)
        return ("<h2>Where their rates sit</h2>"
                f"<p>Across {len(rows)} comparable code(s) their median position "
                f"is <b>p{facts['median_percentile']:g}</b> against their market. "
                "Weakest lines:</p>"
                + (f"<p class='note'>{e(scope_note)}</p>" if scope_note else "")
                + "<table><thead><tr><th>Code</th><th class='num'>Their rate</th>"
                "<th class='num'>Market median</th><th class='num'>Position</th>"
                "<th class='num'>Peers</th></tr></thead>"
                f"<tbody>{body}</tbody></table>"
                f"<p class='note'>{e(b.get('basis_note') or '')}</p>")

    # 4. which payer is weakest for them
    def _weakest():
        from .benchmark import compute_payer_negotiation
        require_geographic_scope(market, "prospect dossier")
        neg = compute_payer_negotiation(store, subject, market)
        secs = neg.get("sections") or []
        if not secs:
            raise BenchmarkError("no payer sections could be built")
        # Position and the below-median count come from the section itself and
        # its rows. They were read from a `summary` block that only the
        # one-payer comparison builds — never this one — so every dossier
        # printed "–" in both columns (tests/mrfx/test_known_answer_leads.py).
        def _below(s):
            rows = [r for r in s["benchmark"]["rows"]
                    if r.get("subject_rate") is not None and r.get("p50") is not None]
            return f"{sum(r['subject_rate'] < r['p50'] for r in rows)} of {len(rows)}"
        rows = "".join(
            f"<tr><td>{e(s['payer'])}</td>"
            f"<td class='num'>{'–' if s.get('headline_percentile') is None else 'p%d' % s['headline_percentile']}</td>"
            f"<td class='num'>{_below(s)}</td></tr>"
            for s in secs[:6])
        facts["weakest_payer"] = secs[0]["payer"]
        return ("<h2>Which payer is weakest for them</h2>"
                "<p>Ordered weakest first — the contract with the most room:</p>"
                "<table><thead><tr><th>Payer</th><th class='num'>Their position</th>"
                "<th class='num'>Codes below median</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>")

    # 5. leverage: how thin is that payer's local network
    def _leverage():
        from .leverage import network_leverage
        if not facts.get("weakest_payer"):
            raise BenchmarkError("no payer identified to measure leverage against")
        lv = network_leverage(store, subject, facts["weakest_payer"], radius_market,
                              radius_miles=radius_miles)
        return ("<h2>Their leverage with that payer</h2>"
                f"<p>{e(lv['headline'])}</p>"
                f"<p class='note'>{e(lv['note'])}</p>")

    # 6. who refers to them
    def _referrals():
        from .medicare import org_referrals
        with store.connect() as con:
            npis = [n for (n,) in con.execute(
                "SELECT DISTINCT npi FROM rates WHERE tin_value IN "
                "(SELECT unnest(?::VARCHAR[])) AND npi IS NOT NULL",
                [tins]).fetchall()]
        if not npis:
            raise BenchmarkError("no NPIs resolved for this practice")
        ref = org_referrals(store, npis, "in", limit=8)
        if not ref["rows"]:
            raise BenchmarkError("no referral data loaded for this practice")
        rows = "".join(
            f"<tr><td>{e(str(r.get('name') or r['npi']))}</td>"
            f"<td class='num'>{int(r.get('patients') or 0):,}</td></tr>"
            for r in ref["rows"])
        return ("<h2>Who sends them patients</h2>"
                f"<p>From {e(str(ref.get('dataset') or 'the loaded release'))}:</p>"
                "<table><thead><tr><th>Referral source</th>"
                "<th class='num'>Shared patients</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>"
                f"<p class='note'>{e(ref.get('caveat') or '')}</p>")

    # 7. is their market growing
    def _market():
        from .demographics import market_sizing
        place = store.placement_relation()
        with store.connect() as con:
            z = con.execute(f"""
                SELECT * FROM ({modal_zip_of("SELECT unnest(?::VARCHAR[])", place)})
            """, [tins]).fetchone()
        home = z[0] if z else None
        if not home:
            raise BenchmarkError("no NPPES location on file for this practice")
        ms = market_sizing(store, home, radius_miles)
        if not ms.get("loaded"):
            raise BenchmarkError(ms.get("reason") or "no demographics loaded")
        return ("<h2>Their market</h2>"
                f"<p>Within {radius_miles:g} miles of {e(home)}: "
                f"<b>{int(ms.get('population') or 0):,}</b> residents, "
                f"<b>{int(ms.get('pop_65_plus') or 0):,}</b> aged 65+, against "
                f"{int(ms.get('practices') or 0):,} therapy practice(s).</p>"
                f"<p class='note'>{e(ms.get('note') or '')}</p>")

    section("Identity", _identity)
    section("Medicare volume", _size)
    section("Rate position", _rates)
    section("Weakest payer", _weakest)
    section("Negotiating leverage", _leverage)
    section("Referral sources", _referrals)
    section("Market size", _market)

    parts.append(_omitted_html(skipped))
    footer = (
        f"Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
        f"by MRF Explorer v{__version__}.\n{DOSSIER_NOTE}")
    title = f"Practice profile — {facts.get('display_name') or subject}"
    return {"subject": subject, "html": _doc(cfg, title, "\n".join(parts), footer),
            "facts": facts, "skipped": skipped, "note": DOSSIER_NOTE}


def build_market_report(cfg: MrfxConfig, store: Store, *, state: str,
                        code: str = "97110", zip_code: str | None = None,
                        radius_miles: float = 25.0,
                        market: dict | None = None) -> dict:
    """The metro-level story, as one branded document."""
    from .benchmark import BenchmarkError
    e = html.escape
    from .states import state_code
    try:
        st = state_code(state)
    except ValueError as exc:
        raise BenchmarkError(str(exc)) from exc
    if not st:
        raise BenchmarkError(
            "a market report needs a two-letter state — reimbursement varies "
            "far more between states than within them")
    code = str(code or "").strip().upper() or "97110"
    m = {**(market or {}), "state": st}

    parts, skipped = [], []

    def section(name, fn):
        try:
            out = fn()
            if out:
                parts.append(out)
        except BenchmarkError as e_:
            skipped.append(f"{name}: {e_}")
        except Exception as e_:  # noqa: BLE001
            log.warning("market report %s failed: %s", name, e_)
            skipped.append(f"{name}: could not be built ({e_})")

    def _cities():
        from .territory import local_rate_map
        r = local_rate_map(store, code, m, state=st)
        if not r["cities"]:
            raise BenchmarkError(r["headline"])
        rows = "".join(
            f"<tr><td>{e(c['city'].title())}</td>"
            f"<td class='num'>${c['median_rate']:,.2f}</td>"
            f"<td class='num'>{'' if c['vs_state_pct'] is None else ('%+.1f%%' % c['vs_state_pct'])}</td>"
            f"<td class='num'>{c['n_practices']:,}</td></tr>" for c in r["cities"][:15])
        return (f"<h2>What {e(code)} pays across {e(st)}</h2>"
                f"<p>{e(r['headline'])}</p>"
                "<table><thead><tr><th>City</th><th class='num'>Median</th>"
                "<th class='num'>vs state</th><th class='num'>Practices</th>"
                "</tr></thead>"
                f"<tbody>{rows}</tbody></table>"
                f"<p class='note'>{e(r['note'])}</p>")

    def _concentration():
        from .territory import payer_concentration
        r = payer_concentration(store, m, code=code)
        if not r["payers"]:
            raise BenchmarkError("no payers in this scope")
        rows = "".join(
            f"<tr><td>{e(p['payer'])}</td><td class='num'>{p['share_pct']}%</td>"
            f"<td class='num'>{p['n_practices']:,}</td>"
            # a payer with no rate for this code has no median — never "$0.00"
            f"<td class='num'>{'–' if p['median_rate'] is None else '$%s' % format(p['median_rate'], ',.2f')}</td></tr>"
            for p in r["payers"][:12])
        return ("<h2>Who holds the contracts</h2>"
                f"<p>{e(r['headline'])}</p>"
                "<table><thead><tr><th>Payer</th><th class='num'>Share</th>"
                f"<th class='num'>Practices</th><th class='num'>Median {e(code)} rate</th>"
                "</tr></thead>"
                f"<tbody>{rows}</tbody></table>"
                f"<p class='note'>{e(r['note'])}</p>")

    def _openings():
        from .nppes import closures, new_enumerations
        ne = new_enumerations(store, state=st, days=365, limit=200)
        cl = closures(store, state=st, days=365, limit=200)
        if ne.get("reason") and cl.get("reason"):
            raise BenchmarkError(ne["reason"])
        return ("<h2>Who opened and who closed</h2>"
                f"<p>In the last year in {e(st)}: "
                f"<b>{ne.get('total', 0):,}</b> therapy NPI(s) newly issued, "
                f"<b>{cl.get('total', 0):,}</b> deactivated and not reactivated.</p>"
                f"<p class='note'>{e(cl.get('note') or ne.get('note') or '')}</p>")

    def _demand():
        from .demographics import market_sizing
        if not zip_code:
            raise BenchmarkError(
                "pass a ZIP to size local demand (the state as a whole has no "
                "single radius)")
        ms = market_sizing(store, zip_code, radius_miles)
        if not ms.get("loaded"):
            raise BenchmarkError(ms.get("reason") or "no demographics loaded")
        return ("<h2>Demand around "
                f"{e(str(zip_code))}</h2>"
                f"<p><b>{int(ms.get('population') or 0):,}</b> residents and "
                f"<b>{int(ms.get('pop_65_plus') or 0):,}</b> aged 65+ within "
                f"{radius_miles:g} miles, against "
                f"{int(ms.get('practices') or 0):,} therapy practice(s)"
                + (f" — about {int(ms['seniors_per_practice']):,} seniors each."
                   if ms.get("seniors_per_practice") else ".")
                + "</p>"
                f"<p class='note'>{e(ms.get('note') or '')}</p>")

    def _quality():
        from .quality import payer_posture
        r = payer_posture(store, m)
        if not r["payers"]:
            raise BenchmarkError("no payers in this scope")
        rows = "".join(
            f"<tr><td>{e(p['payer'])}</td><td>{e(p['verdict'])}</td></tr>"
            for p in r["payers"][:12])
        return ("<h2>Which payers negotiate</h2>"
                f"<p>{e(r['headline'])}</p>"
                "<table><thead><tr><th>Payer</th><th>Read</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>"
                f"<p class='note'>{e(r['note'])}</p>")

    section("City rate map", _cities)
    section("Payer concentration", _concentration)
    section("Payer posture", _quality)
    section("Openings and closures", _openings)
    section("Local demand", _demand)

    if not parts:
        raise BenchmarkError(
            "no section of a market report could be built for "
            f"{st} — ingest payer files covering that state first")
    parts.append(_omitted_html(skipped))
    footer = (
        f"Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
        f"by MRF Explorer v{__version__}.\n{DOSSIER_NOTE}")
    return {"state": st, "code": code,
            "html": _doc(cfg, f"Therapy market report — {st}",
                         "\n".join(parts), footer),
            "skipped": skipped, "note": DOSSIER_NOTE}
