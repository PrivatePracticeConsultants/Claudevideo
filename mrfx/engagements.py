"""Engagement win-tracking: the before/after that proves the fee was earned.

At the start of an engagement, snapshot the client's position — every payer x
code rate, its market percentile, and the month the snapshot describes. Months
later, once a renegotiated contract lands in the payer's published files, the
same measurement re-run against the current month produces the document that
renews the engagement: "p31 -> p57 on 14 codes, worth $38k/yr at your volumes."

Honesty rules specific to this feature, because it is a self-serving number:

- The baseline is a STORED SNAPSHOT of what the app saw, with its month and
  basis. It is never recomputed from today's data (which would let a later
  ingest quietly rewrite history and inflate the win).
- A code the client did not have then and does not have now is not a win.
  Codes are matched pair-wise; new and dropped codes are reported separately.
- Market movement is separated from client movement: if every practice's rate
  rose, the percentile change says so and the report shows both.
- Dollar value uses owner-supplied volumes only. No volumes, no dollar claim.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from .benchmark import BenchmarkError, compute_benchmark, normalize_market
from .store import Store

log = logging.getLogger(__name__)


def _ensure_table(con) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS engagement_baselines (
            subject VARCHAR,
            label VARCHAR,
            created_at TIMESTAMP,
            month VARCHAR,
            payload VARCHAR,          -- JSON: the measured snapshot
            PRIMARY KEY (subject, label)
        )
    """)


def _concrete_month(store: Store, month) -> str | None:
    """'latest' resolved to the store's newest file month, for LABELS only.
    A baseline row or a wins report that says 'as of latest' is meaningless
    the day after it is written — the vintage-honesty rule everywhere else
    (packets pin max(file_month)) applies here too. The computation itself
    still runs on the 'latest' basis (newest file per contract)."""
    if month and str(month).strip().lower() != "latest":
        return month
    with store.connect() as con:
        m = (con.execute(
            "SELECT max(file_month) FROM rates_by_tin").fetchone() or [None])[0]
    return m or month


def _snapshot(store: Store, subject: str, market: dict) -> dict:
    """The measurement both ends of the comparison use — one function, so a
    baseline and a 'now' can never be computed differently."""
    bench = compute_benchmark(store, subject, market)
    rows = {}
    for r in bench["rows"]:
        if r.get("subject_rate") is None:
            continue
        rows[r["billing_code"]] = {
            "rate": r["subject_rate"],
            "percentile": r.get("subject_percentile"),
            "p50": r.get("p50"), "n_peers": r.get("n_peers"),
            "description": r.get("description"),
        }
    return {"month": bench["market"].get("month"), "codes": rows,
            "basis_note": bench.get("basis_note"),
            "peer_set": bench.get("peer_set"),
            "headline_percentile": bench.get("summary", {}).get("headline_percentile")
            if isinstance(bench.get("summary"), dict) else None}


def save_baseline(store: Store, subject: str, market: dict | None = None,
                  label: str = "engagement start") -> dict:
    """Freeze where this client stands today. Re-saving the same label
    REPLACES it (a mis-set baseline must be fixable) and says so."""
    subject = str(subject or "").strip()
    if not subject:
        raise BenchmarkError("pick the practice to baseline")
    label = (str(label or "").strip() or "engagement start")[:60]
    market = normalize_market(market or {"month": "latest"})
    snap = _snapshot(store, subject, market)
    snap["month"] = _concrete_month(store, snap["month"])
    if not snap["codes"]:
        raise BenchmarkError(
            f"no published rates for '{subject}' under the current basis — "
            "nothing to baseline yet")
    with store.write_lock, store.connect() as con:
        _ensure_table(con)
        existed = con.execute(
            "SELECT count(*) FROM engagement_baselines WHERE subject = ? AND label = ?",
            [subject, label]).fetchone()[0]
        con.execute(
            "INSERT OR REPLACE INTO engagement_baselines VALUES (?, ?, ?, ?, ?)",
            [subject, label, dt.datetime.now(dt.timezone.utc), snap["month"],
             json.dumps({"market": market, "snapshot": snap})])
    log.info("baseline %s for %s (%s codes, month %s)",
             "replaced" if existed else "saved", subject, len(snap["codes"]),
             snap["month"])
    return {"subject": subject, "label": label, "month": snap["month"],
            "codes": len(snap["codes"]), "replaced": bool(existed)}


def list_baselines(store: Store, subject: str | None = None) -> list[dict]:
    with store.connect() as con:
        try:
            q = ("SELECT subject, label, month, created_at, payload "
                 "FROM engagement_baselines")
            args: list = []
            if subject:
                q += " WHERE subject = ?"
                args = [subject]
            rows = con.execute(q + " ORDER BY subject, created_at", args).fetchall()
        except Exception:  # noqa: BLE001 — table absent = none saved
            return []
    out = []
    for s, label, month, created, payload in rows:
        try:
            n = len(json.loads(payload)["snapshot"]["codes"])
        except Exception:  # noqa: BLE001 — a corrupt row must not hide the rest
            n = 0
        out.append({"subject": s, "label": label, "month": month,
                    "created_at": str(created)[:19], "codes": n})
    return out


def delete_baseline(store: Store, subject: str, label: str) -> None:
    with store.write_lock, store.connect() as con:
        _ensure_table(con)
        con.execute("DELETE FROM engagement_baselines WHERE subject = ? AND label = ?",
                    [subject, label])


def compare_to_baseline(store: Store, subject: str, label: str = "engagement start",
                        market: dict | None = None,
                        volumes: dict[str, float] | None = None) -> dict:
    """Baseline vs now, code by code. Dollar value only with volumes."""
    subject = str(subject or "").strip()
    with store.connect() as con:
        try:
            row = con.execute(
                "SELECT payload, month, created_at FROM engagement_baselines "
                "WHERE subject = ? AND label = ?", [subject, label]).fetchone()
        except Exception:  # noqa: BLE001
            row = None
    if not row:
        raise BenchmarkError(
            f"no baseline named '{label}' for '{subject}' — save one first "
            "(Clients tab -> baseline this client)")
    saved = json.loads(row[0])
    base = saved["snapshot"]
    # The comparison runs on the SAME market definition the baseline used
    # (peer set, state, discipline, class), with only the month moved to now —
    # otherwise a changed filter would masquerade as a rate win. The saved
    # month is DROPPED, not defaulted around: it pinned the baseline, and
    # inheriting it would measure "after" in the past and report every real
    # win as zero.
    cmp_market = {k: v for k, v in saved.get("market", {}).items() if k != "month"}
    cmp_market.update(market or {})
    cmp_market.setdefault("month", "latest")
    now = _snapshot(store, subject, normalize_market(cmp_market))
    now["month"] = _concrete_month(store, now["month"])

    try:
        volumes = {str(k).strip().upper(): float(v)
                   for k, v in (volumes or {}).items()}
    except (TypeError, ValueError, AttributeError):
        raise BenchmarkError(
            "volumes must map a billing code to annual units, e.g. 97110: 1200")
    both, gained, lost = [], [], []
    total_value = 0.0
    for code, b in sorted(base["codes"].items()):
        n = now["codes"].get(code)
        if n is None:
            lost.append({"billing_code": code, "was": b["rate"],
                         "description": b.get("description")})
            continue
        delta = round(n["rate"] - b["rate"], 2)
        units = volumes.get(code)
        value = round(delta * units, 2) if units is not None else None
        if value:
            total_value += value
        both.append({
            "billing_code": code, "description": b.get("description") or n.get("description"),
            "before": b["rate"], "after": n["rate"], "delta": delta,
            "delta_pct": round(100.0 * delta / b["rate"], 1) if b["rate"] else None,
            "pctile_before": b.get("percentile"), "pctile_after": n.get("percentile"),
            # market context: if the peer median moved too, the client's gain
            # is partly the tide, and the report must be able to say so
            "market_before": b.get("p50"), "market_after": n.get("p50"),
            "units": units, "annual_value": value,
        })
    for code, n in sorted(now["codes"].items()):
        if code not in base["codes"]:
            gained.append({"billing_code": code, "now": n["rate"],
                           "description": n.get("description")})

    improved = [r for r in both if r["delta"] > 0]
    worsened = [r for r in both if r["delta"] < 0]
    pb = [r["pctile_before"] for r in both if r["pctile_before"] is not None]
    pa = [r["pctile_after"] for r in both if r["pctile_after"] is not None]
    mb = [r["market_before"] for r in both if r["market_before"] is not None]
    ma = [r["market_after"] for r in both if r["market_after"] is not None]

    def med(xs):
        if not xs:
            return None
        xs = sorted(xs)
        n = len(xs)
        return round((xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2), 1)

    # A win measured in nominal dollars overstates itself: prices rose between
    # the baseline and now, so part of any gain is inflation, not negotiation.
    # Cosmetic tier — no index year covering the span means NO real-terms
    # figure, never an estimate, and never a failed comparison.
    real = None
    try:
        from .inflation import BASIS_CAVEAT, deflator, load_index, real_change_pct
        d = deflator(load_index(store), row[1], now["month"])
        before, after = med(pb), med(pa)
        gain_pct = None
        if volumes and total_value:
            gain_pct = None       # a dollar total has no percentage to deflate
        real = {
            "basis": d.get("basis"), "reason": d.get("reason"),
            "factor": round(d["factor"], 4) if d.get("factor") else None,
            "caveat": BASIS_CAVEAT,
            "value_in_baseline_dollars": (
                round(total_value / d["factor"], 2)
                if volumes and total_value and d.get("factor") else None),
            "note": (
                "Prices rose between the baseline and now, so a gain measured in "
                "today's dollars is worth less than the same figure at the "
                "baseline. Where an index covers the span, the win is also shown "
                "in baseline dollars."),
        }
    except Exception:  # noqa: BLE001
        real = None
    return {
        "subject": subject, "label": label,
        "baseline_month": row[1], "baseline_saved": str(row[2])[:19],
        "current_month": now["month"],
        "real_terms": real,
        "rows": both, "gained_codes": gained, "lost_codes": lost,
        "basis_note": now.get("basis_note"), "peer_set": now.get("peer_set"),
        "market": cmp_market,
        "summary": {
            "n_codes": len(both), "n_improved": len(improved),
            "n_worsened": len(worsened), "n_unchanged": len(both) - len(improved) - len(worsened),
            "median_pctile_before": med(pb), "median_pctile_after": med(pa),
            "median_market_before": med(mb), "median_market_after": med(ma),
            "has_volumes": bool(volumes),
            "total_annual_value": round(total_value, 2) if volumes else None,
            "n_valued": sum(1 for r in both if r["annual_value"] is not None),
        },
        "note": ("Before/after compares a stored snapshot of what this app saw at "
                 "the baseline month against the same measurement today, on the "
                 "same market basis. Codes present in only one of the two are "
                 "listed separately, never counted as movement. Where the peer "
                 "median moved as well, part of any gain is the market, not the "
                 "negotiation — both are shown."),
    }
