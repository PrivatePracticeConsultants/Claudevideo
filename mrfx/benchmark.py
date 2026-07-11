"""Benchmarking & negotiation-pitch analytics (§7B).

All market math runs over **dollar-rate, base-modifier rows by default**
(no CQ/CO, no percentage rows) at the TIN/entity grain, pinned to an explicit
as-of month — deviations are labeled toggles passed in by the caller. The
opportunity model never invents utilization: volumes come from the caller.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging

from . import __version__
from .catalog import code_info
from .config import MrfxConfig
from .store import Store, mask_tin

log = logging.getLogger(__name__)

PERCENTILES = (10, 25, 40, 50, 75, 90)

BASIS_NOTE = (
    "Market statistics computed over dollar-rate, base-modifier rows "
    "(no CQ/CO assistant modifiers, no percentage/per-diem rows), "
    "professional billing class unless stated otherwise."
)
GHOST_RATE_NOTE = (
    "Ghost rates: payers publish rates for provider-code combinations that are "
    "never actually billed; presence of a rate does not mean a peer performs "
    "that code. Scope the market to a discipline or explicit code list to "
    "mitigate this — the market definition above states which filters were "
    "actually applied."
)
COLLECTION_NOTE = (
    "A published negotiated rate is not proof a peer collects it (contract "
    "vintages, carve-outs, lesser-of clauses). Benchmarks are directional "
    "market positioning, not a guarantee of attainability. A negotiated rate "
    "is also not per-visit revenue: MPPR, assistant-modifier reductions, "
    "sequestration, denials and patient cost-share sit between rate and cash."
)


class BenchmarkError(ValueError):
    pass


def _market_where(market: dict, include_assistant: bool, include_non_dollar: bool) -> tuple[str, list]:
    """Shared WHERE over rates_by_tin joined tin_directory (alias t/td)."""
    clauses, params = ["t.tin_value IS NOT NULL", "NOT t.tin_is_really_npi"], []
    month = market.get("month")
    if not month:
        raise BenchmarkError("an as-of month is required (7A.5) — pass market.month")
    clauses.append("t.file_month = ?")
    params.append(month)
    payers = market.get("payers") or []
    if payers:
        clauses.append(f"t.payer IN ({', '.join('?' for _ in payers)})")
        params += payers
    if not include_non_dollar:
        clauses.append("t.is_dollar_rate")
    if not include_assistant:
        clauses.append("t.modifier_set NOT LIKE '%CQ%' AND t.modifier_set NOT LIKE '%CO%'")
    if market.get("base_only", True):
        if include_assistant:
            # base + assistant: any combination of discipline (GP/GO/GN) and
            # assistant (CQ/CO) modifiers — still excludes KX/59/X{EPSU} rows.
            # Without this, base_only filtered assistant rows straight back
            # out and the include_assistant toggle changed nothing but the
            # methodology note (a false provenance claim on reports).
            clauses.append(
                "(t.modifier_set = '' OR regexp_full_match(t.modifier_set, "
                "'(GP|GO|GN|CQ|CO)(\\|(GP|GO|GN|CQ|CO))*'))")
        else:
            # base = no modifiers at all, OR discipline modifier only
            clauses.append("(t.modifier_set = '' OR t.modifier_set IN ('GP','GO','GN'))")
    if market.get("billing_class", "professional"):
        clauses.append("t.billing_class = ?")
        params.append(market.get("billing_class", "professional"))
    if market.get("discipline"):
        clauses.append("(t.discipline = ? OR t.discipline = 'unspecified')")
        params.append(market["discipline"])
    if market.get("pos"):
        clauses.append("('|' || t.service_code_set || '|') LIKE ('%|' || ? || '|%')")
        params.append(str(market["pos"]))
    if market.get("state"):
        clauses.append("list_contains(td.states, ?)")
        params.append(market["state"].upper())
    if market.get("city"):
        clauses.append(
            "len(list_filter(coalesce(td.cities, []), c -> upper(c) = upper(?))) > 0"
        )
        params.append(market["city"])
    if market.get("codes"):
        codes = market["codes"]
        clauses.append(f"t.billing_code IN ({', '.join('?' for _ in codes)})")
        params += codes
    return " AND ".join(clauses), params


def resolve_subject_tins(store: Store, subject: str) -> list[str]:
    """Subject may be a manually-mapped entity name, an AUTO-grouped org name
    (every TIN whose NPPES organization name is `subject`), or a raw TIN."""
    emap = store.entity_map()
    tins = [t for t, name in emap.items() if name == subject]
    if tins:
        return tins
    # auto-grouped org: the same NPPES display_name the entity grain groups on
    with store.connect() as con:
        named = [r[0] for r in con.execute(
            "SELECT tin_value FROM tin_directory WHERE display_name = ?", [subject],
        ).fetchall()]
    if named:
        return named
    return [subject.replace("-", "").strip()]


def resolve_peer_set(store: Store, market: dict) -> tuple[str, dict]:
    """Returns (description, market-with-curated-tins) for provenance."""
    name = market.get("peer_set")
    if not name:
        return ("auto market (all entities matching the market definition)", market)
    sets = store.peer_sets()
    if name not in sets:
        raise BenchmarkError(f"peer set {name!r} not found")
    defn = sets[name]
    market = dict(market)
    market["curated_tins"] = [str(t).replace("-", "") for t in defn.get("tins", [])]
    return (f"curated peer set {name!r} ({len(market['curated_tins'])} TINs)", market)


def compute_benchmark(store: Store, subject: str, market: dict) -> dict:
    """Per-code subject vs market percentiles (§7B.1)."""
    subject_tins = resolve_subject_tins(store, subject)
    peer_desc, market = resolve_peer_set(store, market)
    include_assistant = bool(market.get("include_assistant", False))
    include_non_dollar = bool(market.get("include_non_dollar", False))
    where, params = _market_where(market, include_assistant, include_non_dollar)

    curated = market.get("curated_tins")
    peer_clause = "AND t.tin_value NOT IN (SELECT tin FROM subject_tins)"
    peer_params: list = []
    if curated:
        peer_clause += f" AND t.tin_value IN ({', '.join('?' for _ in curated)})"
        peer_params = list(curated)

    pct_selects = ", ".join(
        f"round(quantile_cont(rate, {p / 100}), 2) AS p{p}" for p in PERCENTILES
    )
    sql = f"""
    WITH subject_tins AS (SELECT unnest(?::VARCHAR[]) AS tin),
    base AS (
        SELECT t.billing_code, t.tin_value, median(t.negotiated_rate) AS rate
        FROM rates_by_tin t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where}
        GROUP BY t.billing_code, t.tin_value
    ),
    subject AS (
        SELECT billing_code, round(median(rate), 2) AS subject_rate
        FROM base WHERE tin_value IN (SELECT tin FROM subject_tins)
        GROUP BY billing_code
    ),
    peers AS (
        SELECT b.billing_code, b.tin_value, b.rate
        FROM base b, rates_by_tin t
        WHERE t.billing_code = b.billing_code AND t.tin_value = b.tin_value
          {peer_clause}
        GROUP BY ALL
    ),
    market_stats AS (
        SELECT billing_code, count(DISTINCT tin_value) AS n_peers, {pct_selects}
        FROM peers GROUP BY billing_code
    ),
    position AS (
        SELECT p.billing_code,
               round(100.0 * avg(CASE WHEN p.rate <= s.subject_rate THEN 1 ELSE 0 END), 0)
                   AS subject_percentile
        FROM peers p JOIN subject s USING (billing_code)
        GROUP BY p.billing_code
    )
    SELECT m.billing_code, s.subject_rate, m.n_peers,
           m.p10, m.p25, m.p40, m.p50, m.p75, m.p90,
           pos.subject_percentile
    FROM market_stats m
    LEFT JOIN subject s USING (billing_code)
    LEFT JOIN position pos USING (billing_code)
    ORDER BY m.billing_code
    """
    with store.connect() as con:
        cur = con.execute(sql, [subject_tins, *params, *peer_params])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        # median across loaded localities: a national PFS extract carries
        # 100+ localities per code, and any_value() anchored "% of Medicare"
        # to whichever locality DuckDB scanned first (nondeterministic)
        mpfs = dict(con.execute(
            "SELECT code, median(non_facility_rate) FROM mpfs GROUP BY code"
        ).fetchall())
        mpfs_source = store.mpfs_loaded()

    target = int(market.get("target_percentile", 50))
    if target not in PERCENTILES:
        raise BenchmarkError(f"target_percentile must be one of {PERCENTILES}")
    for r in rows:
        desc, _, timed = code_info(r["billing_code"])
        r["description"] = desc
        r["is_timed"] = timed
        t = r.get(f"p{target}")
        r["target_rate"] = t
        r["gap_to_target"] = (
            round(t - r["subject_rate"], 2)
            if t is not None and r["subject_rate"] is not None else None
        )
        if mpfs and r["billing_code"] in mpfs and mpfs[r["billing_code"]]:
            base = mpfs[r["billing_code"]]
            r["subject_pct_medicare"] = (
                round(100 * r["subject_rate"] / base, 0) if r["subject_rate"] else None
            )
            r["median_pct_medicare"] = round(100 * r["p50"] / base, 0) if r["p50"] else None

    return {
        "subject": subject,
        "subject_tins": [mask_tin(t) for t in subject_tins],
        "market": {k: v for k, v in market.items() if k != "curated_tins"},
        "peer_set": peer_desc,
        "target_percentile": target,
        "rows": rows,
        "mpfs_loaded": bool(mpfs),
        "mpfs_source": mpfs_source,
        "basis_note": BASIS_NOTE + (
            " [assistant rows INCLUDED by explicit toggle]" if include_assistant else ""
        ) + (
            " [non-dollar rows INCLUDED by explicit toggle]" if include_non_dollar else ""
        ) + (
            # both escape hatches are API-reachable; the note must never
            # claim a basis the query didn't apply
            " [ALL modifier rows included: market.base_only=false]"
            if market.get("base_only", True) is False else ""
        ) + (
            " [all billing classes included: market.billing_class='']"
            if not market.get("billing_class", "professional") else ""
        ),
    }


def compute_opportunity(benchmark: dict, volumes: dict[str, float],
                        conservative_percentile: int = 40) -> dict:
    """(target-percentile rate − subject rate) × annual units, per code (§7B.3).

    `volumes` is owner-supplied {code: annual_units} — never defaulted.
    """
    if not volumes:
        raise BenchmarkError("opportunity model requires user-supplied annual units per code")
    if conservative_percentile not in PERCENTILES:
        raise BenchmarkError(
            f"conservative_percentile must be one of {PERCENTILES} — an unknown "
            "percentile would silently report a $0 conservative opportunity")
    target = benchmark["target_percentile"]
    if conservative_percentile > target:
        raise BenchmarkError(
            f"conservative_percentile ({conservative_percentile}) must not exceed the "
            f"target percentile ({target}) — the 'conservative' band would be the "
            "LARGER number and the report labels would mislead")
    rows, total_target, total_conservative = [], 0.0, 0.0
    for r in benchmark["rows"]:
        units = volumes.get(r["billing_code"])
        if units is None or r["subject_rate"] is None:
            continue
        t_rate = r.get(f"p{target}")
        c_rate = r.get(f"p{conservative_percentile}")
        gap_t = max(0.0, (t_rate or 0) - r["subject_rate"]) if t_rate is not None else 0.0
        gap_c = max(0.0, (c_rate or 0) - r["subject_rate"]) if c_rate is not None else 0.0
        rows.append({
            "billing_code": r["billing_code"],
            "description": r["description"],
            "annual_units": units,
            "subject_rate": r["subject_rate"],
            "target_rate": t_rate,
            "conservative_rate": c_rate,
            "opportunity_at_target": round(gap_t * units, 2),
            "opportunity_at_conservative": round(gap_c * units, 2),
        })
        total_target += gap_t * units
        total_conservative += gap_c * units
    return {
        "rows": rows,
        "total_at_target": round(total_target, 2),
        "total_at_conservative": round(total_conservative, 2),
        "target_percentile": target,
        "conservative_percentile": conservative_percentile,
        "assumptions": (
            f"Gap computed as (p{target} − subject rate) × owner-supplied annual units, "
            f"floored at $0 per code; conservative band at p{conservative_percentile}. "
            # the basis must echo what the benchmark actually ran with — an
            # unconditional "base-modifier basis" here was false whenever the
            # include_assistant / base_only toggles were used
            f"Rates from the pinned as-of month. {benchmark.get('basis_note', BASIS_NOTE)} "
            "Negotiated rate ≠ collections — MPPR, cost-share, denials and "
            "sequestration sit between rate and cash."
        ),
    }


# ---------------------------------------------------------------------------
# payer-negotiation one-pager (§7B.4)
# ---------------------------------------------------------------------------


def subject_payers(store: Store, subject: str, market: dict) -> list[str]:
    """Distinct payers the subject actually has published rates with, scoped to
    the market's as-of month (and payer-scope, if the caller pre-narrowed it).

    A negotiation one-pager is per-payer, so this is the list of tables to
    build — computed from the subject's own rows, not from every payer in the
    store (a payer the subject doesn't contract with has nothing to negotiate).
    """
    subject_tins = resolve_subject_tins(store, subject)
    if not subject_tins:
        return []
    month = market.get("month")
    if not month:
        raise BenchmarkError("an as-of month is required (7A.5) — pass market.month")
    clauses = ["tin_value IN (SELECT unnest(?::VARCHAR[]))", "file_month = ?",
               "payer IS NOT NULL"]
    params: list = [subject_tins, month]
    scope = market.get("payers") or []
    if scope:
        clauses.append(f"payer IN ({', '.join('?' for _ in scope)})")
        params += scope
    with store.connect() as con:
        rows = con.execute(
            f"SELECT DISTINCT payer FROM rates_by_tin WHERE {' AND '.join(clauses)} "
            "ORDER BY payer",
            params,
        ).fetchall()
    return [r[0] for r in rows if r[0]]


def compute_payer_negotiation(store: Store, subject: str, market: dict,
                              volumes: dict[str, float] | None = None,
                              conservative_percentile: int = 40) -> dict:
    """Per-payer negotiation view (§7B.4).

    For each payer the subject contracts with, benchmark the subject against
    *that payer's other providers only* (intra-payer peer comparison — the
    number that matters at a contract renewal is "what is THIS payer paying my
    peers", not a blended cross-payer market). Optionally attach the annual
    opportunity per payer when the caller supplies volumes.
    """
    payers = subject_payers(store, subject, market)
    if not payers:
        raise BenchmarkError(
            "subject has no rates in the pinned month for the given market scope "
            "— nothing to build a per-payer negotiation view from")
    volumes = {str(k): float(v) for k, v in (volumes or {}).items()} or None
    sections, total_target, total_conservative = [], 0.0, 0.0
    covered_payers = 0
    for payer in payers:
        pmarket = {**market, "payers": [payer]}
        bench = compute_benchmark(store, subject, pmarket)
        # only codes where the subject actually has a rate with this payer AND
        # a peer set exists — an all-null section is noise on a one-pager
        priced = [r for r in bench["rows"] if r.get("subject_rate") is not None]
        if not priced:
            continue
        covered_payers += 1
        opp = None
        if volumes:
            try:
                opp = compute_opportunity(bench, volumes, conservative_percentile)
                total_target += opp["total_at_target"]
                total_conservative += opp["total_at_conservative"]
            except BenchmarkError:
                opp = None  # e.g. no overlapping code — skip this payer's dollars
        # headline percentile: median of the per-code positions we have
        pcts = [r["subject_percentile"] for r in priced
                if r.get("subject_percentile") is not None]
        headline_pct = round(sum(pcts) / len(pcts)) if pcts else None
        sections.append({
            "payer": payer,
            "benchmark": bench,
            "opportunity": opp,
            "n_codes": len(priced),
            "headline_percentile": headline_pct,
        })
    if not sections:
        raise BenchmarkError(
            "no payer has a benchmarkable code for this subject in the pinned "
            "month (subject-priced codes with at least one peer)")
    # lowest-percentile payer first: that's where the subject is most underpaid
    # relative to peers, i.e. the strongest renegotiation case
    sections.sort(key=lambda s: (s["headline_percentile"] is None,
                                 s["headline_percentile"] if s["headline_percentile"] is not None else 999))
    return {
        "subject": subject,
        "subject_tins": sections[0]["benchmark"]["subject_tins"],
        "market": {k: v for k, v in market.items() if k != "payers"},
        "payers": [s["payer"] for s in sections],
        "sections": sections,
        "has_volumes": bool(volumes),
        "total_at_target": round(total_target, 2) if volumes else None,
        "total_at_conservative": round(total_conservative, 2) if volumes else None,
        "target_percentile": sections[0]["benchmark"]["target_percentile"],
        "conservative_percentile": conservative_percentile if volumes else None,
    }


def render_negotiation_report(cfg: MrfxConfig, store: Store, neg: dict) -> str:
    """Print-ready per-payer negotiation one-pager. One benchmark table per
    payer the subject contracts with, ordered weakest-position-first, with a
    cross-payer opportunity summary when volumes were supplied."""
    if not neg.get("market", {}).get("month"):
        raise BenchmarkError("negotiation report requires a pinned as-of month")
    e = html.escape
    target = neg["target_percentile"]
    subject = neg["subject"]
    month = neg["market"].get("month")

    summary = ""
    if neg.get("has_volumes"):
        summary = (
            f'<p class="band">Across {len(neg["sections"])} payer(s), lifting every '
            f'contract to p{target} is worth <b>{_m(neg["total_at_target"])}</b>/yr '
            f'(conservative p{neg["conservative_percentile"]}: '
            f'<b>{_m(neg["total_at_conservative"])}</b>).</p>'
        )

    def payer_section(sec) -> str:
        bench = sec["benchmark"]
        payer = sec["payer"]
        hp = sec.get("headline_percentile")
        hp_txt = (f"You sit at roughly <b>p{hp:.0f}</b> among this payer's other "
                  f"providers." if hp is not None else "")
        rows_html = "".join(
            f"<tr><td>{e(r['billing_code'])}<div class='sub'>{e(r['description'] or '')}</div></td>"
            f"<td class='num'>{_m(r['subject_rate'])}</td>"
            f"<td class='num'>{_m(r['p25'])}</td><td class='num'>{_m(r['p50'])}</td>"
            f"<td class='num'>{_m(r['p75'])}</td>"
            f"<td class='num'>{_m(r['target_rate'])}</td>"
            f"<td class='num gap'>{_m(r['gap_to_target'])}</td>"
            f"<td class='num sub'>{r['n_peers'] or 0}</td></tr>"
            for r in bench["rows"] if r.get("subject_rate") is not None
        )
        opp = sec.get("opportunity")
        opp_line = ""
        if opp:
            opp_line = (
                f'<p class="note">Annual opportunity with {e(payer)}: '
                f'<b>{_m(opp["total_at_target"])}</b> at p{target} '
                f'(conservative {_m(opp["total_at_conservative"])}).</p>'
            )
        return f"""
        <h2>{e(payer)}</h2>
        <p class="meta">{hp_txt} {sec['n_codes']} benchmarked code(s), as of {e(str(month))}.</p>
        <table><thead><tr><th>Code</th><th class="num">Your rate</th>
        <th class="num">P25</th><th class="num">Median</th><th class="num">P75</th>
        <th class="num">Target (p{target})</th><th class="num">Gap</th>
        <th class="num">Peers</th></tr></thead>
        <tbody>{rows_html}</tbody></table>
        {opp_line}
        """

    body_sections = "".join(payer_section(s) for s in neg["sections"])
    # methodology: reuse the footer of the weakest-position payer's benchmark,
    # but scope the payer list to every payer in the report
    foot_bench = dict(neg["sections"][0]["benchmark"])
    foot_bench["market"] = {**foot_bench["market"], "payers": neg["payers"]}
    footer = methodology_footer(store, foot_bench)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Payer negotiation one-pager — {e(subject)}</title>
<style>
 body {{ font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #0b0b0b;
        max-width: 900px; margin: 32px auto; padding: 0 24px; }}
 h1 {{ font-size: 20px; margin-bottom: 2px; }} h2 {{ font-size: 15px; margin-top: 30px;
        border-bottom: 2px solid #0b0b0b; padding-bottom: 3px; }}
 .brand {{ color: #52514e; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }}
 .brandbar {{ display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }}
 .brandbar .brand {{ margin: 0; }} .logo {{ max-height: 40px; max-width: 200px; }}
 .meta {{ color: #52514e; margin-bottom: 10px; }}
 table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
 th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #e1e0d9; vertical-align: middle; }}
 th {{ font-size: 11px; color: #898781; text-transform: uppercase; letter-spacing: .04em; }}
 .num {{ text-align: right; }} .sub {{ color: #898781; font-size: 11.5px; }}
 .gap {{ font-weight: 650; }}
 .band {{ font-size: 14px; background: #f4f3ee; padding: 10px 14px; border-radius: 6px; }}
 .note {{ color: #52514e; font-size: 12px; }}
 footer {{ margin-top: 36px; border-top: 1px solid #c3c2b7; padding-top: 12px;
          color: #52514e; font-size: 11px; white-space: pre-wrap; }}
 @media print {{ body {{ margin: 0; }} h2 {{ break-after: avoid; }} }}
</style></head><body>
{_brand_header(cfg)}
<h1>Payer negotiation one-pager — {e(subject)}</h1>
<div class="meta">Where each payer pays you versus the peers it pays, as of {e(str(month))}.
 Ordered weakest position first.</div>
{summary}
{body_sections}
<footer>METHODOLOGY\n{e(footer)}</footer>
</body></html>"""


# ---------------------------------------------------------------------------
# pitch report (§7B.5)
# ---------------------------------------------------------------------------


def methodology_footer(store: Store, benchmark: dict) -> str:
    market = benchmark["market"]
    with store.connect() as con:
        # scope the source list to the market's payers when the market is
        # payer-scoped — a single-payer report must not claim every payer's
        # files as its sources
        payers = market.get("payers") or []
        q = ("SELECT filename, payer, last_updated_on FROM files WHERE status = 'done' "
             "AND file_type = 'in_network' ")
        if payers:
            q += f"AND payer IN ({', '.join('?' for _ in payers)}) "
        files = con.execute(q + "ORDER BY filename", payers).fetchall()
    lines = [
        f"Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by MRF Explorer v{__version__}.",
        f"As-of month: {market.get('month')}. Peer basis: {benchmark['peer_set']}.",
        f"Market definition: {json.dumps({k: v for k, v in market.items() if v not in (None, [], '')})}.",
        benchmark["basis_note"],
        "Dedup rule: one row per (payer, TIN, code, modifier-set, billing class, "
        "place-of-service set, month); a TIN's rate is the median of its distinct "
        "published values (variants flagged).",
        "Rate files in the store matching the market's payer scope (the "
        "benchmark draws on the subset matching the full market definition): "
        f"{'; '.join(f'{f[0]} ({f[1]}, {f[2]})' for f in files) or 'none'}.",
        GHOST_RATE_NOTE,
        COLLECTION_NOTE,
    ]
    if benchmark.get("mpfs_loaded"):
        lines.append(
            "% of Medicare anchor: the MEDIAN non-facility rate across all "
            f"localities in the loaded MPFS extract ({benchmark.get('mpfs_source')}) — "
            "not your specific locality's fee schedule.")
    return "\n".join(lines)


def render_pitch_report(cfg: MrfxConfig, store: Store, benchmark: dict,
                        opportunity: dict | None = None) -> str:
    """Print-ready standalone HTML. Refuses to render without an as-of month
    or methodology footer (§8.10 — they are generated here, unconditionally)."""
    if not benchmark.get("market", {}).get("month"):
        raise BenchmarkError("pitch report requires a pinned as-of month")
    footer = methodology_footer(store, benchmark)
    e = html.escape
    target = benchmark["target_percentile"]

    def strip(r) -> str:
        pct = r.get("subject_percentile")
        if pct is None:
            return ""
        return (
            f'<div class="strip"><div class="you" style="left:{min(max(pct, 0), 100)}%"></div></div>'
            f'<span class="pctlbl">P{pct:.0f}</span>'
        )

    mp = benchmark.get("mpfs_loaded")
    rows_html = "".join(
        f"<tr><td>{e(r['billing_code'])}<div class='sub'>{e(r['description'] or '')}"
        f"{' · timed 15-min' if r['is_timed'] else ''}</div></td>"
        f"<td class='num'>{_m(r['subject_rate'])}</td>"
        f"<td class='num'>{_m(r['p25'])}</td><td class='num'>{_m(r['p50'])}</td>"
        f"<td class='num'>{_m(r['p75'])}</td>"
        f"<td class='num'>{_m(r['target_rate'])}</td>"
        f"<td class='num gap'>{_m(r['gap_to_target'])}</td>"
        + (f"<td class='num'>{_pctnum(r.get('subject_pct_medicare'))}</td>"
           f"<td class='num'>{_pctnum(r.get('median_pct_medicare'))}</td>" if mp else "")
        + f"<td class='pos'>{strip(r)}</td></tr>"
        for r in benchmark["rows"]
    )
    opp_html = ""
    if opportunity:
        opp_rows = "".join(
            f"<tr><td>{e(o['billing_code'])}</td><td class='num'>{o['annual_units']:,.0f}</td>"
            f"<td class='num'>{_m(o['subject_rate'])}</td><td class='num'>{_m(o['target_rate'])}</td>"
            f"<td class='num'>{_m(o['opportunity_at_conservative'])}</td>"
            f"<td class='num'>{_m(o['opportunity_at_target'])}</td></tr>"
            for o in opportunity["rows"]
        )
        opp_html = f"""
        <h2>Annual gross opportunity</h2>
        <p class="band">Conservative (p{opportunity['conservative_percentile']}):
           <b>{_m(opportunity['total_at_conservative'])}</b> &nbsp;·&nbsp;
           At target (p{opportunity['target_percentile']}):
           <b>{_m(opportunity['total_at_target'])}</b> per year</p>
        <table><thead><tr><th>Code</th><th class="num">Annual units</th>
        <th class="num">Your rate</th><th class="num">Target</th>
        <th class="num">Opportunity (conservative)</th><th class="num">Opportunity (target)</th></tr></thead>
        <tbody>{opp_rows}</tbody></table>
        <p class="note">{e(opportunity['assumptions'])}</p>
        """

    mp_heads = "<th class='num'>% Medicare (you)</th><th class='num'>% Medicare (median)</th>" if mp else ""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Rate benchmark — {e(benchmark['subject'])}</title>
<style>
 body {{ font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #0b0b0b;
        max-width: 900px; margin: 32px auto; padding: 0 24px; }}
 h1 {{ font-size: 20px; margin-bottom: 2px; }} h2 {{ font-size: 15px; margin-top: 28px; }}
 .brand {{ color: #52514e; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }}
 .brandbar {{ display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }}
 .brandbar .brand {{ margin: 0; }} .logo {{ max-height: 40px; max-width: 200px; }}
 .meta {{ color: #52514e; margin-bottom: 18px; }}
 table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
 th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #e1e0d9; vertical-align: middle; }}
 th {{ font-size: 11px; color: #898781; text-transform: uppercase; letter-spacing: .04em; }}
 .num {{ text-align: right; }} .sub {{ color: #898781; font-size: 11.5px; }}
 .gap {{ font-weight: 650; }}
 .strip {{ position: relative; width: 120px; height: 8px; background: #eee;
          border-radius: 4px; display: inline-block; vertical-align: middle; }}
 .you {{ position: absolute; top: -3px; width: 3px; height: 14px; background: #2a78d6; border-radius: 2px; }}
 .pctlbl {{ font-size: 11px; color: #52514e; margin-left: 6px; }}
 .band {{ font-size: 14px; }}
 .note {{ color: #52514e; font-size: 12px; }}
 footer {{ margin-top: 36px; border-top: 1px solid #c3c2b7; padding-top: 12px;
          color: #52514e; font-size: 11px; white-space: pre-wrap; }}
 @media print {{ body {{ margin: 0; }} }}
</style></head><body>
{_brand_header(cfg)}
<h1>Negotiated-rate benchmark — {e(benchmark['subject'])}</h1>
<div class="meta">As of {e(str(benchmark['market'].get('month')))} ·
 {e(benchmark['peer_set'])} · target: p{target}</div>
<table><thead><tr><th>Code</th><th class="num">Your rate</th><th class="num">P25</th>
<th class="num">Median</th><th class="num">P75</th><th class="num">Target (p{target})</th>
<th class="num">Gap</th>{mp_heads}<th>Your position</th></tr></thead>
<tbody>{rows_html}</tbody></table>
{opp_html}
<footer>METHODOLOGY\n{e(footer)}</footer>
</body></html>"""


def _brand_header(cfg: MrfxConfig) -> str:
    """Report-as-a-service header: the consultant's brand name plus, when a
    readable ``report_branding.logo_path`` is configured, their logo inlined as
    a data URI (reports must be self-contained single files — no external
    fetches when a client opens the HTML offline). A missing or unreadable logo
    is cosmetic and never fails the report (invariant 3)."""
    e = html.escape
    name = e(cfg.report_branding.name)
    logo = cfg.report_branding.logo_path
    if logo:
        try:
            import base64
            import mimetypes
            data = logo.read_bytes()
            mime = mimetypes.guess_type(str(logo))[0] or "image/png"
            b64 = base64.b64encode(data).decode("ascii")
            return (f'<div class="brandbar"><img class="logo" alt="{name}" '
                    f'src="data:{mime};base64,{b64}"><span class="brand">{name}</span></div>')
        except Exception as exc:  # noqa: BLE001 — logo is decoration, never blocks a report
            log.warning("report logo %s could not be embedded (%s); using text brand", logo, exc)
    return f'<div class="brand">{name}</div>'


def _m(v) -> str:
    return f"${v:,.2f}" if v is not None else "–"


def _pctnum(v) -> str:
    return f"{v:.0f}%" if v is not None else "–"
