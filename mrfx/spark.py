"""Sparklines: a rate's path, small enough to sit inside a table cell.

A printed deliverable states one number per code as of one month. The client's
actual question — "is this getting better or worse?" — is answered by the shape
of the series, and until now that shape only existed on screen, on a different
tab, under different filters.

Inline SVG, drawn here with no library and no external request: a report is a
single self-contained HTML file the user emails to a client, so a chart that
needs a CDN would be a blank box on their screen.

HONESTY:
- A sparkline of ONE point is not a trend and is not drawn — the cell says so.
- Gaps are real: a month where the payer published nothing is not interpolated
  into a smooth line. Points are placed by their position in the series and the
  months are named in the tooltip, so a two-point line spanning a year cannot
  be misread as monthly movement.
- The line is drawn from the same values printed beside it; it never re-scales
  a number to look steeper.
"""

from __future__ import annotations

import html
import math
from .rounding import half_up, signed_pct


def _fmt(v) -> str:
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "–"


def sparkline(points, *, width: int = 84, height: int = 22,
              label: str | None = None) -> str:
    """`points` = [(month, value), …] oldest first. Returns inline SVG.

    Returns a plain dash for fewer than two usable points: a single dot styled
    like a trend is a claim the data does not support.
    """
    # Only FINITE numbers may reach the path: a NaN or inf coordinate writes
    # literal "nan" into the SVG `d` attribute — a visibly broken picture in a
    # client-facing report (found by the adversarial harness). The store never
    # produces such rates on purpose, but a parquet DOUBLE can carry them.
    pts = [(str(m), float(v)) for m, v in (points or [])
           if v is not None and isinstance(v, (int, float))
           and math.isfinite(float(v))]
    if len(pts) < 2:
        return '<span class="spark-none" title="one month only — no trend to draw">–</span>'

    vals = [v for _m, v in pts]
    lo, hi = min(vals), max(vals)
    span = hi - lo
    if not math.isfinite(span):
        # two finite extremes whose DIFFERENCE overflows (±1e308). Not a rate
        # that can exist; a line drawn from it would be a lie either way.
        return '<span class="spark-none" title="values out of drawable range">–</span>' 
    n = len(pts)
    pad = 2.0
    w, h = float(width), float(height)

    def x(i):
        return pad + (w - 2 * pad) * (i / (n - 1))

    def y(v):
        # a flat series draws on the mid-line rather than dividing by zero
        if span <= 0:
            return h / 2
        return pad + (h - 2 * pad) * (1 - (v - lo) / span)

    path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}"
                    for i, (_m, v) in enumerate(pts))
    first, last = vals[0], vals[-1]
    # colour by direction, but only when there IS a direction
    stroke = "#667085" if last == first else ("#d92d20" if last < first else "#12805c")
    end_x, end_y = x(n - 1), y(last)
    tip = (f"{label + ' — ' if label else ''}median contract: "
           + " → ".join(f"{m} {_fmt(v)}" for m, v in pts))
    # half-up like every printed number: $80.00 -> $81.00 is +1.25 %, which
    # Python's format printed "+1.2%" beside "+1.3" in Rate changes (4.9)
    pct = (f" ({signed_pct((last - first) / first * 100)})" if first else "")
    return (
        f'<svg class="spark" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{html.escape(tip + pct)}">'
        f'<title>{html.escape(tip + pct)}</title>'
        f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="1.8" fill="{stroke}"/>'
        f'</svg>')


SPARK_CSS = """
 .spark { vertical-align: middle; }
 .spark-none { color: #98a2b3; }
 .sparkhead { color: #475467; font-size: 11px; }
"""

SPARK_NOTE = (
    "Trend lines follow the SAME contracts in every month they show: a code's "
    "line is the median of this practice's rates with the payers it has in the "
    "newest month, back as far as all of them published (if that is only one "
    "month, it follows those that did publish earlier). A payer joining or "
    "leaving never moves the line; only a published rate change does. A single "
    "month draws no line. The line is the median CONTRACT (one payer x one tax "
    "ID), so its last point can differ from a rate printed beside it that pools "
    "a practice's payers first. It ends at the document's as-of month."
)


def _panel_series(cells: dict, months: list[str]) -> list[tuple[str, float]]:
    """[(month, median over a FIXED panel)] for one code.

    `cells` maps (payer, tin) -> {month: rate}. The panel is every contract
    present in the newest month, and the line goes back while ALL of them are
    present — so a payer's real move shows. The last point is the median
    CONTRACT in the newest month walked, NOT the practice rate a table prints
    beside it (which pools a TIN's payers first): re-pooling per practice
    would let a payer joining move the line again — so the line is labelled
    a contract trend (SPARK_NOTE) instead of claiming to be the printed
    number (audit 8 P1-7). If that leaves one month, the panel narrows
    month by month instead (the contracts present in every month walked).
    Either way the panel is constant along the line, so a payer entering or
    leaving can never make it move (round 7 P0.3: payer B appearing at $50
    next to A's unchanged $40 read as a +12.5% raise)."""
    import statistics

    def walk(narrow: bool):
        window: list[str] = []
        panel: set = {k for k in cells if months and months[-1] in cells[k]}
        for m in reversed(months):
            keep = {k for k in panel if m in cells[k]}
            if not keep or (not narrow and keep != panel):
                break
            panel = keep
            window.append(m)
        window.reverse()
        return [(m, half_up(statistics.median(cells[k][m] for k in sorted(panel)), 2))
                for m in window]

    full = walk(narrow=False)
    return full if len(full) >= 2 else walk(narrow=True)


def series_by_code(store, subject_tins: list[str], market: dict,
                   codes: list[str] | None = None,
                   max_months: int = 24) -> dict[str, list]:
    """{billing_code: [(month, rate), …]} for one practice, oldest first.

    Reads the same TIN-grain spine every other number does, with the market's
    basis filters applied, but WITHOUT pinning a month — the series is the
    point. A pinned as-of month is its LAST month: a report dated 2026-05
    never shows a June point (audit 8 P1-7). Each code's line is a fixed panel of (payer, TIN) contracts (see
    _panel_series). Returns {} rather than raising: a sparkline is
    decoration, and decoration must never cost a deliverable (invariant 3).
    """
    from .benchmark import _market_where, normalize_market, spine_relation

    if not subject_tins:
        return {}
    try:
        pinned = (market or {}).get("month")
        pinned = pinned if pinned and str(pinned).lower() != "latest" else None
        m = normalize_market({**(market or {}), "month": "latest"})
        where, params = _market_where(m, bool(m.get("include_assistant")),
                                      bool(m.get("include_non_dollar")))
        rel = spine_relation(m)
        clause, extra = "", []
        if codes:
            clause = f" AND t.billing_code IN ({', '.join('?' for _ in codes)})"
            extra = list(codes)
        if pinned:
            clause += " AND t.file_month <= ?"
            extra.append(str(pinned))
        sql = f"""
            SELECT t.billing_code, t.file_month, t.payer, t.tin_value,
                   median(t.negotiated_rate) AS rate
            FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
            WHERE {where}{clause}
              AND t.tin_value IN (SELECT unnest(?::VARCHAR[]))
              AND t.file_month IS NOT NULL
            GROUP BY t.billing_code, t.file_month, t.payer, t.tin_value
            ORDER BY t.billing_code, t.file_month, t.payer, t.tin_value
        """
        with store.connect() as con:
            rows = con.execute(sql, [*params, *extra, subject_tins]).fetchall()
    except Exception:  # noqa: BLE001 — cosmetic tier: never fail the report
        return {}
    by_code: dict[str, dict] = {}
    months_of: dict[str, set] = {}
    for code, month, payer, tin, rate in rows:
        if rate is None:
            continue
        by_code.setdefault(code, {}).setdefault((payer, tin), {})[month] = float(rate)
        months_of.setdefault(code, set()).add(month)
    out: dict[str, list] = {}
    for code, cells in by_code.items():
        months = sorted(months_of[code])[-max_months:]
        pts = _panel_series(cells, months)
        if pts:
            out[code] = pts
    return out
