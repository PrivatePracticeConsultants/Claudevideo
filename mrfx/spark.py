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


def _fmt(v) -> str:
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "–"


def sparkline(points, *, width: int = 84, height: int = 22,
              label: str | None = None) -> str:
    """`points` = [(month, value), …] oldest first. Returns inline SVG.

    Returns a plain dash for fewer than two usable points: a single dot styled
    like a trend is a claim the data does not support.
    """
    pts = [(str(m), float(v)) for m, v in (points or [])
           if v is not None and isinstance(v, (int, float))]
    if len(pts) < 2:
        return '<span class="spark-none" title="one month only — no trend to draw">–</span>'

    vals = [v for _m, v in pts]
    lo, hi = min(vals), max(vals)
    span = hi - lo
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
    tip = (f"{label + ': ' if label else ''}"
           + " → ".join(f"{m} {_fmt(v)}" for m, v in pts))
    pct = (f" ({(last - first) / first * 100:+.1f}%)" if first else "")
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
    "Trend columns draw the same monthly values printed in the table, oldest "
    "to newest, from the months this store actually has. A month a payer did "
    "not publish is a gap, not a flat segment, and a single month draws no "
    "line at all."
)


def series_by_code(store, subject_tins: list[str], market: dict,
                   codes: list[str] | None = None,
                   max_months: int = 24) -> dict[str, list]:
    """{billing_code: [(month, rate), …]} for one practice, oldest first.

    Reads the same TIN-grain spine every other number does, with the market's
    basis filters applied, but WITHOUT pinning a month — the series is the
    point. Returns {} rather than raising: a sparkline is decoration, and
    decoration must never cost a deliverable (invariant 3).
    """
    from .benchmark import _market_where, normalize_market, spine_relation

    if not subject_tins:
        return {}
    try:
        m = normalize_market({**(market or {}), "month": "latest"})
        where, params = _market_where(m, bool(m.get("include_assistant")),
                                      bool(m.get("include_non_dollar")))
        rel = spine_relation(m)
        clause, extra = "", []
        if codes:
            clause = f" AND t.billing_code IN ({', '.join('?' for _ in codes)})"
            extra = list(codes)
        sql = f"""
            WITH per_tin AS (
                SELECT t.billing_code, t.file_month, t.tin_value,
                       median(t.negotiated_rate) AS rate
                FROM {rel} t LEFT JOIN tin_directory td USING (tin_value)
                WHERE {where}{clause}
                  AND t.tin_value IN (SELECT unnest(?::VARCHAR[]))
                GROUP BY t.billing_code, t.file_month, t.tin_value
            )
            SELECT billing_code, file_month, round(median(rate), 2) AS rate
            FROM per_tin
            WHERE file_month IS NOT NULL
            GROUP BY billing_code, file_month
            ORDER BY billing_code, file_month
        """
        with store.connect() as con:
            rows = con.execute(sql, [*params, *extra, subject_tins]).fetchall()
    except Exception:  # noqa: BLE001 — cosmetic tier: never fail the report
        return {}
    out: dict[str, list] = {}
    for code, month, rate in rows:
        out.setdefault(code, []).append((month, rate))
    return {c: pts[-max_months:] for c, pts in out.items()}
