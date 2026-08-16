"""Monthly client packets — the deliverable, generated for the whole book.

The Clients tab shows what changed. This writes what you SEND: for every
watchlist client, a folder of branded, self-contained documents for the month
— rate card, what moved, contract gaps, Medicare referral alerts, and the
before/after win report when a baseline exists — each carrying its own
methodology. Open the folder, review, attach.

Design rules:

- FAULT ISOLATION per client AND per section. One client whose name no longer
  resolves, or one section that legitimately has nothing to say, must never
  cost you the other fourteen packets. Every failure is written into the
  packet's own index as a plain sentence, so a thin packet explains itself.
- NOTHING FABRICATED. A section with no content is omitted and listed as
  omitted, with the reason. An empty branded document is worse than none —
  the same rule the rate card and pitch report already follow.
- The index names the store's as-of month and every section's basis, so a
  packet mailed in November is unambiguous in February.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import re
from pathlib import Path

from .benchmark import (BenchmarkError, compute_benchmark, contract_gaps,
                        resolve_subject_tins,
                        render_pitch_report)
from .config import MrfxConfig
from .schedule import (compute_fee_schedule, payer_scorecard,
                       render_rate_card, require_rate_card_content)
from .store import Store

log = logging.getLogger(__name__)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:60] or "practice"


def _month_dir(root: Path, month: str) -> Path:
    d = Path(root) / month
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_client_packet(cfg: MrfxConfig, store: Store, subject: str,
                        out_dir: Path, market: dict | None = None) -> dict:
    """One client's packet. Returns what was written and what was skipped."""
    from .clients import client_digest  # local import: avoids a cycle

    market = dict(market or {})
    # A packet is a MONTHLY deliverable: pin the concrete month rather than
    # "latest", so a packet mailed in November still says which vintage it
    # describes when it is opened in February.
    if not market.get("month") or str(market["month"]).lower() == "latest":
        with store.connect() as con:
            m = (con.execute(
                "SELECT max(file_month) FROM rates_by_tin").fetchone() or [None])[0]
        market["month"] = m or dt.date.today().strftime("%Y-%m")
    market.setdefault("therapy_only", True)
    written: list[str] = []
    skipped: list[str] = []
    display = subject
    try:
        from .benchmark import resolve_subject_tins
        with store.connect() as con:
            name = (con.execute(
                "SELECT any_value(display_name) FROM tin_directory WHERE tin_value "
                "IN (SELECT unnest(?::VARCHAR[]))",
                [resolve_subject_tins(store, subject)]).fetchone() or [None])[0]
        display = name or subject
    except Exception:  # noqa: BLE001 — a name is cosmetic; the packet still builds
        pass

    def attempt(section: str, fn) -> None:
        try:
            fn()
        except BenchmarkError as e:      # a real, expected refusal
            skipped.append(f"{section}: {e}")
        except Exception as e:           # noqa: BLE001 — never lose the packet
            log.warning("packet %s / %s failed: %s", subject, section, e)
            skipped.append(f"{section}: could not be built ({e})")

    pdir = Path(out_dir) / _safe(subject)
    pdir.mkdir(parents=True, exist_ok=True)

    # 1. rate card — the anchor document
    state: dict = {}

    def _rate_card():
        fs = compute_fee_schedule(store, subject, market)
        require_rate_card_content(fs)
        sc = payer_scorecard(fs)
        state["fs"], state["sc"] = fs, sc
        (pdir / "1_rate_card.html").write_text(
            render_rate_card(cfg, store, fs, sc), encoding="utf-8")
        written.append("1_rate_card.html")
    attempt("Rate card", _rate_card)

    # 2. market position + opportunity (the pitch report)
    def _pitch():
        bench = compute_benchmark(store, subject, market)
        if not bench.get("rows"):
            raise BenchmarkError("no benchmarkable codes this month")
        (pdir / "2_market_position.html").write_text(
            render_pitch_report(cfg, store, bench), encoding="utf-8")
        written.append("2_market_position.html")
    attempt("Market position", _pitch)

    # 3. what moved this month + gaps + Medicare alerts, from the SAME digest
    #    the Clients tab renders (so the packet and the screen agree)
    def _changes():
        dig = client_digest(store)
        row = next((c for c in dig["clients"] if c["subject"] == subject), None)
        if row is None:
            raise BenchmarkError("this practice is not on the watchlist")
        if row.get("error"):
            raise BenchmarkError(row["error"])
        (pdir / "3_this_month.html").write_text(
            _month_summary_html(cfg, display, row, dig, store, subject, market),
            encoding="utf-8")
        written.append("3_this_month.html")
    attempt("What changed this month", _changes)

    # 4. contract gaps as a working list
    def _gaps():
        g = contract_gaps(store, subject, market)
        if not g.get("gaps"):
            raise BenchmarkError("no contract gaps found — nothing to chase")
        rows = "".join(
            f"<tr><td>{html.escape(x['billing_code'])}</td>"
            f"<td>{html.escape(x.get('description') or '')}</td>"
            f"<td class='num'>{x.get('n_peers', '')}</td>"
            f"<td class='num'>${x.get('peer_median', 0) or 0:,.2f}</td></tr>"
            for x in g["gaps"])
        (pdir / "4_contract_gaps.html").write_text(_doc(
            cfg, f"Contract gaps — {display}",
            "<p class='meta'>Codes this practice's peers publish rates for, that it "
            "has no published rate on. Directional: absence in a machine-readable "
            "file is a publishing gap as often as a real coverage gap, so each row "
            "is a question for the payer.</p>"
            "<table><thead><tr><th>Code</th><th>Description</th>"
            "<th class='num'>Peers pricing it</th><th class='num'>Peer median</th>"
            "</tr></thead><tbody>" + rows + "</tbody></table>"), encoding="utf-8")
        written.append("4_contract_gaps.html")
    attempt("Contract gaps", _gaps)

    # 5. before/after, when this client has a baseline
    def _wins():
        from .engagements import compare_to_baseline, list_baselines
        bl = list_baselines(store, subject)
        if not bl:
            raise BenchmarkError("no engagement baseline saved for this practice")
        cmp = compare_to_baseline(store, subject, bl[0]["label"])
        if not cmp["rows"]:
            raise BenchmarkError("no codes in common with the baseline yet")
        (pdir / "5_results_since_baseline.html").write_text(
            _wins_html(cfg, display, cmp), encoding="utf-8")
        written.append("5_results_since_baseline.html")
    attempt("Results since baseline", _wins)

    # 6. referral sources that closed — the client should hear this from their
    #    consultant, not from a quiet month. Cross-references the practice's own
    #    inbound referral partners against NPPES deactivations.
    def _closed_sources():
        from .benchmark import resolve_subject_tins
        from .medicare import org_referrals, taxonomy_label
        from .nppes import closure_status, closures

        st = closure_status(store)
        if not st["ready"]:
            raise BenchmarkError(st["reason"])
        # same TIN -> NPI resolution the Clients digest uses, so the packet and
        # the screen name the same practice
        tins = resolve_subject_tins(store, subject)
        with store.connect() as con:
            npis = [r[0] for r in con.execute(
                "SELECT DISTINCT npi FROM rates WHERE tin_value IN "
                "(SELECT unnest(?::VARCHAR[])) AND npi IS NOT NULL",
                [tins]).fetchall()]
        if not npis:
            raise BenchmarkError("no NPIs resolved for this practice")
        refs = org_referrals(store, npis, direction="in", limit=500)
        if not refs["rows"]:
            raise BenchmarkError(
                "no referral data loaded for this practice (Medicare tab)")
        partners = {str(r.get("npi")): r for r in refs["rows"] if r.get("npi")}
        # a wide closure window, then intersect: the feed is the authority on
        # what closed, the referral list on who matters to THIS client
        gone = [r for r in closures(store, days=730, therapy_only=False,
                                    limit=1000)["rows"]
                if r["npi"] in partners]
        if not gone:
            raise BenchmarkError("no referral source of this practice has closed")
        rows = "".join(
            f"<tr><td>{html.escape(str(x.get('org_name') or partners[x['npi']].get('name') or x['npi']))}</td>"
            f"<td>{html.escape(x['deactivated'])}</td>"
            f"<td class='num'>{partners[x['npi']].get('patients') or ''}</td>"
            f"<td>{html.escape(taxonomy_label(partners[x['npi']].get('taxonomy')))}</td>"
            f"<td>{html.escape(str(x.get('phone') or ''))}</td></tr>"
            for x in sorted(gone, key=lambda r: r["deactivated"], reverse=True))
        (pdir / "6_closed_referral_sources.html").write_text(_doc(
            cfg, f"Referral sources that closed — {display}",
            "<p class='meta'>Practices that referred patients to this one whose NPI "
            "has since been DEACTIVATED in NPPES and not reactivated. A deactivated "
            "NPI is not proof a practice closed — NPPES deactivates for paperwork "
            "lapses, mergers and retirements too, and it records the date the "
            "registry was updated, not the date the doors shut. Each row is a call "
            "to make about replacing that volume, never a fact to act on blind. "
            "Shared-patient counts come from the referral release named in the "
            "Medicare tab and describe that period only.</p>"
            "<table><thead><tr><th>Referral source</th><th>Deactivated</th>"
            "<th class='num'>Shared patients</th><th>Specialty</th><th>Phone</th>"
            "</tr></thead><tbody>" + rows + "</tbody></table>"), encoding="utf-8")
        written.append("6_closed_referral_sources.html")
    attempt("Closed referral sources", _closed_sources)

    index = _index_html(cfg, display, subject, written, skipped, market,
                        market["month"])
    (pdir / "index.html").write_text(index, encoding="utf-8")
    return {"subject": subject, "display_name": display, "dir": str(pdir),
            "written": written, "skipped": skipped}


def build_all_packets(cfg: MrfxConfig, store: Store, out_root: Path | str | None = None,
                      market: dict | None = None,
                      progress=None) -> dict:
    """Every watchlist client, into <out_root>/<month>/<practice>/."""
    from .clients import watchlist

    subjects = watchlist(store)
    if not subjects:
        raise BenchmarkError(
            "no clients saved yet — add them on the Clients tab first")
    with store.connect() as con:
        month = (con.execute(
            "SELECT max(file_month) FROM rates_by_tin").fetchone() or [None])[0]
    month = month or dt.date.today().strftime("%Y-%m")
    root = Path(out_root) if out_root else (Path(store.dir).parent / "packets")
    mdir = _month_dir(root, month)

    packets = []
    for i, s in enumerate(subjects, 1):
        if progress:
            progress(f"building packet {i} of {len(subjects)}: {s}")
        packets.append(build_client_packet(cfg, store, s, mdir, market))
    (mdir / "index.html").write_text(
        _book_index_html(cfg, month, packets), encoding="utf-8")
    log.info("packets: %s client(s) written to %s", len(packets), mdir)
    return {"month": month, "dir": str(mdir), "packets": packets,
            "clients": len(packets),
            "documents": sum(len(p["written"]) for p in packets)}


# ---------------------------------------------------------------- rendering --

_CSS = """
 body { font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #101828;
        max-width: 940px; margin: 32px auto; padding: 0 24px; }
 h1 { font-size: 20px; margin-bottom: 2px; } h2 { font-size: 15px; margin-top: 26px; }
 .brand { color: #475467; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }
 .brandbar { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
 .brandbar .brand { margin: 0; } .logo { max-height: 40px; max-width: 200px; }
 .meta { color: #475467; margin-bottom: 10px; }
 table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
 th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eaecf0; }
 th { font-size: 11px; color: #667085; text-transform: uppercase; letter-spacing: .04em; }
 .num { text-align: right; } .sub { color: #667085; font-size: 11.5px; }
 .band { font-size: 13.5px; background: #f2f4f7; padding: 10px 14px; border-radius: 6px; }
 .up { color: #1b7f4b; font-weight: 650; } .down { color: #b4321f; font-weight: 650; }
 .warn { background: #fbe9d0; border: 1px solid #d99a3a; color: #7a4a00;
         padding: 10px 14px; border-radius: 6px; font-size: 12.5px; margin: 12px 0; }
 ul { margin: 6px 0 6px 18px; } a { color: #2a78d6; }
 footer { margin-top: 34px; border-top: 1px solid #d0d5dd; padding-top: 12px;
          color: #475467; font-size: 11px; }
 @media print { body { margin: 0; } h2 { break-after: avoid; } }
"""


def _doc(cfg: MrfxConfig, title: str, body: str, footer: str = "") -> str:
    from .benchmark import _brand_header
    e = html.escape
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{e(title)}</title>"
            f"<style>{_CSS}</style></head><body>{_brand_header(cfg)}"
            f"<h1>{e(title)}</h1>{body}"
            f"<footer>{e(footer) if footer else ''}</footer></body></html>")


def _month_summary_html(cfg: MrfxConfig, display: str, row: dict, dig: dict,
                        store: Store | None = None, subject: str | None = None,
                        market: dict | None = None) -> str:
    e = html.escape
    parts = []
    ch = row.get("changes")
    if not dig.get("can_diff"):
        parts.append("<p>Rate-change monitoring needs two published months of a "
                     "payer in the store; only one is loaded so far.</p>")
    elif not ch or ch.get("error"):
        parts.append(f"<p>No rate-change comparison available"
                     f"{' (' + e(ch['error']) + ')' if ch and ch.get('error') else ''}.</p>")
    elif not ch.get("n"):
        parts.append(f"<p>No published rate changed for this practice in "
                     f"<b>{e(str(ch.get('month')))}</b> versus "
                     f"{e(str(ch.get('prev') or 'the prior month'))}.</p>")
    else:
        parts.append(
            f"<p class='band'><b>{ch['n']}</b> contract line(s) moved in "
            f"<b>{e(str(ch['month']))}</b> versus {e(str(ch.get('prev') or 'the prior month'))}. "
            f"Largest: <span class='{'up' if (ch.get('biggest_pct') or 0) > 0 else 'down'}'>"
            f"{ch.get('biggest_pct')}%</span> on {e(str(ch.get('biggest_code') or ''))} "
            f"({e(str(ch.get('biggest_payer') or ''))}).</p>")
    g = row.get("gaps") or {}
    if g.get("n"):
        parts.append(f"<h2>Contract gaps</h2><p>{g['n']} code(s) peers publish that "
                     f"this practice does not: {e(', '.join(g.get('top') or []))}"
                     f"{'…' if g['n'] > len(g.get('top') or []) else ''}. "
                     "The full list is in <i>4_contract_gaps.html</i>.</p>")
    # The shape behind the numbers above: one line per code, oldest to newest.
    # Cosmetic tier — no history, or any error, means no section at all.
    if store is not None and subject:
        try:
            from .spark import SPARK_NOTE, series_by_code, sparkline
            series = series_by_code(store, resolve_subject_tins(store, subject),
                                    market or {})
            drawable = {c: pts for c, pts in series.items() if len(pts) >= 2}
            if drawable:
                rows_html = "".join(
                    f"<tr><td>{e(c)}</td>"
                    f"<td>{sparkline(pts, label=c)}</td>"
                    f"<td class='num'>${pts[0][1]:,.2f} → ${pts[-1][1]:,.2f}</td>"
                    f"<td class='sub'>{e(pts[0][0])} → {e(pts[-1][0])}</td></tr>"
                    for c, pts in sorted(drawable.items()))
                parts.append(
                    "<h2>Where each rate has been going</h2>"
                    "<table><thead><tr><th>Code</th><th>Trend</th>"
                    "<th class='num'>First → latest</th><th>Span</th></tr>"
                    f"</thead><tbody>{rows_html}</tbody></table>"
                    f"<p class='sub'>{e(SPARK_NOTE)}</p>")
        except Exception:  # noqa: BLE001
            pass
    lost = row.get("lost_referrers")
    if lost:
        rows = "".join(
            f"<tr><td>{e(l.get('name') or l['npi'])}</td><td>{e(l['npi'])}</td>"
            f"<td class='num'>{l.get('patients') or ''}</td>"
            f"<td>{'dropped off the roster' if l['kind'] == 'removed' else 'lost Part B'}</td></tr>"
            for l in lost)
        parts.append(
            "<h2>Medicare referral alerts</h2>"
            "<div class='warn'>These providers shared Medicare patients with this "
            "practice and lost order/refer standing in the latest CMS roster. "
            "Claims they order or refer are at denial risk.</div>"
            "<table><thead><tr><th>Provider</th><th>NPI</th>"
            "<th class='num'>Shared patients</th><th>Change</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>")
    elif row.get("lost_referrers") == []:
        parts.append("<h2>Medicare referral alerts</h2><p>No referral source lost "
                     "order/refer standing in the latest roster.</p>")
    return _doc(cfg, f"This month — {display}", "".join(parts),
                "Figures come from payers' published machine-readable files as "
                "ingested by this app; see each attached document for its full "
                "methodology.")


def _wins_html(cfg: MrfxConfig, display: str, cmp: dict) -> str:
    e = html.escape
    s = cmp["summary"]
    def row_html(r: dict) -> str:
        cls = "up" if r["delta"] > 0 else "down" if r["delta"] < 0 else ""
        sign = "+" if r["delta"] > 0 else ""
        pct = f" ({sign}{r['delta_pct']}%)" if r.get("delta_pct") is not None else ""
        pb = "" if r["pctile_before"] is None else f"p{round(r['pctile_before'])}"
        pa = "" if r["pctile_after"] is None else f"p{round(r['pctile_after'])}"
        val = "" if r["annual_value"] is None else f"${r['annual_value']:,.2f}"
        return (f"<tr><td>{e(r['billing_code'])}"
                f"<div class='sub'>{e(r.get('description') or '')}</div></td>"
                f"<td class='num'>${r['before']:,.2f}</td>"
                f"<td class='num'>${r['after']:,.2f}</td>"
                f"<td class='num {cls}'>{sign}{r['delta']:,.2f}{pct}</td>"
                f"<td class='num'>{pb} &rarr; {pa}</td>"
                f"<td class='num'>{val}</td></tr>")

    rows = "".join(row_html(r) for r in cmp["rows"])
    band = (f"<p class='band'><b>{s['n_improved']}</b> of {s['n_codes']} code(s) improved "
            f"since {e(str(cmp['baseline_month']))}"
            + (f"; median position moved from p{s['median_pctile_before']} to "
               f"p{s['median_pctile_after']}" if s.get("median_pctile_before") is not None
               and s.get("median_pctile_after") is not None else "")
            + (f". At the practice's stated volumes that is "
               f"<b>${s['total_annual_value']:,.2f}</b> per year "
               f"({s['n_valued']} of {s['n_codes']} codes carry volumes)."
               if s.get("total_annual_value") is not None else ".")
            + "</p>")
    return _doc(cfg, f"Results since baseline — {display}",
                band +
                f"<p class='meta'>Baseline month {e(str(cmp['baseline_month']))} "
                f"(saved {e(cmp['baseline_saved'])}) versus "
                f"{e(str(cmp['current_month']))}.</p>"
                "<table><thead><tr><th>Code</th><th class='num'>Before</th>"
                "<th class='num'>After</th><th class='num'>Change</th>"
                "<th class='num'>Percentile</th><th class='num'>Annual value</th>"
                "</tr></thead><tbody>" + rows + "</tbody></table>"
                + (f"<h2>Codes added since baseline</h2><p>"
                   + e(", ".join(x["billing_code"] for x in cmp["gained_codes"]))
                   + "</p>" if cmp["gained_codes"] else "")
                + (f"<h2>Codes no longer published</h2><p>"
                   + e(", ".join(x["billing_code"] for x in cmp["lost_codes"]))
                   + "</p>" if cmp["lost_codes"] else ""),
                cmp["note"])


def _index_html(cfg: MrfxConfig, display: str, subject: str, written: list[str],
                skipped: list[str], market: dict, month) -> str:
    e = html.escape
    docs = "".join(f"<li><a href='{e(f)}'>{e(f)}</a></li>" for f in written)
    miss = "".join(f"<li>{e(x)}</li>" for x in skipped)
    return _doc(
        cfg, f"{display} — monthly packet",
        f"<p class='meta'>As of month: <b>{e(str(month or market.get('month')))}</b>. "
        f"Practice: {e(subject)}.</p>"
        f"<h2>Documents</h2><ul>{docs or '<li>(none)</li>'}</ul>"
        + (f"<h2>Not included this month</h2><ul>{miss}</ul>"
           "<p class='sub'>A section is omitted when it has nothing to say — an "
           "empty branded document would be worse than none.</p>" if skipped else ""),
        "Generated by MRF Explorer from payers' published machine-readable files. "
        "Each document carries its own methodology.")


def _book_index_html(cfg: MrfxConfig, month: str, packets: list[dict]) -> str:
    e = html.escape
    rows = "".join(
        f"<tr><td><a href='{e(_safe(p['subject']))}/index.html'>{e(p['display_name'])}</a></td>"
        f"<td class='num'>{len(p['written'])}</td>"
        f"<td class='sub'>{e('; '.join(p['skipped']) if p['skipped'] else '')}</td></tr>"
        for p in packets)
    return _doc(cfg, f"Client packets — {month}",
                f"<p class='meta'>{len(packets)} client(s), "
                f"{sum(len(p['written']) for p in packets)} document(s).</p>"
                "<table><thead><tr><th>Client</th><th class='num'>Documents</th>"
                "<th>Omitted sections</th></tr></thead><tbody>" + rows +
                "</tbody></table>")
