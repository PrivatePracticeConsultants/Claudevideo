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
    "that code. Benchmarks are computed only within the discipline-scoped code "
    "set to mitigate this."
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
    """Subject may be a mapped entity name or a raw TIN."""
    emap = store.entity_map()
    tins = [t for t, name in emap.items() if name == subject]
    if tins:
        return tins
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
        mpfs = dict(con.execute(
            "SELECT code, any_value(non_facility_rate) FROM mpfs GROUP BY code"
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
        ),
    }


def compute_opportunity(benchmark: dict, volumes: dict[str, float],
                        conservative_percentile: int = 40) -> dict:
    """(target-percentile rate − subject rate) × annual units, per code (§7B.3).

    `volumes` is owner-supplied {code: annual_units} — never defaulted.
    """
    if not volumes:
        raise BenchmarkError("opportunity model requires user-supplied annual units per code")
    target = benchmark["target_percentile"]
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
            "Rates from the pinned as-of month, base-modifier basis. "
            "Negotiated rate ≠ collections — MPPR, cost-share, denials and "
            "sequestration sit between rate and cash."
        ),
    }


# ---------------------------------------------------------------------------
# pitch report (§7B.5)
# ---------------------------------------------------------------------------


def methodology_footer(store: Store, benchmark: dict) -> str:
    market = benchmark["market"]
    with store.connect() as con:
        files = con.execute(
            "SELECT filename, payer, last_updated_on FROM files WHERE status = 'done' "
            "AND file_type = 'in_network' ORDER BY filename"
        ).fetchall()
    lines = [
        f"Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by MRF Explorer v{__version__}.",
        f"As-of month: {market.get('month')}. Peer basis: {benchmark['peer_set']}.",
        f"Market definition: {json.dumps({k: v for k, v in market.items() if v not in (None, [], '')})}.",
        benchmark["basis_note"],
        "Dedup rule: one row per (payer, TIN, code, modifier-set, billing class, "
        "place-of-service set, month); a TIN's rate is the median of its distinct "
        "published values (variants flagged).",
        f"Source files: {'; '.join(f'{f[0]} ({f[1]}, {f[2]})' for f in files) or 'none'}.",
        GHOST_RATE_NOTE,
        COLLECTION_NOTE,
    ]
    if benchmark.get("mpfs_loaded"):
        lines.append(f"% of Medicare uses the loaded MPFS extract: {benchmark.get('mpfs_source')}.")
    return "\n".join(lines)


def render_pitch_report(cfg: MrfxConfig, store: Store, benchmark: dict,
                        opportunity: dict | None = None) -> str:
    """Print-ready standalone HTML. Refuses to render without an as-of month
    or methodology footer (§8.10 — they are generated here, unconditionally)."""
    if not benchmark.get("market", {}).get("month"):
        raise BenchmarkError("pitch report requires a pinned as-of month")
    footer = methodology_footer(store, benchmark)
    e = html.escape
    brand = cfg.report_branding.name
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
<div class="brand">{e(brand)}</div>
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


def _m(v) -> str:
    return f"${v:,.2f}" if v is not None else "–"


def _pctnum(v) -> str:
    return f"{v:.0f}%" if v is not None else "–"
