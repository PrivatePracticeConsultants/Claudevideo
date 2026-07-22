"""Fee-schedule reconstruction + payer scorecard (§7C).

Two subject-centric, zero-assumption deliverables for a single practice:

- **Fee schedule ("your rate card")** — every code the practice has a
  negotiated rate for, laid out by payer. The MRF *is* the fee schedule; most
  practices have never seen their own written down.
- **Payer scorecard ("who pays best")** — the practice's payers ranked by how
  generously they pay, normalized so the comparison is fair: median % of
  Medicare when an MPFS anchor is loaded (absolute, comparable across payers),
  and median % of the best payer over head-to-head codes otherwise (needs no
  Medicare data at all).

Both run over the SAME base-modifier, dollar-rate, professional-class rows the
benchmark uses (shared `_market_where`), so the numbers agree across features.
No volumes, no collections, no invented data — only what the payer published.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import statistics

from . import __version__
from .benchmark import (
    BASIS_NOTE,
    COLLECTION_NOTE,
    GHOST_RATE_NOTE,
    BenchmarkError,
    _brand_header,
    _m,
    _market_where,
    _pctnum,
    _rates_relation,
    normalize_market,
    month_label,
    resolve_subject_tins,
)
from .catalog import code_info
from .config import MrfxConfig
from .store import Store, defuse_csv, mask_tin


def compute_fee_schedule(store: Store, subject: str, market: dict) -> dict:
    """The subject's negotiated rate for every code, by payer (§7C.1).

    Rate for a (payer, code) = median across the subject's TINs of each TIN's
    median published rate — the same entity-grain rule the rest of the app uses.
    """
    market = normalize_market(market)
    subject_tins = resolve_subject_tins(store, subject)
    include_assistant = bool(market.get("include_assistant", False))
    include_non_dollar = bool(market.get("include_non_dollar", False))
    where, params = _market_where(market, include_assistant, include_non_dollar)
    rel = _rates_relation(market)

    sql = f"""
    WITH subject_tins AS (SELECT unnest(?::VARCHAR[]) AS tin),
    per_tin AS (
        SELECT t.payer, t.billing_code, t.tin_value,
               median(t.negotiated_rate) AS rate
        FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
        WHERE {where} AND t.is_dollar_rate
          AND t.tin_value IN (SELECT tin FROM subject_tins)
        GROUP BY t.payer, t.billing_code, t.tin_value
    )
    SELECT payer, billing_code,
           round(median(rate), 2)     AS rate,
           count(DISTINCT tin_value)  AS n_tins
    FROM per_tin
    GROUP BY payer, billing_code
    ORDER BY billing_code, payer
    """
    with store.connect() as con:
        cur = con.execute(sql, [subject_tins, *params])
        raw = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        mpfs = dict(con.execute(
            "SELECT code, median(non_facility_rate) FROM mpfs GROUP BY code"
        ).fetchall())
        mpfs_source = store.mpfs_loaded()

        # For each (payer, code) the subject is priced on, what does THAT payer
        # pay the subject's PEERS for the same code — the median across every
        # other practice that payer prices, on the same basis. This is the
        # negotiation hook per cell ("Aetna pays you $45; its median for your
        # peers is $52 — you're 13% under"). Scoped to the subject's own payers
        # and codes so the billing_code clustering prunes and we don't scan the
        # whole book; peers exclude the subject's TINs (self-exclusion matches
        # the benchmark's position metric).
        subj_payers = sorted({r["payer"] for r in raw})
        subj_codes = sorted({r["billing_code"] for r in raw})
        market_by: dict[tuple, dict] = {}
        if subj_payers and subj_codes:
            msql = f"""
            WITH subject_tins AS (SELECT unnest(?::VARCHAR[]) AS tin),
            per_tin AS (
                SELECT t.payer, t.billing_code, t.tin_value,
                       median(t.negotiated_rate) AS rate
                FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
                WHERE {where} AND t.is_dollar_rate
                  AND t.payer IN ({', '.join('?' for _ in subj_payers)})
                  AND t.billing_code IN ({', '.join('?' for _ in subj_codes)})
                  AND t.tin_value NOT IN (SELECT tin FROM subject_tins)
                GROUP BY t.payer, t.billing_code, t.tin_value
            )
            SELECT payer, billing_code,
                   round(median(rate), 2)     AS market_median,
                   count(DISTINCT tin_value)  AS n_peers
            FROM per_tin GROUP BY payer, billing_code
            """
            mcur = con.execute(msql, [subject_tins, *params, *subj_payers, *subj_codes])
            for row in mcur.fetchall():
                d = dict(zip([c[0] for c in mcur.description], row))
                market_by[(d["payer"], d["billing_code"])] = d

    payers = sorted({r["payer"] for r in raw})
    by_code: dict[str, dict] = {}
    for r in raw:
        code = r["billing_code"]
        entry = by_code.get(code)
        if entry is None:
            desc, _, timed = code_info(code)
            entry = {
                "billing_code": code, "description": desc, "is_timed": timed,
                "rates": {},  # payer -> {rate, pct_medicare, market_median, n_peers, vs_market_pct}
            }
            by_code[code] = entry
        pct = None
        base = mpfs.get(code)
        if base and r["rate"]:
            pct = round(100 * r["rate"] / base, 0)
        mk = market_by.get((r["payer"], code)) or {}
        mkt_med, n_peers = mk.get("market_median"), mk.get("n_peers") or 0
        # signed % the subject sits above (+) or below (-) what this payer pays
        # peers; None when there are no peers (only the subject prices it here)
        vs = (round(100 * (r["rate"] - mkt_med) / mkt_med)
              if r["rate"] and mkt_med else None)
        entry["rates"][r["payer"]] = {
            "rate": r["rate"], "pct_medicare": pct,
            "market_median": mkt_med, "n_peers": n_peers, "vs_market_pct": vs,
        }

    codes = sorted(by_code.values(), key=lambda e: e["billing_code"])
    return {
        "subject": subject,
        "subject_tins": [mask_tin(t) for t in subject_tins],
        "market": {k: v for k, v in market.items() if v not in (None, [], "")},
        "month": market.get("month"),
        "payers": payers,
        "codes": codes,
        "mpfs_loaded": bool(mpfs),
        "mpfs_source": mpfs_source,
        "basis_note": BASIS_NOTE,
    }


def payer_scorecard(fee_schedule: dict) -> dict:
    """Rank the subject's payers by how well they pay (§7C.2).

    Derived from an already-computed fee schedule (mirrors the
    benchmark→opportunity split). Two normalized metrics:
      - median % of Medicare  (when MPFS is loaded — absolute, comparable)
      - median % of the best payer over HEAD-TO-HEAD codes (codes ≥2 payers
        priced) — needs no Medicare data, but only ranks payers that actually
        compete on shared codes.
    Primary rank uses % of Medicare if available, else % of best.
    """
    codes = fee_schedule["codes"]
    # ONE ranking scale for the whole scorecard: % of Medicare when the anchor
    # is loaded (absolute, ~100-200%), else % of best (capped at 100%). Mixing
    # them — ranking a Medicare-anchored payer against a %-of-best fallback
    # payer — compares different scales and mis-orders them.
    use_medicare = bool(fee_schedule.get("mpfs_loaded"))
    # best rate per code and how many payers priced it (for head-to-head)
    best_rate: dict[str, float] = {}
    n_payers_for_code: dict[str, int] = {}
    for e in codes:
        priced = {p: v["rate"] for p, v in e["rates"].items() if v["rate"] is not None}
        if priced:
            best_rate[e["billing_code"]] = max(priced.values())
            n_payers_for_code[e["billing_code"]] = len(priced)

    rows = []
    for payer in fee_schedule["payers"]:
        rates, pcts_med, pcts_best = [], [], []
        for e in codes:
            v = e["rates"].get(payer)
            if not v or v["rate"] is None:
                continue
            code = e["billing_code"]
            rates.append(v["rate"])
            if v["pct_medicare"] is not None:
                pcts_med.append(v["pct_medicare"])
            if n_payers_for_code.get(code, 0) >= 2 and best_rate.get(code):
                pcts_best.append(100 * v["rate"] / best_rate[code])
        if not rates:
            continue
        med_medicare = round(statistics.median(pcts_med), 0) if pcts_med else None
        med_best = round(statistics.median(pcts_best), 0) if pcts_best else None
        rows.append({
            "payer": payer,
            "n_codes": len(rates),
            "n_comparable": len(pcts_best),
            "median_rate": round(statistics.median(rates), 2),
            "median_pct_medicare": med_medicare,
            "median_pct_of_best": med_best,
            # one scale for everyone (see use_medicare): a payer that lacks the
            # chosen metric is unranked, never cross-scale-compared
            "rank_metric": med_medicare if use_medicare else med_best,
        })

    # payers with a rank metric first (best → worst); the rest by raw median rate
    ranked = sorted(
        [r for r in rows if r["rank_metric"] is not None],
        key=lambda r: r["rank_metric"], reverse=True,
    )
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    unranked = sorted(
        [r for r in rows if r["rank_metric"] is None],
        key=lambda r: r["median_rate"], reverse=True,
    )
    for r in unranked:
        r["rank"] = None
    return {
        "metric": "pct_medicare" if fee_schedule.get("mpfs_loaded") else "pct_of_best",
        "rows": ranked + unranked,
        "best_payer": ranked[0]["payer"] if ranked else None,
        "worst_payer": ranked[-1]["payer"] if ranked else None,
    }


# ---------------------------------------------------------------------------
# rate-card report (§7C.3)
# ---------------------------------------------------------------------------


def _methodology(store: Store, fee_schedule: dict) -> str:
    market = fee_schedule["market"]
    payers = fee_schedule.get("payers") or []
    if payers:
        with store.connect() as con:
            files = con.execute(
                "SELECT filename, payer, last_updated_on FROM files "
                "WHERE status = 'done' AND file_type = 'in_network' "
                f"AND payer IN ({', '.join('?' for _ in payers)}) "
                "ORDER BY filename", payers).fetchall()
    else:
        # an empty fee schedule matched no payers — listing EVERY ingested file
        # as its "source" would over-claim provenance for a report with no rows
        files = []
    lines = [
        f"Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by MRF Explorer v{__version__}.",
        f"As-of month: {month_label(market.get('month'))}. Subject tax IDs: {', '.join(fee_schedule['subject_tins']) or 'n/a'}.",
        f"Market definition: {json.dumps({k: v for k, v in market.items()})}.",
        fee_schedule["basis_note"],
        "Rate for a (payer, code) is the median across the subject's tax IDs of "
        "each tax ID's median published value (variants collapse per the app's "
        "dedup rule).",
        "Peer comparison ('peer $X · ±Y% vs peers'): the median rate THIS payer "
        "pays every OTHER practice for the same code (subject excluded), on the "
        "same basis and market scope; +Y% means the subject is paid above that "
        "peer median, -Y% below it. A published rate is not proof of collection.",
        "Payer scorecard: payers are ranked by median % of Medicare when an MPFS "
        "anchor is loaded (absolute), otherwise by the median ratio of this "
        "payer's rate to the BEST payer's rate over codes at least two payers "
        "priced (a payer with no head-to-head codes is listed but unranked).",
        "Source files: " + ("; ".join(f"{f[0]} ({f[1]}, {f[2]})" for f in files) or "none") + ".",
        GHOST_RATE_NOTE,
        COLLECTION_NOTE,
    ]
    if fee_schedule.get("mpfs_loaded"):
        lines.append(
            "% of Medicare anchor: the MEDIAN non-facility rate across all "
            f"localities in the loaded MPFS extract ({fee_schedule.get('mpfs_source')}) — "
            "not your specific locality's fee schedule.")
    return "\n".join(lines)


def render_rate_card(cfg: MrfxConfig, store: Store, fee_schedule: dict,
                     scorecard: dict) -> str:
    """Print-ready branded rate card: payer scorecard on top, full fee-schedule
    matrix below (payer columns ordered best→worst). Refuses without a month."""
    if not fee_schedule.get("market", {}).get("month"):
        raise BenchmarkError("rate card requires a pinned as-of month")
    e = html.escape
    month = fee_schedule["market"].get("month")
    mp = fee_schedule.get("mpfs_loaded")
    # order payer columns by scorecard rank (unranked payers keep their place)
    order = {r["payer"]: i for i, r in enumerate(scorecard["rows"])}
    payers = sorted(fee_schedule["payers"], key=lambda p: order.get(p, 1e9))

    def sc_row(r) -> str:
        rank = f"{r['rank']}" if r.get("rank") else "–"
        best = " class='best'" if r.get("rank") == 1 else ""
        return (
            f"<tr{best}><td class='num'>{rank}</td><td>{e(r['payer'])}</td>"
            f"<td class='num'>{r['n_codes']}</td>"
            + (f"<td class='num'>{_pctnum(r['median_pct_medicare'])}</td>" if mp else "")
            + f"<td class='num'>{_pctnum(r['median_pct_of_best'])}</td>"
            f"<td class='num'>{_m(r['median_rate'])}</td></tr>"
        )

    sc_head = ("<th class='num'>Rank</th><th>Payer</th><th class='num'>Codes</th>"
               + ("<th class='num'>Median % Medicare</th>" if mp else "")
               + "<th class='num'>Median % of best</th><th class='num'>Median rate</th>")
    sc_html = "".join(sc_row(r) for r in scorecard["rows"])

    def fee_row(entry) -> str:
        best = max((v["rate"] for v in entry["rates"].values() if v["rate"] is not None),
                   default=None)
        cells = ""
        for p in payers:
            v = entry["rates"].get(p)
            if not v or v["rate"] is None:
                cells += "<td class='num'>–</td>"
                continue
            top = " class='num top'" if best is not None and v["rate"] == best else " class='num'"
            sub = (f"<div class='sub'>{_pctnum(v['pct_medicare'])} MC</div>"
                   if mp and v["pct_medicare"] is not None else "")
            # peer comparison: what this payer pays your peers for this code
            if v.get("market_median") is not None:
                vs = v.get("vs_market_pct")
                if vs is None:
                    vs_span = ""
                elif vs == 0:
                    vs_span = " · even w/ peers"
                else:
                    cls = "up" if vs > 0 else "down"
                    vs_span = f" · <span class='{cls}'>{'+' if vs > 0 else ''}{vs}% vs peers</span>"
                sub += f"<div class='sub'>peer {_m(v['market_median'])}{vs_span}</div>"
            cells += f"<td{top}>{_m(v['rate'])}{sub}</td>"
        return (f"<tr><td>{e(entry['billing_code'])}"
                f"<div class='sub'>{e(entry['description'] or '')}"
                f"{' · timed 15-min' if entry['is_timed'] else ''}</div></td>{cells}</tr>")

    fee_head = "<th>Code</th>" + "".join(f"<th class='num'>{e(p)}</th>" for p in payers)
    fee_html = "".join(fee_row(entry) for entry in fee_schedule["codes"])
    footer = _methodology(store, fee_schedule)
    best_line = (f"Best-paying payer: <b>{e(scorecard['best_payer'])}</b>."
                 if scorecard.get("best_payer") else "")

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Rate card — {e(fee_schedule['subject'])}</title>
<style>
 body {{ font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #0b0b0b;
        max-width: 1100px; margin: 32px auto; padding: 0 24px; }}
 h1 {{ font-size: 20px; margin-bottom: 2px; }} h2 {{ font-size: 15px; margin-top: 28px; }}
 .brand {{ color: #52514e; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }}
 .brandbar {{ display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }}
 .brandbar .brand {{ margin: 0; }} .logo {{ max-height: 40px; max-width: 200px; }}
 .meta {{ color: #52514e; margin-bottom: 12px; }}
 .wrap {{ overflow-x: auto; }}
 table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
 th, td {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid #e1e0d9; vertical-align: top; white-space: nowrap; }}
 th {{ font-size: 11px; color: #898781; text-transform: uppercase; letter-spacing: .04em; }}
 .num {{ text-align: right; }} .sub {{ color: #898781; font-size: 11px; font-weight: 400; }}
 tr.best {{ background: #eef6ee; }} td.top {{ font-weight: 700; color: #1c6b3a; }}
 .sub .up {{ color: #1c6b3a; font-weight: 600; }} .sub .down {{ color: #b3261e; font-weight: 600; }}
 footer {{ margin-top: 32px; border-top: 1px solid #c3c2b7; padding-top: 12px;
          color: #52514e; font-size: 11px; white-space: pre-wrap; }}
 @media print {{ body {{ margin: 0; }} }}
</style></head><body>
{_brand_header(cfg)}
<h1>Rate card — {e(fee_schedule['subject'])}</h1>
<div class="meta">Your negotiated rates by payer, as of {e(month_label(month))}. {best_line}</div>
<h2>Payer scorecard — who pays best</h2>
<div class="wrap"><table><thead><tr>{sc_head}</tr></thead><tbody>{sc_html}</tbody></table></div>
<h2>Fee schedule</h2>
<div class="wrap"><table><thead><tr>{fee_head}</tr></thead><tbody>{fee_html}</tbody></table></div>
<footer>METHODOLOGY\n{e(footer)}</footer>
</body></html>"""


def fee_schedule_csv(fee_schedule: dict, scorecard: dict, store: Store) -> str:
    """Long-format CSV (payer, code, rate, % Medicare) with a methodology header
    block as leading comment lines (honesty invariant: exports carry their
    methodology)."""
    import csv
    import io
    out = io.StringIO()
    for line in _methodology(store, fee_schedule).splitlines():
        out.write(f"# {line}\n")
    w = csv.writer(out)
    w.writerow(["payer", "scorecard_rank", "billing_code", "description",
                "rate", "pct_of_medicare",
                "payer_peer_median", "peer_practices", "vs_peer_median_pct"])
    rank = {r["payer"]: r.get("rank") for r in scorecard["rows"]}
    for entry in fee_schedule["codes"]:
        for payer in fee_schedule["payers"]:
            v = entry["rates"].get(payer)
            if not v or v["rate"] is None:
                continue
            w.writerow([defuse_csv(payer), rank.get(payer) or "", entry["billing_code"],
                        defuse_csv(entry["description"]) or "", v["rate"],
                        "" if v["pct_medicare"] is None else int(v["pct_medicare"]),
                        "" if v.get("market_median") is None else v["market_median"],
                        v.get("n_peers") or 0,
                        "" if v.get("vs_market_pct") is None else v["vs_market_pct"]])
    return out.getvalue()
